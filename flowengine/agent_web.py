"""Agent operations for finding data on the web: search, pages, links and files, APIs and pagination.

Search uses real services: GOV.UK's search API and data.gov.uk's CKAN catalogue (official, no key),
any CKAN portal by URL, DuckDuckGo's HTML endpoint for general web search (no key), and Brave
Search through a saved Web/API connection when the user has a key. Every retrieval records its
final URL, HTTP status and time; saved files get a .source.json provenance record.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from . import __version__
from .errors import EngineError

USER_AGENT = f'Flowbench/{__version__} (+https://github.com/stevenmcsorley/alteryx-parity-research)'
DATA_EXTENSIONS = ('.csv', '.tsv', '.xlsx', '.xlsm', '.xls', '.ods', '.json', '.jsonl', '.geojson', '.xml', '.zip',
                   '.gz', '.parquet', '.txt', '.pdf', '.shp', '.kml', '.gpkg')


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')


def _session():
    import requests
    s = requests.Session()
    s.headers['User-Agent'] = USER_AGENT
    return s


def _get(url: str, **kw):
    if not re.match(r'^https?://', url, re.I):
        raise EngineError('Only http:// and https:// URLs can be retrieved', code='bad-url')
    try:
        r = _session().get(url, timeout=(15, 120), **kw)
    except Exception as e:
        raise EngineError(f'Could not reach {url}: {type(e).__name__}: {e}', code='network-error') from None
    return r


# ---------------------------------------------------------------------------------------- HTML
class _Page(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'svg', 'template'}
    BLOCK = {'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'br', 'section', 'article', 'table', 'dt', 'dd'}

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.title, self.links, self.parts = base, '', [], []
        self._skip, self._in_title, self._link = 0, False, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self._skip += 1
        elif tag == 'title':
            self._in_title = True
        elif tag == 'a' and a.get('href'):
            self._link = {'href': urljoin(self.base, a['href']), 'text': ''}
        if tag in self.BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == 'title':
            self._in_title = False
        elif tag == 'a' and self._link is not None:
            self._link['text'] = ' '.join(self._link['text'].split())
            self.links.append(self._link)
            self._link = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._link is not None:
            self._link['text'] += data
        self.parts.append(data)

    def text(self):
        lines = [' '.join(line.split()) for line in ''.join(self.parts).split('\n')]
        return '\n'.join(line for line in lines if line)


def _is_data_link(href: str) -> bool:
    parsed = urlparse(href)
    path = unquote(parsed.path).lower()
    if parsed.netloc.endswith('gov.uk') and path.startswith('/csv-preview/'):
        return False  # an HTML preview page, not the file
    return path.endswith(DATA_EXTENSIONS)


def _govuk_attachments(url: str) -> list[dict]:
    """GOV.UK pages list their published files in the content API; use it instead of scraping."""
    parsed = urlparse(url)
    if parsed.netloc not in ('www.gov.uk', 'gov.uk'):
        return []
    r = _get(f'https://www.gov.uk/api/content{parsed.path}')
    if r.status_code != 200:
        return []
    details = r.json().get('details', {})
    out = []
    for a in details.get('attachments', []) or []:
        if a.get('url'):
            out.append({'title': a.get('title'), 'url': urljoin('https://www.gov.uk', a['url']), 'content_type': a.get('content_type'),
                        'file_size': a.get('file_size'), 'filename': a.get('filename')})
    return out


def fetch_page(ctx, url: str) -> dict:
    """A web page's title, readable text, every link and the data files it links to."""
    r = _get(url)
    ctype = r.headers.get('Content-Type', '')
    result = {'url': url, 'final_url': r.url, 'http_status': r.status_code, 'content_type': ctype, 'retrieved_at': _now()}
    if r.status_code >= 400:
        raise EngineError(f'The server returned HTTP {r.status_code} for {url}', code='http-error')
    if 'html' not in ctype and not r.text.lstrip().startswith('<'):
        return {**result, 'title': None, 'text': r.text, 'links': [], 'data_files': []}
    page = _Page(r.url)
    page.feed(r.text)
    files = [{'title': l['text'], 'url': l['href']} for l in page.links if _is_data_link(l['href'])]
    attachments = _govuk_attachments(r.url)
    seen = {f['url'] for f in attachments}
    files = attachments + [f for f in files if f['url'] not in seen]
    return {**result, 'title': ' '.join(page.title.split()), 'text': page.text(), 'links': page.links, 'data_files': files}


def find_data_files(ctx, url: str, depth: int = 1, same_site: bool = True) -> dict:
    """Follows a page's links (to `depth` levels on the same site) and lists every data file found."""
    start = urlparse(url).netloc
    seen_pages, files, queue = set(), {}, [(url, 0)]
    while queue and len(seen_pages) < 60:
        page_url, level = queue.pop(0)
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        try:
            page = fetch_page(ctx, page_url)
        except EngineError:
            continue
        for f in page['data_files']:
            files.setdefault(f['url'], {**f, 'found_on': page['final_url']})
        if level < depth:
            for link in page['links']:
                href = link['href'].split('#')[0]
                if href.startswith('http') and not _is_data_link(href) and (not same_site or urlparse(href).netloc == start):
                    queue.append((href, level + 1))
    return {'start': url, 'pages_visited': sorted(seen_pages), 'data_files': list(files.values()), 'retrieved_at': _now()}


# ---------------------------------------------------------------------------------------- search
def _duckduckgo(query: str, limit: int) -> list[dict]:
    import requests
    r = requests.post('https://html.duckduckgo.com/html/', data={'q': query}, timeout=(15, 60), headers={'User-Agent': USER_AGENT})
    if r.status_code != 200:
        raise EngineError(f'DuckDuckGo returned HTTP {r.status_code}', code='search-unavailable')
    results = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|\Z)', r.text, re.S):
        href = m.group(1)
        if 'uddg=' in href:
            href = unquote(parse_qs(urlparse(href).query).get('uddg', [href])[0])
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', m.group(3), re.S)
        clean = lambda h: html.unescape(' '.join(re.sub(r'<[^>]+>', '', h).split()))
        results.append({'title': clean(m.group(2)), 'url': href, 'snippet': clean(snippet.group(1)) if snippet else ''})
        if len(results) >= limit:
            break
    return results


SETTINGS_FILE = 'agent-settings.json'


def search_settings() -> dict:
    """Where web search goes: a SearXNG instance (preferred; self-hosted, no key) when one is configured."""
    import os
    from .runtimes import home
    saved = {}
    try:
        saved = json.loads((home() / SETTINGS_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    url = os.environ.get('FLOWBENCH_SEARXNG_URL') or saved.get('searxng_url')
    engines = os.environ.get('FLOWBENCH_SEARXNG_ENGINES') or saved.get('searxng_engines')
    return {'searxng_url': url.rstrip('/') if url else None, 'searxng_engines': engines or None}


def save_search_settings(searxng_url: str | None, engines: str | None = None) -> dict:
    from .runtimes import home
    if searxng_url and not re.match(r'^https?://[^/\s]+', searxng_url):
        raise EngineError('The SearXNG address must be an http:// or https:// URL, such as http://localhost:8888', code='bad-url')
    path = home() / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        saved = {}
    saved['searxng_url'] = searxng_url.rstrip('/') if searxng_url else None
    cleaned = ','.join(e.strip() for e in (engines or '').split(',') if e.strip())
    if cleaned and not re.fullmatch(r'[a-z0-9 _.,-]+', cleaned, re.I):
        raise EngineError('Engines are SearXNG engine names separated by commas, such as "bing,mojeek,wikipedia"', code='bad-engines')
    saved['searxng_engines'] = cleaned or None
    path.write_text(json.dumps(saved, indent=2), encoding='utf-8')
    return search_settings()


def _searxng(base: str, query: str, limit: int, engines: str | None = None) -> tuple[list[dict], list]:
    params = {'q': query, 'format': 'json'}
    if engines:
        params['engines'] = engines
    r = _get(f'{base}/search', params=params)
    if r.status_code == 403:
        raise EngineError('SearXNG refused JSON output; add "json" under search.formats in its settings.yml', code='search-unavailable')
    if r.status_code != 200:
        raise EngineError(f'SearXNG returned HTTP {r.status_code}', code='search-unavailable')
    body = r.json()
    results = [{'title': x.get('title'), 'url': x.get('url'), 'snippet': x.get('content', ''), 'engine': x.get('engine')}
               for x in body.get('results', [])][:limit]
    return results, body.get('unresponsive_engines', [])


def test_searxng(base: str, engines: str | None = None) -> dict:
    results, unresponsive = _searxng(base.rstrip('/'), 'official statistics', 5, engines)
    return {'ok': True, 'results': len(results), 'unresponsive_engines': unresponsive}


def _brave(query: str, limit: int, connection_id: str) -> list[dict]:
    from .service_connections import ServiceConnectionStore
    store = ServiceConnectionStore()
    conn = store.get(connection_id)
    url = f'https://api.search.brave.com/res/v1/web/search?q={quote(query)}&count={min(limit, 20)}'
    headers = conn.authorize(url, store.secret(conn))
    r = _get(url, headers={**headers, 'Accept': 'application/json'})
    if r.status_code != 200:
        raise EngineError(f'Brave Search returned HTTP {r.status_code}', code='search-unavailable')
    return [{'title': x.get('title'), 'url': x.get('url'), 'snippet': re.sub(r'<[^>]+>', '', x.get('description', ''))}
            for x in r.json().get('web', {}).get('results', [])][:limit]


def web_search(ctx, query: str, provider: str = 'auto', limit: int = 10, connection_id: str | None = None,
               engines: str | None = None) -> dict:
    notes = []
    if provider in ('auto', 'searxng'):
        settings = search_settings()
        base = settings['searxng_url']
        if base:
            results, unresponsive = _searxng(base, query, limit, engines or settings['searxng_engines'])
            out = {'query': query, 'provider': 'searxng', 'searxng_url': base, 'results': results, 'retrieved_at': _now()}
            if unresponsive:
                out['unresponsive_engines'] = unresponsive
            if results or provider == 'searxng':
                return out
            notes.append(f'SearXNG returned no results (unresponsive engines: {unresponsive}); fell back to DuckDuckGo.')
        elif provider == 'searxng':
            raise EngineError('No SearXNG address is set; add it in AI settings > Web search, or set FLOWBENCH_SEARXNG_URL', code='not-configured')
        provider = 'duckduckgo'
    if provider == 'brave':
        if not connection_id:
            raise EngineError('Brave Search needs a saved Web/API connection for https://api.search.brave.com with the '
                              'key sent in the X-Subscription-Token header; pass its connection_id', code='connection-required')
        results = _brave(query, limit, connection_id)
    elif provider == 'duckduckgo':
        results = _duckduckgo(query, limit)
    else:
        raise EngineError(f'Unknown search provider {provider!r}', code='bad-provider')
    out = {'query': query, 'provider': provider, 'results': results, 'retrieved_at': _now()}
    if notes:
        out['notes'] = notes
    return out


def search_datasets(ctx, query: str, catalogue: str = 'govuk', ckan_url: str | None = None, limit: int = 10) -> dict:
    """Official dataset catalogues: GOV.UK statistics and data sets, data.gov.uk, or any CKAN portal."""
    if catalogue == 'govuk':
        url = (f'https://www.gov.uk/api/search.json?q={quote(query)}&count={limit}'
               '&filter_content_store_document_type=national_statistics&filter_content_store_document_type=official_statistics'
               '&filter_content_store_document_type=statistical_data_set&filter_content_store_document_type=statistics'
               '&fields=title&fields=link&fields=description&fields=public_timestamp&fields=organisations')
        r = _get(url)
        if r.status_code != 200:
            raise EngineError(f'GOV.UK search returned HTTP {r.status_code}', code='search-unavailable')
        hits = [{'title': x.get('title'), 'url': urljoin('https://www.gov.uk', x.get('link', '')), 'description': x.get('description'),
                 'updated': x.get('public_timestamp'), 'publisher': ', '.join(o.get('title', '') for o in x.get('organisations', []) if isinstance(o, dict))}
                for x in r.json().get('results', [])]
        note = 'Call fetch_page on a result: GOV.UK pages list their files (data_files) with direct download URLs.'
    else:
        base = {'datagovuk': 'https://ckan.publishing.service.gov.uk'}.get(catalogue) or (ckan_url or '').rstrip('/')
        if not base:
            raise EngineError('Pass ckan_url for a CKAN catalogue, or use catalogue govuk or datagovuk', code='bad-catalogue')
        r = _get(f'{base}/api/3/action/package_search?q={quote(query)}&rows={limit}')
        if r.status_code != 200:
            raise EngineError(f'The catalogue returned HTTP {r.status_code}', code='search-unavailable')
        hits = []
        for p in r.json().get('result', {}).get('results', []):
            hits.append({'title': p.get('title'), 'url': f"{base}/dataset/{p.get('name')}", 'description': (p.get('notes') or '')[:500],
                         'updated': p.get('metadata_modified'), 'publisher': (p.get('organization') or {}).get('title'),
                         'files': [{'title': res.get('name'), 'url': res.get('url'), 'format': res.get('format')} for res in p.get('resources', [])]})
        note = 'Each result lists its files; download one with download_file.'
    return {'query': query, 'catalogue': catalogue, 'results': hits, 'note': note, 'retrieved_at': _now()}


# ---------------------------------------------------------------------------------------- APIs
INTROSPECTION = '{ __schema { queryType { name } types { name kind description fields { name description args { name type { name kind ofType { name kind } } } type { name kind ofType { name kind } } } } } }'


def inspect_api(ctx, url: str, kind: str = 'auto') -> dict:
    """Summarise an OpenAPI/Swagger document or a GraphQL endpoint (by introspection)."""
    if kind in ('graphql', 'auto') and (kind == 'graphql' or url.rstrip('/').endswith('graphql')):
        import requests
        r = requests.post(url, json={'query': INTROSPECTION}, timeout=(15, 120), headers={'User-Agent': USER_AGENT})
        data = r.json().get('data', {}).get('__schema') if r.status_code == 200 else None
        if not data:
            raise EngineError(f'GraphQL introspection failed (HTTP {r.status_code})', code='api-unreadable')
        query_type = data['queryType']['name']
        types = {t['name']: t for t in data['types']}
        queries = [{'name': f['name'], 'description': f.get('description'), 'args': [a['name'] for a in f.get('args') or []]}
                   for f in (types.get(query_type, {}).get('fields') or [])]
        objects = [t['name'] for t in data['types'] if t['kind'] == 'OBJECT' and not t['name'].startswith('__')]
        return {'kind': 'graphql', 'url': url, 'queries': queries, 'object_types': objects, 'retrieved_at': _now()}
    r = _get(url)
    if r.status_code >= 400:
        raise EngineError(f'The server returned HTTP {r.status_code} for {url}', code='http-error')
    try:
        doc = r.json()
    except ValueError:
        try:
            import yaml
            doc = yaml.safe_load(r.text)
        except Exception:
            raise EngineError('The document is neither JSON nor readable YAML OpenAPI', code='api-unreadable') from None
    if not isinstance(doc, dict) or not ('openapi' in doc or 'swagger' in doc):
        raise EngineError('Not an OpenAPI/Swagger document', code='api-unreadable')
    servers = [s.get('url') for s in doc.get('servers', [])] or ([f"{doc.get('schemes', ['https'])[0]}://{doc.get('host')}{doc.get('basePath', '')}"]
                                                                  if doc.get('host') else [])
    endpoints = []
    for path, ops in (doc.get('paths') or {}).items():
        for method, op in ops.items():
            if method.lower() not in ('get', 'post', 'put', 'patch', 'delete'):
                continue
            endpoints.append({'method': method.upper(), 'path': path, 'summary': op.get('summary') or op.get('description', '')[:200],
                              'parameters': [{'name': p.get('name'), 'in': p.get('in'), 'required': p.get('required', False)}
                                             for p in op.get('parameters', []) if isinstance(p, dict)]})
    info = doc.get('info', {})
    return {'kind': 'openapi', 'url': url, 'title': info.get('title'), 'version': info.get('version'), 'servers': servers,
            'endpoints': endpoints, 'retrieved_at': _now()}


def _dig(value, path: str | None):
    for part in (path or '').split('.'):
        if not part:
            continue
        value = value[int(part)] if isinstance(value, list) else (value or {}).get(part)
    return value


def fetch_api(ctx, url: str, params: dict | None = None, items_path: str | None = None, pagination: dict | None = None,
              connection_id: str | None = None, save_as: str | None = None) -> dict:
    """GET a JSON API, following pagination, and optionally save all items to the workspace (JSON or CSV)."""
    from pathlib import Path
    headers = {'Accept': 'application/json'}
    if connection_id:
        from .service_connections import ServiceConnectionStore
        store = ServiceConnectionStore()
        conn = store.get(connection_id)
        headers.update(conn.authorize(url, store.secret(conn)))
    page = dict(pagination or {})
    mode, max_pages = page.get('mode', 'none'), int(page.get('max_pages', 100))
    items, pages, next_url, query = [], [], url, dict(params or {})
    number, offset = int(page.get('start', 1)), 0
    for _ in range(max_pages):
        if mode == 'page':
            query[page.get('param', 'page')] = number
        elif mode == 'offset':
            query[page.get('param', 'offset')] = offset
            if page.get('size'):
                query[page.get('size_param', 'limit')] = page['size']
        r = _get(next_url, params=query if next_url == url or mode != 'link' else None, headers=headers)
        if r.status_code >= 400:
            raise EngineError(f'The API returned HTTP {r.status_code} for {r.url}', code='http-error')
        body = r.json()
        batch = _dig(body, items_path) if items_path else body
        batch = batch if isinstance(batch, list) else [batch]
        items.extend(batch)
        pages.append({'url': r.url, 'items': len(batch), 'http_status': r.status_code})
        if mode == 'none' or not batch:
            break
        if mode == 'page':
            number += 1
        elif mode == 'offset':
            offset += len(batch)
        elif mode == 'cursor':
            cursor = _dig(body, page.get('cursor_path'))
            if not cursor:
                break
            query[page.get('param', 'cursor')] = cursor
        elif mode == 'link':
            nxt = r.links.get('next', {}).get('url') or _dig(body, page.get('next_path'))
            if not nxt:
                break
            next_url = urljoin(r.url, nxt)
    result = {'url': url, 'pages': pages, 'item_count': len(items), 'retrieved_at': _now()}
    if save_as:
        target = ctx.workspace_path(save_as)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix.lower() == '.csv':
            import pandas as pd
            pd.json_normalize(items).to_csv(target, index=False)
        else:
            target.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding='utf-8')
        from .agent import _sha256
        provenance = {**result, 'saved_as': str(target), 'sha256': _sha256(target), 'tool': f'flowbench-agent/{__version__}'}
        Path(f'{target}.source.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')
        result.update(saved_as=str(target), sha256=provenance['sha256'])
    else:
        result['items'] = items
    return result
