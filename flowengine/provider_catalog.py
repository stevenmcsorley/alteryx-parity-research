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

register(Provider(
    id='amazon-s3', family='object-storage', name='Amazon S3 (and S3-compatible storage)', kind='vendor',
    serves=('connectors:amazon-s3-download', 'connectors:amazon-s3-upload'), needs=('connection:web',),
    homepage='https://docs.aws.amazon.com/AmazonS3/latest/API/Welcome.html',
    semantics={'auth': 'AWS Signature Version 4, header signing with the payload hash',
               'addressing': 'path-style <endpoint>/<bucket>/<key>',
               'formats': 'whatever Input Data reads / Output Data writes'},
    setup='Add a Web/API connection: base URL https://s3.<region>.amazonaws.com (or your S3-compatible endpoint), '
          'authentication AWS Signature V4, the access key id as username and the secret access key as the secret.',
    verification='Signing checked against the AWS documentation example signature; download, upload and multipart '
                 'run live against MinIO (an S3-compatible server) in the service-integration CI job. '
                 'Live acceptance against AWS itself is pending (no AWS account available).'))

register(Provider(
    id='mongodb', family='document-database', name='MongoDB', kind='open-source',
    serves=('connectors:mongodb-input', 'connectors:mongodb-output'), licence='SSPL-1.0 (server); Apache-2.0 (pymongo driver)',
    needs=('python:pymongo',), homepage='https://www.mongodb.com/docs/drivers/pymongo/',
    semantics={'reads': 'find with filter, projection, sort and limit in MongoDB Extended JSON',
               'types': 'top-level scalars keep their type; ObjectId as text; nested documents and arrays as JSON text; '
                        'dates in UTC',
               'writes': 'insert, upsert on key fields, or replace the collection'},
    setup='Add a database connection of type MongoDB under Databases (host, port, database, user; the password goes '
          'to the OS vault). Set the connection option auth_source if the user is not defined in admin.',
    verification='Reads and writes run live against MongoDB 7.0 in the service-integration CI job.'))

register(Provider(
    id='microsoft-graph', family='microsoft-365', name='Microsoft 365 (Microsoft Graph)', kind='vendor',
    serves=('connectors:onedrive-input', 'connectors:onedrive-output', 'connectors:sharepoint-files-input',
            'connectors:sharepoint-files-output', 'connectors:sharepoint-list-input', 'connectors:sharepoint-list-output',
            'connectors:outlook-365-input'),
    needs=('connection:web',), homepage='https://learn.microsoft.com/en-us/graph/api/overview',
    semantics={'api': 'Microsoft Graph v1.0', 'auth': 'OAuth 2.0: client credentials, or refresh token from device sign-in',
               'files': 'read as Input Data / written as Output Data; uploads over 4 MB use an upload session',
               'lists': 'column internal names; dates in UTC', 'mail': 'newest first; plain-text body'},
    setup='Register an app in Microsoft Entra ID. Add a Web/API connection: base URL https://graph.microsoft.com/v1.0, '
          'authentication OAuth, token URL https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token, the client id, '
          'and either the client secret with scope https://graph.microsoft.com/.default (app-only), or device sign-in URL '
          'https://login.microsoftonline.com/<tenant>/oauth2/v2.0/devicecode with scopes such as '
          '"Files.ReadWrite Sites.ReadWrite.All Mail.Read offline_access", then Sign in.',
    verification='Checked against a controlled local fixture of the documented Graph and Microsoft identity platform '
                 'endpoints (token grants, device code, drive items, upload sessions, list items, messages, paging, '
                 'throttling). Live acceptance against a Microsoft 365 tenant is pending (no tenant available).'))

register(Provider(
    id='microsoft-dataverse', family='microsoft-dataverse', name='Microsoft Dataverse / Dynamics 365 (Web API)', kind='vendor',
    serves=('connectors:dataverse-input', 'connectors:dataverse-output', 'connectors:dynamics-crm-input',
            'connectors:dynamics-crm-output'),
    needs=('connection:web',), homepage='https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/overview',
    semantics={'api': 'Dataverse Web API v9.2 (OData v4); Dynamics 365 apps store their data in Dataverse',
               'reads': 'entity set with $select/$filter/$orderby, paged with odata.maxpagesize',
               'writes': 'create, update (If-Match: *), upsert by primary or alternate key, delete',
               'types': 'lookups as _<name>_value ids; dates in UTC'},
    setup='Register an app in Microsoft Entra ID with Dataverse access. Add a Web/API connection: base URL '
          'https://<org>.crm.dynamics.com/api/data/v9.2, OAuth, token URL https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token, '
          'the client id, and either a client secret with scope https://<org>.crm.dynamics.com/.default, or device sign-in.',
    verification='Checked against a controlled local fixture of the documented Web API (paging, create, update, upsert by '
                 'alternate key, delete). Live acceptance against a Dataverse environment is pending (none available).'))

register(Provider(
    id='microsoft-power-bi', family='microsoft-power-bi', name='Microsoft Power BI (push datasets)', kind='vendor',
    serves=('connectors:microsoft-power-bi-output',), needs=('connection:web',),
    homepage='https://learn.microsoft.com/en-us/rest/api/power-bi/push-datasets',
    semantics={'api': 'Power BI REST API v1.0 push datasets', 'batches': 'at most 10,000 rows per request',
               'limits': '75 columns; 4,000 characters per text value', 'types': 'Int64, Double, Decimal, Boolean, Datetime, String'},
    setup='Register an app in Microsoft Entra ID with the Power BI Service Dataset.ReadWrite.All permission. Add a Web/API '
          'connection: base URL https://api.powerbi.com/v1.0/myorg, OAuth, token URL '
          'https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token, the client id, and device sign-in with scope '
          '"https://analysis.windows.net/powerbi/api/Dataset.ReadWrite.All offline_access" (push datasets do not work with '
          'service principal profiles).',
    verification='Checked against a controlled local fixture of the documented push dataset endpoints. Live acceptance '
                 'against a Power BI workspace is pending (none available).'))

register(Provider(
    id='google', family='google-cloud', name='Google (Drive, Sheets, BigQuery, Analytics 4)', kind='vendor',
    serves=('connectors:google-drive-input', 'connectors:google-drive-output', 'connectors:google-sheets-input',
            'connectors:google-sheets-output', 'connectors:google-bigquery-input', 'connectors:google-bigquery-output',
            'connectors:google-analytics-4-input'),
    needs=('connection:web', 'python:cryptography'), homepage='https://developers.google.com/workspace',
    semantics={'apis': 'Drive v3, Sheets v4, BigQuery v2, Analytics Data v1beta (one connection per API host)',
               'auth': 'OAuth 2.0: browser sign-in (code + PKCE, loopback) or a service account key (JWT bearer)',
               'bigquery': 'GoogleSQL; TIMESTAMP as UTC; RECORD/REPEATED as JSON text; loads are load jobs (all or nothing)',
               'sheets': 'unformatted values; dates as displayed text', 'drive': 'Google Sheets files exported as Excel'},
    setup='Create an OAuth client (Desktop app) or a service account in Google Cloud and enable the API. Add a Web/API '
          'connection per API: base URL https://www.googleapis.com (Drive), https://sheets.googleapis.com, '
          'https://bigquery.googleapis.com or https://analyticsdata.googleapis.com; OAuth; token URL '
          'https://oauth2.googleapis.com/token; authorization URL https://accounts.google.com/o/oauth2/v2/auth; the client id '
          'and client secret; the scope; then Sign in in browser. For a service account, paste its JSON key as the secret.',
    verification='Checked against a controlled local fixture of the documented endpoints, including RS256 service-account '
                 'JWTs verified with the public key, PKCE, resumable uploads (308) and paging. Live acceptance against Google '
                 'is pending (no Google Cloud project available).'))

register(Provider(
    id='box', family='box', name='Box', kind='vendor', serves=('connectors:box-input', 'connectors:box-output'),
    needs=('connection:web',), homepage='https://developer.box.com/reference/',
    semantics={'api': 'Box Content API 2.0', 'files': 'read as Input Data / written as Output Data; existing files get a new version',
               'downloads': 'pre-signed links fetched without the token'},
    setup='Create a Box app (OAuth 2.0). Add a Web/API connection: base URL https://api.box.com/2.0, OAuth, token URL '
          'https://api.box.com/oauth2/token, authorization URL https://account.box.com/api/oauth2/authorize, the client id '
          'and secret, then Sign in in browser.',
    verification='Checked against a controlled local fixture of the documented endpoints (folder items, content redirect, '
                 'upload host). Live acceptance against Box is pending (no Box account available).'))

register(Provider(
    id='dropbox', family='dropbox', name='Dropbox', kind='vendor', serves=('connectors:dropbox-input', 'connectors:dropbox-output'),
    needs=('connection:web',), homepage='https://www.dropbox.com/developers/documentation/http/documentation',
    semantics={'api': 'Dropbox API v2', 'files': 'read as Input Data / written as Output Data; upload sessions above 8 MB'},
    setup='Create a Dropbox app with files.content.read/write. Add a Web/API connection: base URL https://api.dropboxapi.com/2, '
          'OAuth, token URL https://api.dropboxapi.com/oauth2/token, authorization URL https://www.dropbox.com/oauth2/authorize, '
          'the app key as client id (and app secret), then Sign in in browser.',
    verification='Checked against a controlled local fixture of the documented endpoints (download, upload, upload sessions, '
                 'conflicts). Live acceptance against Dropbox is pending (no Dropbox account available).'))

register(Provider(
    id='azure-data-lake', family='azure-data-lake', name='Azure Data Lake Storage Gen2', kind='vendor',
    serves=('connectors:azure-data-lake-file-input', 'connectors:azure-data-lake-file-output'), needs=('connection:web',),
    homepage='https://learn.microsoft.com/en-us/rest/api/storageservices/data-lake-storage-gen2',
    semantics={'api': 'Data Lake Storage Gen2 REST (dfs endpoint, x-ms-version 2023-11-03)',
               'writes': 'create, append in 8 MB chunks, flush; If-None-Match: * when the file must not exist'},
    setup='Grant your app (or user) Storage Blob Data Contributor on the account. Add a Web/API connection: base URL '
          'https://<account>.dfs.core.windows.net, OAuth, token URL https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token, '
          'the client id, and a client secret with scope https://storage.azure.com/.default (or device sign-in).',
    verification='Checked against a controlled local fixture of the documented path operations. Live acceptance against an '
                 'Azure storage account is pending (none available).'))

register(Provider(
    id='salesforce', family='salesforce', name='Salesforce (REST API)', kind='vendor',
    serves=('connectors:salesforce-input', 'connectors:salesforce-output-new'), needs=('connection:web',),
    homepage='https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/',
    semantics={'reads': 'SOQL query / queryAll, all pages; relationship fields as dotted names; date-times in UTC',
               'writes': 'sObject Collections, 200 per request; per-record id, success and errors'},
    setup='Create a connected app (OAuth, PKCE). Add a Web/API connection: base URL https://<domain>.my.salesforce.com, '
          'OAuth, token URL https://login.salesforce.com/services/oauth2/token, authorization URL '
          'https://login.salesforce.com/services/oauth2/authorize, the consumer key as client id, scope "api refresh_token", '
          'then Sign in in browser (or use the client credentials flow with the consumer secret).',
    verification='Checked against a controlled local fixture of the documented query and sObject Collections endpoints. '
                 'Live acceptance against a Salesforce org is pending (no org available).'))

register(Provider(
    id='servicenow', family='servicenow', name='ServiceNow (Table API)', kind='vendor',
    serves=('connectors:servicenow-input',), needs=('connection:web',),
    homepage='https://www.servicenow.com/docs/bundle/xanadu-api-reference/page/integrate/inbound-rest/concept/c_TableAPI.html',
    semantics={'reads': 'Table API with an encoded query, offset paging', 'types': 'all values as text (stored values by default)'},
    setup='Add a Web/API connection: base URL https://<instance>.service-now.com, with basic authentication (an integration '
          'user) or OAuth (token URL https://<instance>.service-now.com/oauth_token.do).',
    verification='Checked against a controlled local fixture of the documented Table API. Live acceptance against a '
                 'ServiceNow instance is pending (none available).'))

register(Provider(
    id='marketo', family='marketo', name='Adobe Marketo Engage (REST API)', kind='vendor',
    serves=('connectors:marketo-input', 'connectors:marketo-output', 'connectors:marketo-append'), needs=('connection:web',),
    homepage='https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/rest-api',
    semantics={'reads': 'leads by filter (300 values per request) or activities since a date, every page',
               'writes': 'leads.json createOrUpdate / createOnly / updateOnly / createDuplicate, 300 per request',
               'errors': '601/602 renew the token once; 606 waits; other codes stop the run'},
    setup='In Marketo Admin create a LaunchPoint custom service for an API user. Add a Web/API connection: base URL '
          'https://<munchkin-id>.mktorest.com/rest, OAuth, token URL https://<munchkin-id>.mktorest.com/identity/oauth/token, '
          'the client id, and the client secret.',
    verification='Checked against a controlled local fixture of the documented identity and REST endpoints. Live acceptance '
                 'against a Marketo instance is pending (none available).'))

register(Provider(
    id='azure-ai-language', family='azure-ai-language', name='Azure AI Language', kind='vendor',
    serves=('connectors:cognitive-services-text-analytics',), needs=('connection:web',),
    homepage='https://learn.microsoft.com/en-us/azure/ai-services/language-service/overview',
    semantics={'api': 'analyze-text, api-version 2023-04-01 (synchronous)',
               'limits': '5,120 characters per text; batches of 10 (sentiment, key phrases), 5 (entities), 1000 (language)'},
    setup='Create a Language resource in Azure. Add a Web/API connection: base URL https://<resource>.cognitiveservices.azure.com, '
          'authentication API key header with header name Ocp-Apim-Subscription-Key and the key as the secret (or OAuth with '
          'scope https://cognitiveservices.azure.com/.default).',
    verification='Checked against a controlled local fixture of the documented analyze-text request and responses. Live '
                 'acceptance against an Azure Language resource is pending (none available).'))

register(Provider(
    id='power-automate', family='power-automate', name='Microsoft Power Automate (HTTP request trigger)', kind='vendor',
    serves=('connectors:power-automate',), needs=('connection:web',),
    homepage='https://learn.microsoft.com/en-us/power-automate/oauth-authentication',
    semantics={'calls': 'JSON POST per record or one array per run', 'auth': 'signed trigger URL (sig) or Microsoft Entra OAuth'},
    setup='In the flow add "When an HTTP request is received" and copy its URL. Add a Web/API connection: authentication '
          'Signed URL, base URL the part before "?", and the part after "?" as the secret (or OAuth for flows restricted to '
          'Microsoft Entra users).',
    verification='Checked against a controlled local fixture of an HTTP request trigger. Live acceptance against a real flow '
                 'is pending (no Power Automate environment available).'))

register(Provider(
    id='odbc', family='odbc', name='ODBC (pyodbc and a system driver)', kind='local-runtime',
    serves=('connect:odbc-loader',), licence='MIT (pyodbc)', needs=('python:pyodbc',),
    homepage='https://github.com/mkleehammer/pyodbc',
    semantics={'reads': 'catalog functions SQLTables, SQLColumns, SQLProcedures through the driver',
               'types': 'type names, sizes and nullability as the driver reports them'},
    setup='pip install pyodbc into the engine Python; install the ODBC driver manager (built into Windows; unixODBC on '
          'Linux and macOS) and the database\'s ODBC driver. Add a database connection of type ODBC with options.driver '
          '(or options.dsn).',
    verification='Live against the SQLite ODBC driver through unixODBC in the service-integration CI job.'))

register(Provider(
    id='anaplan', family='anaplan', name='Anaplan (Integration API v2)', kind='vendor',
    serves=('connectors:anaplan-input', 'connectors:anaplan-output'), needs=('connection:web',),
    homepage='https://help.anaplan.com/',
    semantics={'reads': 'export action, then the export file downloaded in chunks', 'writes': 'chunked file upload, then the import action',
               'results': 'a COMPLETE task is checked for success; failure dumps are reported'},
    setup='Add a Web/API connection: base URL https://api.anaplan.com/2/0, basic authentication with the Anaplan user '
          'email and password (single sign-on users must be exception users).',
    verification='Checked against a controlled local fixture of the documented authentication, files, chunks, imports, '
                 'exports and task endpoints. Live acceptance against an Anaplan model is pending (none available).'))

register(Provider(
    id='tableau', family='tableau', name='Tableau (Hyper API and REST API)', kind='vendor',
    serves=('connectors:tableau-output', 'connectors:publish-to-tableau-server'), licence='Apache-2.0 (tableauhyperapi)',
    needs=('python:tableauhyperapi',), homepage='https://tableau.github.io/hyper-db/',
    semantics={'extracts': 'one table Extract.Extract; WKT/GeoJSON fields as GEOGRAPHY',
               'publishing': 'REST API data source publish (overwrite or append), chunked above 64 MB'},
    setup='pip install tableauhyperapi into the engine Python (Windows or Linux x86-64). To publish, create a personal '
          'access token and add a Web/API connection: base URL your Tableau Server or Tableau Cloud address, basic '
          'authentication with the token name as username and the token secret as the secret.',
    verification='Hyper extracts written and read back with the real Hyper API in the service-integration CI job; '
                 'publishing checked against a controlled fixture of the documented REST endpoints. Live publishing to '
                 'Tableau Server or Cloud is pending (no site available).'))

register(Provider(
    id='us-census-geocoder', family='geocoding', name='US Census Bureau Geocoder', kind='open-source',
    serves=('address:street-geocoder', 'address:us-geocoder', 'address:reverse-geocoder'), licence='Public domain (US Government data)',
    needs=('connection:web',), homepage='https://geocoding.geo.census.gov/geocoder/',
    semantics={'coverage': 'United States, Puerto Rico and island areas; TIGER/Line address ranges (interpolated)',
               'batches': 'up to 10,000 addresses per request', 'reverse': 'census geographies (state, county, tract, block), not addresses'},
    setup='Add a Web/API connection: base URL https://geocoding.geo.census.gov/geocoder, authentication None.',
    verification='Checked against a controlled fixture of the documented batch and coordinates endpoints, and live against the '
                 'public service during development.'))

register(Provider(
    id='nominatim', family='geocoding', name='Nominatim (OpenStreetMap)', kind='open-source',
    serves=('address:street-geocoder', 'address:us-geocoder', 'address:reverse-geocoder'),
    licence='GPL-2.0 (software); ODbL-1.0 (OpenStreetMap data)', needs=('connection:web',),
    homepage='https://nominatim.org',
    semantics={'coverage': 'worldwide, as complete as OpenStreetMap', 'public server': 'one request per second, no bulk geocoding'},
    setup='Run your own Nominatim (recommended for more than a few hundred addresses), or use https://nominatim.openstreetmap.org '
          'within its usage policy. Add a Web/API connection with that base URL and authentication None.',
    verification='Checked against a controlled fixture of the documented search and reverse endpoints. Live acceptance against '
                 'a Nominatim server is pending.'))

register(Provider(
    id='smarty', family='address-validation', name='Smarty US Street API (USPS CASS-certified)', kind='vendor',
    serves=('address:cass',), needs=('connection:web',), homepage='https://www.smarty.com/docs/cloud/us-street-api',
    semantics={'validation': 'USPS CASS-certified standardisation, ZIP+4, delivery point, DPV', 'batches': '100 addresses per request'},
    setup='Create Smarty secret keys. Add a Web/API connection: base URL https://us-street.api.smarty.com, basic authentication '
          'with the auth-id as username and the auth-token as the secret.',
    verification='Checked against a controlled fixture of the documented API. Live acceptance is pending (no Smarty account available).'))

register(Provider(
    id='zip4-data-pack', family='zip4-centroids', name='ZIP+4 centroid data pack', kind='local-runtime',
    serves=('address:us-zip9-coder',), needs=('datapack:zip4-centroids',),
    semantics={'source': 'the licensed ZIP+4 centroid file you register; ZIP5 centroids used when the +4 is unknown'},
    setup='Register your licensed ZIP+4 centroid CSV (columns Zip5, Plus4, Latitude, Longitude) under Data packs as family '
          'zip4-centroids.',
    verification='Checked with a synthetic data pack. Results depend on the data you supply.'))

register(Provider(
    id='us-census-acs', family='demographics', name='US Census Bureau American Community Survey (API)', kind='open-source',
    serves=('demographic-analysis:allocate-input', 'demographic-analysis:allocate-append', 'demographic-analysis:allocate-metainfo',
            'demographic-analysis:allocate-report'),
    licence='Public domain (US Government data)', needs=('connection:web',), homepage='https://www.census.gov/data/developers.html',
    semantics={'data': "ACS estimates (not the reference's licensed demographic dataset); the vintage you choose",
               'geographies': 'state, county, tract, block group, ZIP code tabulation area by GEOID; no custom polygons',
               'nulls': "the Bureau's not-available codes become null"},
    setup='Request a free key at https://api.census.gov/data/key_signup.html. Add a Web/API connection: base URL '
          'https://api.census.gov/data, authentication Signed URL, secret key=<your key>.',
    verification='Checked against a controlled fixture of the documented data, variables and groups endpoints. The data API now '
                 'requires a key; live acceptance is pending (no key available here; keys are free).'))

register(Provider(
    id='segmentation-data-pack', family='segmentation', name='Segmentation data pack (your licensed or own segmentation)',
    kind='local-runtime',
    serves=tuple(f'behavior-analysis-deprecated:{t}' for t in ('create-profile', 'profile-output', 'profile-input', 'compare-behavior',
                                                              'detail-fields', 'cluster-code', 'behavior-metainfo',
                                                              'profile-comparison-report', 'profile-rank-report', 'profile-detail-report')),
    needs=('datapack:segmentation',),
    semantics={'clusters': 'the codes, names and counts in the data pack you register (not the reference segmentation)',
               'index': '100 x target share / base share; penetration = target count / base count'},
    setup='Register a CSV with GeoKey, Cluster and Count columns (optionally ClusterName and Group) under Data packs as family '
          'segmentation. Cluster Code and geography-key profiles need it; profiles from cluster codes in your records do not.',
    verification='Checked with a synthetic segmentation data pack; index, share and penetration arithmetic asserted by hand.'))

register(Provider(
    id='openai-compatible', family='llm', name='OpenAI-compatible model service (OpenAI, Ollama, LM Studio, vLLM, ...)', kind='open-source',
    serves=tuple(f'ai-tools:{t}' for t in ('llm-override', 'prompt', 'precision-match', 'synthetic-input', 'invoice-extractor', 'schema-fit')),
    needs=('connection:web',), homepage='https://platform.openai.com/docs/api-reference/chat',
    semantics={'api': 'Chat Completions; structured output with response_format json_schema, validated by Flowbench',
               'attachments': 'images; PDFs rendered to page images', 'results': 'model output varies by model and run'},
    setup='For a local model, run Ollama (or LM Studio) and add a Web/API connection: base URL http://localhost:11434/v1, '
          'authentication None. For OpenAI: base URL https://api.openai.com/v1, Bearer token with your API key.',
    verification='Checked against a controlled fixture, and live against a local Ollama model (qwen2.5) during development and '
                 'in the llm-runtime CI job.'))

register(Provider(
    id='anthropic', family='llm', name='Anthropic (Messages API)', kind='vendor',
    serves=tuple(f'ai-tools:{t}' for t in ('llm-override', 'prompt', 'precision-match', 'synthetic-input', 'invoice-extractor', 'schema-fit')),
    needs=('connection:web',), homepage='https://docs.anthropic.com/en/api/messages',
    semantics={'api': 'Messages API (anthropic-version 2023-06-01); JSON answers validated by Flowbench',
               'attachments': 'images and PDFs (document blocks)'},
    setup='Add a Web/API connection: base URL https://api.anthropic.com, authentication API key header with header name '
          'x-api-key and your API key as the secret.',
    verification='Checked against a controlled fixture of the documented Messages API. Live acceptance is pending (no API key here).'))

register(Provider(
    id='datarobot', family='datarobot', name='DataRobot (API v2)', kind='vendor',
    serves=('predictive:datarobot-automodel', 'predictive:datarobot-predict'), needs=('connection:web',),
    homepage='https://docs.datarobot.com/en/docs/api/reference/public-api/index.html',
    semantics={'modelling': "DataRobot's Autopilot and models, not Flowbench's", 'async': 'polled through Location until redirected'},
    setup='In DataRobot, create an API key (Developer Tools). Add a Web/API connection: base URL https://app.datarobot.com/api/v2 '
          '(or your instance), Bearer token authentication with the API key.',
    verification='Checked against a controlled fixture of the documented dataset, project, Autopilot and prediction endpoints. '
                 'Live acceptance is pending (no DataRobot account available).'))

register(Provider(
    id='adobe-analytics', family='adobe-analytics', name='Adobe Analytics (Reporting API 2.0)', kind='vendor',
    serves=('connectors:adobe-analytics',), needs=('connection:web',),
    homepage='https://developer.adobe.com/analytics-apis/docs/2.0/',
    semantics={'api': 'Reporting API 2.0 (the reference tool uses 1.4)', 'reports': 'metrics by one dimension or totals, paged'},
    setup='Create an OAuth server-to-server (or user) credential in the Adobe Developer Console with the Analytics API. Add a '
          'Web/API connection: base URL https://analytics.adobe.io/api/<global-company-id>, OAuth, token URL '
          'https://ims-na1.adobelogin.com/ims/token/v3, the client id and secret, and the scopes from the console.',
    verification='Checked against a controlled fixture of the documented reports endpoint. Live acceptance is pending '
                 '(no Adobe Analytics account available).'))
