"""Built-in providers. Each entry says what it serves, what it needs, and what has been verified."""
from .providers import Provider, register

register(Provider(
    id='r', family='r', name='R (Rscript)', kind='local-runtime', serves=('developer:r',), licence='GPL-2.0-or-later',
    needs=('rscript',), homepage='https://www.r-project.org',
    semantics={'exchange': 'CSV; output fields are text'},
    setup='Install R from CRAN, or set FLOWENGINE_RSCRIPT to the Rscript executable.',
    verification='Runtime-gated test runs in the r-runtime CI job against R 4.6.0.'))

register(Provider(
    id='spark-local', family='spark', name='Apache Spark (local mode)', kind='local-runtime',
    serves=('developer:apache-spark-code',), licence='Apache-2.0', needs=('python:pyspark', 'exe:java'),
    homepage='https://spark.apache.org',
    semantics={'execution': 'Spark local mode in one machine', 'languages': ['python'], 'timezone': 'UTC',
               'types': 'Flowbench types mapped to Spark SQL types and back'},
    setup='pip install pyspark into the Python environment the tool uses, and install a Java 17+ runtime.',
    verification='Runtime-gated test runs in the spark-runtime CI job (PySpark with Java 17). '
                 'Remote clusters (Livy, Databricks) are not served by this provider.'))

register(Provider(
    id='alteryx-server', family='alteryx-server', name='Alteryx Server (Web API)', kind='vendor',
    serves=('developer:server-api',), needs=('entitlement:Alteryx Server', 'connection:web'),
    homepage='https://help.alteryx.com/current/en/server/api-overview.html',
    semantics={'auth': 'OAuth 2.0 client credentials at <base>/oauth2/token, bearer token per call',
               'parsing': 'HTTP 200 JSON parsed one level deep; nested values kept as JSON text'},
    setup='Record your Alteryx Server licence, then add a Web/API connection: base URL https://<server>/webapi, '
          'basic authentication, the API access key as username and the API secret as the secret.',
    verification='Checked against a controlled local fixture of the documented token flow and v3 list/detail calls. '
                 'Live acceptance against a real Alteryx Server is pending (no server or API key available).'))

register(Provider(
    id='valhalla', family='routing', name='Valhalla routing server', kind='open-source',
    serves=('spatial:non-overlap-drivetime',), licence='MIT', needs=('connection:web',),
    homepage='https://github.com/valhalla/valhalla',
    semantics={'crs': 'EPSG:4326', 'travel_model': 'Valhalla costing (auto, truck, bicycle, pedestrian) with its own speeds',
               'coverage': 'the road data the server was built from, usually an OpenStreetMap extract (ODbL)',
               'overlap': 'grid cells assigned to the nearest point by straight-line distance'},
    setup='Run a Valhalla server over the region you need (for example the ghcr.io/valhalla/valhalla-scripted Docker '
          'image with an OpenStreetMap extract), then add a Web/API connection to it, for example http://localhost:8002 '
          'with authentication None. Register the extract under Data packs to record its licence.',
    verification='Checked against a controlled local fixture of the documented /isochrone API (polygons=true). '
                 'Live acceptance against a real Valhalla build is pending; areas are not expected to match the '
                 "reference's licensed drivetime dataset."))

register(Provider(
    id='alteryx-license-portal', family='alteryx-license-portal', name='Alteryx Licensing & Downloads Portal API',
    kind='vendor', serves=('developer:license-api',), needs=('entitlement:Alteryx Designer', 'connection:web'),
    homepage='https://us1.alteryxcloud.com/license-portal/api/swagger-ui/index.html',
    semantics={'endpoints': 'published OpenAPI document (Licenses API 1.0.0), for example v1/devices, v2/licenses',
               'auth': 'bearer access token from the refresh token (OAuth 2.0 refresh grant at the token URL)',
               'parsing': 'HTTP 200 JSON with nested objects flattened to dotted field names'},
    setup='On the portal API Access page select Generate tokens. Add a Web/API connection with base URL '
          'https://us1.alteryxcloud.com/license-portal/api, Bearer authentication, the refresh token as the secret, '
          "and the portal's Authentication URL as the token URL.",
    verification='Checked against a controlled local fixture of the published endpoints with the RFC 6749 refresh grant. '
                 "The portal does not publish its token exchange, so live acceptance against the real portal is pending "
                 '(no portal account available).'))
