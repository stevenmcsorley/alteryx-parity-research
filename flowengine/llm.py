"""Calls to large language models for the AI workflow tools, through saved connections only.

Two provider APIs: ``openai-compatible`` (the Chat Completions API served by OpenAI, Ollama, LM Studio, vLLM and
Google's OpenAI endpoint; base URL such as https://api.openai.com/v1 or http://localhost:11434/v1) and ``anthropic``
(the Messages API, base URL https://api.anthropic.com, the key in an ``x-api-key`` header connection). Workflows name
the provider, the connection id and the model; keys stay in the vault.

Structured output: with a JSON schema, OpenAI-compatible servers are asked for ``response_format: json_schema`` (Ollama
and OpenAI enforce it); Anthropic is asked to answer with JSON only. Either way the answer is parsed and validated
against the schema here, never trusted blindly. Attachments are images (PNG, JPEG, GIF, WebP) and PDFs; for
OpenAI-compatible servers PDFs are rendered to page images first, because chat completions take images.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request

from .errors import ConfigError, DataError

PROVIDERS = ('openai-compatible', 'anthropic')
ANTHROPIC_VERSION = '2023-06-01'
RETRIES = 3
MAX_PDF_PAGES = 10


def media_type(data: bytes) -> str | None:
    if data.startswith(b'\x89PNG'):
        return 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith(b'%PDF'):
        return 'application/pdf'
    return None


def pdf_pages_as_png(data: bytes, limit=MAX_PDF_PAGES) -> list[bytes]:
    import io
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    try:
        if len(pdf) > limit:
            raise DataError(f'The PDF has {len(pdf)} pages; at most {limit} are sent to the model')
        out = []
        for i in range(len(pdf)):
            img = pdf[i].render(scale=1.5).to_pil()
            buf = io.BytesIO()
            img.save(buf, 'PNG')
            out.append(buf.getvalue())
        return out
    finally:
        pdf.close()


class Model:
    """A provider, connection and model, with generation settings (what LLM Override passes downstream)."""

    def __init__(self, spec: dict):
        from .service_connections import ServiceConnectionStore
        if spec.get('provider') not in PROVIDERS:
            raise ConfigError(f"Choose an LLM provider ({', '.join(PROVIDERS)}); Flowbench does not pick one")
        if not spec.get('model'):
            raise ConfigError('Name the model, for example gpt-4o-mini, qwen2.5:3b or claude-sonnet-4-5')
        self.spec = spec
        store = ServiceConnectionStore()
        self.conn = store.get(spec['connection_id'], kind='web')
        self.secret = store.secret(self.conn)
        self.base = self.conn.base_url.rstrip('/')
        self.timeout = float(spec.get('timeout_seconds') or 180)
        self.calls = 0

    def _post(self, path, body, extra_headers=None):
        url = f'{self.base}/{path}'
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', **(extra_headers or {})}
        if self.conn.auth != 'none':
            headers.update(self.conn.authorize(url, self.secret))
        from .tools.download import _HttpOnlyRedirects
        opener = urllib.request.build_opener(_HttpOnlyRedirects(self.conn))
        data = json.dumps(body).encode()
        for attempt in range(RETRIES + 1):
            req = urllib.request.Request(url, data=data, method='POST', headers=headers)
            try:
                with opener.open(req, timeout=self.timeout) as r:
                    self.calls += 1
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                with e:
                    detail = e.read(4000).decode('utf-8', 'replace')
                if e.code in (429, 500, 502, 503, 529) and attempt < RETRIES:
                    retry = e.headers.get('Retry-After')
                    time.sleep(min(float(retry) if retry and retry.replace('.', '', 1).isdigit() else 2 ** attempt, 30))
                    continue
                try:
                    err = json.loads(detail).get('error')
                    detail = err.get('message', detail) if isinstance(err, dict) else (err or detail)
                except ValueError:
                    pass
                raise DataError(f'The model service returned {e.code}: {str(detail)[:400]}') from None
            except (urllib.error.URLError, OSError) as e:
                if attempt < RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                raise DataError(f'Could not reach the model service at {self.base}: {getattr(e, "reason", None) or e}') from None

    def complete(self, prompt: str, *, system: str | None = None, schema: dict | None = None,
                 attachments: list[bytes] = ()) -> str:
        """The model's answer to one prompt (with optional attachments); JSON text when a schema is given."""
        s = self.spec
        if self.spec['provider'] == 'anthropic':
            content = []
            for data in attachments:
                mt = media_type(data)
                if mt is None:
                    raise DataError('An attachment is not a PNG, JPEG, GIF, WebP image or a PDF')
                kind = 'document' if mt == 'application/pdf' else 'image'
                content.append({'type': kind, 'source': {'type': 'base64', 'media_type': mt, 'data': base64.b64encode(data).decode()}})
            text = prompt if schema is None else (
                f'{prompt}\n\nAnswer with JSON only (no prose, no code fences) that matches this JSON schema:\n{json.dumps(schema)}')
            content.append({'type': 'text', 'text': text})
            body = {'model': s['model'], 'max_tokens': int(s.get('max_tokens') or 4096), 'messages': [{'role': 'user', 'content': content}]}
            if system:
                body['system'] = system
            for k in ('temperature', 'top_p'):
                if s.get(k) is not None:
                    body[k] = s[k]
            doc = self._post('v1/messages', body, {'anthropic-version': ANTHROPIC_VERSION})
            answer = ''.join(b.get('text', '') for b in doc.get('content', []) if b.get('type') == 'text')
        else:
            parts = [{'type': 'text', 'text': prompt}]
            for data in attachments:
                mt = media_type(data)
                if mt is None:
                    raise DataError('An attachment is not a PNG, JPEG, GIF, WebP image or a PDF')
                images = pdf_pages_as_png(data) if mt == 'application/pdf' else [data]
                mt = 'image/png' if mt == 'application/pdf' else mt
                parts += [{'type': 'image_url', 'image_url': {'url': f'data:{mt};base64,{base64.b64encode(im).decode()}'}} for im in images]
            messages = ([{'role': 'system', 'content': system}] if system else []) + \
                [{'role': 'user', 'content': parts if attachments else prompt}]
            body = {'model': s['model'], 'messages': messages}
            if s.get('max_tokens'):
                body['max_tokens'] = int(s['max_tokens'])
            for k in ('temperature', 'top_p'):
                if s.get(k) is not None:
                    body[k] = s[k]
            if schema is not None:
                body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'output', 'schema': schema}}
            doc = self._post('chat/completions', body)
            choices = doc.get('choices') or [{}]
            answer = (choices[0].get('message') or {}).get('content') or ''
        return answer.strip()

    def complete_json(self, prompt: str, schema: dict, **kw):
        """Parsed JSON validated against the schema; raises DataError with the reason when the model's answer is invalid."""
        import jsonschema
        text = self.complete(prompt, schema=schema, **kw)
        cleaned = text.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.strip('`').split('\n', 1)[-1].rsplit('```', 1)[0]
        try:
            value = json.loads(cleaned)
            jsonschema.validate(value, schema)
        except (ValueError, jsonschema.ValidationError) as e:
            raise DataError(f'The model did not return valid JSON for the schema: {getattr(e, "message", e)}'[:400]) from None
        return value


MODEL_FIELDS = ('Provider', 'ConnectionId', 'Model', 'Temperature', 'MaxTokens', 'TopP')


def model_from(cfg, inputs, anchor='model'):
    """The model a tool uses: the LLM Override record on its model input when connected, else its own settings."""
    table = inputs.get(anchor)
    if table is not None:
        if len(table) != 1:
            raise ConfigError('The model input takes the single record from LLM Override')
        rec = table.to_dicts()[0]
        spec = {'provider': rec.get('Provider'), 'connection_id': rec.get('ConnectionId'), 'model': rec.get('Model') or cfg.get('model'),
                'temperature': rec.get('Temperature'), 'max_tokens': rec.get('MaxTokens'), 'top_p': rec.get('TopP')}
    else:
        if not cfg.get('provider') or not cfg.get('connection_id'):
            raise ConfigError('Connect LLM Override to the model input, or set provider, connection_id and model')
        spec = {k: cfg.get(k) for k in ('provider', 'connection_id', 'model', 'temperature', 'max_tokens', 'top_p')}
    spec['timeout_seconds'] = cfg.get('timeout_seconds')
    from . import providers
    providers.resolve({'provider': spec['provider']}, 'llm')
    return Model(spec)
