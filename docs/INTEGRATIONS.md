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

Apache Spark Code runs PySpark in local mode. `read_input(n)` returns a Spark DataFrame whose types are mapped from Flowbench types. `write_output(n, df)` collects the DataFrame and maps its types back exactly; types Flowbench cannot hold are rejected with a message. Remote clusters (Livy, Databricks) are not served by this provider.

## Vendor APIs (Server API, License API)

- Each record on the optional Data input makes one call. `{field}` placeholders in the endpoint, query and payload are filled from that record.
- Results go to three outputs: Success (HTTP 200), Fail (any other status) and Log (one line per call, with the full response).
- The token never travels through the workflow, so there are no authorization anchors. The reference tools pass the token between tools in the data stream; Flowbench keeps it in the connection.
- Permissions: GET needs the network permission. POST, PUT and PATCH also need remote writes. DELETE also needs `confirm_delete`.
- Server API keeps nested values as JSON text (parsed one level deep). License API flattens nested objects into dotted field names.
- A refresh token is sent only to the token URL saved with the connection, never to a URL from the workflow. A refresh token the server rotates is saved back to the vault.

## Non Overlap Drivetime

1. Each point gets an isochrone from the provider.
2. Where isochrones overlap, the overlap is cut into square grid cells (Grid Size in miles or kilometres). Each cell goes to the point nearest its centroid, by straight-line distance, among the points whose area covers it; this follows the reference help. An exact tie goes to the earlier record.
3. Overlays run on a 1 mm grid in a local metric projection, so the output areas meet exactly and stay valid.

## Not implementable from public information

| Entry | Why |
|---|---|
| In/Out: Auto Insights Uploader | The upload protocol is not published. The public help documents only the tool's settings, and no Auto Insights API documentation exists, so an adapter would mean inventing an API. It will be added if Alteryx publishes the protocol. |
