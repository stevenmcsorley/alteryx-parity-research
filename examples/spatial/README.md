# UK cities and depots (spatial example)

`uk_spatial.flow.json` makes points from latitude/longitude (EPSG:4326), measures the geodesic distance from
each city to London, buffers each city by 50 km, finds the two nearest cities to each depot, matches
depots that fall within a city's 50 km area, reports the geodesic area of the buffers (about 7,850 km²
each) and builds a 100 km grid clipped to the cities. Each result feeds a Browse node.

`data/uk_cities.csv` holds approximate city-centre coordinates. `data/depots.csv` holds illustrative
depot locations invented for this example.
