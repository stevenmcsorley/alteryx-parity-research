# Flowbench 1.0.0-rc.1 (release candidate)

This is a **release candidate**, not the final 1.0.0. The 1.0.0 gates call for every one of the 323 catalogue entries to be resolved to a working or provider-backed implementation. **307 are implemented.** Six more work but have a named gap, and ten can't be built from public information. All sixteen are listed below.

Catalogue coverage is **not** the same as parity with the reference product. Most implementations are tested in this project but **not yet compared** with the reference: 32 entries have passing differential cases, one has a recorded difference, and 290 have none yet. Vendor services that no account was available for are verified against controlled fixtures of their documented APIs, and their live acceptance is listed as pending.

## Coverage (generated from the evidence ledger)

| Category | Entries | Implemented | Partial (in progress) | Not implementable |
|---|---:|---:|---:|---:|
| AB Testing | 4 | 4 | 0 | 0 |
| AI Tools | 6 | 6 | 0 | 0 |
| Address | 6 | 6 | 0 | 0 |
| Behavior Analysis (Deprecated) | 10 | 10 | 0 | 0 |
| Calgary | 5 | 5 | 0 | 0 |
| Computer Vision | 9 | 9 | 0 | 0 |
| Connect | 3 | 1 | 0 | 2 |
| Connectors | 44 | 42 | 0 | 2 |
| Data Investigation | 14 | 14 | 0 | 0 |
| Demographic Analysis | 4 | 4 | 0 | 0 |
| Developer | 30 | 30 | 0 | 0 |
| Documentation | 3 | 2 | 1 | 0 |
| In-Database | 16 | 16 | 0 | 0 |
| In/Out | 8 | 7 | 0 | 1 |
| Interface | 18 | 13 | 5 | 0 |
| Join | 7 | 7 | 0 | 0 |
| Laboratory | 7 | 5 | 0 | 2 |
| Machine Learning | 11 | 9 | 0 | 2 |
| Parse | 4 | 4 | 0 | 0 |
| Predictive | 25 | 25 | 0 | 0 |
| Predictive Grouping | 9 | 9 | 0 | 0 |
| Preparation | 21 | 21 | 0 | 0 |
| Prescriptive | 4 | 4 | 0 | 0 |
| Reporting | 13 | 13 | 0 | 0 |
| SDK Examples | 1 | 0 | 0 | 1 |
| Spatial | 15 | 15 | 0 | 0 |
| Text Mining | 9 | 9 | 0 | 0 |
| Time Series | 9 | 9 | 0 | 0 |
| Transform | 8 | 8 | 0 | 0 |
| **Total** | **323** | **307** | **6** | **10** |

The full per-entry ledger is in `data/tool-status.json` and `docs/ALTERYX_PARITY_ROADMAP.md`.

## What's new since 0.5.1

**Integrations & Licences** (toolbar → Integrations) gathers everything tools need from outside Flowbench:
- connections;
- declared licences, recorded as declared and never validated with a vendor;
- 32 providers, with readiness, setup, licence, result semantics and what has been verified;
- data packs with licence and checksum;
- runtimes;
- per-tool availability.

Workflows name their provider and connection, never a secret, and Flowbench never switches provider for you.

**Saved connections** gain:
- OAuth 2.0: client credentials, device sign-in, browser sign-in with PKCE, and service-account keys;
- AWS Signature V4;
- signed trigger URLs;
- open (no-auth) connections;
- ODBC and MongoDB database connections.

Secrets stay in the OS credential vault.

**New tools by family.** Each lists its provider and verification status in Integrations:

- **Cloud storage and files:** Amazon S3, OneDrive, SharePoint Files, Box, Dropbox, Azure Data Lake Storage Gen2, Google Drive.
- **Databases and warehouses:** MongoDB, Google BigQuery, Dataverse, Dynamics CRM, ODBC Loader.
- **SaaS:** Salesforce, ServiceNow, Marketo (3 tools), SharePoint Lists, Outlook 365, Google Sheets, Google Analytics 4, Adobe Analytics, Anaplan, Power BI, Power Automate, Tableau (Hyper extracts and publishing).
- **Alteryx platform:** Server API, License API.
- **Spatial and address:** Non Overlap Drivetime (Valhalla); Parse Address (usaddress); Street, US and Reverse Geocoders (US Census Geocoder or Nominatim); CASS (Smarty); US ZIP9 Coder (your ZIP+4 data pack).
- **Demographics, Calgary and Behaviour:**
  - Allocate Input, Append, MetaInfo and Report, on US Census ACS data;
  - Calgary Loader, Input, Join and Cross Count, on an open indexed store (`.fbcal`);
  - the 10 Behavior Analysis tools, on your segmentation data pack.
- **AI and machine learning:**
  - LLM Override, Prompt, Precision Match, Synthetic Input, Invoice Extractor and Schema Fit, on any OpenAI-compatible service (including a local Ollama) or Anthropic;
  - DataRobot AutoModel and Predict;
  - Apache Spark Code (local PySpark).
- **Interface and documentation:** Map and saved-connection questions; options read from files or folders; formula and Input Data actions; Explorer Box.

**AI and MCP.** The AI panel and `python -m flowengine mcp` now see:
- every registered tool, with its provider setup;
- saved database and web connections, with their sign-in state but never secrets;
- provider readiness, through the new `list_providers` operation.

Large tool results are stored in full, and the model receives a bounded view of them (0.5.2 work, now released).

**Evidence.** These services run **live in CI**:
- R;
- PySpark;
- S3-compatible storage that validates signatures (SeaweedFS);
- MongoDB;
- ODBC with SQLite;
- Tableau Hyper;
- AI tools against a local Ollama model.

Gated jobs fail on any skipped test. The US Census Geocoder was also exercised live during development.

## Not complete

**Partial: works, with a named gap.**

| Entry | Gap |
|---|---|
| Documentation: Comment | Alteryx TextBox styling isn't imported (only the text) |
| Interface: Condition | Imported Alteryx Condition XML is preserved, not evaluated |
| Interface: DCM Connection | Alteryx DCM questions aren't mapped by the importer |
| Interface: Map | No interactive base map; answers are GeoJSON or a typed point |
| Interface: Tree | Allocate Geography/Variables sources (licensed data); Alteryx Tree import |
| Interface: Action | Imported UpdateValueFormula, UpdateInput and other variants aren't mapped |

**Not implementable from public information.** Details are in `docs/INTEGRATIONS.md`.

| Entry | Reason |
|---|---|
| Connect: Connect JSON Builder, Connect Output; Laboratory: same two | The Alteryx Connect API v2.0 isn't publicly documented |
| Machine Learning: Send, Predict | No public Alteryx Machine Learning (Alteryx One) API |
| Connectors: Automation Anywhere 360 | The Bot Deploy schema is published only on each customer's Control Room Swagger |
| Connectors: Google Analytics | Universal Analytics was shut down by Google in 2024 (GA4 is covered by Google Analytics 4 Input) |
| In/Out: Auto Insights Uploader | The upload protocol is unpublished |
| SDK Examples: HTML GUI Library | A showcase of the proprietary Alteryx HTML GUI SDK; Flowbench plugins get generated configuration forms instead |

## Known differences

- **Sort, locale collation (1033):** the recorded reference case orders mixed text and numbers differently from Flowbench, and the reference's E1 and E2 engines disagree with each other on it. The difference is kept on record and not "fixed" without evidence.
- Provider-backed tools return the provider's data and semantics (for example Census ACS demographics, OpenStreetMap routing and geocoding, your own segmentation), not the reference's licensed datasets.

## Live acceptance pending

No account, credential or licensed dataset was available for these. Each adapter is finished and checked against a fixture of the provider's documented API:
- Alteryx Server and the License Portal;
- AWS itself (S3 runs live against SeaweedFS);
- Microsoft 365, Dataverse and Power BI;
- Google (Drive, Sheets, BigQuery, GA4);
- Box, Dropbox and Azure storage;
- Salesforce and ServiceNow;
- Marketo, Azure AI Language and Power Automate;
- Anaplan and Tableau publishing;
- Nominatim, Smarty and Census ACS (the ACS API needs a free key);
- Anthropic;
- DataRobot and Adobe Analytics.

## Upgrade notes (from 0.5.x)

- Existing workflows and saved formats load unchanged.
- New engine dependencies, bundled in the installers: `pymongo`, `cryptography`, `usaddress`.
- Optional, user-installed runtimes: `pyspark` with Java (Spark), `tableauhyperapi` (Tableau extracts; Windows and Linux x86-64), `pyodbc` plus an ODBC driver (ODBC Loader).
- **MCP / AI clients:** `list_connections` now returns `{databases, web_connections}` instead of a single `connections` list.
- The engine reports version `1.0.0rc1`.
