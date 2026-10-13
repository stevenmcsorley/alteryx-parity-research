"""Extract short factual metadata from a public tool help page.

Stores only: title, headings, anchor labels/letters/direction guesses, and which
dependency keywords appear. Never stores page body text. Every value produced
here is ``auto-extracted`` and must be reviewed before it becomes a contract.
"""
from __future__ import annotations

import re

# Keyword -> dependency signal. Presence only; the page text itself is not kept.
DEPENDENCY_SIGNALS = {
    r'\bR\b(?: tool| code| package| installation|-based)|Predictive Tools': 'r-runtime',
    r'\bPython\b': 'python-runtime',
    r'\bAPI key|access token|OAuth|client secret|credentials?\b': 'credentials',
    r'\bODBC|OLE ?DB|JDBC|database driver|\bdriver\b': 'database-driver',
    r'\bIn-Database|In-DB\b': 'in-database-connection',
    r'\bdataset|data package|data install|Data Products?\b': 'licensed-dataset',
    r'\blicen[cs]e|subscription|add-on\b': 'licensing',
    r'\bAlteryx Server|Gallery\b': 'alteryx-server',
    r'\bAlteryx (?:One|Auto Insights|Machine Learning|Intelligence Suite|Analytics Cloud)\b': 'vendor-cloud-service',
    r'\bmacro\b': 'macro-runtime',
    r'\bspatial object|\bgeocod|\bCASS\b|\bpostal\b': 'spatial-or-address',
    r'\bdeprecat|retired|no longer supported': 'deprecated',
    r'\bLLM|generative|OpenAI|large language model|AI model': 'llm-service',
    r'\bGPU|TensorFlow|PyTorch|deep learning|neural': 'ml-framework',
    r'\bOCR|image\b': 'image-processing',
}

_ANCHOR_RE = re.compile(r'^\s*(?P<label>[^:]{1,80}?)\s+anchors?\s*[:\-–]', re.I)


def _clean(text: str, limit: int = 120) -> str:
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def _section_for(heading):
    return heading.find_parent('section') or heading.parent


def _anchor_from_element(el):
    text = _clean(el.get_text(' ', strip=True), 300)
    m = _ANCHOR_RE.match(text)
    if not m:
        return None
    strong = el.find('strong')
    letter = _clean(strong.get_text(' ', strip=True), 8) if strong else None
    label = m.group('label')
    if letter and label.startswith(letter):
        label = label[len(letter):].strip()
    label = label.strip('() ').strip() or letter
    if not label or len(label.split()) > 6 or re.match(r'(?i)(the\b|.*\bhas\b)', label):
        return None
    after = text[m.end():m.end() + 120].lower()
    if re.search(r'\b(input|question)\b', label.lower()):
        direction = 'input'
    elif re.search(r'\boutput\b', label.lower()):
        direction = 'output'
    elif re.search(r'\boutputs?\b|\bdisplays\b|\bresults?\b', after):
        direction = 'output'
    elif re.search(r'\binputs?\b|\bconnect\b', after):
        direction = 'input'
    else:
        direction = 'unknown'
    return {'label': _clean(label, 60), 'letter': letter, 'direction_guess': direction}


def extract_anchors(soup):
    """Return (anchors, method) from 'Tool Components'-style sections, else whole-page lines."""
    def collect(elements):
        out, seen = [], set()
        for el in elements:
            if el.find(['li', 'p']) and el.name == 'li' and el.find('li'):
                continue
            a = _anchor_from_element(el)
            if a and (a['label'], a['letter']) not in seen:
                seen.add((a['label'], a['letter']))
                out.append(a)
        return out
    for heading in soup.find_all(['h2', 'h3']):
        title = heading.get_text(' ', strip=True).lower()
        if 'component' in title or 'anchor' in title:
            anchors = collect(_section_for(heading).find_all(['li', 'p']))
            if anchors:
                return anchors, 'tool-components-section'
    anchors = collect(soup.find_all(['li', 'p']))
    return anchors, ('page-anchor-lines' if anchors else 'none-found')


def extract_metadata(html: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main') or soup.find('article') or soup.body or soup
    for noise in main.find_all(['nav', 'script', 'style', 'footer']):
        noise.decompose()
    headings = []
    for el in main.find_all(['h1', 'h2', 'h3', 'h4']):
        text = _clean(el.get_text(' ', strip=True))
        if text and text.lower() != 'search results':
            headings.append({'level': int(el.name[1]), 'text': text})
    body = main.get_text(' ', strip=True)
    anchors, anchor_method = extract_anchors(main)
    signals = sorted({sig for pattern, sig in DEPENDENCY_SIGNALS.items() if re.search(pattern, body, re.I)})
    return {
        'page_title': _clean(soup.title.get_text(' ', strip=True), 200) if soup.title else None,
        'headings': headings[:60],
        'anchors': anchors,
        'anchor_extraction': anchor_method,
        'dependency_signals': signals,
    }
