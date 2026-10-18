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

Apache Spark Code runs PySpark in local mode. `read_input(n)` returns a Spark DataFrame whose types are mapped from Flowbench types. `write_output(n, df)` collects the DataFrame and maps its types back exactly; types Flowbench cannot hold are rejected with a message. Remote clusters (Livy, Databricks) are not served by this provider.
