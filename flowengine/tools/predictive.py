"""Predictive tools: models, scoring, evaluation and diagnostics (statsmodels, scikit-learn, networkx).

Standard, documented methods, independently implemented. They are not the vendor's R macros, and
where an Alteryx tool uses a specific R package (for example MARS for the Spline Model), the method
used here is stated in the tool description. Models travel on a ``model`` anchor (see
``flowengine.modeling``). Fitting is deterministic for a given ``seed``.
"""
from __future__ import annotations

import json
import math
import warnings

from .. import charts, modeling
from ..errors import ConfigError, DataError, SchemaError
from ..modeling import (MODEL_SCHEMA, Design, classification_metrics, expect_model_schema, missing, model_table,
                        predict, read_models, register_fitter, regression_metrics, target_vector)
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
MODEL = Anchor('model', 'Model', description='Fitted model for Score, Model Comparison and other tools')
CHART = Anchor('chart', 'Chart', description='Rendered SVG chart')
COEF_SCHEMA = Schema([Field('Term', FieldType.V_WSTRING, TEXT), Field('Estimate', FieldType.DOUBLE),
                      Field('StdError', FieldType.DOUBLE), Field('Statistic', FieldType.DOUBLE),
                      Field('PValue', FieldType.DOUBLE), Field('ExpEstimate', FieldType.DOUBLE)])
FIT_SCHEMA = Schema([Field('Metric', FieldType.V_WSTRING, 64), Field('Value', FieldType.DOUBLE)])
IMPORTANCE_SCHEMA = Schema([Field('Field', FieldType.V_WSTRING, TEXT), Field('Importance', FieldType.DOUBLE),
                            Field('Rank', FieldType.INT32)])
_FIELDS = {'type': 'array', 'uniqueItems': True, 'minItems': 1, 'items': {'type': 'string', 'minLength': 1}}
_BASE = {'target_field': {'type': 'string', 'minLength': 1}, 'predictors': _FIELDS,
         'model_name': {'type': ['string', 'null'], 'default': None}}


def _f(name, t=FieldType.DOUBLE):
    return Field(name, t, TEXT if t == FieldType.V_WSTRING else None)


def _fit_table(metrics: dict):
    rows = [(k, float(v)) for k, v in metrics.items() if v is not None and isinstance(v, (int, float)) and math.isfinite(v)]
    return Table.from_rows(FIT_SCHEMA, rows)


def _check_fields(schema, cfg, extra=()):
    require_fields(schema, [cfg['target_field'], *cfg['predictors'], *[e for e in extra if e]])
    if cfg['target_field'] in cfg['predictors']:
        raise ConfigError('The target field cannot also be a predictor')


def _training(table, cfg, kind, drop_first, classes=None, extra_mask=None):
    """(design, X, y, mask, classes, excluded) on usable rows."""
    import numpy as np
    design = Design.fit(table, cfg['predictors'], drop_first)
    X, xmask, _ = design.matrix(table)
    y, ymask, classes = target_vector(table, cfg['target_field'], kind, classes)
    mask = xmask & ymask & (extra_mask if extra_mask is not None else True)
    excluded = int(len(table) - mask.sum())
    if mask.sum() < max(3, X.shape[1] + 1 if drop_first else 3):
        raise DataError(f'Not enough complete records to fit: {int(mask.sum())} usable of {len(table)}')
    return design, X[mask], y[mask], mask, classes, excluded


def _name(cfg, default):
    return cfg.get('model_name') or default


def _info(cfg, tool_id, family, task, design, classes=None, **extra):
    return {'family': family, 'task': task, 'target': cfg['target_field'], 'predictors': cfg['predictors'],
            'design': design.to_json(), 'classes': classes, 'spec': {'tool': tool_id, 'config': cfg}, **extra}


# =========================================================================== coefficient models (statsmodels)

def _coef_rows(names, res, intercept, exp=False):
    import numpy as np
    terms = (['(Intercept)'] if intercept else []) + names
    params, bse = np.asarray(res.params), np.asarray(res.bse)
    stats_ = np.asarray(res.tvalues)
    pv = np.asarray(res.pvalues)
    rows = []
    for i, term in enumerate(terms):
        est = float(params[i])
        rows.append((term, est, float(bse[i]), float(stats_[i]), float(pv[i]), float(math.exp(est)) if exp and abs(est) < 700 else None))
    return rows


def _glm_fit(ctx, cfg, table, tool_id, family):
    import numpy as np
    import statsmodels.api as sm
    kind = 'categorical' if family == 'logistic' else 'numeric'
    classes = None
    if family == 'logistic':
        labels = sorted({format_value(v) for v in table.column(cfg['target_field']) if not missing(v)})
        if len(labels) != 2:
            raise DataError(f'Logistic regression needs exactly two target classes; found {len(labels)}')
        positive = cfg.get('positive_class') or labels[1]
        if positive not in labels:
            raise ConfigError(f'positive_class {positive!r} is not a target value')
        classes = [l for l in labels if l != positive] + [positive]
    offset = cfg.get('offset_field')
    extra = None
    if offset:
        vals = table.column(offset)
        extra = np.array([not missing(v) and float(v) > 0 for v in vals])
    design, X, y, mask, classes, excluded = _training(table, cfg, kind, True, classes, extra)
    intercept = cfg.get('intercept', True)
    Xc = sm.add_constant(X, has_constant='add') if intercept else X
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        try:
            if family == 'linear':
                w = None
                if cfg.get('weight_field'):
                    w = np.array([float(v) for v in np.array(table.column(cfg['weight_field']), dtype=object)[mask]])
                res = (sm.WLS(y, Xc, weights=w) if w is not None else sm.OLS(y, Xc)).fit()
            elif family == 'negative_binomial':
                kw = {'exposure': np.array([float(v) for v in np.array(table.column(offset), dtype=object)[mask]])} if offset else {}
                res = sm.NegativeBinomial(y, Xc, **kw).fit(disp=0, maxiter=200)
            else:
                fam = {'logistic': sm.families.Binomial(), 'poisson': sm.families.Poisson(),
                       'gamma': sm.families.Gamma(link=sm.families.links.Log())}[family]
                if family == 'gamma' and (y <= 0).any():
                    raise DataError('Gamma regression needs a strictly positive target')
                if family == 'poisson' and ((y < 0).any() or (y != np.round(y)).any()):
                    raise DataError('Count regression needs non-negative whole-number targets')
                kw = {'offset': np.log([float(v) for v in np.array(table.column(offset), dtype=object)[mask]])} if offset else {}
                res = sm.GLM(y, Xc, family=fam, **kw).fit()
        except np.linalg.LinAlgError as e:
            raise DataError(f'Model could not be fitted ({e}); check for constant or collinear predictors') from None
        except Exception as e:
            if 'Separation' in type(e).__name__ or 'separation' in str(e):
                raise DataError('Perfect separation: a predictor determines the target exactly, so coefficients are infinite') from None
            raise
    # statsmodels 0.15 warns (rather than raises) on separation; never return its divergent estimates.
    if family == 'logistic' and any('separation' in str(w.message).lower() or 'Separation' in w.category.__name__ for w in caught):
        raise DataError('Perfect separation: a predictor determines the target exactly, so coefficients are infinite')
    params = np.asarray(res.params)
    if family == 'negative_binomial':
        alpha = float(params[-1])
        params = params[:-1]
    names = design.names
    payload = {'intercept': float(params[0]) if intercept else None,
               'coefficients': [float(v) for v in (params[1:] if intercept else params)]}
    if family == 'gamma':
        payload['scale'] = float(res.scale)
    rows = _coef_rows(names, res, intercept, exp=family != 'linear')
    if family == 'negative_binomial':
        payload['alpha'] = alpha  # the dispersion is reported in Fit, not as a coefficient row
    metrics = {'N': float(len(y)), 'Excluded': float(excluded), 'LogLikelihood': float(res.llf), 'AIC': float(res.aic),
               'BIC': float(getattr(res, 'bic_llf', None) if getattr(res, 'bic_llf', None) is not None else res.bic)}
    if family == 'linear':
        metrics.update(R2=float(res.rsquared), AdjustedR2=float(res.rsquared_adj), F=float(res.fvalue) if res.fvalue is not None else None,
                       FPValue=float(res.f_pvalue) if res.f_pvalue is not None else None,
                       RMSE=float(math.sqrt(res.mse_resid)), DFResidual=float(res.df_resid))
    elif family == 'logistic':
        p1 = res.predict(Xc)
        metrics.update(Deviance=float(res.deviance), McFaddenR2=float(1 - res.llf / res.llnull),
                       Accuracy=float(((p1 >= 0.5).astype(int) == y).mean()), AUC=modeling.auc(y.astype(int), p1))
    elif family != 'negative_binomial':
        metrics.update(Deviance=float(res.deviance), PearsonChi2=float(res.pearson_chi2))
    else:
        metrics['Alpha'] = alpha
    info = _info(cfg, tool_id, family, 'classification' if family == 'logistic' else 'regression', design, classes,
                 coefficients=rows, metrics=metrics, offset_field=offset if family != 'linear' else None,
                 n_params=int(len(params)))
    return info, json.dumps(payload).encode('utf-8')


class _CoefficientModel(Tool):
    family = None
    title = None

    def infer(self, ctx, cfg, inputs):
        _check_fields(inputs['input'], cfg, [cfg.get('weight_field'), cfg.get('offset_field')])
        return {'model': MODEL_SCHEMA, 'coefficients': COEF_SCHEMA, 'fit': FIT_SCHEMA}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        info, payload = modeling.FITTERS[self.manifest.tool_id](ctx, cfg, table)
        if info['metrics'].get('Excluded'):
            ctx.info(f"{int(info['metrics']['Excluded'])} record(s) with missing values were excluded")
        return {'model': model_table(_name(cfg, self.title), info['family'], cfg['target_field'], info, payload),
                'coefficients': Table.from_rows(COEF_SCHEMA, info['coefficients']), 'fit': _fit_table(info['metrics'])}


def _coef_manifest(tool_id, name, catalog, description, extra):
    return ToolManifest(
        tool_id=tool_id, version='1.0.0', display_name=name, catalog_ids=(catalog,),
        inputs=(Anchor('input', 'Input', description='Training records'),),
        outputs=(MODEL, Anchor('coefficients', 'Coefficients'), Anchor('fit', 'Fit', description='Training fit statistics')),
        description=description,
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['target_field', 'predictors'],
                       'properties': {**_BASE, 'intercept': {'type': 'boolean', 'default': True}, **extra}})


class LinearRegression(_CoefficientModel):
    title = 'Linear regression'
    manifest = _coef_manifest(
        'core.linear_regression', 'Linear Regression', 'predictive:linear-regression',
        'Ordinary (or weighted) least-squares regression of a numeric target on numeric and categorical predictors '
        '(one-hot, first category as baseline). Outputs coefficients with standard errors, t statistics and '
        'p-values, plus R², adjusted R², F test, RMSE, AIC and BIC. Records with missing values are excluded.',
        {'weight_field': {'type': ['string', 'null'], 'default': None}})


class LogisticRegression(_CoefficientModel):
    title = 'Logistic regression'
    manifest = _coef_manifest(
        'core.logistic_regression', 'Logistic Regression', 'predictive:logistic-regression',
        'Binary logistic regression (binomial GLM, logit link) of a two-class target. positive_class sets the '
        'modelled class (default: the later class alphabetically). Outputs coefficients with z statistics, '
        'p-values and odds ratios, plus deviance, McFadden R², AIC and training accuracy and AUC. Perfect '
        'separation is reported rather than producing infinite coefficients.',
        {'positive_class': {'type': ['string', 'null'], 'default': None}})


class CountRegression(_CoefficientModel):
    title = 'Count regression'
    manifest = _coef_manifest(
        'core.count_regression', 'Count Regression', 'predictive:count-regression',
        'Poisson or negative binomial regression (log link) of a non-negative count target, with an optional '
        'exposure field (log offset). Outputs coefficients, rate ratios, deviance, AIC and, for negative '
        'binomial, the dispersion alpha.',
        {'distribution': {'enum': ['poisson', 'negative_binomial'], 'default': 'poisson'},
         'offset_field': {'type': ['string', 'null'], 'default': None, 'description': 'Exposure (must be > 0)'}})

    def run(self, ctx, cfg, inputs):
        return super().run(ctx, cfg, inputs)


class GammaRegression(_CoefficientModel):
    title = 'Gamma regression'
    manifest = _coef_manifest(
        'core.gamma_regression', 'Gamma Regression', 'predictive:gamma-regression',
        'Gamma GLM with log link for a strictly positive, right-skewed target (costs, durations), with an optional '
        'exposure offset. Outputs coefficients, multiplicative effects, deviance, dispersion and AIC.',
        {'offset_field': {'type': ['string', 'null'], 'default': None}})


register_fitter('core.linear_regression')(lambda ctx, cfg, t: _glm_fit(ctx, cfg, t, 'core.linear_regression', 'linear'))
register_fitter('core.logistic_regression')(lambda ctx, cfg, t: _glm_fit(ctx, cfg, t, 'core.logistic_regression', 'logistic'))
register_fitter('core.count_regression')(lambda ctx, cfg, t: _glm_fit(ctx, cfg, t, 'core.count_regression', cfg.get('distribution', 'poisson')))
register_fitter('core.gamma_regression')(lambda ctx, cfg, t: _glm_fit(ctx, cfg, t, 'core.gamma_regression', 'gamma'))


# =========================================================================== scikit-learn models

def _task(table, cfg):
    f = table.schema.field(cfg['target_field'])
    if cfg.get('task', 'auto') != 'auto':
        return cfg['task']
    return 'regression' if f.type.is_numeric and f.type is not FieldType.BOOL else 'classification'


def _sk_fit(ctx, cfg, table, tool_id, family, make):
    task = _task(table, cfg)
    design, X, y, mask, classes, excluded = _training(table, cfg, 'numeric' if task == 'regression' else 'categorical', False)
    if task == 'classification' and len(classes) < 2:
        raise DataError('Classification needs at least two target classes')
    estimator = make(task, design, X)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        estimator.fit(X, y)
    import numpy as np
    if task == 'classification':
        metrics = classification_metrics(y, estimator.predict_proba(X), classes)
    else:
        metrics = regression_metrics(y, estimator.predict(X))
    metrics['Excluded'] = excluded
    importance = None
    final = estimator[-1] if hasattr(estimator, 'steps') else estimator
    if hasattr(final, 'feature_importances_'):
        importance = _by_field(design, np.asarray(final.feature_importances_))
    info = _info(cfg, tool_id, family, task, design, classes, metrics=metrics, importance=importance)
    if hasattr(final, 'oob_score_'):
        info['metrics']['OOBScore'] = float(final.oob_score_)
    return info, modeling.sk_dumps(estimator), estimator


def _by_field(design, values):
    """Sum one-hot column importances back to their source field."""
    out, i = {}, 0
    for c in design.columns:
        width = 1 if c['kind'] == 'numeric' else len(c['categories']) - (1 if c.get('drop_first') else 0)
        out[c['field']] = float(sum(values[i:i + width]))
        i += width
    return out


def _importance_table(importance):
    ranked = sorted((importance or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    return Table.from_rows(IMPORTANCE_SCHEMA, [(k, v, i + 1) for i, (k, v) in enumerate(ranked)])


_TASK = {'task': {'enum': ['auto', 'classification', 'regression'], 'default': 'auto'},
         'seed': {'type': 'integer', 'minimum': 0, 'default': 1}}


def _sk_manifest(tool_id, name, catalog, description, extra, outputs=()):
    return ToolManifest(
        tool_id=tool_id, version='1.0.0', display_name=name, catalog_ids=(catalog,),
        inputs=(Anchor('input', 'Input', description='Training records'),),
        outputs=(MODEL, Anchor('fit', 'Fit', description='Training metrics'), *outputs), description=description,
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['target_field', 'predictors'],
                       'properties': {**_BASE, **_TASK, **extra}})


class _SkModel(Tool):
    title = None

    def make(self, cfg, task, design, X):
        raise NotImplementedError

    def infer(self, ctx, cfg, inputs):
        _check_fields(inputs['input'], cfg)
        out = {'model': MODEL_SCHEMA, 'fit': FIT_SCHEMA}
        if any(a.name == 'importance' for a in self.manifest.outputs):
            out['importance'] = IMPORTANCE_SCHEMA
        return out

    def extra(self, info, estimator, cfg):
        return {}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        info, payload, estimator = _sk_fit(ctx, cfg, table, self.manifest.tool_id, self.family,
                                           lambda task, design, X: self.make(cfg, task, design, X))
        out = {'model': model_table(_name(cfg, self.title), self.family, cfg['target_field'], info, payload),
               'fit': _fit_table(info['metrics'])}
        if any(a.name == 'importance' for a in self.manifest.outputs):
            out['importance'] = _importance_table(info.get('importance'))
        out.update(self.extra(info, estimator, cfg))
        return out


def _register_sk(cls):
    register_fitter(cls.manifest.tool_id)(
        lambda ctx, cfg, t: _sk_fit(ctx, cfg, t, cls.manifest.tool_id, cls.family,
                                    lambda task, design, X: cls().make(cfg, task, design, X))[:2])
    return cls


TREE_SCHEMA = Schema([Field('Node', FieldType.INT32), Field('Depth', FieldType.INT32), _f('Split', FieldType.V_WSTRING),
                      Field('Left', FieldType.INT32), Field('Right', FieldType.INT32), Field('Samples', FieldType.INT64),
                      _f('Prediction', FieldType.V_WSTRING), Field('Impurity', FieldType.DOUBLE)])


@_register_sk
class DecisionTree(_SkModel):
    family, title = 'decision_tree', 'Decision tree'
    manifest = _sk_manifest(
        'core.decision_tree', 'Decision Tree', 'predictive:decision-tree',
        'CART decision tree (Gini for classification, squared error for regression) with depth and leaf-size '
        'limits. Outputs the model, training metrics, field importance and the tree as rows (split rule, '
        'children, samples and prediction per node).',
        {'max_depth': {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 50, 'default': 5},
         'min_samples_leaf': {'type': 'integer', 'minimum': 1, 'default': 5}},
        (Anchor('importance', 'Importance'), Anchor('tree', 'Tree', description='One row per tree node')))

    def make(self, cfg, task, design, X):
        from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
        cls = DecisionTreeClassifier if task == 'classification' else DecisionTreeRegressor
        return cls(max_depth=cfg['max_depth'], min_samples_leaf=cfg['min_samples_leaf'], random_state=cfg['seed'])

    def infer(self, ctx, cfg, inputs):
        return {**super().infer(ctx, cfg, inputs), 'tree': TREE_SCHEMA}

    def extra(self, info, estimator, cfg):
        t = estimator.tree_
        names = Design.from_json(info['design']).names
        rows = []
        depth = {0: 0}
        for node in range(t.node_count):
            left, right = int(t.children_left[node]), int(t.children_right[node])
            if left >= 0:
                depth[left] = depth[right] = depth[node] + 1
            value = t.value[node][0]
            pred = info['classes'][int(value.argmax())] if info['task'] == 'classification' else format_value(float(value[0]))
            split = f'{names[t.feature[node]]} <= {t.threshold[node]:.6g}' if left >= 0 else None
            rows.append((node, depth[node], split, left if left >= 0 else None, right if right >= 0 else None,
                         int(t.n_node_samples[node]), pred, float(t.impurity[node])))
        return {'tree': Table.from_rows(TREE_SCHEMA, rows)}


@_register_sk
class ForestModel(_SkModel):
    family, title = 'forest', 'Random forest'
    manifest = _sk_manifest(
        'core.forest_model', 'Forest Model', 'predictive:forest-model',
        'Random forest of CART trees with bootstrap samples, out-of-bag score and field importance (mean '
        'impurity decrease).',
        {'n_trees': {'type': 'integer', 'minimum': 1, 'maximum': 2000, 'default': 200},
         'max_features': {'enum': ['sqrt', 'log2', 'all'], 'default': 'sqrt'},
         'min_samples_leaf': {'type': 'integer', 'minimum': 1, 'default': 1}},
        (Anchor('importance', 'Importance'),))

    def make(self, cfg, task, design, X):
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        cls = RandomForestClassifier if task == 'classification' else RandomForestRegressor
        return cls(n_estimators=cfg['n_trees'], max_features=None if cfg['max_features'] == 'all' else cfg['max_features'],
                   min_samples_leaf=cfg['min_samples_leaf'], oob_score=True, random_state=cfg['seed'], n_jobs=1)


@_register_sk
class BoostedModel(_SkModel):
    family, title = 'boosted', 'Boosted model'
    manifest = _sk_manifest(
        'core.boosted_model', 'Boosted Model', 'predictive:boosted-model',
        'Gradient-boosted trees (log loss for classification, squared error for regression) with learning rate, '
        'depth and subsampling. Outputs field importance. This is scikit-learn gradient boosting, not the '
        'vendor\'s gbm R package.',
        {'n_trees': {'type': 'integer', 'minimum': 1, 'maximum': 5000, 'default': 200},
         'learning_rate': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1, 'default': 0.1},
         'max_depth': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 3},
         'subsample': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1, 'default': 1.0}},
        (Anchor('importance', 'Importance'),))

    def make(self, cfg, task, design, X):
        from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
        cls = GradientBoostingClassifier if task == 'classification' else GradientBoostingRegressor
        return cls(n_estimators=cfg['n_trees'], learning_rate=cfg['learning_rate'], max_depth=cfg['max_depth'],
                   subsample=cfg['subsample'], random_state=cfg['seed'])


def _scaled(estimator):
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    return Pipeline([('scale', StandardScaler()), ('model', estimator)])


@_register_sk
class SupportVectorMachine(_SkModel):
    family, title = 'svm', 'Support vector machine'
    manifest = _sk_manifest(
        'core.support_vector_machine', 'Support Vector Machine', 'predictive:support-vector-machine',
        'Support vector classification (Platt-scaled probabilities) or regression on standardised predictors, '
        'with an RBF, linear or polynomial kernel.',
        {'kernel': {'enum': ['rbf', 'linear', 'poly'], 'default': 'rbf'},
         'C': {'type': 'number', 'exclusiveMinimum': 0, 'default': 1.0},
         'gamma': {'enum': ['scale', 'auto'], 'default': 'scale'}})

    def make(self, cfg, task, design, X):
        from sklearn.svm import SVC, SVR
        if task == 'classification':
            return _scaled(SVC(kernel=cfg['kernel'], C=cfg['C'], gamma=cfg['gamma'], probability=True, random_state=cfg['seed']))
        return _scaled(SVR(kernel=cfg['kernel'], C=cfg['C'], gamma=cfg['gamma']))


@_register_sk
class NeuralNetwork(_SkModel):
    family, title = 'neural_network', 'Neural network'
    manifest = _sk_manifest(
        'core.neural_network', 'Neural Network', 'predictive:neural-network',
        'Feed-forward neural network (multi-layer perceptron, Adam optimiser, early stopping off) on '
        'standardised predictors, with configurable hidden layers and activation.',
        {'hidden_layers': {'type': 'array', 'minItems': 1, 'maxItems': 5, 'items': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
                           'default': [10]},
         'activation': {'enum': ['relu', 'tanh', 'logistic'], 'default': 'relu'},
         'max_iterations': {'type': 'integer', 'minimum': 10, 'maximum': 10000, 'default': 500},
         'alpha': {'type': 'number', 'minimum': 0, 'default': 0.0001, 'description': 'L2 penalty'}})

    def make(self, cfg, task, design, X):
        from sklearn.neural_network import MLPClassifier, MLPRegressor
        cls = MLPClassifier if task == 'classification' else MLPRegressor
        return _scaled(cls(hidden_layer_sizes=tuple(cfg['hidden_layers']), activation=cfg['activation'],
                           max_iter=cfg['max_iterations'], alpha=cfg['alpha'], random_state=cfg['seed']))


@_register_sk
class SplineModel(_SkModel):
    family, title = 'spline', 'Spline model'
    manifest = _sk_manifest(
        'core.spline_model', 'Spline Model', 'predictive:spline-model',
        'Additive regression splines: each numeric predictor is expanded into a B-spline basis (n_knots, degree), '
        'categorical predictors are one-hot, and a ridge (regression) or logistic (classification) model is '
        'fitted. This is penalised spline regression, not the MARS (earth) algorithm the vendor tool uses.',
        {'n_knots': {'type': 'integer', 'minimum': 2, 'maximum': 50, 'default': 5},
         'degree': {'type': 'integer', 'minimum': 1, 'maximum': 5, 'default': 3},
         'penalty': {'type': 'number', 'exclusiveMinimum': 0, 'default': 1.0}})

    def make(self, cfg, task, design, X):
        from sklearn.compose import ColumnTransformer
        from sklearn.linear_model import LogisticRegression as SkLogistic, Ridge
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import SplineTransformer
        numeric, i = [], 0
        for c in design.columns:
            width = 1 if c['kind'] == 'numeric' else len(c['categories'])
            if c['kind'] == 'numeric':
                numeric.append(i)
            i += width
        basis = ColumnTransformer([('spline', SplineTransformer(n_knots=cfg['n_knots'], degree=cfg['degree']), numeric)],
                                  remainder='passthrough')
        final = (SkLogistic(C=1 / cfg['penalty'], max_iter=2000) if task == 'classification' else Ridge(alpha=cfg['penalty']))
        return Pipeline([('basis', basis), ('model', final)])


@register_fitter('core.naive_bayes')
def _nb_fitter(ctx, cfg, table):
    import numpy as np
    model = modeling.nb_fit(table, cfg['target_field'], cfg['predictors'], cfg.get('laplace', 1.0))
    design = Design.fit(table, cfg['predictors'], False)
    info = _info(cfg, 'core.naive_bayes', 'naive_bayes', 'classification', design, model['classes'])
    y, ymask, _ = target_vector(table, cfg['target_field'], 'categorical', model['classes'])
    proba = modeling._nb_proba(model, table, info)
    info['metrics'] = classification_metrics(np.where(ymask, y, -1), proba, model['classes'])
    return info, json.dumps(model).encode('utf-8')


class NaiveBayes(Tool):
    manifest = ToolManifest(
        tool_id='core.naive_bayes', version='1.0.0', display_name='Naive Bayes Classifier',
        catalog_ids=('predictive:naive-bayes-classifier',),
        inputs=(Anchor('input', 'Input'),), outputs=(MODEL, Anchor('fit', 'Fit')),
        description='Naive Bayes classifier mixing Gaussian likelihoods for numeric predictors and Laplace-smoothed '
                    'frequencies for categorical ones. A missing predictor value is skipped for that record. '
                    'Stored as plain parameters.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['target_field', 'predictors'],
                       'properties': {**_BASE, 'laplace': {'type': 'number', 'minimum': 0, 'default': 1.0}}})

    def infer(self, ctx, cfg, inputs):
        _check_fields(inputs['input'], cfg)
        return {'model': MODEL_SCHEMA, 'fit': FIT_SCHEMA}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        info, payload = _nb_fitter(ctx, cfg, table)
        if len(info['classes']) < 2:
            raise DataError('Naive Bayes needs at least two target classes')
        return {'model': model_table(_name(cfg, 'Naive Bayes'), 'naive_bayes', cfg['target_field'], info, payload),
                'fit': _fit_table(info['metrics'])}


# =========================================================================== scoring and evaluation

def _one_model(table, what='model'):
    models = read_models(table, what)
    if len(models) != 1:
        raise DataError(f'{what} input holds {len(models)} models; connect one')
    return models[0]


def _all_models(tables):
    out = []
    for t in tables if isinstance(tables, list) else [tables]:
        out.extend(read_models(t))
    return out


class Score(Tool):
    manifest = ToolManifest(
        tool_id='core.score', version='1.0.0', display_name='Score', catalog_ids=('predictive:score',),
        inputs=(Anchor('model', 'Model'), Anchor('input', 'Data')), outputs=(Anchor('output', 'Output'),),
        description='Apply a fitted model to records. Regression models add Score. Classification models add '
                    'Score_<class> probabilities and Prediction (the most probable class, or the positive class '
                    'when its probability reaches threshold for binary models). Records with a missing predictor '
                    'get null scores. Unseen categories encode as all-zero and are counted in the log.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'threshold': {'type': ['number', 'null'], 'minimum': 0, 'maximum': 1, 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        expect_model_schema(inputs['model'])
        return None  # added fields depend on the model's classes

    def run(self, ctx, cfg, inputs):
        import numpy as np
        name, info, payload = _one_model(inputs['model'])
        table = inputs['input']
        res = predict(info, payload, table)
        if res['unseen']:
            ctx.warn(f"{res['unseen']} value(s) were categories the model did not see in training")
        rows = list(table.rows())
        if 'value' in res:
            schema = Schema([*table.schema, Field('Score', FieldType.DOUBLE)])
            out = [(*r, float(v) if m and np.isfinite(v) else None) for r, v, m in zip(rows, res['value'], res['mask'])]
            return {'output': Table.from_rows(schema, out)}
        classes = info['classes']
        names = [f'Score_{c}' for c in classes]
        clash = [n for n in names + ['Prediction'] if n in table.schema]
        if clash:
            raise SchemaError(f'Scoring would overwrite field(s) {clash}')
        schema = Schema([*table.schema, *[Field(n, FieldType.DOUBLE) for n in names], Field('Prediction', FieldType.V_WSTRING, TEXT)])
        out = []
        for r, p, m in zip(rows, res['proba'], res['mask']):
            if not m or not np.all(np.isfinite(p)):
                out.append((*r, *([None] * len(classes)), None))
                continue
            if cfg['threshold'] is not None and len(classes) == 2:
                label = classes[1] if p[1] >= cfg['threshold'] else classes[0]
            else:
                label = classes[int(p.argmax())]
            out.append((*r, *[float(x) for x in p], label))
        return {'output': Table.from_rows(schema, out)}


def _evaluate(info, payload, table):
    """(metrics, y, proba_or_value) of a model on labelled data."""
    import numpy as np
    res = predict(info, payload, table)
    if info['task'] == 'classification':
        y, ymask, _ = target_vector(table, info['target'], 'categorical', info['classes'])
        y = np.where(ymask & res['mask'], y, -1)
        return classification_metrics(y, res['proba'], info['classes']), y, res['proba']
    y, ymask, _ = target_vector(table, info['target'], 'numeric')
    value = np.where(ymask & res['mask'], res['value'], np.nan)
    return regression_metrics(y, value), y, value


METRIC_NAMES = ('N', 'RMSE', 'MAE', 'R2', 'MAPE', 'Accuracy', 'AUC', 'MacroF1', 'LogLoss')
COMPARISON_SCHEMA = Schema([Field('Model', FieldType.V_WSTRING, 256), Field('ModelType', FieldType.V_WSTRING, 64)] +
                           [Field(m, FieldType.DOUBLE) for m in METRIC_NAMES])
CONFUSION_SCHEMA = Schema([Field('Model', FieldType.V_WSTRING, 256), Field('Actual', FieldType.V_WSTRING, TEXT),
                           Field('Predicted', FieldType.V_WSTRING, TEXT), Field('Count', FieldType.INT64)])


class ModelComparison(Tool):
    manifest = ToolManifest(
        tool_id='core.model_comparison', version='1.0.0', display_name='Model Comparison',
        catalog_ids=('predictive:model-comparison',),
        inputs=(Anchor('models', 'Models', multiple=True), Anchor('input', 'Validation data')),
        outputs=(Anchor('output', 'Metrics'), Anchor('confusion', 'Confusion', description='Classification confusion counts')),
        description='Evaluate one or more fitted models on the same labelled records: RMSE, MAE, R² and MAPE for '
                    'regression; accuracy, macro F1, log loss and (binary) AUC for classification; and confusion '
                    'counts. All models must predict the same target.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    def infer(self, ctx, cfg, inputs):
        for s in inputs['models']:
            expect_model_schema(s)
        return {'output': COMPARISON_SCHEMA, 'confusion': CONFUSION_SCHEMA}

    def run(self, ctx, cfg, inputs):
        models = _all_models(inputs['models'])
        if len({m[1]['target'] for m in models}) > 1:
            raise DataError('Model Comparison models predict different targets')
        rows, conf = [], []
        for name, info, payload in models:
            metrics, y, pred = _evaluate(info, payload, inputs['input'])
            rows.append((name, info['family'], *[metrics.get(m) for m in METRIC_NAMES]))
            if info['task'] == 'classification':
                conf += [(name, a, p, n) for (a, p), n in sorted(modeling.confusion(y, pred, info['classes']).items())]
        return {'output': Table.from_rows(COMPARISON_SCHEMA, rows), 'confusion': Table.from_rows(CONFUSION_SCHEMA, conf)}


class CrossValidation(Tool):
    FOLDS = Schema([Field('Model', FieldType.V_WSTRING, 256), Field('Fold', FieldType.INT32)] +
                   [Field(m, FieldType.DOUBLE) for m in METRIC_NAMES])
    SUMMARY = Schema([Field('Model', FieldType.V_WSTRING, 256), Field('Metric', FieldType.V_WSTRING, 32),
                      Field('Mean', FieldType.DOUBLE), Field('StdDev', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.cross_validation', version='1.0.0', display_name='Cross-Validation',
        catalog_ids=('predictive:cross-validation',),
        inputs=(Anchor('models', 'Models', multiple=True), Anchor('input', 'Data')),
        outputs=(Anchor('folds', 'Folds'), Anchor('summary', 'Summary')),
        description='k-fold cross-validation. Each connected model is refitted with its own settings on k-1 folds '
                    'and evaluated on the held-out fold (stratified by class for classification), with per-fold '
                    'metrics and their mean and standard deviation. Folds are reproducible for a seed and repeats.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'folds': {'type': 'integer', 'minimum': 2, 'maximum': 50, 'default': 5},
            'repeats': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 1},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 1}}})

    def infer(self, ctx, cfg, inputs):
        for s in inputs['models']:
            expect_model_schema(s)
        return {'folds': self.FOLDS, 'summary': self.SUMMARY}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        from sklearn.model_selection import KFold, StratifiedKFold
        table = inputs['input']
        rows, summary = [], []
        for name, info, _ in _all_models(inputs['models']):
            if info['task'] == 'classification':
                y, ymask, _ = target_vector(table, info['target'], 'categorical', info['classes'])
            else:
                y, ymask, _ = target_vector(table, info['target'], 'numeric')
            usable = np.flatnonzero(ymask)
            per_metric = {}
            fold_no = 0
            for rep in range(cfg['repeats']):
                splitter = (StratifiedKFold if info['task'] == 'classification' else KFold)(
                    n_splits=cfg['folds'], shuffle=True, random_state=cfg['seed'] + rep)
                try:
                    splits = list(splitter.split(usable, y[usable]))
                except ValueError as e:
                    raise DataError(f'Cross-validation cannot split the data: {e}') from None
                for train_i, test_i in splits:
                    ctx.check_cancelled()
                    fold_no += 1
                    fitted_info, fitted_payload = modeling.refit(ctx, info, table.take(list(usable[train_i])))
                    metrics, _, _ = _evaluate(fitted_info, fitted_payload, table.take(list(usable[test_i])))
                    rows.append((name, fold_no, *[metrics.get(m) for m in METRIC_NAMES]))
                    for m in METRIC_NAMES:
                        if metrics.get(m) is not None and m != 'N':
                            per_metric.setdefault(m, []).append(metrics[m])
            for m, vals in per_metric.items():
                summary.append((name, m, float(np.mean(vals)), float(np.std(vals, ddof=1)) if len(vals) > 1 else None))
        return {'folds': Table.from_rows(self.FOLDS, rows), 'summary': Table.from_rows(self.SUMMARY, summary)}


class LiftChart(Tool):
    SCHEMA = Schema([Field('Model', FieldType.V_WSTRING, 256), Field('Decile', FieldType.INT32),
                     Field('Records', FieldType.INT64), Field('Positives', FieldType.INT64), Field('Rate', FieldType.DOUBLE),
                     Field('CumulativeCapturePct', FieldType.DOUBLE), Field('Lift', FieldType.DOUBLE),
                     Field('CumulativeLift', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.lift_chart', version='1.0.0', display_name='Lift Chart', catalog_ids=('predictive:lift-chart',),
        inputs=(Anchor('models', 'Models', multiple=True), Anchor('input', 'Validation data')),
        outputs=(Anchor('output', 'Lift table'), CHART),
        description='Cumulative gains and lift of binary classification models on labelled records: records are '
                    'ranked by the positive-class probability and cut into bins (deciles by default), with the '
                    'share of all positives captured and the lift over random selection. The chart compares the '
                    'models\' cumulative gains with the random baseline.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'positive_class': {'type': ['string', 'null'], 'default': None},
            'bins': {'type': 'integer', 'minimum': 2, 'maximum': 100, 'default': 10}}})

    def infer(self, ctx, cfg, inputs):
        for s in inputs['models']:
            expect_model_schema(s)
        return {'output': self.SCHEMA, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        table = inputs['input']
        rows, series = [], {}
        for name, info, payload in _all_models(inputs['models']):
            if info['task'] != 'classification' or len(info['classes']) != 2:
                raise DataError(f'Lift Chart needs binary classification models; {name!r} is not')
            positive = cfg['positive_class'] or info['classes'][1]
            if positive not in info['classes']:
                raise ConfigError(f'positive_class {positive!r} is not a class of {name!r}')
            k = info['classes'].index(positive)
            res = predict(info, payload, table)
            y, ymask, _ = target_vector(table, info['target'], 'categorical', info['classes'])
            ok = ymask & res['mask']
            score, hit = res['proba'][ok, k], (y[ok] == k).astype(int)
            order = np.argsort(-score, kind='stable')
            hit = hit[order]
            total_pos, n = int(hit.sum()), len(hit)
            if not total_pos:
                raise DataError(f'Validation data has no positive ({positive!r}) records')
            base = total_pos / n
            bins = np.array_split(np.arange(n), cfg['bins'])
            cum_pos = cum_n = 0
            curve = [(0.0, 0.0)]
            for b, idx in enumerate(bins, start=1):
                pos = int(hit[idx].sum())
                cum_pos += pos
                cum_n += len(idx)
                rate = pos / len(idx) if len(idx) else 0.0
                rows.append((name, b, len(idx), pos, rate, 100 * cum_pos / total_pos, rate / base, (cum_pos / cum_n) / base))
                curve.append((100 * cum_n / n, 100 * cum_pos / total_pos))
            series[name] = curve
        series['Random'] = [(0.0, 0.0), (100.0, 100.0)]
        canvas = charts.line_chart(series, title='Cumulative gains', x_label='% of records (ranked by score)',
                                   y_label='% of positives captured')
        return {'output': Table.from_rows(self.SCHEMA, rows), 'chart': charts.chart_table(canvas, 'Cumulative gains')}


class ModelCoefficients(Tool):
    SCHEMA = Schema([Field('Model', FieldType.V_WSTRING, 256), *COEF_SCHEMA, Field('Importance', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.model_coefficients', version='1.0.0', display_name='Model Coefficients',
        catalog_ids=('predictive:model-coefficients',),
        inputs=(Anchor('models', 'Models', multiple=True),), outputs=(Anchor('output', 'Coefficients'),),
        description='List the coefficients (estimate, standard error, statistic, p-value and exp(estimate)) of '
                    'regression models, or field importances of tree-based models, one row per term.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    def infer(self, ctx, cfg, inputs):
        for s in inputs['models']:
            expect_model_schema(s)
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        rows = []
        for name, info, payload in _all_models(inputs['models']):
            if info.get('coefficients'):
                rows += [(name, *r, None) for r in info['coefficients']]
            elif info.get('importance'):
                rows += [(name, term, None, None, None, None, None, value) for term, value in
                         sorted(info['importance'].items(), key=lambda kv: -kv[1])]
            else:
                raise DataError(f'Model {name!r} ({info["family"]}) has no coefficients or importances')
        return {'output': Table.from_rows(self.SCHEMA, rows)}


class Stepwise(Tool):
    STEPS = Schema([Field('Step', FieldType.INT32), Field('Action', FieldType.V_WSTRING, 16),
                    Field('Field', FieldType.V_WSTRING, TEXT), Field('Criterion', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.stepwise', version='1.0.0', display_name='Stepwise', catalog_ids=('predictive:stepwise',),
        inputs=(Anchor('model', 'Model', description='A linear, logistic, count or gamma regression model'),
                Anchor('input', 'Data', description='The training records')),
        outputs=(MODEL, Anchor('steps', 'Steps'), Anchor('coefficients', 'Coefficients')),
        description='Select predictors for a regression model by AIC or BIC: backward elimination from all '
                    'predictors, forward selection from none, or both. Each step adds or removes the field that '
                    'improves the criterion most, and stops when nothing improves it. Outputs the refitted final '
                    'model and the steps taken.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'direction': {'enum': ['backward', 'forward', 'both'], 'default': 'backward'},
            'criterion': {'enum': ['AIC', 'BIC'], 'default': 'AIC'}}})

    def infer(self, ctx, cfg, inputs):
        expect_model_schema(inputs['model'])
        return {'model': MODEL_SCHEMA, 'steps': self.STEPS, 'coefficients': COEF_SCHEMA}

    def run(self, ctx, cfg, inputs):
        name, info, _ = _one_model(inputs['model'])
        if info['family'] not in ('linear', 'logistic', 'poisson', 'negative_binomial', 'gamma'):
            raise DataError('Stepwise works on linear, logistic, count and gamma regression models')
        table = inputs['input']
        spec = info['spec']
        all_fields = list(spec['config']['predictors'])
        crit = cfg['criterion']

        def score(fields):
            if not fields:
                fields_cfg = dict(spec['config'], predictors=[])
                return self._null_criterion(ctx, fields_cfg, table, spec, crit)
            fitted, _ = modeling.FITTERS[spec['tool']](ctx, dict(spec['config'], predictors=list(fields)), table)
            return fitted['metrics'][crit]
        current = all_fields if cfg['direction'] == 'backward' else []
        best = score(current)
        steps = [(0, 'start', ', '.join(current) or '(intercept only)', best)]
        while True:
            ctx.check_cancelled()
            options = []
            if cfg['direction'] in ('backward', 'both'):
                options += [('remove', f, [x for x in current if x != f]) for f in current]
            if cfg['direction'] in ('forward', 'both'):
                options += [('add', f, current + [f]) for f in all_fields if f not in current]
            scored = []
            for action, field, fields in options:
                try:
                    scored.append((score(fields), action, field, fields))
                except DataError:
                    continue
            if not scored:
                break
            value, action, field, fields = min(scored, key=lambda s: (s[0], s[2]))
            if value >= best - 1e-9:
                break
            best, current = value, fields
            steps.append((len(steps), action, field, value))
        if not current:
            raise DataError('Stepwise removed every predictor; the intercept-only model is best')
        final_info, payload = modeling.FITTERS[spec['tool']](ctx, dict(spec['config'], predictors=current), table)
        return {'model': model_table(f'{name} (stepwise)', final_info['family'], final_info['target'], final_info, payload),
                'steps': Table.from_rows(self.STEPS, steps), 'coefficients': Table.from_rows(COEF_SCHEMA, final_info['coefficients'])}

    @staticmethod
    def _null_criterion(ctx, cfg, table, spec, crit):
        import numpy as np
        import statsmodels.api as sm
        family = {'core.linear_regression': 'linear', 'core.logistic_regression': 'logistic',
                  'core.gamma_regression': 'gamma'}.get(spec['tool'], spec['config'].get('distribution', 'poisson'))
        kind = 'categorical' if family == 'logistic' else 'numeric'
        y, mask, classes = target_vector(table, cfg['target_field'], kind)
        y = y[mask]
        X = np.ones((len(y), 1))
        if family == 'linear':
            res = sm.OLS(y, X).fit()
        elif family == 'negative_binomial':
            res = sm.NegativeBinomial(y, X).fit(disp=0)
        else:
            fam = {'logistic': sm.families.Binomial(), 'poisson': sm.families.Poisson(),
                   'gamma': sm.families.Gamma(link=sm.families.links.Log())}[family]
            res = sm.GLM(y if family != 'logistic' else (y == 1).astype(float), X, family=fam).fit()
        return float(res.aic if crit == 'AIC' else res.bic)


class NestedTest(Tool):
    SCHEMA = Schema([Field('ReducedModel', FieldType.V_WSTRING, 256), Field('FullModel', FieldType.V_WSTRING, 256),
                     Field('LogLikReduced', FieldType.DOUBLE), Field('LogLikFull', FieldType.DOUBLE),
                     Field('LRStatistic', FieldType.DOUBLE), Field('DF', FieldType.INT32), Field('PValue', FieldType.DOUBLE),
                     Field('N', FieldType.INT64)])
    manifest = ToolManifest(
        tool_id='core.nested_test', version='1.0.0', display_name='Nested Test', catalog_ids=('predictive:nested-test',),
        inputs=(Anchor('models', 'Models', multiple=True, description='Exactly two models: reduced and full'),
                Anchor('input', 'Data')),
        outputs=(Anchor('output', 'Test'),),
        description='Likelihood-ratio test of whether the extra predictors of a full model improve on a reduced '
                    'model whose predictors are a subset, for the same target and family. Both models are refitted '
                    'on the given records, and the test reports the LR statistic, degrees of freedom and '
                    'chi-square p-value.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    def infer(self, ctx, cfg, inputs):
        for s in inputs['models']:
            expect_model_schema(s)
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        from scipy import stats
        models = _all_models(inputs['models'])
        if len(models) != 2:
            raise DataError(f'Nested Test needs exactly two models; got {len(models)}')
        (n1, i1, _), (n2, i2, _) = sorted(models, key=lambda m: len(m[1]['predictors']))
        if i1['family'] != i2['family'] or i1['target'] != i2['target']:
            raise DataError('Nested Test models must share a family and target')
        if not set(i1['predictors']) < set(i2['predictors']):
            raise DataError('The reduced model\'s predictors must be a strict subset of the full model\'s')
        table = inputs['input']
        fit1, p1 = modeling.refit(ctx, i1, table)
        fit2, p2 = modeling.refit(ctx, i2, table)
        ll1, n_1 = modeling.loglik(fit1, p1, table)
        ll2, n_2 = modeling.loglik(fit2, p2, table)
        if n_1 != n_2:
            raise DataError('The models use different records (missing values differ); filter the data first')
        df = fit2['n_params'] - fit1['n_params']
        lr = max(0.0, 2 * (ll2 - ll1))
        return {'output': Table.from_rows(self.SCHEMA, [(n1, n2, ll1, ll2, lr, df, float(stats.chi2.sf(lr, df)), n_1)])}


class TestOfMeans(Tool):
    SCHEMA = Schema([Field('Group1', FieldType.V_WSTRING, TEXT), Field('Group2', FieldType.V_WSTRING, TEXT),
                     Field('N1', FieldType.INT64), Field('N2', FieldType.INT64), Field('Mean1', FieldType.DOUBLE),
                     Field('Mean2', FieldType.DOUBLE), Field('Difference', FieldType.DOUBLE), Field('T', FieldType.DOUBLE),
                     Field('DF', FieldType.DOUBLE), Field('PValue', FieldType.DOUBLE), Field('CILower', FieldType.DOUBLE),
                     Field('CIUpper', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.test_of_means', version='1.0.0', display_name='Test of Means', catalog_ids=('predictive:test-of-means',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Tests'),),
        description='Two-sample t-tests of the mean of value_field between every pair of groups: Welch (unequal '
                    'variances, default) or pooled Student. Reports the difference with its confidence interval, '
                    't, degrees of freedom and two-sided p-value. Missing values are excluded.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['value_field', 'group_field'], 'properties': {
            'value_field': {'type': 'string', 'minLength': 1}, 'group_field': {'type': 'string', 'minLength': 1},
            'equal_variance': {'type': 'boolean', 'default': False},
            'confidence': {'type': 'number', 'exclusiveMinimum': 0, 'exclusiveMaximum': 1, 'default': 0.95}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['value_field'], cfg['group_field']])
        if not inputs['input'].field(cfg['value_field']).type.is_numeric:
            raise SchemaError('Test of Means value_field must be numeric')
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        from scipy import stats
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        groups = {}
        for g, v in zip(table.column(cfg['group_field']), table.column(cfg['value_field'])):
            if not (missing(g) or missing(v)):
                groups.setdefault(format_value(g), []).append(float(v))
        names = sorted(groups)
        rows = []
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                xa, xb = np.array(groups[a]), np.array(groups[b])
                if len(xa) < 2 or len(xb) < 2:
                    rows.append((a, b, len(xa), len(xb), float(xa.mean()), float(xb.mean()), float(xa.mean() - xb.mean()),
                                 None, None, None, None, None))
                    continue
                res = stats.ttest_ind(xa, xb, equal_var=cfg['equal_variance'])
                ci = res.confidence_interval(cfg['confidence'])
                rows.append((a, b, len(xa), len(xb), float(xa.mean()), float(xb.mean()), float(xa.mean() - xb.mean()),
                             float(res.statistic), float(res.df), float(res.pvalue), float(ci.low), float(ci.high)))
        return {'output': Table.from_rows(self.SCHEMA, rows)}


class VarianceInflationFactors(Tool):
    SCHEMA = Schema([Field('Term', FieldType.V_WSTRING, TEXT), Field('VIF', FieldType.DOUBLE), Field('Tolerance', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.variance_inflation_factors', version='1.0.0', display_name='Variance Inflation Factors',
        catalog_ids=('predictive:variance-inflation-factors',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'VIF'),),
        description='Variance inflation factor of each predictor term (1 / (1 - R²) of that term regressed on the '
                    'others, with an intercept) to detect multicollinearity. Values above 5 or 10 are commonly '
                    'treated as problematic. Perfectly collinear terms have infinite VIF, reported as null.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['predictors'], 'properties': {
            'predictors': {**_FIELDS, 'minItems': 2}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], cfg['predictors'])
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        table = inputs['input']
        design = Design.fit(table, cfg['predictors'], True)
        X, mask, _ = design.matrix(table)
        X = X[mask]
        if len(X) <= X.shape[1] + 1:
            raise DataError('Not enough complete records to compute VIFs')
        rows = []
        for j, term in enumerate(design.names):
            others = np.column_stack([np.ones(len(X)), np.delete(X, j, axis=1)])
            coef, *_ = np.linalg.lstsq(others, X[:, j], rcond=None)
            resid = X[:, j] - others @ coef
            ss_tot = float(((X[:, j] - X[:, j].mean()) ** 2).sum())
            r2 = 1 - float((resid ** 2).sum()) / ss_tot if ss_tot > 0 else 1.0
            vif = 1 / (1 - r2) if r2 < 1 - 1e-12 else None
            rows.append((term, vif, 1 / vif if vif else 0.0))
        return {'output': Table.from_rows(self.SCHEMA, rows)}


# =========================================================================== survival

KM_SCHEMA = Schema([Field('Group', FieldType.V_WSTRING, TEXT), Field('Time', FieldType.DOUBLE), Field('AtRisk', FieldType.INT64),
                    Field('Events', FieldType.INT64), Field('Censored', FieldType.INT64), Field('Survival', FieldType.DOUBLE),
                    Field('CILower', FieldType.DOUBLE), Field('CIUpper', FieldType.DOUBLE)])


def kaplan_meier(times, events, z=1.959963984540054):
    """Kaplan-Meier with Greenwood variance and log(-log) confidence limits."""
    order = sorted(set(times))
    rows, s, greenwood = [], 1.0, 0.0
    for t in order:
        at_risk = sum(1 for x in times if x >= t)
        d = sum(1 for x, e in zip(times, events) if x == t and e)
        c = sum(1 for x, e in zip(times, events) if x == t and not e)
        if d:
            s *= 1 - d / at_risk
            if at_risk > d:
                greenwood += d / (at_risk * (at_risk - d))
        lo = hi = None
        if 0 < s < 1 and greenwood > 0:
            se = math.sqrt(greenwood) / abs(math.log(s))
            lo, hi = s ** math.exp(z * se), s ** math.exp(-z * se)
        rows.append((t, at_risk, d, c, s, lo, hi))
    return rows


@register_fitter('core.survival_analysis')
def _cox_fitter(ctx, cfg, table):
    import numpy as np
    import statsmodels.api as sm
    time_f, event_f = cfg['time_field'], cfg['event_field']
    design = Design.fit(table, cfg['predictors'], True)
    X, xmask, _ = design.matrix(table)
    t = np.array([np.nan if missing(v) else float(v) for v in table.column(time_f)])
    e = np.array([np.nan if missing(v) else float(bool(v) if not isinstance(v, str) else v not in ('0', 'false', 'False', ''))
                  for v in table.column(event_f)])
    mask = xmask & np.isfinite(t) & np.isfinite(e) & (t >= 0)
    if mask.sum() < X.shape[1] + 2 or e[mask].sum() == 0:
        raise DataError('Cox model needs complete records with at least one event')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        res = sm.PHReg(t[mask], X[mask], status=e[mask], ties='efron').fit()
    bch = res.baseline_cumulative_hazard[0]
    rows = [(n, float(b), float(se), float(z), float(p), math.exp(b) if abs(b) < 700 else None)
            for n, b, se, z, p in zip(design.names, res.params, res.bse, res.tvalues, res.pvalues)]
    info = {'family': 'cox', 'task': 'survival', 'target': time_f, 'predictors': cfg['predictors'], 'design': design.to_json(),
            'classes': None, 'event_field': event_f, 'coefficients': rows,
            'metrics': {'N': float(mask.sum()), 'Events': float(e[mask].sum()), 'LogLikelihood': float(res.llf)},
            'spec': {'tool': 'core.survival_analysis', 'config': cfg}}
    payload = {'coefficients': [float(b) for b in res.params], 'baseline_times': [float(x) for x in bch[0]],
               'baseline_cumhaz': [float(x) for x in bch[1]]}
    return info, json.dumps(payload).encode('utf-8')


class SurvivalAnalysis(Tool):
    manifest = ToolManifest(
        tool_id='core.survival_analysis', version='1.0.0', display_name='Survival Analysis',
        catalog_ids=('predictive:survival-analysis',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('survival', 'Kaplan-Meier', description='Survival curve per group'), CHART, MODEL,
                 Anchor('coefficients', 'Coefficients', description='Cox model hazard ratios')),
        description='Kaplan-Meier survival curves (Greenwood log(-log) confidence limits), optionally per group, '
                    'from a time field and an event field (1 or true = event, 0 or false = censored). With '
                    'predictors, also a Cox proportional-hazards model (Efron ties) with hazard ratios, for Survival '
                    'Score.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['time_field', 'event_field'], 'properties': {
            'time_field': {'type': 'string', 'minLength': 1}, 'event_field': {'type': 'string', 'minLength': 1},
            'group_field': {'type': ['string', 'null'], 'default': None},
            'predictors': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}, 'default': []},
            'model_name': {'type': ['string', 'null'], 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['time_field'], cfg['event_field'], *cfg['predictors']] +
                       ([cfg['group_field']] if cfg['group_field'] else []))
        return {'survival': KM_SCHEMA, 'chart': charts.chart_schema(), 'model': MODEL_SCHEMA, 'coefficients': COEF_SCHEMA}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        groups = {}
        for rec in table.to_dicts():
            t, e = rec[cfg['time_field']], rec[cfg['event_field']]
            if missing(t) or missing(e):
                continue
            g = format_value(rec[cfg['group_field']]) if cfg['group_field'] and not missing(rec[cfg['group_field']]) else 'All'
            event = bool(e) if not isinstance(e, str) else e not in ('0', 'false', 'False', '')
            groups.setdefault(g, ([], []))
            groups[g][0].append(float(t))
            groups[g][1].append(event)
        rows, series = [], {}
        for g in sorted(groups):
            km = kaplan_meier(*groups[g])
            rows += [(g, *r) for r in km]
            pts, last = [(0.0, 1.0)], 1.0
            for t, _, _, _, s, _, _ in km:
                pts += [(t, last), (t, s)]  # step function
                last = s
            series[g] = pts
        canvas = charts.line_chart(series, title='Kaplan-Meier survival', x_label=cfg['time_field'], y_label='Survival probability')
        out = {'survival': Table.from_rows(KM_SCHEMA, rows), 'chart': charts.chart_table(canvas, 'Kaplan-Meier survival'),
               'model': Table.empty(MODEL_SCHEMA), 'coefficients': Table.empty(COEF_SCHEMA)}
        if cfg['predictors']:
            info, payload = _cox_fitter(ctx, cfg, table)
            out['model'] = model_table(_name(cfg, 'Cox model'), 'cox', cfg['time_field'], info, payload)
            out['coefficients'] = Table.from_rows(COEF_SCHEMA, info['coefficients'])
        return out


class SurvivalScore(Tool):
    manifest = ToolManifest(
        tool_id='core.survival_score', version='1.0.0', display_name='Survival Score', catalog_ids=('predictive:survival-score',),
        inputs=(Anchor('model', 'Cox model'), Anchor('input', 'Data')), outputs=(Anchor('output', 'Output'),),
        description='Score records with a Cox model from Survival Analysis: the linear predictor, the relative risk '
                    'exp(xβ), and the survival probability at each time in times (S(t) = exp(-H₀(t)·exp(xβ)) using '
                    'the Breslow baseline cumulative hazard).',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'times': {'type': 'array', 'items': {'type': 'number', 'minimum': 0}, 'default': []}}})

    def infer(self, ctx, cfg, inputs):
        expect_model_schema(inputs['model'], 'Cox model')
        fields = [Field('LinearPredictor', FieldType.DOUBLE), Field('RelativeRisk', FieldType.DOUBLE)] + \
                 [Field(f'Survival_{format_value(t)}', FieldType.DOUBLE) for t in cfg['times']]
        return {'output': Schema([*inputs['input'], *fields])}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        name, info, payload = _one_model(inputs['model'], 'Cox model')
        if info['family'] != 'cox':
            raise DataError('Survival Score needs a Cox model from Survival Analysis')
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'model': inputs['model'].schema, 'input': table.schema})['output']
        p = json.loads(payload)
        res = predict(info, payload, table)
        times, cumhaz = np.array(p['baseline_times']), np.array(p['baseline_cumhaz'])

        def H0(t):
            i = np.searchsorted(times, t, side='right') - 1
            return float(cumhaz[i]) if i >= 0 else 0.0
        out = []
        for row, lp, m in zip(table.rows(), res['value'], res['mask']):
            if not m:
                out.append((*row, None, None, *([None] * len(cfg['times']))))
                continue
            rr = math.exp(lp)
            out.append((*row, float(lp), rr, *[math.exp(-H0(t) * rr) for t in cfg['times']]))
        return {'output': Table.from_rows(schema, out)}


# =========================================================================== network analysis

class NetworkAnalysis(Tool):
    NODES = Schema([Field('Node', FieldType.V_WSTRING, TEXT), Field('Degree', FieldType.INT64), Field('InDegree', FieldType.INT64),
                    Field('OutDegree', FieldType.INT64), Field('WeightedDegree', FieldType.DOUBLE), Field('Betweenness', FieldType.DOUBLE),
                    Field('Closeness', FieldType.DOUBLE), Field('PageRank', FieldType.DOUBLE), Field('Community', FieldType.INT32)])
    SUMMARY = Schema([Field('Metric', FieldType.V_WSTRING, 64), Field('Value', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.network_analysis', version='1.0.0', display_name='Network Analysis', catalog_ids=('predictive:network-analysis',),
        inputs=(Anchor('input', 'Edges', description='One record per edge'),),
        outputs=(Anchor('nodes', 'Nodes'), Anchor('summary', 'Summary'), CHART),
        description='Build a directed or undirected graph from edge records (optional weights) and compute per-node '
                    'degree, weighted degree, betweenness and closeness centrality, PageRank, and communities '
                    '(greedy modularity), plus graph-level statistics and a network diagram (seeded spring '
                    'layout). Uses networkx.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['from_field', 'to_field'], 'properties': {
            'from_field': {'type': 'string', 'minLength': 1}, 'to_field': {'type': 'string', 'minLength': 1},
            'weight_field': {'type': ['string', 'null'], 'default': None}, 'directed': {'type': 'boolean', 'default': False},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 1},
            'max_nodes_drawn': {'type': 'integer', 'minimum': 2, 'maximum': 5000, 'default': 300}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['from_field'], cfg['to_field']] + ([cfg['weight_field']] if cfg['weight_field'] else []))
        return {'nodes': self.NODES, 'summary': self.SUMMARY, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        import networkx as nx
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        g = nx.DiGraph() if cfg['directed'] else nx.Graph()
        for rec in table.to_dicts():
            a, b = rec[cfg['from_field']], rec[cfg['to_field']]
            if missing(a) or missing(b):
                continue
            w = float(rec[cfg['weight_field']]) if cfg['weight_field'] and not missing(rec[cfg['weight_field']]) else 1.0
            a, b = format_value(a), format_value(b)
            if g.has_edge(a, b):
                g[a][b]['weight'] += w
            else:
                g.add_edge(a, b, weight=w)
        if g.number_of_nodes() == 0:
            raise DataError('Network Analysis received no edges')
        between = nx.betweenness_centrality(g)
        close = nx.closeness_centrality(g)
        rank = nx.pagerank(g, weight='weight')
        undirected = g.to_undirected() if cfg['directed'] else g
        comms = sorted((sorted(c) for c in nx.algorithms.community.greedy_modularity_communities(undirected, weight='weight')),
                       key=lambda c: (-len(c), c[0]))
        member = {n: i + 1 for i, c in enumerate(comms) for n in c}
        rows = []
        for n in sorted(g.nodes):
            indeg = g.in_degree(n) if cfg['directed'] else g.degree(n)
            outdeg = g.out_degree(n) if cfg['directed'] else g.degree(n)
            rows.append((n, int(g.degree(n)), int(indeg), int(outdeg), float(g.degree(n, weight='weight')), float(between[n]),
                         float(close[n]), float(rank[n]), member[n]))
        summary = {'Nodes': g.number_of_nodes(), 'Edges': g.number_of_edges(), 'Density': nx.density(g),
                   'Components': nx.number_weakly_connected_components(g) if cfg['directed'] else nx.number_connected_components(g),
                   'Communities': len(comms), 'Modularity': nx.algorithms.community.modularity(undirected, comms, weight='weight')}
        return {'nodes': Table.from_rows(self.NODES, rows),
                'summary': Table.from_rows(self.SUMMARY, [(k, float(v)) for k, v in summary.items()]),
                'chart': charts.chart_table(self._draw(nx, g, member, rank, cfg), 'Network')}

    @staticmethod
    def _draw(nx, g, member, rank, cfg):
        nodes = sorted(g.nodes, key=lambda n: -rank[n])[:cfg['max_nodes_drawn']]
        sub = g.subgraph(nodes)
        pos = nx.spring_layout(sub, seed=cfg['seed'], weight='weight')
        c = charts.Canvas(720, 560, 'Network')
        c.text(360, 24, 'Network' + (f' (top {len(nodes)} nodes by PageRank)' if len(nodes) < g.number_of_nodes() else ''),
               size=15, anchor='middle', weight='bold')
        xs, ys = [p[0] for p in pos.values()], [p[1] for p in pos.values()]
        sx = charts.Linear(min(xs), max(xs), 40, 680, pad=0.05)
        sy = charts.Linear(min(ys), max(ys), 530, 50, pad=0.05)
        for a, b in sub.edges:
            c.line(sx(pos[a][0]), sy(pos[a][1]), sx(pos[b][0]), sy(pos[b][1]), stroke='#b8c2cc', width=1)
        peak = max(rank[n] for n in nodes)
        for n in nodes:
            r = 4 + 10 * math.sqrt(rank[n] / peak)
            c.circle(sx(pos[n][0]), sy(pos[n][1]), r, fill=charts.series_color(member[n] - 1), stroke='#ffffff',
                     title=f'{n} · community {member[n]} · PageRank {rank[n]:.3g}')
            if len(nodes) <= 40:
                c.text(sx(pos[n][0]), sy(pos[n][1]) - r - 3, n[:20], size=9, anchor='middle')
        return c


TOOLS = [LinearRegression, LogisticRegression, CountRegression, GammaRegression, DecisionTree, ForestModel, BoostedModel,
         SupportVectorMachine, NeuralNetwork, SplineModel, NaiveBayes, Score, ModelComparison, CrossValidation, LiftChart,
         ModelCoefficients, Stepwise, NestedTest, TestOfMeans, VarianceInflationFactors, SurvivalAnalysis, SurvivalScore,
         NetworkAnalysis]
