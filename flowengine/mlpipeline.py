"""Machine-learning pipelines: feature types, preparation steps, estimators, fitting and cross-validation.

**Pipeline streams.** Feature Types, Transformation, Classification and Regression pass the data on
with one extra text field, ``ML_Pipeline``, which holds the pipeline built so far as JSON
(``{"format": "flowbench.ml.v1", ...}``). Every record carries the same value, so the stream can still
be browsed, sampled or filtered before Fit. Fit reads the pipeline, prepares the data, trains the
estimator and outputs a model (``modeling.MODEL_SCHEMA``, family ``ml_pipeline``) that Predict and
the predictive Score tool can apply.

**Feature types** follow the common woodwork names: Integer, Double, Boolean, Categorical, Datetime,
NaturalLanguage, Index and Unknown. Autodetection: numeric storage types are Integer or Double, Bool is
Boolean and Date/DateTime are Datetime. Text that is true/false/yes/no is Boolean, and ISO dates are
Datetime. Text with at most 10 distinct values, or distinct values no more than 20% of the records, is
Categorical. Longer free text (mean length at least 10 characters, half the values containing a space)
is NaturalLanguage. Anything else is Unknown.

**Preparation** (fitted on the training records and stored in the model):

* Numeric and Boolean features are used as numbers (Boolean as 0/1).
* Categorical features, and any feature chosen for one-hot encoding, become one indicator column per
  training category.
* Unknown categories at scoring time encode as all zeros (``ignore``) or fail (``error``).
* Index (ID), Datetime, NaturalLanguage and Unknown features are left out unless they are one-hot
  encoded. Use Build Features to turn dates and text into model inputs.
* Missing values are filled by the chosen clean-up method (mean, median, mode or a constant). A feature
  that still has missing values in training stops the fit with the list of affected fields.
"""
from __future__ import annotations

import json
import math
import re
import warnings

from .errors import ConfigError, DataError, SchemaError
from .modeling import classification_metrics, missing, regression_metrics, target_vector
from .table import Table
from .types import Field, FieldType, Schema, format_value

PIPELINE_FIELD = 'ML_Pipeline'
FORMAT = 'flowbench.ml.v1'
TEXT = 2**30 - 1
FEATURE_TYPES = ('Integer', 'Double', 'Boolean', 'Categorical', 'Datetime', 'NaturalLanguage', 'Index', 'Unknown')
DATA_TYPES = ('Numeric', 'Categorical', 'Boolean', 'ID')  # Transformation "Set Data Types"
_KIND_OF_FEATURE = {'Integer': 'numeric', 'Double': 'numeric', 'Boolean': 'boolean', 'Categorical': 'categorical',
                    'Datetime': 'datetime', 'NaturalLanguage': 'text', 'Index': 'id', 'Unknown': 'text'}
_KIND_OF_DATA_TYPE = {'Numeric': 'numeric', 'Categorical': 'categorical', 'Boolean': 'boolean', 'ID': 'id'}
_TRUE = {'true', 'yes', 'y', 't', '1'}
_FALSE = {'false', 'no', 'n', 'f', '0'}
_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$')
MAX_CATEGORIES = 500

CLASSIFIERS = ('logistic_regression', 'decision_tree', 'random_forest', 'gradient_boosting')
REGRESSORS = ('linear_regression', 'decision_tree', 'random_forest')
ALGORITHM_NAMES = {'logistic_regression': 'Logistic Regression', 'linear_regression': 'Linear Regression',
                   'decision_tree': 'Decision Tree', 'random_forest': 'Random Forest',
                   'gradient_boosting': 'Gradient Boosting'}


# =========================================================================== pipeline streams

def empty_info():
    return {'format': FORMAT, 'feature_types': {}, 'transform': empty_transform(), 'estimator': None}


def empty_transform():
    return {'types': {}, 'impute': {}, 'constant': {}, 'exclude': [], 'one_hot': [], 'unknown': 'ignore'}


def pipeline_field():
    return Field(PIPELINE_FIELD, FieldType.V_WSTRING, TEXT)


def data_schema(schema: Schema) -> Schema:
    return Schema([f for f in schema if f.name != PIPELINE_FIELD])


def stream_schema(schema: Schema) -> Schema:
    return Schema([*data_schema(schema), pipeline_field()])


def read_stream(table: Table):
    """(data, info): the records without ``ML_Pipeline`` and the pipeline (empty for plain data)."""
    if PIPELINE_FIELD not in table.schema:
        return table, empty_info()
    data = table.select(data_schema(table.schema).names)
    text = next((v for v in table.column(PIPELINE_FIELD) if not missing(v)), None)
    if text is None:
        return data, empty_info()
    try:
        info = json.loads(text)
    except ValueError:
        raise DataError(f'{PIPELINE_FIELD} does not hold a Flowbench pipeline') from None
    if not isinstance(info, dict) or info.get('format') != FORMAT:
        raise DataError(f'{PIPELINE_FIELD} does not hold a {FORMAT} pipeline')
    base = empty_info()
    base.update(info)
    base['transform'] = {**empty_transform(), **(info.get('transform') or {})}
    return data, base


def write_stream(data: Table, info: dict) -> Table:
    text = json.dumps(info, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return data.with_column(pipeline_field(), [text] * len(data))


# =========================================================================== feature types

def detect_feature_type(field: Field, values) -> str:
    t = field.type
    if t is FieldType.BOOL:
        return 'Boolean'
    if t.is_integer:
        return 'Integer'
    if t.is_numeric:
        return 'Double'
    if t in (FieldType.DATE, FieldType.DATETIME):
        return 'Datetime'
    if not t.is_string:
        return 'Unknown'
    present = [v.strip() for v in values if not missing(v) and isinstance(v, str) and v.strip()]
    if not present:
        return 'Unknown'
    lowered = {v.lower() for v in present}
    if lowered <= (_TRUE | _FALSE) - {'1', '0'} and len(lowered) <= 2:
        return 'Boolean'
    if all(_ISO.match(v) for v in present):
        return 'Datetime'
    distinct = len(set(present))
    if distinct <= 10 or distinct / len(present) <= 0.2:
        return 'Categorical'
    if sum(map(len, present)) / len(present) >= 10 and sum(' ' in v for v in present) >= len(present) / 2:
        return 'NaturalLanguage'
    return 'Unknown'


def feature_types(data: Table, info: dict) -> dict:
    """Feature type of every field: the pipeline's assigned types, else autodetected."""
    assigned = info.get('feature_types') or {}
    return {f.name: assigned.get(f.name) or detect_feature_type(f, data.column(f.name)) for f in data.schema}


def kind_of(name, ftypes, transform):
    if name in (transform.get('types') or {}):
        return _KIND_OF_DATA_TYPE[transform['types'][name]]
    return _KIND_OF_FEATURE[ftypes[name]]


# =========================================================================== preparation

def to_bool(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return 1.0 if v else 0.0
    text = str(v).strip().lower()
    if text in _TRUE:
        return 1.0
    if text in _FALSE:
        return 0.0
    raise DataError(f'{v!r} is not a Boolean value')


def to_number(name, v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        raise DataError(f'Numeric feature {name!r} has a non-numeric value {v!r}') from None


def _mode(values):
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return min(counts, key=lambda v: (-counts[v], str(v))) if counts else None


def fit_prep(data: Table, predictors, info: dict, warn=None):
    """Fit the preparation (fill values, categories) on training records; JSON-ready."""
    import numpy as np
    transform = info['transform']
    ftypes = feature_types(data, info)
    columns, unfilled, skipped, auto_encoded = [], [], [], []
    for name in predictors:
        kind = kind_of(name, ftypes, transform)
        onehot = name in transform['one_hot']
        if kind == 'id' or (kind in ('datetime', 'text') and not onehot):
            skipped.append(f'{name} ({ftypes[name] if name not in transform["types"] else transform["types"][name]})')
            continue
        values = data.column(name)
        method = transform['impute'].get(name)
        if kind == 'categorical' and not onehot:
            auto_encoded.append(name)
        encode = onehot or kind == 'categorical'
        present = [v for v in values if not missing(v)]
        fill = None
        if encode:
            labels = [format_value(v) for v in present]
            if method in ('mean', 'median'):
                raise ConfigError(f'{method.title()} clean-up needs a numeric feature; {name!r} is encoded as categories')
            if method == 'mode':
                fill = _mode(labels)
            elif method == 'constant':
                fill = format_value(transform['constant'].get(name, 'Missing'))
            cats = sorted(set(labels) | ({fill} if fill is not None else set()))
            if len(cats) > MAX_CATEGORIES:
                raise DataError(f'Feature {name!r} has {len(cats)} categories (max {MAX_CATEGORIES})')
            col = {'field': name, 'kind': 'onehot', 'categories': cats, 'fill': fill}
        else:
            convert = to_bool if kind == 'boolean' else (lambda v, n=name: to_number(n, v))
            nums = [convert(v) for v in present]
            if method == 'mean':
                fill = float(np.mean(nums)) if nums else None
            elif method == 'median':
                fill = float(np.median(nums)) if nums else None
            elif method == 'mode':
                fill = _mode(nums)
            elif method == 'constant':
                fill = to_number(name, transform['constant'].get(name, 0))
            col = {'field': name, 'kind': kind, 'fill': fill}
        if fill is None and len(present) < len(values):
            unfilled.append(name)
        columns.append(col)
    if unfilled:
        raise DataError(f'Feature(s) {unfilled} have missing values; choose a Clean Up Missing Values method for them '
                        'in a Transformation tool')
    if not columns:
        raise DataError('No usable features: every predictor is an ID, date or text field that is not encoded')
    if warn and skipped:
        warn(f'Left out of the model: {", ".join(skipped)}')
    if warn and auto_encoded:
        warn(f'One-hot encoded categorical feature(s) {auto_encoded}')
    return {'columns': columns, 'unknown': transform.get('unknown', 'ignore')}


def prep_names(prep):
    out = []
    for c in prep['columns']:
        out += [f"{c['field']}_{cat}" for cat in c['categories']] if c['kind'] == 'onehot' else [c['field']]
    return out


def prep_matrix(prep, table: Table):
    """(X, usable_mask, unseen): rows with a missing value and no fill are unusable."""
    import numpy as np
    n = len(table)
    blocks, mask, unseen = [], np.ones(n, dtype=bool), 0
    for c in prep['columns']:
        if c['field'] not in table.schema:
            raise SchemaError(f"Data has no feature field {c['field']!r}")
        values = table.column(c['field'])
        if c['kind'] == 'onehot':
            index = {cat: i for i, cat in enumerate(c['categories'])}
            block = np.zeros((n, len(index)))
            for r, v in enumerate(values):
                label = c['fill'] if missing(v) else format_value(v)
                if label is None:
                    mask[r] = False
                elif label in index:
                    block[r, index[label]] = 1.0
                else:
                    unseen += 1
                    if prep.get('unknown') == 'error':
                        raise DataError(f"Feature {c['field']!r} has category {label!r} that was not seen in training")
            blocks.append(block)
            continue
        col = np.zeros(n)
        for r, v in enumerate(values):
            if missing(v):
                if c['fill'] is None:
                    mask[r] = False
                else:
                    col[r] = c['fill']
            else:
                x = to_bool(v) if c['kind'] == 'boolean' else to_number(c['field'], v)
                if math.isfinite(x):
                    col[r] = x
                else:
                    mask[r] = False
        blocks.append(col[:, None])
    return (np.hstack(blocks) if blocks else np.zeros((n, 0))), mask, unseen


# =========================================================================== estimators

def _max_features(value, regression):
    if value in (None, 'none'):
        return None
    if value == 'auto':
        return None if regression else 'sqrt'
    return value


def _depth(p, default):
    """Max depth: null means the algorithm's default, 'unlimited' grows trees fully."""
    value = p.get('max_depth')
    if value is None:
        return default
    return None if value == 'unlimited' else value


def _split(value):
    return 2 if value is None else value


def make_estimator(task, algorithm, p: dict):
    """A scikit-learn estimator; ``p`` holds the documented parameters, None meaning the default."""
    seed = p.get('seed', 10)
    g = lambda k, d: d if p.get(k) is None else p[k]  # noqa: E731
    if task == 'classification':
        if algorithm == 'logistic_regression':
            from sklearn.linear_model import LogisticRegression
            from sklearn.multiclass import OneVsRestClassifier
            solver = g('solver', 'liblinear')
            penalty = g('penalty', 'l2')
            if penalty == 'l1' and solver not in ('liblinear', 'saga'):
                raise ConfigError(f'The L1 penalty needs the liblinear or saga solver, not {solver}')
            if g('dual', False) and (penalty != 'l2' or solver != 'liblinear'):
                raise ConfigError('The dual formulation needs the L2 penalty and the liblinear solver')
            est = LogisticRegression(C=g('C', 1.0), l1_ratio=1.0 if penalty == 'l1' else 0.0, dual=g('dual', False),
                                     tol=g('tol', 1e-4), fit_intercept=g('fit_intercept', True),
                                     intercept_scaling=g('intercept_scaling', 1.0), class_weight=g('class_weight', None),
                                     random_state=seed, solver=solver, max_iter=g('max_iter', 100))
            multiclass = g('multiclass', 'auto')
            if multiclass == 'ovr' or (multiclass == 'auto' and solver == 'liblinear'):
                return OneVsRestClassifier(est)
            return est
        if algorithm == 'decision_tree':
            from sklearn.tree import DecisionTreeClassifier
            return DecisionTreeClassifier(criterion=g('criterion', 'gini'), splitter=g('splitter', 'best'),
                                          max_depth=_depth(p, None), min_samples_split=_split(p.get('min_samples_split')),
                                          min_weight_fraction_leaf=g('min_weight_fraction_leaf', 0.0),
                                          max_features=_max_features(g('max_features', 'auto'), False),
                                          max_leaf_nodes=p.get('max_leaf_nodes'),
                                          min_impurity_decrease=g('min_impurity_decrease', 0.0),
                                          class_weight=g('class_weight', None), random_state=seed)
        if algorithm == 'random_forest':
            from sklearn.ensemble import RandomForestClassifier
            return RandomForestClassifier(n_estimators=g('n_estimators', 100), criterion=g('criterion', 'gini'),
                                          max_depth=_depth(p, 10),
                                          min_samples_split=_split(p.get('min_samples_split')),
                                          min_weight_fraction_leaf=g('min_weight_fraction_leaf', 0.0),
                                          max_features=_max_features(g('max_features', 'auto'), False),
                                          min_impurity_decrease=g('min_impurity_decrease', 0.0),
                                          bootstrap=g('bootstrap', True), class_weight=g('class_weight', None),
                                          random_state=seed, n_jobs=1)
        if algorithm == 'gradient_boosting':
            from sklearn.ensemble import GradientBoostingClassifier
            return GradientBoostingClassifier(learning_rate=g('learning_rate', 0.05), n_estimators=g('n_estimators', 100),
                                              max_depth=_depth(p, 3) or 3, subsample=g('subsample', 1.0),
                                              max_features=None if g('colsample', 1.0) >= 1 else p['colsample'],
                                              random_state=seed)
    else:
        if algorithm == 'linear_regression':
            from sklearn.linear_model import LinearRegression
            from sklearn.pipeline import Pipeline
            from sklearn.preprocessing import StandardScaler
            est = LinearRegression(fit_intercept=g('fit_intercept', True))
            return Pipeline([('scale', StandardScaler()), ('model', est)]) if g('normalize', True) else est
        if algorithm in ('decision_tree', 'random_forest'):
            from sklearn.ensemble import RandomForestRegressor
            from sklearn.tree import DecisionTreeRegressor
            common = dict(criterion=g('criterion', 'squared_error'), max_depth=_depth(p, None),
                          min_samples_split=_split(p.get('min_samples_split')),
                          min_weight_fraction_leaf=g('min_weight_fraction_leaf', 0.0),
                          max_features=_max_features(g('max_features', 'auto'), True),
                          min_impurity_decrease=g('min_impurity_decrease', 0.0), random_state=seed)
            if algorithm == 'decision_tree':
                return DecisionTreeRegressor(splitter=g('splitter', 'best'), max_leaf_nodes=p.get('max_leaf_nodes'), **common)
            return RandomForestRegressor(n_estimators=g('n_estimators', 100), bootstrap=g('bootstrap', True), n_jobs=1, **common)
    raise ConfigError(f'Algorithm {algorithm!r} is not available for {task}')


def importance(estimator, names):
    """Importance per output column (impurity decrease, or absolute coefficients for linear models)."""
    import numpy as np
    final = estimator[-1] if hasattr(estimator, 'steps') else estimator
    if hasattr(final, 'feature_importances_'):
        return np.asarray(final.feature_importances_, dtype=float)
    if hasattr(final, 'estimators_') and hasattr(final.estimators_[0], 'coef_'):  # one-vs-rest
        return np.mean([np.abs(e.coef_).ravel() for e in final.estimators_], axis=0)
    if hasattr(final, 'coef_'):
        coef = np.abs(np.atleast_2d(final.coef_))
        return coef.mean(axis=0)
    return np.zeros(len(names))


def importance_by_field(prep, values):
    out, i = {}, 0
    for c in prep['columns']:
        width = len(c['categories']) if c['kind'] == 'onehot' else 1
        out[c['field']] = float(sum(values[i:i + width]))
        i += width
    total = sum(out.values())
    return {k: v / total for k, v in out.items()} if total > 0 else out


# =========================================================================== fitting

def task_of(data: Table, target, requested='auto'):
    if requested != 'auto':
        return requested
    f = data.schema.field(target)
    return 'regression' if f.type.is_numeric and f.type is not FieldType.BOOL else 'classification'


def predictors_of(data: Table, info: dict, target):
    chosen = (info.get('estimator') or {}).get('features')
    names = chosen or [n for n in data.schema.names if n != target]
    excluded = set(info['transform']['exclude'])
    missing_fields = [n for n in names if n not in data.schema]
    if missing_fields:
        raise SchemaError(f'Unknown feature field(s) {missing_fields}')
    if target in names:
        raise ConfigError('The target field cannot also be a feature')
    return [n for n in names if n not in excluded]


def _target(data, target, task, classes=None):
    if target not in data.schema:
        raise SchemaError(f'Unknown target field {target!r}')
    if task == 'regression':
        return target_vector(data, target, 'numeric')
    return target_vector(data, target, 'categorical', classes)


def _train(estimator, X, y):
    from sklearn.exceptions import ConvergenceWarning
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', ConvergenceWarning)
        estimator.fit(X, y)
    return [str(w.message).split('.')[0] for w in caught if issubclass(w.category, ConvergenceWarning)]


def _proba(estimator, X, n_classes):
    """Probabilities over all classes, also when a training fold lacked some classes."""
    import numpy as np
    raw = estimator.predict_proba(X)
    out = np.zeros((len(X), n_classes))
    out[:, np.asarray(estimator.classes_, dtype=int)] = raw
    return out


def fit(data: Table, info: dict, warn=None):
    """Fit a pipeline on data; returns (model_info, estimator)."""
    est = info.get('estimator')
    if not est:
        raise ConfigError('The pipeline has no algorithm; add a Classification or Regression tool before Fit')
    target, task = est['target'], est['task']
    y_all, ymask, classes = _target(data, target, task)
    if task == 'classification' and len(classes or []) < 2:
        raise DataError('Classification needs at least two target classes')
    excluded = int((~ymask).sum())
    if excluded and warn:
        warn(f'{excluded} record(s) with a missing target were left out of training')
    import numpy as np
    rows = np.flatnonzero(ymask)
    train = data.take(rows.tolist())
    predictors = predictors_of(data, info, target)
    prep = fit_prep(train, predictors, info, warn)
    X, xmask, _ = prep_matrix(prep, train)
    X, y = X[xmask], y_all[rows][xmask]
    if len(y) < 3:
        raise DataError(f'Not enough complete records to fit: {len(y)}')
    estimator = make_estimator(task, est['algorithm'], est.get('params') or {})
    notes = _train(estimator, X, y)
    if notes and warn:
        warn(f'{ALGORITHM_NAMES[est["algorithm"]]}: {notes[0]}; consider more iterations')
    if task == 'classification':
        metrics = classification_metrics(y, _proba(estimator, X, len(classes)), classes)
    else:
        metrics = regression_metrics(y, estimator.predict(X))
    metrics['Excluded'] = excluded + int((~xmask).sum())
    names = prep_names(prep)
    model_info = {'family': 'ml_pipeline', 'task': task, 'target': target, 'predictors': [c['field'] for c in prep['columns']],
                  'prep': prep, 'classes': classes, 'algorithm': est['algorithm'], 'params': est.get('params') or {},
                  'metrics': metrics, 'importance': importance_by_field(prep, importance(estimator, names)),
                  'spec': {'tool': 'core.ml_fit', 'config': {'pipeline': info}}}
    return model_info, estimator


def cross_validate(data: Table, info: dict, folds: int, seed: int, warn=None):
    """Out-of-fold metrics for a pipeline; the preparation is refitted inside each fold."""
    import numpy as np
    from sklearn.model_selection import KFold, StratifiedKFold
    est = info['estimator']
    target, task = est['target'], est['task']
    y_all, ymask, classes = _target(data, target, task)
    rows = np.flatnonzero(ymask)
    usable = data.take(rows.tolist())
    y = y_all[rows]
    predictors = predictors_of(data, info, target)
    if task == 'classification':
        smallest = int(np.bincount(y).min()) if len(y) else 0
        splitter = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed) if smallest >= folds else \
            KFold(n_splits=folds, shuffle=True, random_state=seed)
        pooled = np.full((len(y), len(classes)), np.nan)
    else:
        splitter = KFold(n_splits=folds, shuffle=True, random_state=seed)
        pooled = np.full(len(y), np.nan)
    for train_idx, test_idx in splitter.split(np.zeros(len(y)), y):
        train, test = usable.take(train_idx.tolist()), usable.take(test_idx.tolist())
        prep = fit_prep(train, predictors, info)
        Xtr, mtr, _ = prep_matrix(prep, train)
        Xte, mte, _ = prep_matrix({**prep, 'unknown': 'ignore'}, test)
        estimator = make_estimator(task, est['algorithm'], est.get('params') or {})
        _train(estimator, Xtr[mtr], y[train_idx][mtr])
        keep = test_idx[mte]
        if not len(keep):
            continue
        if task == 'classification':
            pooled[keep] = _proba(estimator, Xte[mte], len(classes))
        else:
            pooled[keep] = estimator.predict(Xte[mte])
    if task == 'classification':
        return classification_metrics(y, pooled, classes)
    return regression_metrics(y.astype(float), pooled)


def predict(info, estimator, table: Table):
    """Same contract as ``modeling.predict``."""
    import numpy as np
    X, mask, unseen = prep_matrix(info['prep'], table)
    if info['task'] == 'classification':
        proba = np.full((len(table), len(info['classes'])), np.nan)
        if mask.any():
            proba[mask] = _proba(estimator, X[mask], len(info['classes']))
        return {'proba': proba, 'mask': mask, 'unseen': unseen}
    value = np.full(len(table), np.nan)
    if mask.any():
        value[mask] = estimator.predict(X[mask])
    return {'value': value, 'mask': mask, 'unseen': unseen}
