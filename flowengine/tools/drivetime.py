"""Non Overlap Drivetime: drive-time trade areas for points that do not overlap; no vendor code or data.

Functional source: the public Non Overlapping Drivetime tool help. Each point gets the area reachable
along the road network within the drive time. Where areas overlap, the overlap is cut into a grid
of square cells (Grid Size, in miles or kilometres) and each cell goes to the point nearest the
cell's centroid by straight-line (geodesic) distance, as the help describes ("point-to-point
distance"). Each point's area is its own non-overlapping part plus the cells it wins.

Drive-time areas come from the provider the node names. ``valhalla`` is an open-source routing
engine (MIT) the user runs, usually over an OpenStreetMap extract (ODbL); its road data and
speeds are not the reference's licensed drivetime dataset, so areas will differ from the
reference. Points must be longitude/latitude (EPSG:4326).
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request

from ..errors import ConfigError, DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema
from ._common import require_fields
from .download import _HttpOnlyRedirects
from .github import _permit
from .spatial import _FORMAT, _geom_field, _local_metric, _transform, check_lonlat, dump, parse

GRID_UNITS = {'mi': 1609.344, 'km': 1000.0}
MAX_CELLS = 250_000
PRECISION_M = 0.001  # overlay on a 1 mm grid so pieces meet exactly and results stay valid


def _isochrone(session, lon, lat, minutes, costing):
    conn, opener, timeout, secret = session
    url = conn.base_url.rstrip('/') + '/isochrone'
    body = json.dumps({'locations': [{'lat': lat, 'lon': lon}], 'costing': costing,
                       'contours': [{'time': minutes}], 'polygons': True, 'denoise': 1}).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={'Content-Type': 'application/json',
                                                                         **conn.authorize(url, secret)})
    try:
        with opener.open(req, timeout=timeout) as r:
            doc = json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read(2000).decode('utf-8', 'replace')
        raise DataError(f'Valhalla could not build a {minutes}-minute area at {lon}, {lat}: HTTP {e.code} {detail}') from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DataError(f'Could not reach the Valhalla server: {getattr(e, "reason", None) or e}') from None
    from shapely.geometry import shape
    from shapely.ops import unary_union
    polys = [shape(f['geometry']) for f in doc.get('features', [])
             if (f.get('geometry') or {}).get('type') in ('Polygon', 'MultiPolygon')]
    if not polys:
        raise DataError(f'Valhalla returned no drive-time polygon for the point {lon}, {lat}')
    return unary_union(polys).buffer(0)


def split_overlaps(areas, points, cell):
    """Non-overlapping areas, all in one metric CRS. Overlaps are gridded; cells go to the nearest point."""
    import shapely
    from shapely.geometry import box
    from shapely.ops import unary_union
    g = PRECISION_M
    areas = [shapely.set_precision(shapely.make_valid(a), g) for a in areas]
    n = len(areas)
    overlap = shapely.union_all([shapely.intersection(areas[i], areas[j], grid_size=g) for i in range(n)
                                 for j in range(i + 1, n) if areas[i].intersects(areas[j])], grid_size=g)
    own = [shapely.difference(a, overlap, grid_size=g) for a in areas]
    if overlap.is_empty:
        return own
    minx, miny, maxx, maxy = overlap.bounds
    cols, rows = math.ceil((maxx - minx) / cell) or 1, math.ceil((maxy - miny) / cell) or 1
    if cols * rows > MAX_CELLS:
        raise ConfigError(f'The overlap needs {cols * rows:,} grid cells; use a larger grid size (limit {MAX_CELLS:,})')
    won = [[] for _ in range(n)]
    prepared = shapely.prepared.prep(overlap)
    for r in range(rows):
        for c in range(cols):
            square = box(minx + c * cell, miny + r * cell, minx + (c + 1) * cell, miny + (r + 1) * cell)
            if not prepared.intersects(square):
                continue
            piece = shapely.intersection(square, overlap, grid_size=g)
            centre = square.centroid
            # Only points whose drive-time area covers the cell can win it; an exact tie goes to the earlier record.
            candidates = [i for i in range(n) if areas[i].intersects(piece)]
            winner = min(candidates, key=lambda i: (points[i].distance(centre), i))
            won[winner].append(shapely.intersection(piece, areas[winner], grid_size=g))
    return [shapely.union_all([own[i], *won[i]], grid_size=g) for i in range(n)]


class NonOverlapDrivetime(Tool):
    manifest = ToolManifest(
        tool_id='core.non_overlap_drivetime', version='1.0.0', display_name='Non Overlap Drivetime',
        catalog_ids=('spatial:non-overlap-drivetime',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Output'),),
        side_effects='network', deterministic=False,
        description='Drive-time trade areas (minutes along the road network) for longitude/latitude points that do not '
                    'overlap: overlaps are cut into grid cells and each cell goes to the nearest point. Areas come from '
                    'the named provider (valhalla: a routing server you run, connected under Web & API).',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'geometry_field', 'minutes'], 'properties': {
            'provider': {'type': 'string', 'enum': ['valhalla']},
            'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'web',
                              'description': 'Saved Web/API connection to the Valhalla server'},
            'geometry_field': {'type': 'string', 'minLength': 1},
            'minutes': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 240},
            'costing': {'enum': ['auto', 'truck', 'bicycle', 'pedestrian'], 'default': 'auto'},
            'grid_size': {'type': 'number', 'minimum': 0.01, 'maximum': 5, 'default': 0.25},
            'grid_units': {'enum': list(GRID_UNITS), 'default': 'mi'},
            'output_field': {'type': 'string', 'minLength': 1, 'default': 'DriveTimeArea'}, 'format': _FORMAT,
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 60}}})

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['geometry_field']])
        clash = [n for n in (cfg['output_field'], 'DriveTimeMinutes') if n in schema]
        if clash:
            raise SchemaError(f'Field(s) {clash} already exist')
        return {'output': Schema([*schema, Field('DriveTimeMinutes', FieldType.DOUBLE), _geom_field(cfg['output_field'])])}

    def run(self, ctx, cfg, inputs):
        from shapely.geometry import MultiPoint
        from .. import providers
        from ..service_connections import ServiceConnectionStore
        providers.resolve(cfg, 'routing')
        _permit(ctx)
        table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        rows, points = [], []
        for number, (row, value) in enumerate(zip(table.rows(), table.column(cfg['geometry_field'])), start=1):
            geom = parse(value, 'error', f' in record {number}')
            if geom is None or geom.geom_type != 'Point' or geom.is_empty:
                raise DataError(f'Record {number}: Non Overlap Drivetime needs a point geometry')
            check_lonlat(geom, f' in record {number}')
            rows.append(row)
            points.append(geom)
        if not rows:
            return {'output': Table.from_rows(schema, [])}
        store = ServiceConnectionStore()
        conn = store.get(cfg['connection_id'], kind='web')
        session = (conn, urllib.request.build_opener(_HttpOnlyRedirects(conn)), cfg['timeout_seconds'], store.secret(conn))
        areas = []
        for p in points:
            ctx.check_cancelled()
            areas.append(_isochrone(session, p.x, p.y, cfg['minutes'], cfg['costing']))
        forward, back = _local_metric(MultiPoint(points))
        metric = split_overlaps([_transform(a, forward) for a in areas], [_transform(p, forward) for p in points],
                                cfg['grid_size'] * GRID_UNITS[cfg['grid_units']])
        import shapely
        out = [(*row, float(cfg['minutes']), dump(shapely.make_valid(_transform(m, back)), cfg['format']))
               for row, m in zip(rows, metric)]
        return {'output': Table.from_rows(schema, out)}


TOOLS = [NonOverlapDrivetime]
