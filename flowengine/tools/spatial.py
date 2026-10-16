"""Spatial tools on WKT/GeoJSON geometry fields, using shapely (GEOS) and pyproj (PROJ).

Geometry travels as text, WKT by default or GeoJSON, so it works with every existing tool and file
format. Each tool states the coordinate reference system of its geometries (``crs``, default
``EPSG:4326`` longitude/latitude). Geographic coordinates are never treated as metres:

* distances, lengths and areas on a geographic CRS are geodesic on the WGS84 ellipsoid (pyproj ``Geod``);
* buffers on a geographic CRS are built in a local azimuthal equidistant projection centred on each
  geometry, then projected back;
* planar measurements need a projected CRS and are converted from its axis unit.

These are Flowbench semantics. Alteryx SpatialObj encoding and Spatial XML settings are not
reproduced; imported instances keep their original XML.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache

from ..errors import ConfigError, DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema
from ._common import require_fields

TEXT = 2**30 - 1
_EARTH_RADIUS = 6_371_008.8  # mean radius (m), for spherical candidate search only
LENGTH_UNITS = {'m': 1.0, 'km': 1000.0, 'mi': 1609.344, 'ft': 0.3048, 'nmi': 1852.0}
AREA_UNITS = {'m2': 1.0, 'km2': 1e6, 'ha': 1e4, 'mi2': 1609.344 ** 2, 'acre': 4046.8564224}
PREDICATES = ('intersects', 'contains', 'within', 'touches', 'overlaps', 'crosses', 'covers', 'covered_by', 'equals')
_CRS = {'type': 'string', 'minLength': 1, 'default': 'EPSG:4326',
        'description': 'Coordinate reference system of the geometry, e.g. EPSG:4326 (lon/lat) or EPSG:27700'}
_FORMAT = {'enum': ['wkt', 'geojson'], 'default': 'wkt'}
_ON_INVALID = {'enum': ['error', 'null'], 'default': 'error',
               'description': 'Unparseable or invalid geometry: stop the run, or output null'}


def _geom_field(name):
    return Field(name, FieldType.V_WSTRING, TEXT)


@lru_cache(maxsize=64)
def crs_info(text: str):
    """(CRS, is_geographic, metres per axis unit or None)."""
    from pyproj import CRS
    from pyproj.exceptions import CRSError
    try:
        crs = CRS.from_user_input(text)
    except CRSError as e:
        raise ConfigError(f'Unknown coordinate reference system {text!r}: {e}') from None
    if crs.is_geographic:
        return crs, True, None
    axis = crs.axis_info[0] if crs.axis_info else None
    # pyproj reports linear axis units with their conversion factor to metres
    factor = float(axis.unit_conversion_factor) if axis is not None and axis.unit_conversion_factor else None
    return crs, False, factor


@lru_cache(maxsize=8)
def _geod(crs_text):
    crs, geographic, _ = crs_info(crs_text)
    return crs.get_geod() if geographic else None


def _lonlat_transformer(crs_text):
    """Transformer to x=longitude, y=latitude order for the CRS's own geographic datum."""
    from pyproj import Transformer
    crs, _, _ = crs_info(crs_text)
    return Transformer.from_crs(crs, crs.geodetic_crs, always_xy=True)


def parse(value, on_invalid='error', where=''):
    """WKT or GeoJSON text -> shapely geometry (None for null input)."""
    import shapely
    from shapely.geometry import shape
    if value is None:
        return None
    text = str(value).strip()
    try:
        geom = shape(json.loads(text)) if text.startswith('{') else shapely.from_wkt(text)
    except Exception:
        if on_invalid == 'null':
            return None
        raise DataError(f'Invalid geometry{where}: expected WKT or GeoJSON') from None
    if not geom.is_valid and on_invalid == 'error' and not geom.is_empty:
        raise DataError(f'Invalid geometry{where}: {shapely.is_valid_reason(geom)}')
    return geom


def dump(geom, fmt):
    import shapely
    if geom is None:
        return None
    if fmt == 'geojson':
        return json.dumps(shapely.geometry.mapping(geom), separators=(',', ':'))
    return shapely.to_wkt(geom, rounding_precision=-1, trim=True)


def check_lonlat(geom, where):
    import shapely
    minx, miny, maxx, maxy = shapely.bounds(geom)
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90):
        raise DataError(f'Coordinates{where} are outside longitude -180..180 / latitude -90..90; '
                        'check the field order (x is longitude) or set the CRS of projected data')


def geodesic_distance(a, b, crs_text):
    """Metres between two geometries on the ellipsoid; exact for points, nearest-point based otherwise."""
    from shapely.ops import nearest_points
    geod = _geod(crs_text)
    if a.equals(b):
        return 0.0  # exact on every platform (the ellipsoid solver can return ~1e-15 m)
    if a.geom_type != 'Point' or b.geom_type != 'Point':
        if a.intersects(b):
            return 0.0
        a, b = nearest_points(a, b)  # nearest pair found in degrees, then measured geodesically
    _, _, d = geod.inv(a.x, a.y, b.x, b.y)
    return float(d)


def measure_distance(a, b, crs_text, method):
    crs, geographic, factor = crs_info(crs_text)
    if method == 'geodesic':
        if not geographic:
            ta = _transform(a, _lonlat_transformer(crs_text))
            tb = _transform(b, _lonlat_transformer(crs_text))
            return geodesic_distance(ta, tb, crs.geodetic_crs.to_string())
        return geodesic_distance(a, b, crs_text)
    if geographic:
        raise ConfigError('Planar distance on a geographic CRS would measure degrees; use geodesic or a projected CRS')
    if factor is None:
        raise ConfigError(f'Cannot convert the axis unit of {crs_text!r} to metres')
    return float(a.distance(b)) * factor


def _transform(geom, transformer):
    from shapely.ops import transform
    return transform(transformer.transform, geom)


# =========================================================================== tools

class CreatePoints(Tool):
    manifest = ToolManifest(
        tool_id='core.create_points', version='1.0.0', display_name='Create Points', catalog_ids=('spatial:create-points',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Output'),),
        description='Make a point geometry from x (longitude or easting) and y (latitude or northing) fields '
                    'in crs, optionally reprojected to output_crs. Geographic coordinates must be within '
                    '-180..180 / -90..90. Records with a missing coordinate get a null point.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['x_field', 'y_field'], 'properties': {
            'x_field': {'type': 'string', 'minLength': 1}, 'y_field': {'type': 'string', 'minLength': 1},
            'crs': _CRS, 'output_crs': {'type': ['string', 'null'], 'default': None},
            'output_field': {'type': 'string', 'minLength': 1, 'default': 'Point'}, 'format': _FORMAT}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['x_field'], cfg['y_field']])
        for name in (cfg['x_field'], cfg['y_field']):
            if not schema.field(name).type.is_numeric:
                raise SchemaError(f'Create Points coordinate field {name!r} must be numeric')
        if cfg['output_field'] in schema:
            raise SchemaError(f"Field {cfg['output_field']!r} already exists")
        crs_info(cfg['crs'])
        if cfg['output_crs']:
            crs_info(cfg['output_crs'])
        return {'output': Schema([*schema, _geom_field(cfg['output_field'])])}

    def run(self, ctx, cfg, inputs):
        from pyproj import Transformer
        from shapely.geometry import Point
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        _, geographic, _ = crs_info(cfg['crs'])
        transformer = (Transformer.from_crs(crs_info(cfg['crs'])[0], crs_info(cfg['output_crs'])[0], always_xy=True)
                       if cfg['output_crs'] else None)
        out = []
        for number, (row, x, y) in enumerate(zip(table.rows(), table.column(cfg['x_field']), table.column(cfg['y_field'])), 1):
            if x is None or y is None or not (math.isfinite(float(x)) and math.isfinite(float(y))):
                out.append((*row, None))
                continue
            x, y = float(x), float(y)
            if geographic and not (-180 <= x <= 180 and -90 <= y <= 90):
                raise DataError(f'Create Points record {number}: ({x}, {y}) is outside longitude -180..180 / '
                                'latitude -90..90; check x is longitude or set the projected CRS')
            if transformer:
                x, y = transformer.transform(x, y)
            out.append((*row, dump(Point(x, y), cfg['format'])))
        return {'output': Table.from_rows(schema, out)}


class Distance(Tool):
    manifest = ToolManifest(
        tool_id='core.distance', version='1.0.0', display_name='Distance', catalog_ids=('spatial:distance',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Output'),),
        description='Distance between two geometry fields of each record. Geodesic (WGS84 ellipsoid, any CRS) or '
                    'planar (projected CRS only, converted from its unit), in m, km, mi, ft or nmi. Geodesic distance '
                    'is exact between points and adds the initial bearing. For other geometries it is measured '
                    'between their nearest points (intersecting geometries are 0).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['source_field', 'target_field'], 'properties': {
            'source_field': {'type': 'string', 'minLength': 1}, 'target_field': {'type': 'string', 'minLength': 1},
            'crs': _CRS, 'method': {'enum': ['geodesic', 'planar'], 'default': 'geodesic'},
            'units': {'enum': list(LENGTH_UNITS), 'default': 'km'}, 'on_invalid': _ON_INVALID}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['source_field'], cfg['target_field']])
        _, geographic, factor = crs_info(cfg['crs'])
        if cfg['method'] == 'planar' and geographic:
            raise ConfigError('Planar distance on a geographic CRS would measure degrees; use geodesic or a projected CRS')
        if cfg['method'] == 'planar' and factor is None:
            raise ConfigError(f"Cannot convert the axis unit of {cfg['crs']!r} to metres")
        clash = [n for n in ('Distance', 'Bearing') if n in schema]
        if clash:
            raise SchemaError(f'Field(s) {clash} already exist')
        return {'output': Schema([*schema, Field('Distance', FieldType.DOUBLE), Field('Bearing', FieldType.DOUBLE)])}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        scale = LENGTH_UNITS[cfg['units']]
        _, geographic, _ = crs_info(cfg['crs'])
        out = []
        for number, (row, a, b) in enumerate(zip(table.rows(), table.column(cfg['source_field']),
                                                 table.column(cfg['target_field'])), 1):
            ga, gb = parse(a, cfg['on_invalid'], f' in record {number}'), parse(b, cfg['on_invalid'], f' in record {number}')
            if ga is None or gb is None or ga.is_empty or gb.is_empty:
                out.append((*row, None, None))
                continue
            if geographic:
                check_lonlat(ga, f' in record {number}')
                check_lonlat(gb, f' in record {number}')
            bearing = None
            if ga.equals(gb):
                metres = 0.0
            elif cfg['method'] == 'geodesic' and geographic and ga.geom_type == gb.geom_type == 'Point':
                az, _, d = _geod(cfg['crs']).inv(ga.x, ga.y, gb.x, gb.y)
                bearing = az % 360 if d else None
                metres = float(d)
            else:
                metres = measure_distance(ga, gb, cfg['crs'], cfg['method'])
            out.append((*row, metres / scale, bearing))
        return {'output': Table.from_rows(schema, out)}


def geodesic_buffer(geom, metres, resolution):
    """Buffer a lon/lat geometry by metres in an azimuthal equidistant projection centred on it."""
    from pyproj import CRS, Transformer
    c = geom.centroid if not geom.is_empty else geom
    local = CRS.from_proj4(f'+proj=aeqd +lat_0={c.y} +lon_0={c.x} +datum=WGS84 +units=m +no_defs')
    wgs84 = CRS.from_epsg(4326)
    forward = Transformer.from_crs(wgs84, local, always_xy=True)
    back = Transformer.from_crs(local, wgs84, always_xy=True)
    return _transform(_transform(geom, forward).buffer(metres, quad_segs=resolution), back)


class Buffer(Tool):
    manifest = ToolManifest(
        tool_id='core.buffer', version='1.0.0', display_name='Buffer', catalog_ids=('spatial:buffer',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Output'),),
        description='Polygon buffer around each geometry by a distance in m, km, mi, ft or nmi (negative values '
                    'shrink polygons). On a geographic CRS the buffer is built in metres in a local azimuthal '
                    'equidistant projection, so degrees are never treated as distance. On a projected CRS the '
                    'distance is converted to its unit. distance_field gives a per-record distance.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['geometry_field'], 'properties': {
            'geometry_field': {'type': 'string', 'minLength': 1}, 'crs': _CRS,
            'distance': {'type': 'number', 'default': 1.0}, 'distance_field': {'type': ['string', 'null'], 'default': None},
            'units': {'enum': list(LENGTH_UNITS), 'default': 'km'},
            'resolution': {'type': 'integer', 'minimum': 1, 'maximum': 128, 'default': 16,
                           'description': 'Segments per quarter circle'},
            'output_field': {'type': 'string', 'minLength': 1, 'default': 'Buffer'}, 'format': _FORMAT,
            'on_invalid': _ON_INVALID}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['geometry_field']] + ([cfg['distance_field']] if cfg['distance_field'] else []))
        if cfg['distance_field'] and not schema.field(cfg['distance_field']).type.is_numeric:
            raise SchemaError('Buffer distance_field must be numeric')
        _, geographic, factor = crs_info(cfg['crs'])
        if not geographic and factor is None:
            raise ConfigError(f"Cannot convert {cfg['units']} to the axis unit of {cfg['crs']!r}")
        if cfg['output_field'] in schema:
            raise SchemaError(f"Field {cfg['output_field']!r} already exists")
        return {'output': Schema([*schema, _geom_field(cfg['output_field'])])}

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        _, geographic, factor = crs_info(cfg['crs'])
        distances = table.column(cfg['distance_field']) if cfg['distance_field'] else [cfg['distance']] * len(table)
        out = []
        for number, (row, value, dist) in enumerate(zip(table.rows(), table.column(cfg['geometry_field']), distances), 1):
            ctx.check_cancelled()
            geom = parse(value, cfg['on_invalid'], f' in record {number}')
            if geom is None or dist is None:
                out.append((*row, None))
                continue
            metres = float(dist) * LENGTH_UNITS[cfg['units']]
            if geographic:
                check_lonlat(geom, f' in record {number}')
                result = geodesic_buffer(geom, metres, cfg['resolution'])
            else:
                result = geom.buffer(metres / factor, quad_segs=cfg['resolution'])
            out.append((*row, dump(result, cfg['format'])))
        return {'output': Table.from_rows(schema, out)}


def _prefix(schema, prefix, avoid):
    fields = []
    for f in schema:
        name = f.name if f.name not in avoid else f'{prefix}{f.name}'
        while name in avoid:
            name = f'{prefix}{name}'
        avoid.add(name)
        fields.append(Field(name, f.type, f.size, f.scale))
    return fields


class FindNearest(Tool):
    manifest = ToolManifest(
        tool_id='core.find_nearest', version='1.0.0', display_name='Find Nearest', catalog_ids=('spatial:find-nearest',),
        inputs=(Anchor('targets', 'Targets'), Anchor('universe', 'Universe')),
        outputs=(Anchor('matched', 'Matched', description='One row per target and nearest universe match, nearest first'),
                 Anchor('unmatched', 'Unmatched', description='Targets with no universe record within max_distance')),
        description='For each target geometry, the max_matches nearest universe geometries, optionally within '
                    'max_distance, with Distance and Rank (ties broken by universe order). Geographic data uses '
                    'geodesic distance: point universes are searched with a 3-D index and re-ranked exactly; '
                    'other geometries use nearest-point distance. Projected data uses planar distance with a '
                    'spatial index. Universe fields that clash with target names get the prefix U_.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['target_geometry_field', 'universe_geometry_field'], 'properties': {
            'target_geometry_field': {'type': 'string', 'minLength': 1},
            'universe_geometry_field': {'type': 'string', 'minLength': 1}, 'crs': _CRS,
            'max_matches': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 1},
            'max_distance': {'type': ['number', 'null'], 'minimum': 0, 'default': None},
            'units': {'enum': list(LENGTH_UNITS), 'default': 'km'},
            'ignore_identical': {'type': 'boolean', 'default': False,
                                 'description': 'Skip universe geometries identical to the target (self-matches)'},
            'on_invalid': _ON_INVALID}})

    def _schema(self, cfg, targets, universe):
        require_fields(targets, [cfg['target_geometry_field']])
        require_fields(universe, [cfg['universe_geometry_field']])
        crs_info(cfg['crs'])
        names = set(targets.names) | {'Distance', 'Rank'}
        return Schema([*targets, *_prefix(universe, 'U_', names), Field('Distance', FieldType.DOUBLE), Field('Rank', FieldType.INT32)])

    def infer(self, ctx, cfg, inputs):
        if {'Distance', 'Rank'} & set(inputs['targets'].names):
            raise SchemaError('Targets already contain a Distance or Rank field')
        return {'matched': self._schema(cfg, inputs['targets'], inputs['universe']), 'unmatched': inputs['targets']}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        import shapely
        targets, universe = inputs['targets'], inputs['universe']
        schema = self.infer(ctx, cfg, {'targets': targets.schema, 'universe': universe.schema})['matched']
        _, geographic, factor = crs_info(cfg['crs'])
        scale = LENGTH_UNITS[cfg['units']]
        limit = cfg['max_distance'] * scale if cfg['max_distance'] is not None else None  # metres
        ugeoms = [parse(v, cfg['on_invalid'], f' in universe record {i + 1}') for i, v in
                  enumerate(universe.column(cfg['universe_geometry_field']))]
        keep = [i for i, g in enumerate(ugeoms) if g is not None and not g.is_empty]
        if geographic:
            for i in keep:
                check_lonlat(ugeoms[i], f' in universe record {i + 1}')
        k = cfg['max_matches']
        kept = [ugeoms[i] for i in keep]
        all_points = geographic and kept and all(g.geom_type == 'Point' for g in kept)
        if all_points:
            from scipy.spatial import cKDTree
            lon, lat = np.radians([g.x for g in kept]), np.radians([g.y for g in kept])
            tree = cKDTree(np.column_stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)]))
        if not geographic and keep and factor is None:
            raise ConfigError(f"Cannot convert the axis unit of {cfg['crs']!r} to metres")
        geod = _geod(cfg['crs'])
        urows = list(universe.rows())
        matched, unmatched = [], []

        def exact(geom, positions):
            found = []
            for p in positions:
                g = kept[p]
                if cfg['ignore_identical'] and g.equals(geom):
                    continue
                if g.equals(geom):
                    d = 0.0
                elif all_points and geom.geom_type == 'Point':
                    d = float(geod.inv(geom.x, geom.y, g.x, g.y)[2])
                else:
                    d = measure_distance(geom, g, cfg['crs'], 'geodesic' if geographic else 'planar')
                if limit is None or d <= limit:
                    found.append((d, keep[p]))
            return found

        for t_index, (row, value) in enumerate(zip(targets.rows(), targets.column(cfg['target_geometry_field']))):
            ctx.check_cancelled()
            geom = parse(value, cfg['on_invalid'], f' in target record {t_index + 1}')
            candidates = []
            if geom is not None and not geom.is_empty and keep:
                if geographic:
                    check_lonlat(geom, f' in target record {t_index + 1}')
                if all_points and geom.geom_type == 'Point':
                    la, lo = math.radians(geom.y), math.radians(geom.x)
                    vec = [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)]
                    _, idx = tree.query(vec, k=min(len(kept), k + 8 + (1 if cfg['ignore_identical'] else 0)))
                    first = exact(geom, [int(i) for i in np.atleast_1d(idx)])
                    first.sort()
                    reach = first[k - 1][0] if len(first) >= k else (limit if limit is not None else math.pi * _EARTH_RADIUS)
                    if limit is not None:
                        reach = min(reach, limit)
                    # Every point within the reach on the sphere (+1% covers ellipsoid/sphere differences).
                    radius = 2 * math.sin(min(math.pi / 2, reach * 1.01 / (2 * _EARTH_RADIUS))) + 1e-12
                    ball = tree.query_ball_point(vec, radius)
                    candidates = exact(geom, sorted(set(ball)))
                elif not geographic:
                    dists = shapely.distance(geom, np.array(kept, dtype=object)) * factor
                    order = np.argsort(dists, kind='stable')
                    for p in order:
                        if limit is not None and dists[p] > limit:
                            break
                        if cfg['ignore_identical'] and kept[p].equals(geom):
                            continue
                        candidates.append((float(dists[p]), keep[p]))
                        if len(candidates) == k:
                            break
                else:
                    candidates = exact(geom, range(len(kept)))
                candidates = sorted(candidates)[:k]
            if not candidates:
                unmatched.append(row)
            for rank, (d, u) in enumerate(candidates, start=1):
                matched.append((*row, *urows[u], d / scale, rank))
        return {'matched': Table.from_rows(schema, matched), 'unmatched': Table.from_rows(targets.schema, unmatched)}


class SpatialMatch(Tool):
    manifest = ToolManifest(
        tool_id='core.spatial_match', version='1.0.0', display_name='Spatial Match', catalog_ids=('spatial:spatial-match',),
        inputs=(Anchor('targets', 'Targets'), Anchor('universe', 'Universe')),
        outputs=(Anchor('matched', 'Matched', description='Target and universe fields for every pair that satisfies the predicate'),
                 Anchor('unmatched', 'Unmatched', description='Targets that matched nothing')),
        description='Pair targets with universe records where "target <predicate> universe" holds: intersects, '
                    'contains, within, touches, overlaps, crosses, covers, covered_by or equals (GEOS semantics, in '
                    'the coordinates of crs). A spatial index finds candidates. Matches keep target order, then '
                    'universe order. Universe fields that clash with target names get the prefix U_.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['target_geometry_field', 'universe_geometry_field'], 'properties': {
            'target_geometry_field': {'type': 'string', 'minLength': 1},
            'universe_geometry_field': {'type': 'string', 'minLength': 1},
            'predicate': {'enum': list(PREDICATES), 'default': 'intersects'}, 'crs': _CRS, 'on_invalid': _ON_INVALID}})

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['targets'], [cfg['target_geometry_field']])
        require_fields(inputs['universe'], [cfg['universe_geometry_field']])
        crs_info(cfg['crs'])
        names = set(inputs['targets'].names)
        return {'matched': Schema([*inputs['targets'], *_prefix(inputs['universe'], 'U_', names)]),
                'unmatched': inputs['targets']}

    def run(self, ctx, cfg, inputs):
        import numpy as np
        import shapely
        targets, universe = inputs['targets'], inputs['universe']
        schema = self.infer(ctx, cfg, {'targets': targets.schema, 'universe': universe.schema})['matched']
        ugeoms = [parse(v, cfg['on_invalid'], f' in universe record {i + 1}') for i, v in
                  enumerate(universe.column(cfg['universe_geometry_field']))]
        keep = [i for i, g in enumerate(ugeoms) if g is not None and not g.is_empty]
        tree = shapely.STRtree([ugeoms[i] for i in keep]) if keep else None
        predicate = {'covered_by': 'covered_by'}.get(cfg['predicate'], cfg['predicate'])
        urows = list(universe.rows())
        matched, unmatched = [], []
        for t_index, (row, value) in enumerate(zip(targets.rows(), targets.column(cfg['target_geometry_field']))):
            ctx.check_cancelled()
            geom = parse(value, cfg['on_invalid'], f' in target record {t_index + 1}')
            hits = []
            if geom is not None and not geom.is_empty and tree is not None:
                if predicate == 'equals':
                    hits = [keep[j] for j in np.atleast_1d(tree.query(geom, predicate='intersects')) if geom.equals(ugeoms[keep[j]])]
                else:
                    hits = [keep[j] for j in np.atleast_1d(tree.query(geom, predicate=predicate))]
            hits.sort()
            if not hits:
                unmatched.append(row)
            matched.extend((*row, *urows[u]) for u in hits)
        return {'matched': Table.from_rows(schema, matched), 'unmatched': Table.from_rows(targets.schema, unmatched)}


class SpatialInfo(Tool):
    FIELDS = (('GeometryType', 'text'), ('IsValid', 'bool'), ('ValidityReason', 'text'), ('IsEmpty', 'bool'),
              ('NumPoints', 'int'), ('MinX', 'dbl'), ('MinY', 'dbl'), ('MaxX', 'dbl'), ('MaxY', 'dbl'),
              ('CentroidX', 'dbl'), ('CentroidY', 'dbl'), ('Area', 'dbl'), ('Length', 'dbl'), ('CRS', 'text'))
    manifest = ToolManifest(
        tool_id='core.spatial_info', version='1.0.0', display_name='Spatial Info', catalog_ids=('spatial:spatial-info',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Output'),),
        description='Geometry type, validity (with the GEOS reason), emptiness, number of points, bounding box, '
                    'centroid, area and length (perimeter for polygons) of each geometry. Area and length are '
                    'geodesic on a geographic CRS and planar in metres on a projected CRS, then reported in the '
                    'chosen units. Invalid geometries are reported rather than rejected.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['geometry_field'], 'properties': {
            'geometry_field': {'type': 'string', 'minLength': 1}, 'crs': _CRS,
            'area_units': {'enum': list(AREA_UNITS), 'default': 'km2'},
            'length_units': {'enum': list(LENGTH_UNITS), 'default': 'km'}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['geometry_field']])
        clash = [n for n, _ in self.FIELDS if n in schema]
        if clash:
            raise SchemaError(f'Field(s) {clash} already exist')
        crs_info(cfg['crs'])
        kinds = {'text': lambda n: _geom_field(n), 'bool': lambda n: Field(n, FieldType.BOOL),
                 'int': lambda n: Field(n, FieldType.INT64), 'dbl': lambda n: Field(n, FieldType.DOUBLE)}
        return {'output': Schema([*schema, *[kinds[k](n) for n, k in self.FIELDS]])}

    def run(self, ctx, cfg, inputs):
        import shapely
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        crs, geographic, factor = crs_info(cfg['crs'])
        geod = _geod(cfg['crs'])
        if not geographic and factor is None:
            raise ConfigError(f"Cannot convert the axis unit of {cfg['crs']!r} to metres")
        a_scale, l_scale = AREA_UNITS[cfg['area_units']], LENGTH_UNITS[cfg['length_units']]
        crs_name = crs.to_string()
        out = []
        for row, value in zip(table.rows(), table.column(cfg['geometry_field'])):
            geom = parse(value, 'null')
            if geom is None:
                out.append((*row, *([None] * (len(self.FIELDS) - 1)), crs_name))
                continue
            valid = bool(geom.is_valid)
            if geom.is_empty:
                out.append((*row, geom.geom_type, valid, None, True, 0, *([None] * 8), crs_name))
                continue
            minx, miny, maxx, maxy = shapely.bounds(geom)
            c = geom.centroid
            if geographic:
                area, length = geod.geometry_area_perimeter(geom) if geom.geom_type.endswith('Polygon') or geom.geom_type == 'GeometryCollection' \
                    else (0.0, geod.geometry_length(geom))
                area = abs(area)
            else:
                area, length = geom.area * factor ** 2, geom.length * factor
            out.append((*row, geom.geom_type, valid, None if valid else shapely.is_valid_reason(geom), False,
                        int(shapely.get_num_coordinates(geom)), float(minx), float(miny), float(maxx), float(maxy),
                        float(c.x), float(c.y), area / a_scale, length / l_scale, crs_name))
        return {'output': Table.from_rows(schema, out)}


class MakeGrid(Tool):
    SCHEMA = Schema([Field('CellID', FieldType.INT64), Field('Row', FieldType.INT32), Field('Column', FieldType.INT32),
                     _geom_field('Cell'), Field('CentreX', FieldType.DOUBLE), Field('CentreY', FieldType.DOUBLE)])
    manifest = ToolManifest(
        tool_id='core.make_grid', version='1.0.0', display_name='Make Grid', catalog_ids=('spatial:make-grid',),
        inputs=(Anchor('input', 'Input', required=False, description='Optional geometries giving the extent'),),
        outputs=(Anchor('output', 'Grid'),),
        description='Square grid cells over an extent: the bounds of the input geometries, or an explicit '
                    'min_x/min_y/max_x/max_y in crs. Cell size is in crs units, or in m/km/mi/ft. A metric size on a '
                    'geographic CRS builds the grid in the UTM zone of the extent centre and returns cells in crs. '
                    'clip keeps only cells that intersect the input geometries. Row 0 is the southernmost row.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['cell_size'], 'properties': {
            'cell_size': {'type': 'number', 'exclusiveMinimum': 0},
            'units': {'enum': ['crs', *LENGTH_UNITS], 'default': 'crs'}, 'crs': _CRS,
            'geometry_field': {'type': ['string', 'null'], 'default': None},
            'min_x': {'type': ['number', 'null'], 'default': None}, 'min_y': {'type': ['number', 'null'], 'default': None},
            'max_x': {'type': ['number', 'null'], 'default': None}, 'max_y': {'type': ['number', 'null'], 'default': None},
            'clip': {'type': 'boolean', 'default': False}, 'format': _FORMAT,
            'max_cells': {'type': 'integer', 'minimum': 1, 'maximum': 5_000_000, 'default': 1_000_000}}})

    def infer(self, ctx, cfg, inputs):
        bbox = [cfg[k] for k in ('min_x', 'min_y', 'max_x', 'max_y')]
        explicit = all(v is not None for v in bbox)
        if any(v is not None for v in bbox) and not explicit:
            raise ConfigError('Make Grid explicit extent needs all of min_x, min_y, max_x and max_y')
        if explicit and (bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
            raise ConfigError('Make Grid extent minimums must be less than maximums')
        if not explicit:
            if 'input' not in inputs or not cfg['geometry_field']:
                raise ConfigError('Make Grid needs an explicit extent, or an input with geometry_field')
            require_fields(inputs['input'], [cfg['geometry_field']])
        if cfg['clip'] and ('input' not in inputs or not cfg['geometry_field']):
            raise ConfigError('Make Grid clip needs an input with geometry_field')
        crs_info(cfg['crs'])
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        import shapely
        from pyproj import CRS, Transformer
        from pyproj.aoi import AreaOfInterest
        from pyproj.database import query_utm_crs_info
        from shapely.geometry import box
        self.infer(ctx, cfg, {k: v.schema for k, v in inputs.items()})
        crs, geographic, factor = crs_info(cfg['crs'])
        geoms = []
        if cfg['geometry_field'] and 'input' in inputs:
            geoms = [g for g in (parse(v, 'error', f' in record {i + 1}') for i, v in
                                 enumerate(inputs['input'].column(cfg['geometry_field']))) if g is not None and not g.is_empty]
        if cfg['min_x'] is not None:
            extent = (cfg['min_x'], cfg['min_y'], cfg['max_x'], cfg['max_y'])
        else:
            if not geoms:
                raise DataError('Make Grid input has no geometries to define the extent')
            extent = tuple(float(v) for v in shapely.bounds(shapely.GeometryCollection(geoms)))
        to_out = None
        work_extent, size = extent, cfg['cell_size']
        if cfg['units'] != 'crs':
            metres = cfg['cell_size'] * LENGTH_UNITS[cfg['units']]
            if geographic:
                lon_c, lat_c = (extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2
                utm = query_utm_crs_info(datum_name='WGS 84', area_of_interest=AreaOfInterest(lon_c, lat_c, lon_c, lat_c))
                if not utm:
                    raise DataError('No UTM zone covers the extent centre; use a projected CRS for metric cells')
                work = CRS.from_epsg(int(utm[0].code))
                fwd = Transformer.from_crs(crs, work, always_xy=True)
                to_out = Transformer.from_crs(work, crs, always_xy=True)
                xs, ys = fwd.transform([extent[0], extent[2], extent[0], extent[2]], [extent[1], extent[1], extent[3], extent[3]])
                work_extent, size = (min(xs), min(ys), max(xs), max(ys)), metres
                ctx.info(f'Make Grid built metric cells in {work.to_string()}')
            else:
                if factor is None:
                    raise ConfigError(f"Cannot convert {cfg['units']} to the axis unit of {cfg['crs']!r}")
                size = metres / factor
        minx, miny, maxx, maxy = work_extent
        cols, rows = max(1, math.ceil((maxx - minx) / size - 1e-9)), max(1, math.ceil((maxy - miny) / size - 1e-9))
        if cols * rows > cfg['max_cells']:
            raise DataError(f'Make Grid would create {cols * rows:,} cells (max_cells {cfg["max_cells"]:,}); increase cell_size')
        clip_tree = shapely.STRtree(geoms) if cfg['clip'] else None
        out, cell_id = [], 0
        for r in range(rows):
            ctx.check_cancelled()
            for c in range(cols):
                cell = box(minx + c * size, miny + r * size, minx + (c + 1) * size, miny + (r + 1) * size)
                if to_out is not None:
                    cell = _transform(cell.segmentize(size / 8), to_out)
                if clip_tree is not None and len(clip_tree.query(cell, predicate='intersects')) == 0:
                    continue
                cell_id += 1
                centre = cell.centroid
                out.append((cell_id, r, c, dump(cell, cfg['format']), float(centre.x), float(centre.y)))
        return {'output': Table.from_rows(self.SCHEMA, out)}


TOOLS = [CreatePoints, Distance, Buffer, FindNearest, SpatialMatch, SpatialInfo, MakeGrid]
