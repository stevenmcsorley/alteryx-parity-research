"""DataRobot AutoModel and Predict, and Adobe Analytics, through their public REST APIs; no vendor SDKs.

* DataRobot (API v2, base URL ``https://app.datarobot.com/api/v2`` or your instance): a Web/API connection with your
  API key as a Bearer token. AutoModel uploads the records as a dataset (``datasets/fromFile/``), creates a project from
  it, and starts Autopilot on the target (``projects/<id>/aim/``); asynchronous steps are polled through the Location
  each 202 answer gives, until it redirects to the result. Predict uploads the records as a prediction dataset, asks the
  chosen (or recommended) model for predictions and joins them back to the records by row.
* Adobe Analytics (Reporting API 2.0): a Web/API connection with OAuth (Adobe IMS, client credentials or browser
  sign-in) whose base URL is ``https://analytics.adobe.io/api/<global-company-id>``; the client id is also sent as
  ``x-api-key``. The reference tool uses the older 1.4 API; this tool uses 2.0 (one dimension per request; Adobe
  recommends several simple calls over one large one).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import uuid

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, cast_value, type_for_values
from ._common import require_fields
from ._remote_files import written_file
from .github import _permit
from .msgraph import Graph

TEXT = 2**30 - 1
POLL_SECONDS = 3


def _t(name):
    return Field(name, FieldType.V_WSTRING, TEXT)


class DataRobot(Graph):
    service = 'DataRobot'
    other_auth = True

    def upload(self, path, local, ctx):
        boundary = uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="data.csv"\r\n'
                'Content-Type: text/csv\r\n\r\n').encode() + local.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
        doc, headers = self.request('POST', path, body, {'Content-Type': f'multipart/form-data; boundary={boundary}'},
                                    with_headers=True)
        return doc or {}, headers.get('Location')

    def wait(self, location, ctx, what, timeout):
        """Poll an asynchronous status URL until it redirects to its result; returns the result URL."""
        if not location:
            raise DataError(f'DataRobot gave no status location for {what}')
        deadline = time.monotonic() + timeout
        while True:
            ctx.check_cancelled()
            doc, headers = self.request('GET', location, with_headers=True, accept_redirect=True)
            if headers.get('Location') and not doc:
                return headers['Location']
            status = (doc or {}).get('status', '').upper()
            if status in ('ERROR', 'ABORTED', 'FAILED'):
                raise DataError(f"DataRobot could not finish {what}: {(doc or {}).get('message') or status}")
            if status == 'COMPLETED' and (doc or {}).get('statusId') is None and headers.get('Location'):
                return headers['Location']
            if time.monotonic() > deadline:
                raise DataError(f'DataRobot did not finish {what} in {timeout:.0f} s')
            time.sleep(POLL_SECONDS)


_DR = {'provider': {'type': 'string', 'enum': ['datarobot']}, 'provider_semantics': {'type': 'string'},
       'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                         'description': 'https://app.datarobot.com/api/v2 (or your instance), Bearer API key'},
       'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 7200, 'default': 1800}}


def _dr_provider(cfg):
    from .. import providers
    providers.resolve(cfg, 'datarobot')


def _project_url(dr, pid):
    base = dr.base[:-len('/api/v2')] if dr.base.endswith('/api/v2') else dr.base
    return f'{base}/projects/{pid}/models'


class DataRobotAutoModel(Tool):
    manifest = ToolManifest(
        tool_id='core.datarobot_automodel', version='1.0.0', display_name='DataRobot AutoModel',
        catalog_ids=('predictive:datarobot-automodel',),
        inputs=(Anchor('input', 'I', description='Training records with the target field'),),
        outputs=(Anchor('output', 'O', description='The project created'),), side_effects='network', deterministic=False,
        description='Upload records to DataRobot, create a project and start Autopilot on the target (quick, auto, '
                    'comprehensive or manual mode, with a worker count). Returns the project id and the link to its models.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'project_name', 'target'],
                       'properties': {**_DR,
            'project_name': {'type': 'string', 'minLength': 1}, 'target': {'type': 'string', 'minLength': 1},
            'mode': {'enum': ['quick', 'auto', 'comprehensive', 'manual'], 'default': 'quick'},
            'worker_count': {'type': 'integer', 'minimum': -1, 'default': 2, 'description': '-1 uses the maximum available'}}})

    def effects(self, cfg):
        return {'network_requests', 'remote_writes'}

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['target']])
        return {'output': Schema([_t('ProjectId'), _t('ProjectName'), _t('Target'), _t('Mode'), _t('ProjectUrl')])}

    def run(self, ctx, cfg, inputs):
        _dr_provider(cfg)
        schema = self.infer(ctx, cfg, {'input': inputs['input'].schema})['output']
        _permit(ctx, writes=True)
        dr = DataRobot(cfg)
        with written_file(ctx, {'format': 'csv'}, inputs, '.csv') as local:
            doc, location = dr.upload('datasets/fromFile/', local, ctx)
        dataset_id = doc.get('catalogId')
        if not dataset_id:
            raise DataError('DataRobot did not return the dataset id')
        dr.wait(location, ctx, 'the dataset upload', cfg['timeout_seconds'])
        created, headers = dr.request('POST', 'projects/', {'datasetId': dataset_id, 'projectName': cfg['project_name']}, with_headers=True)
        pid = (created or {}).get('pid')
        if not pid:
            raise DataError('DataRobot did not return the project id')
        dr.wait(headers.get('Location'), ctx, 'project creation', cfg['timeout_seconds'])
        dr.request('PATCH', f'projects/{urllib.parse.quote(pid, safe="")}/', {'workerCount': cfg['worker_count']})
        _, aim = dr.request('PATCH', f'projects/{urllib.parse.quote(pid, safe="")}/aim/', {'target': cfg['target'], 'mode': cfg['mode']},
                            with_headers=True)
        dr.wait(aim.get('Location'), ctx, 'starting Autopilot', cfg['timeout_seconds'])
        url = _project_url(dr, pid)
        ctx.info(f'DataRobot project {pid} started ({cfg["mode"]} Autopilot): {url}')
        return {'output': Table.from_rows(schema, [(pid, cfg['project_name'], cfg['target'], cfg['mode'], url)])}


class DataRobotPredict(Tool):
    manifest = ToolManifest(
        tool_id='core.datarobot_predict', version='1.0.0', display_name='DataRobot Predict', catalog_ids=('predictive:datarobot-predict',),
        inputs=(Anchor('input', 'I', description='Records to score'),),
        outputs=(Anchor('output', 'O', description='The records with predictions'),), side_effects='network', deterministic=False,
        description='Score records with a DataRobot model (a model id, or the project’s recommended model): adds Prediction '
                    'and, for classification, one probability field per class.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id', 'project_id'],
                       'properties': {**_DR, 'project_id': {'type': 'string', 'minLength': 1},
            'model_id': {'type': ['string', 'null'], 'default': None, 'description': 'Empty uses the recommended model'},
            'prefix': {'type': 'string', 'default': ''}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        _dr_provider(cfg)
        table: Table = inputs['input']
        _permit(ctx)
        dr = DataRobot(cfg)
        pid = urllib.parse.quote(cfg['project_id'], safe='')
        model_id = cfg['model_id'] or (dr.request('GET', f'projects/{pid}/recommendedModels/recommendedModel/') or {}).get('modelId')
        if not model_id:
            raise DataError('The project has no recommended model yet; wait for Autopilot or name a model_id')
        with written_file(ctx, {'format': 'csv'}, inputs, '.csv') as local:
            _, location = dr.upload(f'projects/{pid}/predictionDatasets/fileUploads/', local, ctx)
        dataset = dr.request('GET', dr.wait(location, ctx, 'the prediction dataset upload', cfg['timeout_seconds']))
        _, headers = dr.request('POST', f'projects/{pid}/predictions/', {'modelId': model_id, 'datasetId': dataset['id']}, with_headers=True)
        result = dr.request('GET', dr.wait(headers.get('Location'), ctx, 'the predictions', cfg['timeout_seconds']))
        by_row = {int(p['rowId']): p for p in result.get('data', [])}
        labels = sorted({str(v['label']) for p in by_row.values() for v in p.get('predictionValues', [])
                         if result.get('task', '').lower() != 'regression'}) if any(p.get('predictionValues') for p in by_row.values()) else []
        if len(labels) <= 1:
            labels = []
        pre = cfg['prefix']
        preds = [by_row.get(i, {}).get('prediction') for i in range(len(table))]
        pred_type = type_for_values(preds)
        added = [Field(f'{pre}Prediction', pred_type, TEXT if pred_type.is_string else None),
                 *[Field(f'{pre}Probability_{l}', FieldType.DOUBLE) for l in labels]]
        clash = [f.name for f in added if f.name in table.schema]
        if clash:
            raise ConfigError(f'Field(s) {clash} already exist; set a prefix')
        rows = []
        for i, row in enumerate(table.rows()):
            p = by_row.get(i, {})
            probs = {str(v['label']): v.get('value') for v in p.get('predictionValues', [])}
            rows.append([*row, cast_value(p.get('prediction'), added[0])[0], *[probs.get(l) for l in labels]])
        missing = len(table) - len(by_row)
        if missing:
            ctx.warn(f'{missing} record(s) came back without a prediction')
        ctx.info(f'scored {len(by_row):,} records with model {model_id}')
        return {'output': Table.from_rows(Schema([*table.schema, *added]), rows)}


# --------------------------------------------------------------------------- Adobe Analytics

class Adobe(Graph):
    service = 'Adobe Analytics'


class AdobeAnalytics(Tool):
    manifest = ToolManifest(
        tool_id='core.adobe_analytics', version='1.0.0', display_name='Adobe Analytics', catalog_ids=('connectors:adobe-analytics',),
        inputs=(), outputs=(Anchor('output', 'R', description='Report rows'),), side_effects='network', deterministic=False,
        description='Run an Adobe Analytics report (Reporting API 2.0) on a report suite: metrics broken down by one '
                    'dimension (or totals), for a date range, optionally filtered by segments; all pages.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'report_suite', 'metrics', 'start', 'end'], 'properties': {
            'provider': {'type': 'string', 'enum': ['adobe-analytics']}, 'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                              'description': 'OAuth connection, base URL https://analytics.adobe.io/api/<global-company-id>'},
            'report_suite': {'type': 'string', 'minLength': 1, 'description': 'Report suite id (rsid)'},
            'metrics': {'type': 'array', 'minItems': 1, 'maxItems': 50, 'items': {'type': 'string', 'pattern': '^metrics/'}},
            'dimension': {'type': ['string', 'null'], 'default': None, 'pattern': '^variables/', 'description': 'e.g. variables/page; empty for totals'},
            'segments': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
            'start': {'type': 'string', 'pattern': '^\\d{4}-\\d\\d-\\d\\d', 'description': 'Start date, inclusive (YYYY-MM-DD)'},
            'end': {'type': 'string', 'pattern': '^\\d{4}-\\d\\d-\\d\\d', 'description': 'End date, exclusive (YYYY-MM-DD)'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50000, 'default': 50000, 'description': 'Rows in all'},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 120}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'adobe-analytics')
        _permit(ctx)
        api = Adobe(cfg)
        if not api.conn.client_id:
            raise ConfigError('The Adobe connection needs its client id (sent as x-api-key)')
        date_range = f"{cfg['start'][:10]}T00:00:00.000/{cfg['end'][:10]}T00:00:00.000"
        body = {'rsid': cfg['report_suite'],
                'globalFilters': [{'type': 'dateRange', 'dateRange': date_range}, *[{'type': 'segment', 'segmentId': s} for s in cfg['segments']]],
                'metricContainer': {'metrics': [{'columnId': str(i), 'id': m} for i, m in enumerate(cfg['metrics'])]},
                'settings': {'countRepeatInstances': True, 'limit': min(cfg['limit'], 50000), 'page': 0}}
        if cfg['dimension']:
            body['dimension'] = cfg['dimension']
        headers = {'x-api-key': api.conn.client_id}
        records, page = [], 0
        while True:
            ctx.check_cancelled()
            body['settings']['page'] = page
            doc = api.request('POST', 'reports', body, headers) or {}
            if not cfg['dimension']:
                totals = (doc.get('summaryData') or {}).get('totals') or []
                records.append({m.split('/', 1)[1]: v for m, v in zip(cfg['metrics'], totals)})
                break
            for r in doc.get('rows', []):
                records.append({'ItemId': r.get('itemId'), cfg['dimension'].split('/', 1)[1]: r.get('value'),
                                **{m.split('/', 1)[1]: v for m, v in zip(cfg['metrics'], r.get('data', []))}})
            page += 1
            if doc.get('lastPage', True) or page >= int(doc.get('totalPages') or 0) or len(records) >= cfg['limit']:
                break
        names = ([] if not cfg['dimension'] else ['ItemId', cfg['dimension'].split('/', 1)[1]]) + [m.split('/', 1)[1] for m in cfg['metrics']]
        fields, cols = [], []
        for n in names:
            vals = [r.get(n) for r in records[:cfg['limit']]]
            t = FieldType.V_WSTRING if n == 'ItemId' else type_for_values(vals)
            f = Field(n, t, TEXT if t.is_string else None)
            fields.append(f)
            cols.append([cast_value(v, f)[0] for v in vals])
        ctx.info(f'{len(records):,} report row(s) from {cfg["report_suite"]}')
        return {'output': Table(Schema(fields), cols)}


TOOLS = [DataRobotAutoModel, DataRobotPredict, AdobeAnalytics]
