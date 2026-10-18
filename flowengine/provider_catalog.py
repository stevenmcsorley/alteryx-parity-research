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
