# Acquisition matrix and blocked prerequisites

**Reference build:** Designer **2026.1.4.126** released 30 September 2026 (published release notes). `current` docs may refer to later features, so installed-build inventory remains mandatory.

This is an explicit list of assets required to reach complete functional coverage. "Complete public research" does **not** mean the commercial application's install media or licensed datasets are included.

| Input | Can retrieve / include in Git? | Acquisition source / action | State at 2026-10-13 |
|---|---|---|---|
| Public tool catalogue (names, XML IDs, roles) | Link and transcribe public metadata; do not redistribute whole vendor pages | https://help.alteryx.com/current/en/designer/tools/designer-tools-list.html | 323 manually transcribed entries; compare with live page and installed build |
| Per-tool docs | URLs and limited structural metadata, not vendor documentation bodies | `python scripts/sync_official_catalog.py`, then `python scripts/audit_public_sources.py --max-pages 323` | Help URLs not yet populated; internet retrieval needed |
| Designer release changelog | Link | https://help.alteryx.com/release-notes/en/release-notes/designer-release-notes/designer-2026-1-release-notes.html | Reference pinned, 2026.1.4.126 |
| Original application executable | **No** | Obtain licensed product via Alteryx One portal (2026.1+), subject to entitlement | Not included; do not commit installers/binaries |
| Product documentation and samples | **No bulk repackaging** | In authorised installation: **Help > Sample Workflows > Learn one tool at a time**; vendor user terms apply | Sample catalogue identified; local sample corpus not supplied |
| Predictive tools/R dependencies | Needs authorised installation and redistributable alternative libs | Designer menu **Options > Download Predictive Tools** and official docs | Not included |
| Geospatial, Address, Demographic / Calgary data packs | Licensed materials usually cannot be redistributed | Evaluate per tool; document alternate freely licensable datasets and geographic limitations | Unavailable without entitlements |
| Enterprise connectors (SAP, cloud, databases etc.) | Can write clean connectors, must use permitted SDKs/APIs | Bring sandbox tenant credentials, supported protocols, driver versions, licensing grants | No credentials; connectors not validated |
| AI/Text Mining/Machine Learning/Computer Vision add-ons | Separate packages/licences may apply | Check edition, role and target build; implement independently with authorised libraries | No assumed entitlements |
| REA | Open-source MIT, install with npm; analysis permission is separate | https://github.com/morluto/rea, `npx rea-agents@latest setup` | Installation instructions documented; not bundled |
| Ghidra/JDK | External tools; licences and compatible builds | https://github.com/NationalSecurityAgency/ghidra, official JDK distribution | Not bundled; REA Windows native P0 is experimental |
| Agent | Local Codex/Claude Code | Your own installation and authentication | Not bundled |
| Test data | Yes, if original and synthetic | `examples/data`, `scripts/build_fixture_pack.py` | Original synthetic test data present |
| Oracle results | Only if generation and retention are legally authorised | Licensed installation, permitted tests, hashed results and settings | Missing; all conformance statuses not-tested |
| Installer/update infrastructure | Can build independently | CI, signing certificate and release process | Not established |

## Files and compatibility work to plan

Official file types and use cases are referenced here: https://help.alteryx.com/current/en/designer/file-types-support/alteryx-file-types.html .

| Family | Extensions | Work to verify |
|---|---|---|
| Workflow / macro / analytic app | `.yxmd`, `.yxmc`, `.yxwz` | Parse, preserve unknown nodes, import/edit/execute/export round trip |
| Packaging / templates / groups | `.yxzp`, `.yxi`, `.yxwg`, `.yxtd`, `.yxtc`, `.yxtz` | Dependency resolution, relative paths, version policies, safe archives |
| Data and indexes | `.yxdb`, `.cydb`, `.cyidx`, `.yxft` | Typed representation, schema, recovery, index behaviour |
| App/report support | `.yxwv`, `.pcxml`, `.rptx`, `.yxbe` | Value substitution, formats, rendering, unsupported fields |
| In-DB transport | `.indbc` and database-specific drivers | Credential handling, dialects, permission scopes, execution pushdown |

`.yxwv` may contain plaintext passwords per vendor docs: treat sample or imported files as potentially sensitive. The importer should never accidentally commit or export credentials.

## Source-authorised workflow

1. Obtain vendor licences/entitlements directly from official vendors, not this repository.
2. Record reference build, locale/timezone, original or AMP engine and installed add-on inventory.
3. Use `scripts/inventory_local_reference.ps1` for metadata-only authorised inventory and keep the report outside Git.
4. Refresh official public pages; resolve catalogue drift; link public help into tool contracts.
5. Independently author tool semantics and tests; use any target-specific runtime/static analysis **only within explicit legal permission**.
6. Implement every listed tool eventually, including deprecated entries; for features dependent on restricted external systems, report requirements and truthfully label incomplete cases.

The Alteryx published EULA (https://www.alteryx.com/end-user-license-agreement, section 3.3) restricts decompilation/reverse engineering and using the purchased software to develop a competing product. Obtain competent legal advice before any software-access-based investigation. Public documentation based independent engineering remains the default.

REA itself forbids the use of its project to circumvent licensing/DRM or copy proprietary assets. See https://github.com/morluto/rea/blob/main/DISCLAIMER.md and `docs/RIGHTS_AND_BOUNDARIES.md`.
