# Integrations & Licences

Some tools delegate to something outside Flowbench: a runtime (R, Spark), an open-source service (a routing or geocoding server) or a vendor service (Alteryx Server, DataRobot). **Settings → Integrations & Licences** (the Integrations button in the toolbar) gathers everything these tools need.

| Section | What it holds | Where it is stored |
|---|---|---|
| Connections | Database, Web/API, GitHub and SMTP connections | `connections.json`, `service_connections.json`; secrets in the OS credential vault |
| Licences | Vendor licences you declare you hold | `entitlements.json`; marked *declared, not validated* |
| Providers | Every registered provider, what it needs, its licence, what its results mean, what has been verified | `flowengine/provider_catalog.py` |
| Data packs & runtimes | Local datasets a provider reads (licence, size, SHA-256), Python environments, R and Java | `data_packs.json` |
| Tool availability | For each provider-backed tool, which providers can serve it and what is missing | computed |

All files live in the Flowbench home folder (`FLOWENGINE_HOME`, default `~/.flowbench`).

## Rules

- **A node names its provider.** A provider-backed tool has a `provider` setting and, when needed, a `connection_id`. Flowbench never picks a provider and never falls back to another one.
- **No secrets in workflows.** Nodes refer to connections by id. Secrets are only ever written to the existing credential vault.
- **Semantics stay visible.** Each provider describes what its results mean (coordinate system, coverage, time zone, type mapping). A node may record the provider's `provider_semantics` fingerprint. If the provider's description later changes, the node fails validation until someone reviews it.
- **Licences are declared, not checked.** Flowbench does not call any vendor to validate an entitlement. A declaration only makes the tool available; the vendor's own sign-in still applies when the tool connects.
- **Data packs are not redistributed.** Registering a pack records where it is and under what licence. Flowbench never copies it.

## Providers in this release

| Provider | Serves | Needs | Verified |
|---|---|---|---|
| `r` (R, GPL-2.0-or-later) | R | Rscript | Runtime-gated test in the `r-runtime` CI job (R 4.6.0) |
| `spark-local` (Apache Spark, Apache-2.0) | Apache Spark Code | pyspark, Java 17+ | Runtime-gated test in the `spark-runtime` CI job (PySpark 4.0.1, Java 17) |

| `alteryx-server` (vendor) | Server API | a declared Alteryx Server licence; a Web/API connection with basic auth (API key and secret) | Controlled fixture of the documented token flow and v3 calls. **Live acceptance pending:** no Alteryx Server is available |
| `alteryx-license-portal` (vendor) | License API | a declared Alteryx Designer licence; a Web/API connection with bearer auth (the refresh token) and a token URL | Controlled fixture of the published OpenAPI endpoints with the RFC 6749 refresh grant. **Live acceptance pending:** no portal account, and the portal does not publish its token exchange |
| `valhalla` (open source, MIT) | Non Overlap Drivetime | a Web/API connection to your Valhalla server (authentication can be None) | Controlled fixture of the documented `/isochrone` API. **Live acceptance pending:** a real Valhalla build. Areas will not match the reference's licensed drivetime data |
| `amazon-s3` (vendor; any S3-compatible store) | Amazon S3 Download, Amazon S3 Upload | a Web/API connection with AWS Signature V4 (access key id as username, secret access key as secret) | Signer reproduces the AWS documentation example signature. Download, upload, multipart and a rejected wrong secret run live against SeaweedFS, which checks signatures, in the `service-integration` CI job. **Live acceptance against AWS is pending:** no AWS account is available |
| `mongodb` (open source) | MongoDB Input, MongoDB Output | a database connection of type MongoDB; pymongo | Live against MongoDB 7.0 in the `service-integration` CI job |
| `microsoft-graph` (vendor) | OneDrive Input/Output, SharePoint Files Input/Output, SharePoint List Input/Output, Outlook 365 Input | a Web/API connection with OAuth 2.0 (app secret, or device sign-in) | Controlled fixture of the documented identity platform and Graph endpoints. **Live acceptance against a Microsoft 365 tenant is pending:** no tenant is available |
| `microsoft-dataverse` (vendor) | Dataverse Input/Output, Dynamics CRM Input/Output | a Web/API connection with OAuth 2.0 | Controlled fixture of the documented Web API. **Live acceptance against a Dataverse environment is pending** |
| `microsoft-power-bi` (vendor) | Microsoft Power BI Output | a Web/API connection with OAuth 2.0 (device sign-in) | Controlled fixture of the documented push-dataset API. **Live acceptance against a Power BI workspace is pending** |
| `google` (vendor) | Google Drive Input/Output, Google Sheets Input/Output, Google BigQuery Input/Output, Google Analytics 4 Input | one Web/API connection with OAuth 2.0 per Google API host; browser sign-in or a service account key | Controlled fixture of the documented endpoints, with service-account JWTs verified against the public key. **Live acceptance against Google is pending:** no Google Cloud project is available |
| `box`, `dropbox`, `azure-data-lake` (vendor) | Box, Dropbox and Azure Data Lake File Input/Output | a Web/API connection with OAuth 2.0 | Controlled fixtures of the documented APIs. **Live acceptance is pending** for each: no Box, Dropbox or Azure storage account is available |
| `salesforce`, `servicenow` (vendor) | Salesforce Input/Output, ServiceNow Input | a Web/API connection: OAuth for Salesforce; basic auth or OAuth for ServiceNow | Controlled fixtures of the documented REST APIs. **Live acceptance is pending:** no Salesforce org or ServiceNow instance is available |
| `marketo`, `azure-ai-language`, `power-automate` (vendor) | Marketo Input/Output/Append, Cognitive Services Text Analytics, Power Automate | a Web/API connection: OAuth (Marketo), API key header or OAuth (Language), Signed URL or OAuth (Power Automate) | Controlled fixtures of the documented APIs. **Live acceptance is pending:** no instances are available |

Apache Spark Code runs PySpark in local mode. `read_input(n)` returns a Spark DataFrame whose types are mapped from Flowbench types. `write_output(n, df)` collects the DataFrame and maps its types back exactly; types Flowbench cannot hold are rejected with a message. Remote clusters (Livy, Databricks) are not served by this provider.

## Vendor APIs (Server API, License API)

- Each record on the optional Data input makes one call. `{field}` placeholders in the endpoint, query and payload are filled from that record.
- Results go to three outputs: Success (HTTP 200), Fail (any other status) and Log (one line per call, with the full response).
- The token never travels through the workflow, so there are no authorization anchors. The reference tools pass the token between tools in the data stream; Flowbench keeps it in the connection.
- Permissions: GET needs the network permission. POST, PUT and PATCH also need remote writes. DELETE also needs `confirm_delete`.
- Server API keeps nested values as JSON text (parsed one level deep). License API flattens nested objects into dotted field names.
- A refresh token is sent only to the token URL saved with the connection, never to a URL from the workflow. A refresh token the server rotates is saved back to the vault.

## OAuth 2.0 connections

A Web/API connection with authentication **OAuth 2.0** holds three settings:
- the token URL;
- the client (application) id;
- the scopes, and optionally a device sign-in URL.

Its vault secret holds what the grant needs:
- **App-only access:** type the client secret. Flowbench uses the client credentials grant.
- **Signing in as yourself:** save the connection, then choose **Sign in…**. Flowbench shows a code; you enter it at the provider's page in any browser. The refresh token it receives is saved in the vault. This is the device authorization grant (RFC 8628), so no redirect server is needed. Include `offline_access` in the scope for Microsoft.

- **Browser sign-in:** set the authorization URL, save the connection, then choose **Sign in in browser…**. The provider's page opens in your browser. Flowbench listens on a one-time loopback address (`http://127.0.0.1:<port>/callback`) and exchanges the code using PKCE (RFC 7636 and RFC 8252). The redirect's `state` must match, so a forged redirect is refused. Google needs this method, because its device sign-in doesn't allow the Sheets, BigQuery, Analytics or full Drive scopes.
- **Service accounts:** paste the JSON key file as the secret. Flowbench signs a JWT with the key (RS256) and exchanges it for an access token (RFC 7523). The key stays in the vault.

Access tokens are kept in memory only, and renewed once if a call returns 401. A refresh token that the provider rotates is saved back to the vault. Tokens are sent only to the connection's own token and sign-in URLs, never to a URL from a workflow. Typing a client secret later keeps the saved sign-in.

## Microsoft 365 (Graph)

- **Base URL:** `https://graph.microsoft.com/v1.0`.
- **Paging and throttling:** pages are followed with `@odata.nextLink`, and a repeated page link stops the read. Throttling (429/503) waits for `Retry-After`.
- **Files:** OneDrive and SharePoint files are read exactly as Input Data reads them and written exactly as Output Data writes them.
  - **Locations:** your OneDrive (`me`), a user's OneDrive (`user`), or a site's document library (`site`, for example `contoso.sharepoint.com:/sites/Sales:`, plus an optional library id).
  - **Uploads over 4 MB** use an upload session. The pre-authenticated upload URL receives only the file bytes, never the access token, in chunks that are multiples of 320 KiB.
- **SharePoint List Input:** returns the item id and the column values (internal names). System columns are dropped, and dates are read as UTC.
- **SharePoint List Output:** appends, updates by item id, or deletes by item id (needs `confirm_delete`).
- **Outlook 365 Input:** reads a folder newest first, with an OData filter or search text. Bodies are plain text.

## Dataverse and Dynamics CRM

Dynamics 365 apps (CRM) store their data in Dataverse. That's why both tool pairs use the Dataverse Web API, at `https://<org>.crm.dynamics.com/api/data/v9.2`.
- **Input** reads an entity set (for example `accounts`), with optional columns, an OData filter and order, and a limit. It pages with `Prefer: odata.maxpagesize`, following `@odata.nextLink` unchanged as the documentation requires. Formatted-value annotations are dropped, lookups arrive as `_<name>_value` ids, and dates are in UTC.
- **Output** has four modes:
  - **create** (POST); the row URL comes from `OData-EntityId`;
  - **update** (PATCH with `If-Match: *`, so it never creates a row);
  - **upsert** (PATCH by the primary key or the columns of an alternate key; key values are kept out of the body);
  - **delete** (needs `confirm_delete`).

## Microsoft Power BI Output

Writes to a push dataset, at `https://api.powerbi.com/v1.0/myorg`, in your workspace or the one named by `workspace_id`.
- The dataset is found by name, or created with one table. The retention policy is None (up to 5,000,000 rows) or basicFIFO (the newest 200,000).
- Rows are appended, or replaced (the table's rows are deleted first), in batches of at most 10,000.
- A table whose columns differ is only changed with `update_schema`.
- The 75-column and 4,000-character limits are checked before anything is sent.
- Push datasets don't work with service principal profiles, so sign in as a user (device sign-in).

## Google

Each Google API has its own host, so each needs its own connection. The token URL is always `https://oauth2.googleapis.com/token`, and browser sign-in uses the authorization URL `https://accounts.google.com/o/oauth2/v2/auth`.

| Connection base URL | Scope | Tools |
|---|---|---|
| `https://www.googleapis.com` | `https://www.googleapis.com/auth/drive` | Google Drive Input/Output |
| `https://sheets.googleapis.com` | `https://www.googleapis.com/auth/spreadsheets` | Google Sheets Input/Output |
| `https://bigquery.googleapis.com` | `https://www.googleapis.com/auth/bigquery` | Google BigQuery Input/Output |
| `https://analyticsdata.googleapis.com` | `https://www.googleapis.com/auth/analytics.readonly` | Google Analytics 4 Input |

- **Drive** reads a file by id, or by name in a folder. Google Sheets files are exported as Excel, so `read.sheet` picks the tab. Writing replaces a file of the same name, or fails with `if_exists: error`. Large files use a resumable upload in 256 KiB-multiple chunks.
- **Sheets** reads unformatted values: numbers and booleans keep their type, and dates arrive as their displayed text. Writing overwrites the tab (cleared first) or appends rows, and creates the tab if it's missing. Values are written as typed (RAW), never interpreted as formulas.
- **BigQuery** runs GoogleSQL, waits for the job and reads every page. TIMESTAMP becomes a UTC datetime and RECORD/REPEATED columns become JSON text. `maximum_bytes_billed` caps what a query may cost.
  - Writing uses a **load job**, so all rows land or none do, with no streaming buffer. It appends or replaces, and creates the table from the input schema.
- **Analytics 4** runs `runReport` with dimensions, metrics and a date range, and pages through all rows. The `date` dimension becomes a date, and integer metrics become integers.

## Box, Dropbox and Azure Data Lake Storage

All three read files exactly as Input Data reads them and write exactly as Output Data writes them. An existing file is replaced, or the run fails when `if_exists` is `error`.

- **Box** (`https://api.box.com/2.0`) finds a file by id, or by name in a folder (folder `0` is All Files). An existing file gets a new version. Uploads go to Box's upload host. Downloads are redirected by Box to a pre-signed link, which Flowbench fetches **without** the access token.
- **Dropbox** (`https://api.dropboxapi.com/2`) addresses files by path. File content goes through Dropbox's content host, with the `Dropbox-API-Arg` header escaped to ASCII as the API requires. Files over 8 MB use an upload session. Browser sign-in asks for offline access so Dropbox issues a refresh token.
- **Azure Data Lake Storage Gen2** (`https://<account>.dfs.core.windows.net`) works on a file system (container) and a path. A file is created, appended in 8 MB chunks at increasing positions, then flushed. `If-None-Match: *` is sent when the file must not already exist.

Each tool may send the token only to its vendor's own documented hosts. That list is fixed in Flowbench, never taken from a connection or a workflow.

## Salesforce and ServiceNow

- **Salesforce Input** runs SOQL (`query`, or `queryAll` with `include_deleted`) and follows `nextRecordsUrl` to the end.
  - Relationship fields become dotted names (`Account.Name`), and child subqueries become JSON text.
  - Dates and date-times are typed; date-times are in UTC.
- **Salesforce Output** writes through sObject Collections, 200 records per request.
  - Modes: insert, update (by `Id`), upsert (by an external id field), or delete (by `Id`, needs `confirm_delete`).
  - Every record comes back with `SalesforceId`, `Success` and `Errors`, so one bad record doesn't hide the rest.
  - `all_or_none` rolls back a whole batch if any record fails. `fail_on_errors` fails the run instead.
- **ServiceNow Input** reads a table through the Table API, using an encoded query, chosen fields and offset paging.
  - Values stay as the text the API returns: stored values, or display values with `display_values`. Reference fields are sys_ids.
  - Basic authentication is supported, for an integration user, as well as OAuth.

## Marketo, Azure AI Language and Power Automate

- **Marketo** uses the REST API at `https://<munchkin-id>.mktorest.com/rest`.
  - **Auth:** client credentials, requested the way Marketo documents it (a GET with query parameters to the identity URL). The token is then sent in the `Authorization` header only.
  - **Input** reads leads by a filter field (300 values per request, every page), or activities since a date for chosen activity types.
  - **Output** sends leads in batches of 300, and each record reports `created`, `updated` or `skipped` with reasons.
  - **Append** adds lead fields to incoming records by looking them up on a field such as email.
  - **Errors:** Marketo returns errors inside 200 responses. 601/602 renew the token once, and 606/615 wait and retry.
- **Cognitive Services Text Analytics** uses Azure AI Language `analyze-text` (2023-04-01) for sentiment, key phrases, entities and language detection.
  - It uses the documented batch sizes: 10, 10, 5 and 1,000.
  - Texts over 5,120 characters, empty texts and texts the service rejects get an `AnalysisError` instead of failing the run.
- **Power Automate** posts each record (or all records as one array) to a flow's HTTP request trigger.
  - The trigger URL's signed query string is the secret (authentication **Signed URL**). Flows limited to Microsoft Entra users use OAuth instead.
  - Each call's status and response are returned. `fail_on_error` controls whether a rejected call fails the run.

## Amazon S3

- Requests use path-style addresses (`<endpoint>/<bucket>/<key>`), so they stay inside the connection's base URL.
- Download reads the object exactly as Input Data reads a local file. Its `read` settings are Input Data's: format, delimiter, sheet, declared fields, `empty_as_null`, and so on.
- Upload writes exactly as Output Data does, then sends the result with one signed PUT, or as a multipart upload above 64 MB. A failed multipart upload is aborted.
- Optional server-side encryption: AES256, or aws:kms with an optional key id. The storage class can be set.

## MongoDB

- MongoDB connections live in the existing database connection store (Databases in the toolbar, type MongoDB), with the password in the OS vault. The SQL tools refuse them.
- **Input** runs a find. Filter, projection and sort are written in MongoDB Extended JSON; a limit is optional. Output is either one field per top-level key or one JSON field per document.
  - Top-level scalars keep their type: numbers, booleans, text, and Decimal128 as fixed decimal.
  - ObjectIds come out as text; nested documents and arrays as JSON text.
  - Dates are converted to UTC.
- **Output** has three modes:
  - insert;
  - upsert on key fields, which replaces the matched document;
  - replace the collection, which needs `confirm_replace`.

  `json_field` writes a field of whole Extended JSON documents. Output needs a connection that allows writes and the run's database-write permission.

## Non Overlap Drivetime

1. Each point gets an isochrone from the provider.
2. Where isochrones overlap, the overlap is cut into square grid cells (Grid Size in miles or kilometres). Each cell goes to the point nearest its centroid, by straight-line distance, among the points whose area covers it; this follows the reference help. An exact tie goes to the earlier record.
3. Overlays run on a 1 mm grid in a local metric projection, so the output areas meet exactly and stay valid.

## Not implementable from public information

| Entry | Why |
|---|---|
| Connectors: Google Analytics | It reads Universal Analytics, whose Reporting API Google shut down in 2024, so no service is left to call. Google Analytics 4 data is read by **Google Analytics 4 Input**. |
| Connectors: Automation Anywhere 360 | The Bot Deploy API's request schema (bot input types, run-as users, device pools) is published only in each customer's Control Room Swagger (`https://<control-room>/swagger/`). The public documentation doesn't include it, and community examples disagree about versions and field types. Building against them would mean guessing an API, so it waits until the schema can be confirmed from an authoritative source. |
| In/Out: Auto Insights Uploader | The upload protocol is not published. The public help documents only the tool's settings, and no Auto Insights API documentation exists, so an adapter would mean inventing an API. It will be added if Alteryx publishes the protocol. |
