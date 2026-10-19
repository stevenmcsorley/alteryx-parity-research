"""Behavior Analysis on your own segmentation data: profiles, comparisons, cluster codes and reports; no vendor data.

Functional source: the public Behavior Analysis tool help (deprecated in the reference since 2022.3). The reference
works from a licensed segmentation system; Flowbench works from a **segmentation data pack you register** (family
``segmentation``): a CSV with ``GeoKey`` (a geography key), ``Cluster`` (a segment code) and ``Count`` (households or
people of that segment in that geography), and optionally ``ClusterName`` and ``Group``. The cluster codes, names and
counts are whatever that data says.

A **profile** is the count of households (or customers) in each cluster, held as JSON text
``{"format": "flowbench.profile.v1", "name": ..., "clusters": {code: count}}`` in a field, so it flows through
workflows like any value. Profile sets are saved as ``.fbprof`` files (JSON Lines). Comparing a target profile with a
base profile gives, per cluster, both shares, the index (100 x target share / base share) and the penetration
(target count / base count).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
FORMAT = 'flowbench.profile.v1'


def _t(name):
    return Field(name, FieldType.V_WSTRING, TEXT)


def profile_text(name, clusters):
    return json.dumps({'format': FORMAT, 'name': name, 'clusters': {k: clusters[k] for k in sorted(clusters)}},
                      ensure_ascii=False, separators=(',', ':'))


def parse_profile(value, where='') -> dict:
    try:
        doc = json.loads(value) if value is not None else None
    except (TypeError, ValueError):
        doc = None
    if not isinstance(doc, dict) or doc.get('format') != FORMAT or not isinstance(doc.get('clusters'), dict):
        raise DataError(f'{where}is not a Flowbench profile; create one with Create Profile')
    return doc


class Segmentation:
    """The registered segmentation data pack: counts per geography and cluster, and cluster names."""

    def __init__(self, pack_id=None):
        from .. import providers
        packs = providers.DataPackStore().for_family('segmentation')
        if pack_id:
            packs = [p for p in packs if p['id'] == pack_id]
        if len(packs) != 1:
            raise ConfigError('Register your segmentation data (CSV with GeoKey, Cluster, Count) as a data pack of family '
                              'segmentation (Settings > Integrations > Data packs)' if not packs else
                              'Several segmentation data packs are registered; name one with data_pack_id')
        self.pack = packs[0]
        self.geo, self.names, self.groups = {}, {}, {}
        with open(Path(self.pack['path']), newline='', encoding='utf-8-sig') as fh:
            reader = csv.DictReader(fh)
            if not {'GeoKey', 'Cluster', 'Count'} <= set(reader.fieldnames or []):
                raise DataError('The segmentation data pack needs GeoKey, Cluster and Count columns')
            for rec in reader:
                try:
                    count = float(rec['Count'])
                except ValueError:
                    raise DataError(f"Count {rec['Count']!r} is not a number in the segmentation data pack") from None
                key, cluster = rec['GeoKey'].strip(), rec['Cluster'].strip()
                self.geo.setdefault(key, {})
                self.geo[key][cluster] = self.geo[key].get(cluster, 0) + count
                if rec.get('ClusterName'):
                    self.names[cluster] = rec['ClusterName'].strip()
                if rec.get('Group'):
                    self.groups[cluster] = rec['Group'].strip()
        self.clusters = sorted({c for counts in self.geo.values() for c in counts} | set(self.names))


def _add(total, counts, factor=1.0):
    for k, v in counts.items():
        total[k] = total.get(k, 0) + v * factor


def _number(v):
    return int(v) if float(v).is_integer() else round(v, 6)


_PROVIDER = {'provider': {'type': 'string', 'enum': ['segmentation-data-pack']}, 'provider_semantics': {'type': 'string'},
             'data_pack_id': {'type': ['string', 'null'], 'default': None}}


def _provider(cfg):
    from .. import providers
    providers.resolve(cfg, 'segmentation')


class CreateProfile(Tool):
    manifest = ToolManifest(
        tool_id='core.create_profile', version='1.0.0', display_name='Create Profile',
        catalog_ids=('behavior-analysis-deprecated:create-profile',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O', description='Profile records'),), side_effects='reads-files',
        description='Build behavior profiles (counts per cluster): from a cluster code in each record (optionally weighted '
                    'by a count field and a multiplier), from geography keys looked up in the segmentation data pack, or by '
                    'combining profile fields. Summarise all records into one profile, one per group, or one per record.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'mode', 'field'], 'properties': {
            **_PROVIDER,
            'mode': {'enum': ['cluster_code', 'geography_key', 'combine_profiles']},
            'field': {'type': 'string', 'minLength': 1, 'description': 'Cluster code, geography key or profile field'},
            'count_field': {'type': ['string', 'null'], 'default': None},
            'multiplier': {'type': 'number', 'exclusiveMinimum': 0, 'default': 1},
            'summarize': {'enum': ['all', 'group_by', 'none'], 'default': 'all'},
            'group_fields': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
            'profile_name': {'type': 'string', 'default': 'Profile'}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['field'], *([cfg['count_field']] if cfg['count_field'] else []), *cfg['group_fields']])
        if cfg['summarize'] == 'group_by' and not cfg['group_fields']:
            raise ConfigError('Group By needs group_fields')
        head = [schema.field(g) for g in cfg['group_fields']] if cfg['summarize'] == 'group_by' else (
            list(schema) if cfg['summarize'] == 'none' else [])
        return {'output': Schema([*[f for f in head if f.name not in ('ProfileName', 'Profile')], _t('ProfileName'), _t('Profile')])}

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        table: Table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        seg = Segmentation(cfg['data_pack_id']) if cfg['mode'] == 'geography_key' else None
        counts = table.column(cfg['count_field']) if cfg['count_field'] else [1] * len(table)
        groups, order = {}, []
        for i, (row, value, weight) in enumerate(zip(table.rows(), table.column(cfg['field']), counts)):
            if cfg['summarize'] == 'all':
                key = ()
            elif cfg['summarize'] == 'group_by':
                key = tuple(row[table.schema.names.index(g)] for g in cfg['group_fields'])
            else:
                key = (i,)
            if key not in groups:
                groups[key] = ({}, row)
                order.append(key)
            total = groups[key][0]
            if value is None:
                continue
            if cfg['mode'] == 'cluster_code':
                _add(total, {format_value(value): float(weight or 0)}, cfg['multiplier'])
            elif cfg['mode'] == 'geography_key':
                found = seg.geo.get(format_value(value).strip())
                if found:
                    _add(total, found, cfg['multiplier'])
            else:
                _add(total, parse_profile(value, f'Record {i + 1}: ')['clusters'], cfg['multiplier'])
        rows = []
        for key in order:
            total, row = groups[key]
            name = cfg['profile_name'] if cfg['summarize'] == 'all' else (
                ' | '.join(format_value(k) for k in key) if cfg['summarize'] == 'group_by' else f"{cfg['profile_name']} {key[0] + 1}")
            head = list(key) if cfg['summarize'] == 'group_by' else (list(row) if cfg['summarize'] == 'none' else [])
            rows.append([*head[:len(schema) - 2], name, profile_text(name, {k: _number(v) for k, v in total.items()})])
        return {'output': Table.from_rows(schema, rows)}


class ProfileOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.profile_output', version='1.0.0', display_name='Profile Output', catalog_ids=('behavior-analysis-deprecated:profile-output',),
        inputs=(Anchor('input', 'I'),), outputs=(), side_effects='writes-files',
        description='Save profiles to a profile set file (.fbprof, JSON Lines): one profile per record, named by a field.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['path', 'profile_field'], 'properties': {
            'path': {'type': 'string', 'minLength': 1}, 'profile_field': {'type': 'string', 'default': 'Profile'},
            'name_field': {'type': ['string', 'null'], 'default': 'ProfileName'}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['profile_field'], *([cfg['name_field']] if cfg['name_field'] else [])])
        return {}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        path = ctx.resolve_path(cfg['path'])
        if path.suffix.lower() != '.fbprof':
            raise ConfigError('Profile sets are saved as .fbprof files')
        lines = []
        names = table.column(cfg['name_field']) if cfg['name_field'] else [None] * len(table)
        for i, (p, n) in enumerate(zip(table.column(cfg['profile_field']), names)):
            doc = parse_profile(p, f'Record {i + 1}: ')
            if n is not None:
                doc['name'] = format_value(n)
            lines.append(json.dumps(doc, ensure_ascii=False))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.fbprof.tmp')
        tmp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        tmp.replace(path)
        ctx.info(f'saved {len(lines)} profile(s) to {path.name}')
        return {}


class ProfileInput(Tool):
    manifest = ToolManifest(
        tool_id='core.profile_input', version='1.0.0', display_name='Profile Input', catalog_ids=('behavior-analysis-deprecated:profile-input',),
        inputs=(), outputs=(Anchor('output', 'O'),), side_effects='reads-files',
        description='Read the profiles of a profile set file (.fbprof), optionally only those with given names.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['path'], 'properties': {
            'path': {'type': 'string', 'minLength': 1}, 'names': {'type': 'array', 'items': {'type': 'string'}, 'default': []}}})

    def infer(self, ctx, cfg, inputs):
        return {'output': Schema([_t('ProfileName'), _t('Profile')])}

    def run(self, ctx, cfg, inputs):
        path = ctx.resolve_path(cfg['path'])
        if not path.is_file():
            raise DataError(f'Profile set not found: {path}')
        rows = []
        for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
            if line.strip():
                doc = parse_profile(line, f'{path.name} line {n}: ')
                if not cfg['names'] or doc.get('name') in cfg['names']:
                    rows.append((doc.get('name'), profile_text(doc.get('name'), doc['clusters'])))
        return {'output': Table.from_rows(self.infer(ctx, cfg, {})['output'], rows)}


def _comparison(target, base, clusters):
    t_total, b_total = sum(target.values()), sum(base.values())
    out = []
    for c in clusters:
        t, b = target.get(c, 0), base.get(c, 0)
        ts = t / t_total if t_total else 0.0
        bs = b / b_total if b_total else 0.0
        out.append((c, t, ts, b, bs, round(100 * ts / bs, 2) if bs else None, round(t / b, 6) if b else None))
    return out


COMPARE_FIELDS = [_t('Cluster'), _t('ClusterName'), Field('TargetCount', FieldType.DOUBLE), Field('TargetShare', FieldType.DOUBLE),
                  Field('BaseCount', FieldType.DOUBLE), Field('BaseShare', FieldType.DOUBLE), Field('Index', FieldType.DOUBLE),
                  Field('Penetration', FieldType.DOUBLE)]


def _pairs(table, cfg):
    require_fields(table.schema, [cfg['target_field'], cfg['base_field']])
    for i, (t, b) in enumerate(zip(table.column(cfg['target_field']), table.column(cfg['base_field']))):
        yield parse_profile(t, f'Record {i + 1} target: '), parse_profile(b, f'Record {i + 1} base: ')


_PAIR = {'target_field': {'type': 'string', 'default': 'Profile'}, 'base_field': {'type': 'string', 'default': 'BaseProfile'}}


class CompareBehavior(Tool):
    manifest = ToolManifest(
        tool_id='core.compare_behavior', version='1.0.0', display_name='Compare Behavior',
        catalog_ids=('behavior-analysis-deprecated:compare-behavior',),
        inputs=(Anchor('input', 'I', description='Records with a target and a base profile'),),
        outputs=(Anchor('output', 'O', description='One record per input record and cluster'),), side_effects='reads-files',
        description='Compare a target profile with a base profile per cluster: counts, shares, index (100 x target share / '
                    'base share) and penetration (target / base). Cluster names come from the data pack when registered.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider'], 'properties': {**_PROVIDER, **_PAIR}})

    def infer(self, ctx, cfg, inputs):
        return {'output': Schema([Field('Record', FieldType.INT64), *COMPARE_FIELDS])}

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        names = _names_if_available(cfg)
        rows = []
        for i, (t, b) in enumerate(_pairs(inputs['input'], cfg), start=1):
            clusters = sorted(set(t['clusters']) | set(b['clusters']))
            for c, tc, ts, bc, bs, idx, pen in _comparison(t['clusters'], b['clusters'], clusters):
                rows.append((i, c, names.get(c), tc, ts, bc, bs, idx, pen))
        return {'output': Table.from_rows(self.infer(ctx, cfg, {})['output'], rows)}


def _names_if_available(cfg):
    try:
        return Segmentation(cfg.get('data_pack_id')).names
    except ConfigError:
        return {}


class DetailFields(Tool):
    manifest = ToolManifest(
        tool_id='core.detail_fields', version='1.0.0', display_name='Detail Fields', catalog_ids=('behavior-analysis-deprecated:detail-fields',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O'),), side_effects='reads-files',
        description='Expand a profile field into one count field and one percent field per cluster (every cluster of the '
                    'data pack, or of the profiles when no pack is registered).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'profile_field'], 'properties': {
            **_PROVIDER, 'profile_field': {'type': 'string', 'default': 'Profile'}, 'prefix': {'type': 'string', 'default': 'Cluster_'}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['profile_field']])
        return None

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        docs = [parse_profile(p, f'Record {i + 1}: ') for i, p in enumerate(table.column(cfg['profile_field']))]
        try:
            clusters = Segmentation(cfg['data_pack_id']).clusters
        except ConfigError:
            clusters = sorted({c for d in docs for c in d['clusters']})
        added = [f for c in clusters for f in (Field(f"{cfg['prefix']}{c}", FieldType.DOUBLE), Field(f"{cfg['prefix']}{c}_Pct", FieldType.DOUBLE))]
        clash = [f.name for f in added if f.name in table.schema]
        if clash:
            raise ConfigError(f'Field(s) {clash[:5]} already exist; set a prefix')
        rows = []
        for row, d in zip(table.rows(), docs):
            total = sum(d['clusters'].values())
            vals = [v for c in clusters for v in (d['clusters'].get(c, 0), round(100 * d['clusters'].get(c, 0) / total, 4) if total else 0.0)]
            rows.append([*row, *vals])
        return {'output': Table.from_rows(Schema([*table.schema, *added]), rows)}


class ClusterCode(Tool):
    manifest = ToolManifest(
        tool_id='core.cluster_code', version='1.0.0', display_name='Cluster Code', catalog_ids=('behavior-analysis-deprecated:cluster-code',),
        inputs=(Anchor('input', 'I', description='Records with a geography key'),), outputs=(Anchor('output', 'O'),), side_effects='reads-files',
        description='Append the dominant cluster (largest count) of each record’s geography from the segmentation data pack, '
                    'with its name, group and share of the geography.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'key_field'], 'properties': {
            **_PROVIDER, 'key_field': {'type': 'string', 'minLength': 1}}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg['key_field']])
        return {'output': Schema([*inputs['input'], _t('ClusterCode'), _t('ClusterName'), _t('ClusterGroup'), Field('ClusterShare', FieldType.DOUBLE)])}

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        seg = Segmentation(cfg['data_pack_id'])
        rows = []
        for row, key in zip(table.rows(), table.column(cfg['key_field'])):
            counts = seg.geo.get(format_value(key).strip()) if key is not None else None
            if counts:
                code = max(sorted(counts), key=lambda c: counts[c])  # ties: the lowest code
                rows.append([*row, code, seg.names.get(code), seg.groups.get(code), round(counts[code] / sum(counts.values()), 6)])
            else:
                rows.append([*row, None, None, None, None])
        return {'output': Table.from_rows(schema, rows)}


class BehaviorMetaInfo(Tool):
    manifest = ToolManifest(
        tool_id='core.behavior_metainfo', version='1.0.0', display_name='Behavior MetaInfo',
        catalog_ids=('behavior-analysis-deprecated:behavior-metainfo',),
        inputs=(), outputs=(Anchor('output', 'O'),), side_effects='reads-files',
        description='List the clusters of the segmentation data pack: code, name, group, total count and share.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider'], 'properties': dict(_PROVIDER)})

    def infer(self, ctx, cfg, inputs):
        return {'output': Schema([_t('Cluster'), _t('ClusterName'), _t('Group'), Field('Count', FieldType.DOUBLE), Field('Share', FieldType.DOUBLE)])}

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        seg = Segmentation(cfg['data_pack_id'])
        totals = {}
        for counts in seg.geo.values():
            _add(totals, counts)
        grand = sum(totals.values())
        rows = [(c, seg.names.get(c), seg.groups.get(c), totals.get(c, 0), round(totals.get(c, 0) / grand, 6) if grand else None)
                for c in seg.clusters]
        return {'output': Table.from_rows(self.infer(ctx, cfg, {})['output'], rows)}


def _report(cfg, rows, columns, title):
    from .. import reports
    return reports.element('table', title=title, group=None, columns=[{'label': c, 'align': 'left' if i < 2 else 'right'}
                                                                       for i, c in enumerate(columns)], rows=rows, total_row=None)


def _pct(v):
    return f'{100 * v:.1f}%'


class _Report(Tool):
    kind = ''

    def infer(self, ctx, cfg, inputs):
        return {'output': Schema([_t('Report')])}

    def run(self, ctx, cfg, inputs):
        _provider(cfg)
        names = _names_if_available(cfg)
        out = []
        table = inputs['input']
        if self.kind == 'detail':
            require_fields(table.schema, [cfg['profile_field']])
            for i, p in enumerate(table.column(cfg['profile_field'])):
                d = parse_profile(p, f'Record {i + 1}: ')
                total = sum(d['clusters'].values())
                rows = [[c, names.get(c) or '', f"{d['clusters'][c]:,}", _pct(d['clusters'][c] / total) if total else '']
                        for c in sorted(d['clusters'])]
                out.append((_report(cfg, rows, ['Cluster', 'Name', 'Count', 'Share'], f"{cfg['title']}: {d.get('name') or ''}"),))
        else:
            for t, b in _pairs(table, cfg):
                comp = _comparison(t['clusters'], b['clusters'], sorted(set(t['clusters']) | set(b['clusters'])))
                if self.kind == 'rank':
                    comp = sorted(comp, key=lambda r: (-(r[5] if r[5] is not None else -1), r[0]))[:cfg['top']]
                rows = [[c, names.get(c) or '', f'{tc:,}', _pct(ts), f'{bc:,}', _pct(bs), '' if idx is None else f'{idx:.0f}']
                        for c, tc, ts, bc, bs, idx, _ in comp]
                out.append((_report(cfg, rows, ['Cluster', 'Name', 'Target', 'Target %', 'Base', 'Base %', 'Index'],
                                    f"{cfg['title']}: {t.get('name') or 'Target'} vs {b.get('name') or 'Base'}"),))
        return {'output': Table.from_rows(self.infer(ctx, cfg, {})['output'], out)}


class ProfileComparisonReport(_Report):
    kind = 'comparison'
    manifest = ToolManifest(
        tool_id='core.profile_comparison_report', version='1.0.0', display_name='Profile Comparison Report',
        catalog_ids=('behavior-analysis-deprecated:profile-comparison-report',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O', description='One report table per record'),), side_effects='reads-files',
        description='A report table comparing a target and a base profile per cluster: counts, shares and index.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider'], 'properties': {
            **_PROVIDER, **_PAIR, 'title': {'type': 'string', 'default': 'Profile comparison'}}})


class ProfileRankReport(_Report):
    kind = 'rank'
    manifest = ToolManifest(
        tool_id='core.profile_rank_report', version='1.0.0', display_name='Profile Rank Report',
        catalog_ids=('behavior-analysis-deprecated:profile-rank-report',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O', description='One report table per record'),), side_effects='reads-files',
        description='A report table of the clusters ranked by index (target share against base share), highest first.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider'], 'properties': {
            **_PROVIDER, **_PAIR, 'top': {'type': 'integer', 'minimum': 1, 'default': 20},
            'title': {'type': 'string', 'default': 'Cluster rank'}}})


class ProfileDetailReport(_Report):
    kind = 'detail'
    manifest = ToolManifest(
        tool_id='core.profile_detail_report', version='1.0.0', display_name='Profile Detail Report',
        catalog_ids=('behavior-analysis-deprecated:profile-detail-report',),
        inputs=(Anchor('input', 'I'),), outputs=(Anchor('output', 'O', description='One report table per profile'),), side_effects='reads-files',
        description='A report table of one profile: count and share of every cluster.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider'], 'properties': {
            **_PROVIDER, 'profile_field': {'type': 'string', 'default': 'Profile'}, 'title': {'type': 'string', 'default': 'Profile detail'}}})


TOOLS = [CreateProfile, ProfileOutput, ProfileInput, CompareBehavior, DetailFields, ClusterCode, BehaviorMetaInfo,
         ProfileComparisonReport, ProfileRankReport, ProfileDetailReport]
