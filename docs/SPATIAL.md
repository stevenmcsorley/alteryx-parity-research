# Flowbench spatial tools

Kernels in `flowengine/tools/spatial.py`, built on shapely (GEOS) and pyproj (PROJ).

**Geometry fields** are text: WKT by default, or GeoJSON when a tool's `format` is `geojson`. Inputs
accept either. Coordinates are always x = longitude or easting, y = latitude or northing. This keeps
geometry compatible with every other tool, CSV, Parquet and the AI agent without a new engine type.

**Coordinate systems.** Every tool takes `crs` (default `EPSG:4326`). On a geographic CRS:

- distances, lengths and areas are **geodesic** on the WGS84 ellipsoid (pyproj `Geod`);
- buffers are built in metres in a local azimuthal equidistant projection centred on each geometry,
  then projected back, so degrees are never used as distance;
- coordinates outside −180..180 / −90..90 are rejected with a hint to check the field order;
- planar measurement is a configuration error, because it would measure degrees.

On a projected CRS, measurements are planar and converted from the CRS axis unit (metres or feet).
Units are m, km, mi, ft and nmi for lengths, and m², km², ha, mi² and acres for areas.

| Tool | Kernel | Behaviour |
|---|---|---|
| Create Points | `core.create_points` | Point from x/y fields in `crs`, optionally reprojected to `output_crs`. A missing coordinate gives a null point. |
| Distance | `core.distance` | Geodesic or planar distance between two geometry fields. Exact between points, with initial bearing. Other geometries use the distance between their nearest points, and intersecting geometries are 0. |
| Buffer | `core.buffer` | Fixed or per-record (`distance_field`) buffer. Negative distances shrink polygons. `resolution` sets segments per quarter circle. |
| Find Nearest | `core.find_nearest` | The `max_matches` nearest universe records per target, optionally within `max_distance`, with Distance and Rank. Ties go to universe order. Geographic point universes use a 3-D KD-tree for candidates, then an exact geodesic re-check whose search radius guarantees no closer point is missed. Projected data uses vectorised GEOS distances. `ignore_identical` skips self-matches. Unmatched targets have their own output. |
| Spatial Match | `core.spatial_match` | Target × universe pairs where `target <predicate> universe` holds: intersects, contains, within, touches, overlaps, crosses, covers, covered_by or equals. Uses an STRtree index. Unmatched targets are output separately. |
| Spatial Info | `core.spatial_info` | Type, validity and GEOS reason, emptiness, point count, bounding box, centroid, area and length or perimeter, and CRS. Invalid geometries are reported, not rejected. |
| Make Grid | `core.make_grid` | Square cells over input bounds or an explicit extent. Size is in CRS units or a metric length. On a geographic CRS, metric cells are built in the UTM zone of the extent centre and returned in `crs`. `clip` keeps cells that touch the input. `max_cells` guards size. |

Universe fields whose names clash with target fields get the prefix `U_`. Invalid geometry stops
the run, naming the record, unless `on_invalid: null`.

Example: `examples/spatial/uk_spatial.flow.json` (UK city coordinates and illustrative depots). Tests:
`tests/test_spatial.py`. They check against haversine distances, spherical cell areas, a
brute-force geodesic nearest-neighbour search, British National Grid reprojection and exact
regular-polygon buffer areas.

Not implemented: Alteryx SpatialObj binary encoding, Spatial XML import (instances keep their XML),
map rendering, and the other Spatial tools (Trade Area, Poly-Build, Generalize and others).
