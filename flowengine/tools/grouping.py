"""Predictive Grouping: clustering, dimension reduction, nearest neighbours and market-basket analysis.

Independent implementations of standard methods. K-Centroids models are stored as plain JSON
(centroids plus the standardisation used), so they are safe to share. Records with a missing value in
a used field are excluded and counted.
"""
from __future__ import annotations

import itertools
import json
import math
from collections import Counter

from .. import charts
from ..errors import ConfigError, DataError, SchemaError
from ..modeling import MODEL_SCHEMA, expect_model_schema, missing, model_table, read_models
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
_NUM_FIELDS = {'type': 'array', 'uniqueItems': True, 'minItems': 1, 'items': {'type': 'string', 'minLength': 1}}
CHART = Anchor('chart', 'Chart', description='Rendered SVG chart')


def _numeric_matrix(table, fields, standardize, stats=None):
    """(X, mask, stats): numeric matrix over complete rows, optionally z-scored with given or fitted stats."""
    import numpy as np
    require_fields(table.schema, fields)
    for f in fields:
        if not table.schema.field(f).type.is_numeric:
            raise SchemaError(f'Field {f!r} must be numeric')
    cols = [np.array([np.nan if missing(v) else float(v) for v in table.column(f)]) for f in fields]
    X = np.column_stack(cols) if cols else np.zeros((len(table), 0))
    mask = np.all(np.isfinite(X), axis=1)
    if stats is None:
        if standardize:
            mean = X[mask].mean(axis=0)
            sd = X[mask].std(axis=0, ddof=1)
            sd[~np.isfinite(sd) | (sd == 0)] = 1.0
        else:
            mean, sd = np.zeros(X.shape[1]), np.ones(X.shape[1])
        stats = {'mean': mean.tolist(), 'sd': sd.tolist()}
    X = (X - np.array(stats['mean'])) / np.array(stats['sd'])
    return X, mask, stats


def _kmedians(X, k, seed, iterations=100):
    import numpy as np
    rng = np.random.default_rng(seed)
    centres = X[rng.choice(len(X), size=k, replace=False)]
    for _ in range(iterations):
        labels = np.abs(X[:, None, :] - centres[None]).sum(axis=2).argmin(axis=1)
        new = np.array([np.median(X[labels == j], axis=0) if (labels == j).any() else centres[j] for j in range(k)])
        if np.allclose(new, centres):
            break
        centres = new
    return centres, labels


def _fit_kcentroids(X, k, method, seed):
    import numpy as np
    from sklearn.cluster import KMeans
    if len(X) < k:
        raise DataError(f'K-Centroids needs at least {k} complete records')
    if method == 'kmeans':
        km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(X)
        return km.cluster_centers_, km.labels_
    best = None
    for start in range(10):  # repeated starts, keep the lowest total L1 distance
        centres, labels = _kmedians(X, k, seed + start)
        cost = np.abs(X - centres[labels]).sum()
        if best is None or cost < best[0]:
            best = (cost, centres, labels)
    return best[1], best[2]


def _assign(X, centres, method):
    import numpy as np
    if method == 'kmedians':
        d = np.abs(X[:, None, :] - centres[None]).sum(axis=2)
    else:
        d = np.sqrt(((X[:, None, :] - centres[None]) ** 2).sum(axis=2))
    return d.argmin(axis=1), d.min(axis=1)


class KCentroidsClusterAnalysis(Tool):
    SUMMARY = Schema([Field('Cluster', FieldType.INT32), Field('Size', FieldType.INT64), Field('WithinDistance', FieldType.DOUBLE),
                      Field('Field', FieldType.V_WSTRING, TEXT), Field('Centroid', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.k_centroids_cluster_analysis', version='1.0.0', display_name='K-Centroids Cluster Analysis',
        catalog_ids=('predictive-grouping:k-centroids-cluster-analysis',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('model', 'Model', description='Cluster model for Append Cluster'), Anchor('summary', 'Summary')),
        description='Partition records into k clusters on numeric fields with k-means (Euclidean, 10 starts) or '
                    'k-medians (L1, median centres), optionally on z-scored fields. The summary gives each '
                    'cluster\'s size, mean distance to its centre, and centroid in original units.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields', 'k'], 'properties': {
            'fields': _NUM_FIELDS, 'k': {'type': 'integer', 'minimum': 2, 'maximum': 500},
            'method': {'enum': ['kmeans', 'kmedians'], 'default': 'kmeans'},
            'standardize': {'type': 'boolean', 'default': True}, 'seed': {'type': 'integer', 'minimum': 0, 'default': 1},
            'model_name': {'type': ['string', 'null'], 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], cfg['fields'])
        return {'model': MODEL_SCHEMA, 'summary': self.SUMMARY}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        X, mask, stats = _numeric_matrix(inputs['input'], cfg['fields'], cfg['standardize'])
        Xm = X[mask]
        centres, labels = _fit_kcentroids(Xm, cfg['k'], cfg['method'], cfg['seed'])
        _, dist = _assign(Xm, centres, cfg['method'])
        original = centres * np.array(stats['sd']) + np.array(stats['mean'])
        rows = []
        for j in range(cfg['k']):
            members = labels == j
            for f, value in zip(cfg['fields'], original[j]):
                rows.append((j + 1, int(members.sum()), float(dist[members].mean()) if members.any() else None, f, float(value)))
        info = {'family': 'kcentroids', 'task': 'clustering', 'target': '', 'predictors': cfg['fields'], 'design': [],
                'classes': None, 'method': cfg['method'], 'stats': stats, 'centres': centres.tolist(),
                'metrics': {'N': int(mask.sum()), 'Excluded': int((~mask).sum())}}
        return {'model': model_table(cfg['model_name'] or 'K-centroids', 'kcentroids', '', info, b'{}'),
                'summary': Table.from_rows(self.SUMMARY, rows)}


class AppendCluster(Tool):
    manifest = ToolManifest(
        tool_id='core.append_cluster', version='1.0.0', display_name='Append Cluster', catalog_ids=('predictive-grouping:append-cluster',),
        inputs=(Anchor('model', 'Model'), Anchor('input', 'Data')), outputs=(Anchor('output', 'Output'),),
        description='Assign each record to the nearest centre of a K-Centroids model, using the model\'s '
                    'standardisation and distance. Adds Cluster (1-based) and Distance. Records with missing '
                    'values get nulls.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    def infer(self, ctx, cfg, inputs):
        expect_model_schema(inputs['model'])
        clash = [n for n in ('Cluster', 'Distance') if n in inputs['input']]
        if clash:
            raise SchemaError(f'Field(s) {clash} already exist')
        return {'output': Schema([*inputs['input'], Field('Cluster', FieldType.INT32), Field('Distance', FieldType.DOUBLE)])}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        models = read_models(inputs['model'])
        name, info, _ = models[0]
        if info.get('family') != 'kcentroids':
            raise DataError('Append Cluster needs a K-Centroids Cluster Analysis model')
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'model': inputs['model'].schema, 'input': table.schema})['output']
        X, mask, _ = _numeric_matrix(table, info['predictors'], False, info['stats'])
        labels, dist = _assign(np.nan_to_num(X), np.array(info['centres']), info['method'])
        out = [(*row, int(l) + 1 if m else None, float(d) if m else None)
               for row, l, d, m in zip(table.rows(), labels, dist, mask)]
        return {'output': Table.from_rows(schema, out)}


class KCentroidsDiagnostics(Tool):
    SCHEMA = Schema([Field('K', FieldType.INT32), Field('WithinSS', FieldType.DOUBLE), Field('Silhouette', FieldType.DOUBLE),
                     Field('CalinskiHarabasz', FieldType.DOUBLE), Field('DaviesBouldin', FieldType.DOUBLE),
                     Field('MeanAdjustedRand', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.k_centroids_diagnostics', version='1.0.0', display_name='K-Centroids Diagnostics',
        catalog_ids=('predictive-grouping:k-centroids-diagnostics',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Diagnostics'), CHART),
        description='Help choose k: for each k in a range, fit k-means and report the within-cluster sum of squares '
                    '(elbow), silhouette, Calinski-Harabasz and Davies-Bouldin indices, and the stability of the '
                    'solution (mean adjusted Rand index between bootstrap refits). The chart shows the elbow and '
                    'silhouette curves.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields'], 'properties': {
            'fields': _NUM_FIELDS, 'k_min': {'type': 'integer', 'minimum': 2, 'default': 2},
            'k_max': {'type': 'integer', 'minimum': 2, 'maximum': 50, 'default': 8},
            'standardize': {'type': 'boolean', 'default': True},
            'bootstrap': {'type': 'integer', 'minimum': 0, 'maximum': 100, 'default': 10},
            'seed': {'type': 'integer', 'minimum': 0, 'default': 1}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], cfg['fields'])
        if cfg['k_min'] > cfg['k_max']:
            raise ConfigError('k_min must not exceed k_max')
        return {'output': self.SCHEMA, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.metrics import (adjusted_rand_score, calinski_harabasz_score, davies_bouldin_score,
                                     silhouette_score)
        X, mask, _ = _numeric_matrix(inputs['input'], cfg['fields'], cfg['standardize'])
        X = X[mask]
        rng = np.random.default_rng(cfg['seed'])
        rows = []
        for k in range(cfg['k_min'], cfg['k_max'] + 1):
            ctx.check_cancelled()
            if len(X) <= k:
                break
            km = KMeans(n_clusters=k, n_init=10, random_state=cfg['seed']).fit(X)
            sil = float(silhouette_score(X, km.labels_, sample_size=min(len(X), 5000), random_state=cfg['seed']))
            aris = []
            for b in range(cfg['bootstrap']):
                idx = rng.choice(len(X), size=len(X), replace=True)
                kb = KMeans(n_clusters=k, n_init=3, random_state=cfg['seed'] + b + 1).fit(X[idx])
                aris.append(adjusted_rand_score(km.labels_, kb.predict(X)))
            rows.append((k, float(km.inertia_), sil, float(calinski_harabasz_score(X, km.labels_)),
                         float(davies_bouldin_score(X, km.labels_)), float(np.mean(aris)) if aris else None))
        if not rows:
            raise DataError('Not enough complete records for the requested k range')
        canvas = charts.line_chart({'Silhouette': [(r[0], r[2]) for r in rows]}, title='Silhouette by k', x_label='k',
                                   y_label='Silhouette (higher is better)')
        return {'output': Table.from_rows(self.SCHEMA, rows), 'chart': charts.chart_table(canvas, 'Silhouette by k')}


class PrincipalComponents(Tool):
    VARIANCE = Schema([Field('Component', FieldType.V_WSTRING, 16), Field('Eigenvalue', FieldType.DOUBLE),
                       Field('VarianceExplained', FieldType.DOUBLE), Field('CumulativeVariance', FieldType.DOUBLE)])
    LOADINGS = Schema([Field('Field', FieldType.V_WSTRING, TEXT), Field('Component', FieldType.V_WSTRING, 16), Field('Loading', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.principal_components', version='1.0.0', display_name='Principal Components',
        catalog_ids=('predictive-grouping:principal-components',),
        inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('output', 'Scores', description='Input records with component scores'),
                 Anchor('variance', 'Variance'), Anchor('loadings', 'Loadings')),
        description='Principal component analysis of numeric fields on the correlation matrix (standardised, '
                    'default) or the covariance matrix. Adds PC1..PCn scores to each record and reports '
                    'eigenvalues, variance explained and loadings. The sign of each component is fixed so its '
                    'largest loading is positive, for reproducibility.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields'], 'properties': {
            'fields': {**_NUM_FIELDS, 'minItems': 2}, 'components': {'type': ['integer', 'null'], 'minimum': 1, 'default': None},
            'standardize': {'type': 'boolean', 'default': True}}})

    def _n(self, cfg):
        return min(cfg['components'] or len(cfg['fields']), len(cfg['fields']))

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], cfg['fields'])
        scores = [Field(f'PC{i + 1}', FieldType.DOUBLE) for i in range(self._n(cfg))]
        return {'output': Schema([*inputs['input'], *scores]), 'variance': self.VARIANCE, 'loadings': self.LOADINGS}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        X, mask, _ = _numeric_matrix(table, cfg['fields'], cfg['standardize'])
        Xm = X[mask] - X[mask].mean(axis=0)
        if len(Xm) < 2:
            raise DataError('Principal Components needs at least two complete records')
        cov = np.cov(Xm, rowvar=False)
        values, vectors = np.linalg.eigh(cov)
        order = np.argsort(values)[::-1]
        values, vectors = values[order], vectors[:, order]
        for j in range(vectors.shape[1]):
            if vectors[np.argmax(np.abs(vectors[:, j])), j] < 0:
                vectors[:, j] *= -1
        n = self._n(cfg)
        scores = (X - X[mask].mean(axis=0)) @ vectors[:, :n]
        total = values.sum()
        var = [(f'PC{i + 1}', float(values[i]), float(values[i] / total), float(values[:i + 1].sum() / total)) for i in range(len(values))]
        load = [(f, f'PC{j + 1}', float(vectors[i, j])) for j in range(n) for i, f in enumerate(cfg['fields'])]
        out = [(*row, *([float(v) for v in s] if m else [None] * n)) for row, s, m in zip(table.rows(), scores, mask)]
        return {'output': Table.from_rows(schema, out), 'variance': Table.from_rows(self.VARIANCE, var),
                'loadings': Table.from_rows(self.LOADINGS, load)}


class MultidimensionalScaling(Tool):
    manifest = ToolManifest(
        tool_id='core.multidimensional_scaling', version='1.0.0', display_name='Multidimensional Scaling',
        catalog_ids=('predictive-grouping:multidimensional-scaling',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Coordinates'), Anchor('fit', 'Fit')),
        description='Classical (Torgerson) multidimensional scaling: place records in a low-dimensional space so '
                    'Euclidean distances approximate their distances on the chosen fields (Euclidean or Manhattan, '
                    'optionally standardised). Adds Dim1..Dimn and reports the eigenvalue goodness of fit and '
                    'Kruskal stress-1. Limited to 3,000 records (it uses an n×n distance matrix).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields'], 'properties': {
            'fields': {**_NUM_FIELDS, 'minItems': 2}, 'dimensions': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 2},
            'metric': {'enum': ['euclidean', 'manhattan'], 'default': 'euclidean'},
            'standardize': {'type': 'boolean', 'default': True}}})
    FIT = Schema([Field('Metric', FieldType.V_WSTRING, 32), Field('Value', FieldType.DOUBLE)])

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], cfg['fields'])
        return {'output': Schema([*inputs['input'], *[Field(f'Dim{i + 1}', FieldType.DOUBLE) for i in range(cfg['dimensions'])]]),
                'fit': self.FIT}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        from scipy.spatial.distance import pdist, squareform
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        X, mask, _ = _numeric_matrix(table, cfg['fields'], cfg['standardize'])
        Xm = X[mask]
        if len(Xm) > 3000:
            raise DataError(f'Multidimensional Scaling is limited to 3,000 records; got {len(Xm)}')
        D = squareform(pdist(Xm, 'cityblock' if cfg['metric'] == 'manhattan' else 'euclidean'))
        n = len(D)
        J = np.eye(n) - np.ones((n, n)) / n
        B = -0.5 * J @ (D ** 2) @ J
        values, vectors = np.linalg.eigh(B)
        order = np.argsort(values)[::-1]
        values, vectors = values[order], vectors[:, order]
        k = cfg['dimensions']
        pos = np.clip(values[:k], 0, None)
        coords = vectors[:, :k] * np.sqrt(pos)
        for j in range(k):
            if coords[np.argmax(np.abs(coords[:, j])), j] < 0:
                coords[:, j] *= -1
        fitted = squareform(pdist(coords))
        iu = np.triu_indices(n, 1)
        stress = math.sqrt(((D[iu] - fitted[iu]) ** 2).sum() / (D[iu] ** 2).sum()) if (D[iu] ** 2).sum() else 0.0
        gof = float(pos.sum() / np.abs(values).sum()) if np.abs(values).sum() else 1.0
        it = iter(coords)
        out = [(*row, *([float(v) for v in next(it)] if m else [None] * k)) for row, m in zip(table.rows(), mask)]
        return {'output': Table.from_rows(schema, out),
                'fit': Table.from_rows(self.FIT, [('GoodnessOfFit', gof), ('Stress', float(stress)), ('N', float(n))])}


class FindNearestNeighbors(Tool):
    SCHEMA_EXTRA = [Field('NeighborRank', FieldType.INT32), Field('Distance', FieldType.DOUBLE)]
    manifest = ToolManifest(
        tool_id='core.find_nearest_neighbors', version='1.0.0', display_name='Find Nearest Neighbors',
        catalog_ids=('predictive-grouping:find-nearest-neighbors',),
        inputs=(Anchor('input', 'Data', description='Records to search'),
                Anchor('query', 'Query', required=False, description='Optional records to find neighbours for (default: Data itself)')),
        outputs=(Anchor('output', 'Neighbours'),),
        description='For each query record (or each data record), the k nearest data records by Euclidean or '
                    'Manhattan distance on numeric fields (z-scored by the data by default), using a KD-tree. '
                    'Output pairs the query id with the neighbour id, with NeighborRank and Distance. When '
                    'searching the data itself, a record is not its own neighbour.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields', 'id_field'], 'properties': {
            'fields': _NUM_FIELDS, 'id_field': {'type': 'string', 'minLength': 1},
            'k': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 5},
            'metric': {'enum': ['euclidean', 'manhattan'], 'default': 'euclidean'},
            'standardize': {'type': 'boolean', 'default': True}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [*cfg['fields'], cfg['id_field']])
        id_field = inputs['input'].field(cfg['id_field'])
        if 'query' in inputs:
            require_fields(inputs['query'], [*cfg['fields'], cfg['id_field']])
        return {'output': Schema([id_field.renamed('QueryID'), id_field.renamed('NeighborID'), *self.SCHEMA_EXTRA])}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        from scipy.spatial import cKDTree
        data = inputs['input']
        schema = self.infer(ctx, cfg, {k: v.schema for k, v in inputs.items()})['output']
        X, mask, stats = _numeric_matrix(data, cfg['fields'], cfg['standardize'])
        ids = np.array(data.column(cfg['id_field']), dtype=object)[mask]
        tree = cKDTree(X[mask])
        self_search = 'query' not in inputs
        query = data if self_search else inputs['query']
        Q, qmask, _ = _numeric_matrix(query, cfg['fields'], False, stats)
        qids = query.column(cfg['id_field'])
        k = min(cfg['k'] + (1 if self_search else 0), len(ids))
        p = 1 if cfg['metric'] == 'manhattan' else 2
        rows = []
        positions = np.flatnonzero(mask)
        for qi in np.flatnonzero(qmask):
            dist, idx = tree.query(Q[qi], k=k, p=p)
            rank = 0
            for d, j in zip(np.atleast_1d(dist), np.atleast_1d(idx)):
                if self_search and positions[j] == qi:
                    continue
                rank += 1
                if rank > cfg['k']:
                    break
                rows.append((qids[qi], ids[j], rank, float(d)))
        return {'output': Table.from_rows(schema, rows)}


# =========================================================================== market basket

def _baskets(table, cfg):
    require_fields(table.schema, [cfg['transaction_field'], cfg['item_field']])
    baskets = {}
    for t, item in zip(table.column(cfg['transaction_field']), table.column(cfg['item_field'])):
        if not (missing(t) or missing(item)):
            baskets.setdefault(t, set()).add(format_value(item))
    if not baskets:
        raise DataError('No transactions with items')
    return list(baskets.values())


_BASKET = {'transaction_field': {'type': 'string', 'minLength': 1}, 'item_field': {'type': 'string', 'minLength': 1}}
RULES_SCHEMA = Schema([Field('Antecedent', FieldType.V_WSTRING, TEXT), Field('Consequent', FieldType.V_WSTRING, TEXT),
                       Field('AntecedentSize', FieldType.INT32), Field('Support', FieldType.DOUBLE), Field('Confidence', FieldType.DOUBLE),
                       Field('Lift', FieldType.DOUBLE), Field('Leverage', FieldType.DOUBLE), Field('Conviction', FieldType.DOUBLE),
                       Field('Count', FieldType.INT64)])


class MBRules(Tool):
    manifest = ToolManifest(
        tool_id='core.mb_rules', version='1.0.0', display_name='MB Rules', catalog_ids=('predictive-grouping:mb-rules',),
        inputs=(Anchor('input', 'Transactions', description='One record per transaction item'),), outputs=(Anchor('output', 'Rules'),),
        description='Association rules {A} → {B} from transaction/item records with the Apriori algorithm: frequent '
                    'itemsets above min_support, then rules above min_confidence with support, confidence, lift, '
                    'leverage and conviction. Item lists are " + "-separated and sorted. Rules are ordered by lift, '
                    'then confidence.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['transaction_field', 'item_field'], 'properties': {
            **_BASKET, 'min_support': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1, 'default': 0.01},
            'min_confidence': {'type': 'number', 'minimum': 0, 'maximum': 1, 'default': 0.2},
            'max_items': {'type': 'integer', 'minimum': 2, 'maximum': 6, 'default': 4},
            'max_rules': {'type': 'integer', 'minimum': 1, 'maximum': 1000000, 'default': 100000}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['transaction_field'], cfg['item_field']])
        return {'output': RULES_SCHEMA}

    def run(self, ctx, cfg, inputs):
        baskets = _baskets(inputs['input'], cfg)
        n = len(baskets)
        minimum = max(1, math.ceil(cfg['min_support'] * n))
        counts = Counter(item for b in baskets for item in b)
        frequent = {frozenset([i]): c for i, c in counts.items() if c >= minimum}
        level, all_frequent = frequent, dict(frequent)
        for size in range(2, cfg['max_items'] + 1):
            ctx.check_cancelled()
            items = sorted({i for s in level for i in s})
            candidates = {frozenset(c) for s in level for c in (s | {i} for i in items if i not in s) if len(c) == size
                          and all(frozenset(sub) in level for sub in itertools.combinations(c, size - 1))}
            if not candidates:
                break
            level = {}
            for c in candidates:
                count = sum(1 for b in baskets if c <= b)
                if count >= minimum:
                    level[c] = count
            all_frequent.update(level)
        rules = []
        for itemset, count in all_frequent.items():
            if len(itemset) < 2:
                continue
            for r in range(1, len(itemset)):
                for antecedent in map(frozenset, itertools.combinations(sorted(itemset), r)):
                    consequent = itemset - antecedent
                    confidence = count / all_frequent[antecedent]
                    if confidence < cfg['min_confidence']:
                        continue
                    support, p_cons = count / n, all_frequent[consequent] / n
                    lift = confidence / p_cons
                    conviction = (1 - p_cons) / (1 - confidence) if confidence < 1 else None
                    rules.append((' + '.join(sorted(antecedent)), ' + '.join(sorted(consequent)), len(antecedent), support,
                                  confidence, lift, support - (all_frequent[antecedent] / n) * p_cons, conviction, count))
        rules.sort(key=lambda r: (-r[5], -r[4], r[0], r[1]))
        if len(rules) > cfg['max_rules']:
            ctx.warn(f"MB Rules kept the top {cfg['max_rules']} of {len(rules)} rules by lift")
            rules = rules[:cfg['max_rules']]
        return {'output': Table.from_rows(RULES_SCHEMA, rules)}


class MBInspect(Tool):
    manifest = ToolManifest(
        tool_id='core.mb_inspect', version='1.0.0', display_name='MB Inspect', catalog_ids=('predictive-grouping:mb-inspect',),
        inputs=(Anchor('input', 'Rules', description='Output of MB Rules'),), outputs=(Anchor('output', 'Rules'), CHART),
        description='Filter and rank association rules from MB Rules by minimum support, confidence and lift, and by '
                    'items that must appear in the antecedent or consequent. Keeps the top n by a chosen measure and '
                    'charts support against confidence, coloured by lift.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'min_support': {'type': 'number', 'minimum': 0, 'default': 0}, 'min_confidence': {'type': 'number', 'minimum': 0, 'default': 0},
            'min_lift': {'type': 'number', 'minimum': 0, 'default': 0},
            'antecedent_contains': {'type': ['string', 'null'], 'default': None},
            'consequent_contains': {'type': ['string', 'null'], 'default': None},
            'sort_by': {'enum': ['Lift', 'Confidence', 'Support'], 'default': 'Lift'},
            'top_n': {'type': ['integer', 'null'], 'minimum': 1, 'default': None}}})

    def infer(self, ctx, cfg, inputs):
        if tuple(inputs['input'].names) != tuple(RULES_SCHEMA.names):
            raise SchemaError('MB Inspect needs the output of MB Rules')
        return {'output': RULES_SCHEMA, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        rows = []
        for r in inputs['input'].to_dicts():
            items_a, items_c = r['Antecedent'].split(' + '), r['Consequent'].split(' + ')
            if r['Support'] < cfg['min_support'] or r['Confidence'] < cfg['min_confidence'] or r['Lift'] < cfg['min_lift']:
                continue
            if cfg['antecedent_contains'] and cfg['antecedent_contains'] not in items_a:
                continue
            if cfg['consequent_contains'] and cfg['consequent_contains'] not in items_c:
                continue
            rows.append(r)
        rows.sort(key=lambda r: -r[cfg['sort_by']])
        if cfg['top_n']:
            rows = rows[:cfg['top_n']]
        groups = {'Rules': [(r['Support'], r['Confidence']) for r in rows]}
        canvas = charts.scatter_chart(groups, title='Rules: support vs confidence', x_label='Support', y_label='Confidence')
        return {'output': Table.from_rows(RULES_SCHEMA, [tuple(r[f] for f in RULES_SCHEMA.names) for r in rows]),
                'chart': charts.chart_table(canvas, 'Rules: support vs confidence')}


class MBAffinity(Tool):
    SCHEMA = Schema([Field('Item1', FieldType.V_WSTRING, TEXT), Field('Item2', FieldType.V_WSTRING, TEXT),
                     Field('CoOccurrence', FieldType.INT64), Field('Support', FieldType.DOUBLE), Field('Jaccard', FieldType.DOUBLE),
                     Field('Lift', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.mb_affinity', version='1.0.0', display_name='MB Affinity', catalog_ids=('predictive-grouping:mb-affinity',),
        inputs=(Anchor('input', 'Transactions'),), outputs=(Anchor('output', 'Affinity'), CHART),
        description='Item-to-item affinity from transactions: for every pair of the most frequent items, the number '
                    'of transactions containing both, their support, Jaccard similarity and lift. The heat map '
                    'shows lift.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['transaction_field', 'item_field'], 'properties': {
            **_BASKET, 'max_items': {'type': 'integer', 'minimum': 2, 'maximum': 200, 'default': 30}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['transaction_field'], cfg['item_field']])
        return {'output': self.SCHEMA, 'chart': charts.chart_schema()}

    def run(self, ctx, cfg, inputs):
        baskets = _baskets(inputs['input'], cfg)
        n = len(baskets)
        counts = Counter(i for b in baskets for i in b)
        items = sorted(counts, key=lambda i: (-counts[i], i))[:cfg['max_items']]
        rows, lift = [], {}
        for a, b in itertools.combinations(items, 2):
            both = sum(1 for basket in baskets if a in basket and b in basket)
            union = counts[a] + counts[b] - both
            value = (both / n) / ((counts[a] / n) * (counts[b] / n))
            rows.append((a, b, both, both / n, both / union if union else 0.0, value))
            lift[(a, b)] = lift[(b, a)] = value
        matrix = [[None if a == b else lift.get((a, b)) for b in items] for a in items]
        canvas = charts.heatmap_chart(items, items, matrix, palette='reds', title='Item lift', value_label='Lift')
        return {'output': Table.from_rows(self.SCHEMA, rows), 'chart': charts.chart_table(canvas, 'Item lift')}


TOOLS = [KCentroidsClusterAnalysis, AppendCluster, KCentroidsDiagnostics, PrincipalComponents, MultidimensionalScaling,
         FindNearestNeighbors, MBRules, MBInspect, MBAffinity]
