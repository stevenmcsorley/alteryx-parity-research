"""Time series tools: ARIMA and ETS models, forecasting, comparison, filling, plotting and model factories.

Uses statsmodels (SARIMAX and ETSModel). Models are stored as plain JSON (settings, fitted parameters
and the training series), and forecasting reapplies the fitted parameters without re-estimating, so
results are reproducible and safe to share. Series must be regularly spaced. TS Filler can make
them so. Independent implementations, not the vendor's R forecast package.
"""
from __future__ import annotations

import datetime as _dt
import itertools
import json
import math
import warnings

from .. import charts
from ..errors import ConfigError, DataError, SchemaError
from ..modeling import MODEL_SCHEMA, expect_model_schema, missing, model_table, read_models
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
CHART = Anchor('chart', 'Chart', description='Rendered SVG chart')
FIT_SCHEMA = Schema([Field('Model', FieldType.V_WSTRING, 256), Field('Metric', FieldType.V_WSTRING, 64), Field('Value', FieldType.DOUBLE)])
_SERIES = {'date_field': {'type': 'string', 'minLength': 1}, 'value_field': {'type': 'string', 'minLength': 1},
           'seasonal_period': {'type': 'integer', 'minimum': 1, 'maximum': 400, 'default': 1,
                               'description': 'Observations per season (12 for monthly data with yearly seasonality; 1 = none)'},
           'model_name': {'type': ['string', 'null'], 'default': None}}


# =========================================================================== series handling

def _to_datetime(v):
    if isinstance(v, _dt.datetime):
        return v
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day)
    if isinstance(v, str):
        try:
            return _dt.datetime.fromisoformat(v)
        except ValueError:
            pass
    raise DataError(f'Date value {v!r} is not a date or ISO date text')


def read_series(table, date_field, value_field, extra=()):
    """Sorted (dates, values, extras) for complete records; duplicate dates are an error."""
    require_fields(table.schema, [date_field, value_field, *extra])
    if not table.schema.field(value_field).type.is_numeric:
        raise SchemaError(f'Time series value field {value_field!r} must be numeric')
    rows = []
    for rec in table.to_dicts():
        if missing(rec[date_field]) or missing(rec[value_field]) or any(missing(rec[e]) for e in extra):
            continue
        rows.append((_to_datetime(rec[date_field]), float(rec[value_field]), [float(rec[e]) for e in extra]))
    rows.sort(key=lambda r: r[0])
    dates = [r[0] for r in rows]
    if len(set(dates)) != len(dates):
        raise DataError('Time series has repeated dates; aggregate or group the records first')
    return dates, [r[1] for r in rows], [r[2] for r in rows]


def step_of(dates):
    """('months', n) for calendar-month spacing, otherwise ('seconds', s) of the typical spacing."""
    if len(dates) < 2:
        raise DataError('A time series needs at least two observations')
    months = [(b.year - a.year) * 12 + b.month - a.month for a, b in zip(dates, dates[1:])]
    if all(m == months[0] and m > 0 for m in months) and all(d.day == dates[0].day or d.day >= 28 for d in dates):
        return ('months', months[0])
    gaps = sorted((b - a).total_seconds() for a, b in zip(dates, dates[1:]))
    return ('seconds', gaps[len(gaps) // 2])


def future_dates(last, step, n):
    kind, size = step
    out, current = [], last
    for _ in range(n):
        if kind == 'months':
            m = current.month - 1 + size
            year, month = current.year + m // 12, m % 12 + 1
            day = min(last.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
            current = current.replace(year=year, month=month, day=day)
        else:
            current = current + _dt.timedelta(seconds=size)
        out.append(current)
    return out


# =========================================================================== fitting

def _arima_fit(values, order, seasonal, exog=None):
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        model = SARIMAX(values, exog=exog, order=order, seasonal_order=seasonal, trend='c' if order[1] == 0 else 'n',
                        enforce_stationarity=False, enforce_invertibility=False)
        return model, model.fit(disp=False, maxiter=200)


def fit_arima(ctx, cfg, dates, values, exog):
    import numpy as np
    m = cfg['seasonal_period']
    y = np.asarray(values)
    X = np.asarray(exog) if cfg.get('covariates') else None
    if cfg.get('order'):
        order, seasonal = tuple(cfg['order']), tuple(cfg.get('seasonal_order') or [0, 0, 0]) + (m if m > 1 else 0,)
        candidates = [(order, seasonal)]
    else:
        seasonal_options = [(P, D, Q, m) for P in (0, 1) for D in (0, 1) for Q in (0, 1)] if m > 1 and len(y) >= 3 * m else [(0, 0, 0, 0)]
        candidates = [((p, d, q), s) for p in range(3) for d in range(2) for q in range(3) for s in seasonal_options]
    best = None
    for order, seasonal in candidates:
        ctx.check_cancelled()
        if len(y) <= sum(order) + sum(seasonal[:3]) * max(seasonal[3], 1) + 2:
            continue
        try:
            _, res = _arima_fit(y, order, seasonal, X)
        except Exception:
            continue
        if math.isfinite(res.aic) and (best is None or res.aic < best[0] - 1e-9):
            best = (res.aic, order, seasonal, res)
    if best is None:
        raise DataError('No ARIMA model could be fitted to this series (too short or constant?)')
    _, order, seasonal, res = best
    return {'kind': 'arima', 'order': list(order), 'seasonal_order': list(seasonal), 'params': [float(p) for p in res.params],
            'aic': float(res.aic), 'bic': float(res.bic), 'sigma2': float(res.params[-1]) if 'sigma2' in res.model.param_names else None,
            'fitted': [float(v) for v in res.fittedvalues]}


def _ets_model(values, spec):
    import pandas as pd
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel
    # statsmodels 0.15 ETS prediction intervals need an indexed series (a plain array fails in get_prediction).
    return ETSModel(pd.Series(values, dtype=float), error=spec['error'], trend=spec['trend'], damped_trend=spec['damped'],
                    seasonal=spec['seasonal'], seasonal_periods=spec['period'] if spec['seasonal'] else None)


def fit_ets(ctx, cfg, dates, values):
    import numpy as np
    y = np.asarray(values)
    m = cfg['seasonal_period']
    positive = (y > 0).all()
    errors = [cfg['error']] if cfg.get('error', 'auto') != 'auto' else (['add', 'mul'] if positive else ['add'])
    trends = [None if cfg['trend'] == 'none' else 'add'] if cfg.get('trend', 'auto') != 'auto' else [None, 'add']
    seasonals = ([None if cfg['seasonal'] == 'none' else cfg['seasonal']] if cfg.get('seasonal', 'auto') != 'auto'
                 else ([None, 'add'] + (['mul'] if positive else [])) if m > 1 and len(y) >= 2 * m else [None])
    damped = [cfg.get('damped', False)] if cfg.get('trend', 'auto') != 'auto' else [False, True]
    best = None
    for error, trend, seasonal, damp in itertools.product(errors, trends, seasonals, damped):
        if damp and trend is None:
            continue
        spec = {'error': error, 'trend': trend, 'seasonal': seasonal, 'damped': bool(damp), 'period': m}
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                res = _ets_model(y, spec).fit(disp=False, maxiter=1000)
        except Exception:
            continue
        if math.isfinite(res.aic) and (best is None or res.aic < best[0] - 1e-9):
            best = (res.aic, spec, res)
    if best is None:
        raise DataError('No ETS model could be fitted to this series')
    _, spec, res = best
    return {'kind': 'ets', 'spec': spec, 'params': [float(p) for p in res.params], 'aic': float(res.aic),
            'bic': float(res.bic), 'fitted': [float(v) for v in res.fittedvalues]}


def _forecast(model, h, levels, future_exog=None):
    """(means, {level: (lower, upper)}) for h steps ahead, reusing fitted parameters."""
    import numpy as np
    y = np.asarray(model['values'])
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        if model['kind'] == 'arima':
            from statsmodels.tsa.statespace.sarimax import SARIMAX
            X = np.asarray(model['exog']) if model.get('covariates') else None
            m = SARIMAX(y, exog=X, order=tuple(model['order']), seasonal_order=tuple(model['seasonal_order']),
                        trend='c' if model['order'][1] == 0 else 'n', enforce_stationarity=False, enforce_invertibility=False)
            res = m.filter(np.asarray(model['params']))
            fc = res.get_forecast(h, exog=np.asarray(future_exog) if future_exog is not None else None)
            mean = np.asarray(fc.predicted_mean)
            bands = {lv: tuple(np.asarray(fc.conf_int(alpha=1 - lv / 100)).T) for lv in levels}
            return mean, bands
        res = _ets_model(y, model['spec']).smooth(np.asarray(model['params']))
        pred = res.get_prediction(start=len(y), end=len(y) + h - 1)
        mean = np.asarray(pred.predicted_mean)
        bands = {}
        for lv in levels:
            frame = pred.summary_frame(alpha=1 - lv / 100)
            bands[lv] = (np.asarray(frame['pi_lower']), np.asarray(frame['pi_upper']))
        return mean, bands


def _model_info(name, kind, cfg, dates, values, fitted, covariates=None, exog=None, group=None):
    step = step_of(dates)
    return {'family': kind, 'task': 'timeseries', 'target': cfg['value_field'], 'predictors': covariates or [], 'design': [],
            'classes': None, 'model': {**fitted, 'values': values, 'covariates': covariates or [], 'exog': exog or []},
            'dates': [d.isoformat() for d in dates], 'step': list(step), 'date_field': cfg['date_field'],
            'group': group, 'name': name}


def _fit_rows(name, f):
    rows = [(name, 'AIC', f['aic']), (name, 'BIC', f['bic'])]
    if f['kind'] == 'arima':
        rows += [(name, 'p', f['order'][0]), (name, 'd', f['order'][1]), (name, 'q', f['order'][2]),
                 (name, 'P', f['seasonal_order'][0]), (name, 'D', f['seasonal_order'][1]), (name, 'Q', f['seasonal_order'][2])]
    return [(n, m, float(v)) for n, m, v in rows if v is not None]


RESID_SCHEMA = Schema([Field('Date', FieldType.DATETIME), Field('Actual', FieldType.DOUBLE), Field('Fitted', FieldType.DOUBLE),
                       Field('Residual', FieldType.DOUBLE)])


class _SeriesModel(Tool):
    kind = None

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['date_field'], cfg['value_field'], *cfg.get('covariates', [])])
        return {'model': MODEL_SCHEMA, 'fit': FIT_SCHEMA, 'fitted': RESID_SCHEMA}

    def fit(self, ctx, cfg, dates, values, exog):
        raise NotImplementedError

    def run(self, ctx, cfg, inputs):
        self.infer(ctx, cfg, {'input': inputs['input'].schema})
        covariates = cfg.get('covariates', [])
        dates, values, exog = read_series(inputs['input'], cfg['date_field'], cfg['value_field'], covariates)
        if len(values) < 4:
            raise DataError(f'{self.manifest.display_name} needs at least 4 observations; got {len(values)}')
        fitted = self.fit(ctx, cfg, dates, values, exog)
        name = cfg['model_name'] or self.manifest.display_name
        info = _model_info(name, self.kind, cfg, dates, values, fitted, covariates, exog)
        resid = [(d, a, f, a - f) for d, a, f in zip(dates, values, fitted['fitted'])]
        return {'model': model_table(name, self.kind, cfg['value_field'], info, b'{}'),
                'fit': Table.from_rows(FIT_SCHEMA, _fit_rows(name, fitted)), 'fitted': Table.from_rows(RESID_SCHEMA, resid)}


_MODEL_OUTPUTS = (Anchor('model', 'Model'), Anchor('fit', 'Fit'), Anchor('fitted', 'Fitted', description='Actual, fitted and residual'))


class ARIMA(_SeriesModel):
    kind = 'arima'
    manifest = ToolManifest(
        tool_id='core.arima', version='1.0.0', display_name='ARIMA', catalog_ids=('time-series:arima',),
        inputs=(Anchor('input', 'Input'),), outputs=_MODEL_OUTPUTS,
        description='Seasonal ARIMA model of a regularly spaced series (statsmodels SARIMAX), with optional '
                    'covariates. By default it searches p, q ∈ {0,1,2}, d ∈ {0,1} and, for seasonal data, P, D, '
                    'Q ∈ {0,1}, keeping the lowest AIC. Or set order and seasonal_order. Outputs the model for TS '
                    'Forecast, the order, AIC and BIC, and the fitted values and residuals.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['date_field', 'value_field'], 'properties': {
            **_SERIES, 'order': {'type': ['array', 'null'], 'items': {'type': 'integer', 'minimum': 0, 'maximum': 5},
                                 'minItems': 3, 'maxItems': 3, 'default': None},
            'seasonal_order': {'type': ['array', 'null'], 'items': {'type': 'integer', 'minimum': 0, 'maximum': 2},
                               'minItems': 3, 'maxItems': 3, 'default': None},
            'covariates': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}, 'default': []}}})

    def fit(self, ctx, cfg, dates, values, exog):
        return fit_arima(ctx, cfg, dates, values, exog)


class ETS(_SeriesModel):
    kind = 'ets'
    manifest = ToolManifest(
        tool_id='core.ets', version='1.0.0', display_name='ETS', catalog_ids=('time-series:ets',),
        inputs=(Anchor('input', 'Input'),), outputs=_MODEL_OUTPUTS,
        description='Exponential smoothing state-space (ETS) model: additive or multiplicative error, none, '
                    'additive or damped trend, and none, additive or multiplicative seasonality. "auto" chooses '
                    'each by AIC; multiplicative forms need positive data. Outputs the model for TS Forecast, '
                    'AIC and BIC, and fitted values.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['date_field', 'value_field'], 'properties': {
            **_SERIES, 'error': {'enum': ['auto', 'add', 'mul'], 'default': 'auto'},
            'trend': {'enum': ['auto', 'none', 'add'], 'default': 'auto'}, 'damped': {'type': 'boolean', 'default': False},
            'seasonal': {'enum': ['auto', 'none', 'add', 'mul'], 'default': 'auto'}}})

    def fit(self, ctx, cfg, dates, values, exog):
        return fit_ets(ctx, cfg, dates, values)


FORECAST_SCHEMA = Schema([Field('Model', FieldType.V_WSTRING, 256), Field('Group', FieldType.V_WSTRING, TEXT),
                          Field('Date', FieldType.DATETIME), Field('Forecast', FieldType.DOUBLE),
                          Field('Lower80', FieldType.DOUBLE), Field('Upper80', FieldType.DOUBLE),
                          Field('Lower95', FieldType.DOUBLE), Field('Upper95', FieldType.DOUBLE)])


def _series_models(table):
    models = read_models(table)
    for name, info, _ in models:
        if info.get('task') != 'timeseries':
            raise DataError(f'Model {name!r} is not a time series model (ARIMA or ETS)')
    return models


def _forecast_rows(name, info, h, future_exog=None):
    model = info['model']
    mean, bands = _forecast(model, h, (80, 95), future_exog)
    dates = future_dates(_dt.datetime.fromisoformat(info['dates'][-1]), tuple(info['step']), h)
    group = json.dumps(info['group']) if info.get('group') else None
    return [(name, group, d, float(m), float(bands[80][0][i]), float(bands[80][1][i]), float(bands[95][0][i]), float(bands[95][1][i]))
            for i, (d, m) in enumerate(zip(dates, mean))]


class TSForecast(Tool):
    manifest = ToolManifest(
        tool_id='core.ts_forecast', version='1.0.0', display_name='TS Forecast', catalog_ids=('time-series:ts-forecast',),
        inputs=(Anchor('models', 'Models', multiple=True),), outputs=(Anchor('output', 'Forecast'), CHART),
        description='Forecast periods steps ahead from ARIMA or ETS models, with 80% and 95% prediction intervals and '
                    'dates continuing the series spacing (calendar months are kept). The chart shows the history, '
                    'forecast and 95% band of the first model. ARIMA models with covariates need TS Covariate '
                    'Forecast.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'periods': {'type': 'integer', 'minimum': 1, 'maximum': 10000, 'default': 12}}})

    def infer(self, ctx, cfg, inputs):
        for s in inputs['models']:
            expect_model_schema(s)
        return {'output': FORECAST_SCHEMA, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        rows = []
        models = [m for t in inputs['models'] for m in _series_models(t)]
        for name, info, _ in models:
            if info['model'].get('covariates'):
                raise DataError(f'Model {name!r} uses covariates; use TS Covariate Forecast with future covariate values')
            rows += _forecast_rows(name, info, cfg['periods'])
        return {'output': Table.from_rows(FORECAST_SCHEMA, rows), 'chart': _forecast_chart(models[0], rows)}


def _forecast_chart(model, rows):
    name, info, _ = model
    hist = [(charts.to_timestamp(_dt.datetime.fromisoformat(d)), v) for d, v in zip(info['dates'], info['model']['values'])]
    mine = [r for r in rows if r[0] == name]
    series = {'Actual': hist, 'Forecast': [(charts.to_timestamp(r[2]), r[3]) for r in mine],
              'Lower 95%': [(charts.to_timestamp(r[2]), r[6]) for r in mine], 'Upper 95%': [(charts.to_timestamp(r[2]), r[7]) for r in mine]}
    canvas = charts.line_chart(series, title=f'Forecast: {name}', y_label=info['target'], time_axis=True)
    return charts.chart_table(canvas, f'Forecast: {name}')


class TSCovariateForecast(Tool):
    manifest = ToolManifest(
        tool_id='core.ts_covariate_forecast', version='1.0.0', display_name='TS Covariate Forecast',
        catalog_ids=('time-series:ts-covariate-forecast',),
        inputs=(Anchor('models', 'Model'), Anchor('input', 'Future covariates', description='One record per future period')),
        outputs=(Anchor('output', 'Forecast'), CHART),
        description='Forecast an ARIMA model fitted with covariates, using the covariate values supplied for each '
                    'future period (records in date order, one per period). The number of records sets the '
                    'horizon. Gives 80% and 95% prediction intervals.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    def infer(self, ctx, cfg, inputs):
        expect_model_schema(inputs['models'])
        return {'output': FORECAST_SCHEMA, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        name, info, payload = _series_models(inputs['models'])[0]
        covariates = info['model'].get('covariates') or []
        if not covariates:
            raise DataError(f'Model {name!r} has no covariates; use TS Forecast')
        future = inputs['input']
        require_fields(future.schema, covariates)
        exog = []
        for rec in sorted(future.to_dicts(), key=lambda r: _to_datetime(r[info['date_field']]) if info['date_field'] in r and not missing(r[info['date_field']]) else _dt.datetime.min):
            if any(missing(rec[c]) for c in covariates):
                raise DataError('Future covariate records must not have missing values')
            exog.append([float(rec[c]) for c in covariates])
        if not exog:
            raise DataError('TS Covariate Forecast needs at least one future record')
        rows = _forecast_rows(name, info, len(exog), exog)
        return {'output': Table.from_rows(FORECAST_SCHEMA, rows), 'chart': _forecast_chart((name, info, payload), rows)}


class TSCompare(Tool):
    METRICS = Schema([Field('Model', FieldType.V_WSTRING, 256), Field('N', FieldType.INT64), Field('ME', FieldType.DOUBLE),
                      Field('RMSE', FieldType.DOUBLE), Field('MAE', FieldType.DOUBLE), Field('MPE', FieldType.DOUBLE),
                      Field('MAPE', FieldType.DOUBLE), Field('MASE', FieldType.DOUBLE)])
    DETAIL = Schema([Field('Model', FieldType.V_WSTRING, 256), Field('Date', FieldType.DATETIME), Field('Actual', FieldType.DOUBLE),
                     Field('Forecast', FieldType.DOUBLE), Field('Error', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.ts_compare', version='1.0.0', display_name='TS Compare', catalog_ids=('time-series:ts-compare',),
        inputs=(Anchor('models', 'Models', multiple=True), Anchor('input', 'Holdout', description='Actual values after the training period')),
        outputs=(Anchor('output', 'Accuracy'), Anchor('detail', 'Detail')),
        description='Compare time series models on holdout actuals that follow their training period: each model '
                    'forecasts the holdout horizon and is scored with ME, RMSE, MAE, MPE, MAPE and MASE (scaled by '
                    'the in-sample seasonal naive error). The detail output pairs each forecast with its actual by '
                    'step.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['date_field', 'value_field'], 'properties': {
            'date_field': {'type': 'string', 'minLength': 1}, 'value_field': {'type': 'string', 'minLength': 1}}})

    def infer(self, ctx, cfg, inputs):
        for s in inputs['models']:
            expect_model_schema(s)
        require_fields(inputs['input'], [cfg['date_field'], cfg['value_field']])
        return {'output': self.METRICS, 'detail': self.DETAIL}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        dates, actual, _ = read_series(inputs['input'], cfg['date_field'], cfg['value_field'])
        metrics, detail = [], []
        for name, info, _ in [m for t in inputs['models'] for m in _series_models(t)]:
            if info['model'].get('covariates'):
                raise DataError(f'Model {name!r} uses covariates; TS Compare uses models without covariates')
            mean, _ = _forecast(info['model'], len(actual), (95,))
            a, f = np.asarray(actual), np.asarray(mean)
            err = a - f
            history = np.asarray(info['model']['values'])
            m = max(int(info['model'].get('spec', {}).get('period') or (info['model'].get('seasonal_order') or [0, 0, 0, 1])[3] or 1), 1)
            scale = np.mean(np.abs(history[m:] - history[:-m])) if len(history) > m else None
            nz = a != 0
            metrics.append((name, len(a), float(err.mean()), float(np.sqrt((err ** 2).mean())), float(np.abs(err).mean()),
                            float((err[nz] / a[nz]).mean() * 100) if nz.any() else None,
                            float(np.abs(err[nz] / a[nz]).mean() * 100) if nz.any() else None,
                            float(np.abs(err).mean() / scale) if scale else None))
            detail += [(name, d, float(x), float(y), float(x - y)) for d, x, y in zip(dates, a, f)]
        return {'output': Table.from_rows(self.METRICS, metrics), 'detail': Table.from_rows(self.DETAIL, detail)}


class TSPlot(Tool):
    COMPONENTS = Schema([Field('Date', FieldType.DATETIME), Field('Observed', FieldType.DOUBLE), Field('Trend', FieldType.DOUBLE),
                         Field('Seasonal', FieldType.DOUBLE), Field('Remainder', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.ts_plot', version='1.0.0', display_name='TS Plot', catalog_ids=('time-series:ts-plot',),
        inputs=(Anchor('input', 'Input'),), outputs=(CHART, Anchor('components', 'Components')),
        description='Time series plot of a value field (optionally one line per group), and an STL decomposition '
                    'into trend, seasonal and remainder components when seasonal_period > 1 (single series).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['date_field', 'value_field'], 'properties': {
            'date_field': {'type': 'string', 'minLength': 1}, 'value_field': {'type': 'string', 'minLength': 1},
            'group_field': {'type': ['string', 'null'], 'default': None},
            'seasonal_period': {'type': 'integer', 'minimum': 1, 'maximum': 400, 'default': 1},
            'title': {'type': ['string', 'null'], 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['date_field'], cfg['value_field']] + ([cfg['group_field']] if cfg['group_field'] else []))
        return {'chart': charts.chart_schema(), 'components': self.COMPONENTS}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        series, components = {}, []
        if cfg['group_field']:
            groups = {}
            for rec in table.to_dicts():
                groups.setdefault(format_value(rec[cfg['group_field']]) if not missing(rec[cfg['group_field']]) else '[Null]', []).append(rec)
            parts = {g: Table.from_rows(table.schema, [tuple(r[f] for f in table.schema.names) for r in recs]) for g, recs in groups.items()}
        else:
            parts = {cfg['value_field']: table}
        for g, part in sorted(parts.items()):
            dates, values, _ = read_series(part, cfg['date_field'], cfg['value_field'])
            series[g] = [(charts.to_timestamp(d), v) for d, v in zip(dates, values)]
        if cfg['seasonal_period'] > 1 and not cfg['group_field']:
            from statsmodels.tsa.seasonal import STL
            dates, values, _ = read_series(table, cfg['date_field'], cfg['value_field'])
            if len(values) < 2 * cfg['seasonal_period'] + 1:
                raise DataError('STL decomposition needs at least two full seasons')
            res = STL(values, period=cfg['seasonal_period'], robust=True).fit()
            components = [(d, v, float(t), float(s), float(r)) for d, v, t, s, r in zip(dates, values, res.trend, res.seasonal, res.resid)]
            series['Trend'] = [(charts.to_timestamp(c[0]), c[2]) for c in components]
        title = cfg['title'] or f"{cfg['value_field']} over time"
        canvas = charts.line_chart(series, title=title, y_label=cfg['value_field'], time_axis=True)
        return {'chart': charts.chart_table(canvas, title), 'components': Table.from_rows(self.COMPONENTS, components)}


_INTERVALS = {'hour': ('seconds', 3600), 'day': ('seconds', 86400), 'week': ('seconds', 7 * 86400),
              'month': ('months', 1), 'quarter': ('months', 3), 'year': ('months', 12)}


class TSFiller(Tool):
    manifest = ToolManifest(
        tool_id='core.ts_filler', version='1.0.0', display_name='TS Filler', catalog_ids=('time-series:ts-filler',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Output'),),
        description='Insert records for missing periods so each series (optionally per group) is regularly spaced '
                    'at the chosen interval between its first and last date. Value fields in inserted records '
                    'are null, the previous value, linearly interpolated or zero. Filled = true marks inserted '
                    'records. Records whose dates are off the interval grid are kept as they are.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['date_field', 'interval'], 'properties': {
            'date_field': {'type': 'string', 'minLength': 1}, 'interval': {'enum': list(_INTERVALS)},
            'group_by': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}, 'default': []},
            'value_fields': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}, 'default': []},
            'fill': {'enum': ['null', 'previous', 'linear', 'zero'], 'default': 'null'}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['date_field'], *cfg['group_by'], *cfg['value_fields']])
        if cfg['fill'] in ('linear', 'zero'):
            bad = [f for f in cfg['value_fields'] if not schema.field(f).type.is_numeric]
            if bad:
                raise SchemaError(f"{cfg['fill']} filling needs numeric value fields: {bad}")
        if 'Filled' in schema:
            raise SchemaError('Input already has a Filled field')
        return {'output': Schema([*schema, Field('Filled', FieldType.BOOL)])}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        dtype = table.schema.field(cfg['date_field']).type
        groups = {}
        for rec in table.to_dicts():
            if missing(rec[cfg['date_field']]):
                continue
            groups.setdefault(tuple(rec[g] for g in cfg['group_by']), []).append(rec)
        names = table.schema.names
        out = []
        for key, recs in groups.items():
            recs.sort(key=lambda r: _to_datetime(r[cfg['date_field']]))
            dates = [_to_datetime(r[cfg['date_field']]) for r in recs]
            by_date = {d: r for d, r in zip(dates, recs)}
            grid = [dates[0]]
            while grid[-1] < dates[-1]:
                grid.append(future_dates(grid[-1], _INTERVALS[cfg['interval']], 1)[0])
                if len(grid) > 1_000_000:
                    raise DataError('TS Filler would create more than a million periods')
            known = sorted(set(grid) | set(dates))
            for i, d in enumerate(known):
                if d in by_date:
                    out.append((*[by_date[d][n] for n in names], False))
                    continue
                rec = {n: None for n in names}
                rec.update(dict(zip(cfg['group_by'], key)))
                rec[cfg['date_field']] = d.date() if dtype is FieldType.DATE else d if dtype is FieldType.DATETIME else d.isoformat()
                prev = next((by_date[x] for x in reversed(known[:i]) if x in by_date), None)
                nxt = next(((x, by_date[x]) for x in known[i + 1:] if x in by_date), None)
                for f in cfg['value_fields']:
                    if cfg['fill'] == 'previous' and prev is not None:
                        rec[f] = prev[f]
                    elif cfg['fill'] == 'zero':
                        rec[f] = 0
                    elif cfg['fill'] == 'linear' and prev is not None and nxt is not None and not missing(prev[f]) and not missing(nxt[1][f]):
                        # Interpolate by period position (months differ in length; the series is per period).
                        p_pos = max(j for j, x in enumerate(known[:i]) if x in by_date)
                        frac = (i - p_pos) / (known.index(nxt[0]) - p_pos)
                        rec[f] = float(prev[f]) + frac * (float(nxt[1][f]) - float(prev[f]))
                out.append((*[rec[n] for n in names], True))
        return {'output': Table.from_rows(schema, out)}


class TSModelFactory(Tool):
    manifest = ToolManifest(
        tool_id='core.ts_model_factory', version='1.0.0', display_name='TS Model Factory',
        catalog_ids=('time-series:ts-model-factory',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('model', 'Models', description='One model per group'), Anchor('fit', 'Fit')),
        description='Fit one ARIMA or ETS model (automatic selection) per group, for example per store or product, '
                    'so many series can be forecast at once with TS Forecast Factory. Groups that cannot be fitted '
                    'are reported as warnings and skipped.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['date_field', 'value_field', 'group_by'], 'properties': {
            **_SERIES, 'group_by': {'type': 'array', 'uniqueItems': True, 'minItems': 1, 'items': {'type': 'string'}},
            'method': {'enum': ['arima', 'ets'], 'default': 'ets'}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['date_field'], cfg['value_field'], *cfg['group_by']])
        return {'model': MODEL_SCHEMA, 'fit': FIT_SCHEMA}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        groups = {}
        for row, rec in zip(table.rows(), table.to_dicts()):
            groups.setdefault(tuple(rec[g] for g in cfg['group_by']), []).append(row)
        models, fits = [], []
        settings = {**cfg, 'error': 'auto', 'trend': 'auto', 'seasonal': 'auto', 'damped': False}
        for key, rows in sorted(groups.items(), key=lambda kv: [format_value(v) or '' for v in kv[0]]):
            ctx.check_cancelled()
            part = Table.from_rows(table.schema, rows)
            label = ' | '.join(format_value(v) or '[Null]' for v in key)
            try:
                dates, values, _ = read_series(part, cfg['date_field'], cfg['value_field'])
                fitted = (fit_arima if cfg['method'] == 'arima' else lambda c, s, d, v, e: fit_ets(c, s, d, v))(ctx, settings, dates, values, [])
            except DataError as e:
                ctx.warn(f'Group {label}: {e.message}')
                continue
            info = _model_info(label, cfg['method'], cfg, dates, values, fitted,
                               group={g: format_value(v) for g, v in zip(cfg['group_by'], key)})
            models.append((label, cfg['method'], cfg['value_field'], json.dumps(info), b'{}'))
            fits += _fit_rows(label, fitted)
        if not models:
            raise DataError('TS Model Factory could not fit any group')
        return {'model': Table.from_rows(MODEL_SCHEMA, models), 'fit': Table.from_rows(FIT_SCHEMA, fits)}


class TSForecastFactory(Tool):
    manifest = ToolManifest(
        tool_id='core.ts_forecast_factory', version='1.0.0', display_name='TS Forecast Factory',
        catalog_ids=('time-series:ts-forecast-factory',),
        inputs=(Anchor('models', 'Models', description='Output of TS Model Factory'),), outputs=(Anchor('output', 'Forecast'),),
        description='Forecast every per-group model from TS Model Factory periods steps ahead with 80% and 95% '
                    'prediction intervals. Group holds the group values as JSON.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'periods': {'type': 'integer', 'minimum': 1, 'maximum': 10000, 'default': 12}}})

    def infer(self, ctx, cfg, inputs):
        expect_model_schema(inputs['models'])
        return {'output': FORECAST_SCHEMA}

    def run(self, ctx, cfg, inputs):
        rows = []
        for name, info, _ in _series_models(inputs['models']):
            ctx.check_cancelled()
            rows += _forecast_rows(name, info, cfg['periods'])
        return {'output': Table.from_rows(FORECAST_SCHEMA, rows)}


TOOLS = [ARIMA, ETS, TSForecast, TSCovariateForecast, TSCompare, TSPlot, TSFiller, TSModelFactory, TSForecastFactory]
