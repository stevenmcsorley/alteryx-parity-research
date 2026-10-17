"""Shared machinery for predictive tools: design matrices, safe model storage, refitting and metrics.

**Model objects** travel on a ``model`` anchor as a one-row table (``MODEL_SCHEMA``):

* ``Info``: JSON with the model family, target, predictors, the design encoding (numeric columns and
  the categories of each categorical column), the classes, training metrics and the ``spec`` (tool id
  and configuration) needed to refit the same model on other data.
* ``Payload``: coefficient models (linear, logistic, count, gamma, Cox, naive Bayes) store plain JSON
  parameters. scikit-learn estimators are stored with ``skops``, whose loader checks every type and
  accepts only scikit-learn, numpy and scipy types. Loading a model can never run arbitrary code,
  unlike pickle.

**Design matrices.** Numeric predictors are used as floats. Text, boolean and date predictors are
one-hot encoded with the categories seen in training, and the first category is dropped for models
with an intercept. Unseen categories at scoring time encode as all zeros and are reported. Records
with a missing target or predictor are excluded from fitting and counted.
"""
from __future__ import annotations

import json
import math

from .errors import ConfigError, DataError, SchemaError
from .table import Table
from .types import Field, FieldType, Schema, format_value

TEXT = 2**30 - 1
MODEL_SCHEMA = Schema([Field('Name', FieldType.V_WSTRING, 256), Field('ModelType', FieldType.V_WSTRING, 64),
                       Field('Target', FieldType.V_WSTRING, 256), Field('Info', FieldType.V_WSTRING, TEXT),
                       Field('Payload', FieldType.BLOB)])
_TRUSTED_PREFIXES = ('sklearn.', 'numpy.', 'scipy.', 'builtins.')


def missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


# =========================================================================== design matrices

class Design:
    """Encoding of predictor fields into a numeric matrix, fitted on training data and reusable."""

    def __init__(self, columns):
        self.columns = columns  # [{'field', 'kind': 'numeric'|'categorical', 'categories': [...]}]

    @classmethod
    def fit(cls, table: Table, predictors, drop_first: bool):
        columns = []
        for name in predictors:
            f = table.schema.field(name)
            if f.type.is_numeric and f.type is not FieldType.BOOL:
                columns.append({'field': name, 'kind': 'numeric'})
            elif f.type.value == 'blob':
                raise SchemaError(f'Predictor {name!r} is a blob and cannot be modelled')
            else:
                cats = sorted({format_value(v) for v in table.column(name) if not missing(v)})
                if len(cats) > 500:
                    raise DataError(f'Predictor {name!r} has {len(cats)} categories (max 500)')
                columns.append({'field': name, 'kind': 'categorical', 'categories': cats, 'drop_first': drop_first})
        return cls(columns)

    def to_json(self):
        return self.columns

    @classmethod
    def from_json(cls, columns):
        return cls(columns)

    @property
    def names(self):
        out = []
        for c in self.columns:
            if c['kind'] == 'numeric':
                out.append(c['field'])
            else:
                cats = c['categories'][1:] if c.get('drop_first') else c['categories']
                out += [f"{c['field']}_{cat}" for cat in cats]
        return out

    def matrix(self, table: Table):
        """(X, usable_row_mask, unseen_count) for a table; rows with missing predictors are unusable."""
        import numpy as np
        n = len(table)
        cols, mask, unseen = [], np.ones(n, dtype=bool), 0
        for c in self.columns:
            if c['field'] not in table.schema:
                raise SchemaError(f"Scoring data has no predictor field {c['field']!r}")
            values = table.column(c['field'])
            miss = np.array([missing(v) for v in values])
            mask &= ~miss
            if c['kind'] == 'numeric':
                arr = np.array([np.nan if m else float(v) for v, m in zip(values, miss)])
                mask &= np.isfinite(arr)
                cols.append(np.nan_to_num(arr)[:, None])
            else:
                cats = c['categories'][1:] if c.get('drop_first') else c['categories']
                index = {cat: i for i, cat in enumerate(cats)}
                known = set(c['categories'])
                block = np.zeros((n, len(cats)))
                for r, (v, m) in enumerate(zip(values, miss)):
                    if m:
                        continue
                    label = format_value(v)
                    if label not in known:
                        unseen += 1
                    elif label in index:
                        block[r, index[label]] = 1.0
                cols.append(block)
        X = np.hstack(cols) if cols else np.zeros((n, 0))
        return X, mask, unseen


def target_vector(table: Table, target: str, kind: str, classes=None):
    """(y, mask, classes) for a numeric or categorical target."""
    import numpy as np
    values = table.column(target)
    if kind == 'numeric':
        f = table.schema.field(target)
        if not f.type.is_numeric:
            raise SchemaError(f'Target {target!r} must be numeric for this model')
        y = np.array([np.nan if missing(v) else float(v) for v in values])
        return y, np.isfinite(y), None
    labels = [None if missing(v) else format_value(v) for v in values]
    classes = classes or sorted({l for l in labels if l is not None})
    index = {c: i for i, c in enumerate(classes)}
    y = np.array([index.get(l, -1) if l is not None else -1 for l in labels])
    return y, y >= 0, classes


# =========================================================================== storage

def model_table(name, model_type, target, info: dict, payload: bytes):
    return Table.from_rows(MODEL_SCHEMA, [(name, model_type, target, json.dumps(info, default=_jsonable), payload)])


def _jsonable(v):
    import numpy as np
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    raise TypeError(f'not JSON serialisable: {type(v).__name__}')


def sk_dumps(estimator) -> bytes:
    import skops.io as sio
    return sio.dumps(estimator)


def sk_loads(payload: bytes):
    import skops.io as sio
    untrusted = sio.get_untrusted_types(data=payload)
    bad = [t for t in untrusted if not t.startswith(_TRUSTED_PREFIXES)]
    if bad:
        raise DataError(f'Model contains types that are not trusted for loading: {bad[:5]}')
    return sio.loads(payload, trusted=untrusted)


def read_models(table: Table, what='model'):
    """List of (name, info, payload) from a model table (one row per model)."""
    if tuple(table.schema.names) != tuple(MODEL_SCHEMA.names):
        raise DataError(f'{what} input is not a Flowbench model; connect the Model output of a predictive tool')
    out = []
    for name, model_type, target, info, payload in table.rows():
        out.append((name, json.loads(info), payload))
    if not out:
        raise DataError(f'{what} input has no models')
    return out


def expect_model_schema(schema, what='model'):
    if tuple(schema.names) != tuple(MODEL_SCHEMA.names):
        raise SchemaError(f'{what} input must be the Model output of a predictive tool')


# =========================================================================== prediction

def _sigmoid(z):
    import numpy as np
    return 1.0 / (1.0 + np.exp(-np.clip(z, -700, 700)))


def predict(info: dict, payload: bytes, table: Table):
    """Predictions for a table: dict with 'value' (regression) or 'proba' (n x classes), and 'mask'."""
    import numpy as np
    if info.get('family') == 'ml_pipeline':
        from . import mlpipeline
        return mlpipeline.predict(info, sk_loads(payload), table)
    design = Design.from_json(info['design'])
    X, mask, unseen = design.matrix(table)
    family = info['family']
    if family in ('linear', 'logistic', 'poisson', 'negative_binomial', 'gamma'):
        p = json.loads(payload)
        beta = np.array(p['coefficients'])
        eta = (p['intercept'] if p.get('intercept') is not None else 0.0) + X @ beta
        if info.get('offset_field'):
            off = np.array([0.0 if missing(v) else math.log(float(v)) for v in table.column(info['offset_field'])])
            eta = eta + off
        if family == 'linear':
            return {'value': eta, 'mask': mask, 'unseen': unseen}
        if family == 'logistic':
            p1 = _sigmoid(eta)
            return {'proba': np.column_stack([1 - p1, p1]), 'mask': mask, 'unseen': unseen}
        return {'value': np.exp(np.clip(eta, -700, 700)), 'mask': mask, 'unseen': unseen}
    if family == 'naive_bayes':
        return {'proba': _nb_proba(json.loads(payload), table, info), 'mask': np.ones(len(table), dtype=bool), 'unseen': 0}
    if family == 'cox':
        p = json.loads(payload)
        lp = X @ np.array(p['coefficients'])
        return {'value': lp, 'mask': mask, 'unseen': unseen}
    estimator = sk_loads(payload)
    Xm = X[mask] if mask.any() else X[:0]
    if info['task'] == 'classification':
        proba = np.full((len(table), len(info['classes'])), np.nan)
        if len(Xm):
            proba[mask] = estimator.predict_proba(Xm)
        return {'proba': proba, 'mask': mask, 'unseen': unseen}
    value = np.full(len(table), np.nan)
    if len(Xm):
        value[mask] = estimator.predict(Xm)
    return {'value': value, 'mask': mask, 'unseen': unseen}


# =========================================================================== naive Bayes (mixed numeric/categorical)

def nb_fit(table: Table, target: str, predictors, laplace: float):
    import numpy as np
    y, ymask, classes = target_vector(table, target, 'categorical')
    model = {'classes': classes, 'priors': [], 'features': []}
    rows = np.flatnonzero(ymask)
    counts = np.bincount(y[rows], minlength=len(classes)).astype(float)
    model['priors'] = ((counts + laplace) / (counts.sum() + laplace * len(classes))).tolist()
    for name in predictors:
        f = table.schema.field(name)
        values = table.column(name)
        if f.type.is_numeric and f.type is not FieldType.BOOL:
            stats = []
            for k in range(len(classes)):
                xs = np.array([float(values[i]) for i in rows if y[i] == k and not missing(values[i])])
                mean = float(xs.mean()) if xs.size else 0.0
                var = float(xs.var(ddof=1)) if xs.size > 1 else 0.0
                stats.append([mean, var])
            allvals = np.array([float(values[i]) for i in rows if not missing(values[i])])
            floor = max(1e-9 * (allvals.var() if allvals.size > 1 else 1.0), 1e-12)
            model['features'].append({'field': name, 'kind': 'numeric', 'stats': [[m, max(v, floor)] for m, v in stats]})
        else:
            cats = sorted({format_value(values[i]) for i in rows if not missing(values[i])})
            table_ = []
            for k in range(len(classes)):
                labels = [format_value(values[i]) for i in rows if y[i] == k and not missing(values[i])]
                total = len(labels) + laplace * len(cats)
                table_.append({c: (labels.count(c) + laplace) / total if total else 1.0 / max(len(cats), 1) for c in cats})
            model['features'].append({'field': name, 'kind': 'categorical', 'probs': table_})
    return model


def _nb_proba(model, table: Table, info):
    import numpy as np
    n, k = len(table), len(model['classes'])
    logp = np.tile(np.log(np.array(model['priors'])), (n, 1))
    for feat in model['features']:
        values = table.column(feat['field'])
        for r, v in enumerate(values):
            if missing(v):
                continue  # a missing feature contributes nothing, as in the standard algorithm
            for c in range(k):
                if feat['kind'] == 'numeric':
                    mean, var = feat['stats'][c]
                    logp[r, c] += -0.5 * math.log(2 * math.pi * var) - (float(v) - mean) ** 2 / (2 * var)
                else:
                    prob = feat['probs'][c].get(format_value(v))
                    if prob is not None:
                        logp[r, c] += math.log(prob)
    logp -= logp.max(axis=1, keepdims=True)
    p = np.exp(logp)
    return p / p.sum(axis=1, keepdims=True)


# =========================================================================== metrics

def regression_metrics(y, pred):
    import numpy as np
    ok = np.isfinite(y) & np.isfinite(pred)
    y, pred = y[ok], pred[ok]
    if not y.size:
        return {'N': 0}
    resid = y - pred
    ss_tot = float(((y - y.mean()) ** 2).sum())
    nz = y != 0
    return {'N': int(y.size), 'RMSE': float(np.sqrt((resid ** 2).mean())), 'MAE': float(np.abs(resid).mean()),
            'R2': 1 - float((resid ** 2).sum()) / ss_tot if ss_tot > 0 else None,
            'MAPE': float(np.abs(resid[nz] / y[nz]).mean() * 100) if nz.any() else None}


def classification_metrics(y, proba, classes):
    """y: class indexes (-1 = unusable); proba: n x k."""
    import numpy as np
    ok = (y >= 0) & np.all(np.isfinite(proba), axis=1)
    y, proba = y[ok], proba[ok]
    if not y.size:
        return {'N': 0}
    pred = proba.argmax(axis=1)
    out = {'N': int(y.size), 'Accuracy': float((pred == y).mean()),
           'LogLoss': float(-np.mean(np.log(np.clip(proba[np.arange(y.size), y], 1e-15, 1))))}
    f1s = []
    for c in range(len(classes)):
        tp = int(((pred == c) & (y == c)).sum())
        fp = int(((pred == c) & (y != c)).sum())
        fn = int(((pred != c) & (y == c)).sum())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    out['MacroF1'] = float(np.mean(f1s))
    if len(classes) == 2 and 0 < y.sum() < y.size:
        out['AUC'] = auc(y, proba[:, 1])
    return out


def auc(y01, score):
    """Area under the ROC curve via the rank-sum (Mann-Whitney) statistic, with ties averaged."""
    from scipy.stats import rankdata
    import numpy as np
    ranks = rankdata(score)
    pos = y01 == 1
    n1, n0 = int(pos.sum()), int((~pos).sum())
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def confusion(y, proba, classes):
    import numpy as np
    ok = (y >= 0) & np.all(np.isfinite(proba), axis=1)
    pred = proba[ok].argmax(axis=1)
    counts = {}
    for a, p in zip(y[ok], pred):
        counts[(classes[a], classes[p])] = counts.get((classes[a], classes[p]), 0) + 1
    return counts


# =========================================================================== refitting

FITTERS = {}  # tool id -> callable(ctx, cfg, table) -> (info, payload)


def register_fitter(tool_id):
    def wrap(fn):
        FITTERS[tool_id] = fn
        return fn
    return wrap


def refit(ctx, info, table):
    spec = info.get('spec') or {}
    fitter = FITTERS.get(spec.get('tool'))
    if fitter is None:
        raise ConfigError(f"Model type {info.get('family')!r} cannot be refitted here")
    return fitter(ctx, spec['config'], table)


def loglik(info, payload, table):
    """Log-likelihood of a fitted coefficient model on data (used by Nested Test)."""
    import numpy as np
    from scipy import special, stats
    family = info['family']
    if family == 'linear':
        y, ymask, _ = target_vector(table, info['target'], 'numeric')
        res = predict(info, payload, table)
        ok = ymask & res['mask']
        resid = y[ok] - res['value'][ok]
        sigma2 = float((resid ** 2).mean())
        return float(-0.5 * ok.sum() * (math.log(2 * math.pi * sigma2) + 1)), int(ok.sum())
    if family == 'logistic':
        y, ymask, _ = target_vector(table, info['target'], 'categorical', info['classes'])
        res = predict(info, payload, table)
        ok = ymask & res['mask']
        p = np.clip(res['proba'][ok, 1], 1e-15, 1 - 1e-15)
        return float((y[ok] * np.log(p) + (1 - y[ok]) * np.log(1 - p)).sum()), int(ok.sum())
    if family == 'poisson':
        y, ymask, _ = target_vector(table, info['target'], 'numeric')
        res = predict(info, payload, table)
        ok = ymask & res['mask']
        return float(stats.poisson.logpmf(y[ok], res['value'][ok]).sum()), int(ok.sum())
    if family == 'gamma':
        y, ymask, _ = target_vector(table, info['target'], 'numeric')
        res = predict(info, payload, table)
        ok = ymask & res['mask']
        shape = 1.0 / json.loads(payload)['scale']
        mu = res['value'][ok]
        return float(stats.gamma.logpdf(y[ok], a=shape, scale=mu / shape).sum()), int(ok.sum())
    raise ConfigError(f'Nested Test supports linear, logistic, Poisson and gamma models, not {family!r}')
