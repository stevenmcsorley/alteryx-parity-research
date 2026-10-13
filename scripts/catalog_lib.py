"""Documented Alteryx Designer tool inventory; no proprietary application data."""
from __future__ import annotations
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / 'data' / 'tool-list-seed.txt'
CATALOG = ROOT / 'data' / 'catalog.json'
SOURCE = 'https://help.alteryx.com/current/en/designer/tools/designer-tools-list.html'
BASE = 'https://help.alteryx.com'


def seed_rows():
    rows = []
    category = None
    for line in SEED.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            category = line[1:-1]
            continue
        if category is None:
            raise ValueError('Tool without category')
        for name in line.split(';'):
            name = name.strip()
            if name:
                key = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
                rows.append({'id': f'{re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")}:{key}',
                             'category': category, 'name': name, 'xml_tool_name': None,
                             'user_role': None, 'documentation_url': None,
                             'source_url': SOURCE, 'source_state': 'seed-name-only',
                             'implementation_status': 'not-started', 'verification_status': 'not-tested'})
    return rows


def save_catalog(rows, metadata):
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    doc = {'metadata': metadata, 'tools': rows}
    CATALOG.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    with (ROOT / 'data' / 'catalog.csv').open('w', encoding='utf-8', newline='') as f:
        cols = ['id', 'category', 'name', 'xml_tool_name', 'user_role', 'documentation_url', 'source_state', 'implementation_status', 'verification_status']
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def clean_name(name):
    name = re.sub(r'\s+', ' ', name).strip()
    return re.sub(r'\s*\(Moved to Connect tool category in 2025\.2\)\s*$', '', name).replace(' Make Columns Tool', '').strip()


def parse_official_html(html, base_url=SOURCE):
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, 'html.parser')
    expected = {r['category'] for r in seed_rows()}
    rows = []
    category = None
    for el in soup.find_all(['h2', 'table']):
        if el.name == 'h2':
            candidate = el.get_text(' ', strip=True)
            category = candidate if candidate in expected else None
        elif category:
            trs = el.find_all('tr')
            if not trs:
                continue
            header = [re.sub(r'\s+', ' ', c.get_text(' ', strip=True)).lower() for c in trs[0].find_all(['th','td'], recursive=False)]
            if len(header) < 3 or 'xml tool name' not in header[1] or 'user role' not in header[2]:
                continue
            for tr in trs[1:]:
                cells = tr.find_all(['td', 'th'], recursive=False)
                if len(cells) < 3:
                    continue
                link = cells[0].find('a', href=True)
                raw_name = link.get_text(' ', strip=True) if link else cells[0].get_text(' ', strip=True)
                name = clean_name(raw_name)
                xml = cells[1].get_text(' ', strip=True)
                role = cells[2].get_text(' ', strip=True)
                if not name or not xml or not role:
                    continue
                url = urljoin(base_url, link['href']) if link else None
                rows.append({'category': category, 'name': name, 'xml_tool_name': xml, 'user_role': role,
                             'documentation_url': url, 'source_state': 'official-catalog-verified'})
    return rows


def merge_official(seed, published):
    """Merge while keeping every seeded row, and add newly discovered tools."""
    import copy
    from collections import defaultdict, deque
    q = defaultdict(deque)
    for r in published:
        q[(r['category'], r['name'])].append(r)
    out = []
    for r in seed:
        r = copy.deepcopy(r)
        matches = q.get((r['category'], r['name']))
        if matches:
            r.update(matches.popleft())
        out.append(r)
    for key, matches in q.items():
        while matches:
            r = matches.popleft()
            if not r['name']:
                continue
            slug = re.sub(r'[^a-z0-9]+', '-', r['name'].lower()).strip('-')
            slug_cat = re.sub(r'[^a-z0-9]+', '-', r['category'].lower()).strip('-')
            r.update({'id': f'{slug_cat}:{slug}', 'source_url': SOURCE,
                      'implementation_status':'not-started','verification_status':'not-tested'})
            out.append(r)
    return out
