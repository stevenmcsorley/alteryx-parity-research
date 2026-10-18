"""Microsoft Power BI Output: write records to a Power BI push semantic model (dataset); no vendor code.

Functional source: the public Microsoft Power BI Output tool help and the Power BI REST API (Push
Datasets). The connection is a Web/API connection with OAuth: base URL
``https://api.powerbi.com/v1.0/myorg`` and scope ``https://analysis.windows.net/powerbi/api/.default``.

The tool finds the dataset by name in the workspace (or ``My workspace``) and creates it as a push
dataset with one table when it is missing. Rows are posted in batches of at most 10,000 (the
documented per-request limit). ``replace`` deletes the table's rows first; a changed schema needs
``update_schema``, which updates the table definition. Push datasets allow 75 columns and 4,000
characters per text value; both are checked before anything is sent.
"""
from __future__ import annotations

import datetime as _dt
import urllib.parse
from decimal import Decimal

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..types import FieldType
from .github import _permit
from .msgraph import Graph

MAX_ROWS_PER_POST = 10_000
MAX_COLUMNS = 75
MAX_TEXT = 4000


class PowerBI(Graph):
    service = 'Power BI'


def _data_type(t: FieldType) -> str:
    if t is FieldType.BOOL:
        return 'Boolean'
    if t.is_integer:
        return 'Int64'
    if t is FieldType.FIXED_DECIMAL:
        return 'Decimal'
    if t in (FieldType.FLOAT, FieldType.DOUBLE):
        return 'Double'
    if t in (FieldType.DATE, FieldType.DATETIME):
        return 'Datetime'
    if t is FieldType.BLOB:
        raise ConfigError('Power BI push datasets cannot hold binary fields; remove or convert them first')
    return 'String'


def _value(v):
    if isinstance(v, _dt.datetime):
        return v.strftime('%Y-%m-%dT%H:%M:%S')
    if isinstance(v, _dt.date):
        return v.isoformat() + 'T00:00:00'
    if isinstance(v, _dt.time):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


class PowerBIOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.power_bi_output', version='1.0.0', display_name='Microsoft Power BI Output',
        catalog_ids=('connectors:microsoft-power-bi-output',),
        inputs=(Anchor('input', 'I', description='Records to push'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Push records to a Power BI push dataset table: created when missing, rows appended or replaced, '
                    'posted in batches of 10,000. A changed schema needs update_schema.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'dataset', 'table'], 'properties': {
            'provider': {'type': 'string', 'enum': ['microsoft-power-bi']},
            'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                              'description': 'Saved Power BI connection (OAuth), base URL https://api.powerbi.com/v1.0/myorg'},
            'workspace_id': {'type': ['string', 'null'], 'default': None, 'description': 'Workspace (group) id; empty for My workspace'},
            'dataset': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'table': {'type': 'string', 'minLength': 1, 'maxLength': 100},
            'mode': {'enum': ['append', 'replace'], 'default': 'append'},
            'retention_policy': {'enum': ['None', 'basicFIFO'], 'default': 'None',
                                 'description': 'For a new dataset: keep every row (up to 5,000,000) or the newest 200,000'},
            'update_schema': {'type': 'boolean', 'default': False},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        if len(schema) > MAX_COLUMNS:
            raise ConfigError(f'Power BI push tables allow {MAX_COLUMNS} columns; the input has {len(schema)}')
        for f in schema:
            _data_type(f.type)
        return {}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'microsoft-power-bi')
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        columns = [{'name': f.name, 'dataType': _data_type(f.type)} for f in table.schema]
        rows = [{k: _value(v) for k, v in rec.items()} for rec in table.to_dicts()]
        for i, r in enumerate(rows, start=1):
            for k, v in r.items():
                if isinstance(v, str) and len(v) > MAX_TEXT:
                    raise DataError(f'Record {i} field {k!r} has {len(v)} characters; Power BI accepts {MAX_TEXT} per value')
        _permit(ctx, writes=True)
        pbi = PowerBI(cfg)
        root = f"groups/{urllib.parse.quote(cfg['workspace_id'], safe='')}" if cfg['workspace_id'] else ''
        datasets = (pbi.request('GET', f'{root}/datasets'.lstrip('/')) or {}).get('value', [])
        match = [d for d in datasets if d.get('name') == cfg['dataset']]
        if len(match) > 1:
            raise DataError(f"{len(match)} datasets are called {cfg['dataset']!r} in this workspace; rename them or use another name")
        tq = urllib.parse.quote(cfg['table'], safe='')
        if not match:
            created = pbi.request('POST', f"{root}/datasets?defaultRetentionPolicy={cfg['retention_policy']}".lstrip('/'), {
                'name': cfg['dataset'], 'defaultMode': 'Push', 'tables': [{'name': cfg['table'], 'columns': columns}]})
            dataset_id = created['id']
            ctx.info(f"created push dataset {cfg['dataset']!r}")
        else:
            dataset_id = match[0]['id']
            base = f'{root}/datasets/{dataset_id}'.lstrip('/')
            tables = {t['name']: t for t in (pbi.request('GET', f'{base}/tables') or {}).get('value', [])}
            existing = tables.get(cfg['table'])
            if existing is None or existing.get('columns') is None or \
                    [(c['name'], c['dataType'].lower()) for c in existing['columns']] != [(c['name'], c['dataType'].lower()) for c in columns]:
                if existing is not None and existing.get('columns') is not None and not cfg['update_schema']:
                    raise DataError(f"Table {cfg['table']!r} in {cfg['dataset']!r} has different columns; set update_schema "
                                    'to change its definition')
                pbi.request('PUT', f'{base}/tables/{tq}', {'name': cfg['table'], 'columns': columns})
        base = f'{root}/datasets/{dataset_id}'.lstrip('/')
        if cfg['mode'] == 'replace':
            pbi.request('DELETE', f'{base}/tables/{tq}/rows')
        for start in range(0, len(rows), MAX_ROWS_PER_POST):
            ctx.check_cancelled()
            pbi.request('POST', f'{base}/tables/{tq}/rows', {'rows': rows[start:start + MAX_ROWS_PER_POST]})
        ctx.info(f"pushed {len(rows):,} rows to {cfg['dataset']}/{cfg['table']} ({cfg['mode']})")
        return {}


TOOLS = [PowerBIOutput]
