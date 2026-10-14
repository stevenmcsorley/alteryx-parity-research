# Milestone 1 report (13 October 2026)

Scope: full 323-entry catalogue retained. This milestone delivers the foundation, the first production path and the measurement system; it does **not** claim parity.

## Metrics (separate axes)

| Axis | Result | Source |
|---|---|---|
| Public inventory | 323 entries / 321 names / 29 categories; 323/323 help URLs resolved; 1 XML-id drift fixed | `data/catalog.json` (live sync), `data/public-doc-audit.json` |
| Tool contracts specified (machine-readable, evidence-linked) | 323/323 generated; anchors known for 181; 127 observed in reference workflows | `tool-contracts/**/*.contract.json` |
| Kernels implemented (original, tested) | 26 | `data/tool-status.json` |
| Conformance comparisons | 48/48 differential cases pass across 17 tools ("partial" verification) on reference build 2026.2.0.8 (E1) | `conformance/results/2026.2.0.8/results.json` |
| Fully verified tools | 0 (by rule; see docs/TOOL_STATUS_RULES.md) | |
| External prerequisites blocking exact behaviour | 78 entries (licensed datasets, vendor cloud, external accounts, LLM services) | contracts, `external_prerequisites_blocking` |
| Whole-workflow verification | 1 original end-to-end workflow with golden outputs and reproducibility test (engine-only; not yet run on the reference) | `examples/sales`, `tests/test_fe_executor.py` |
| Tests | 98 unit/property/integration/golden/service tests, all passing | `python -m unittest discover -s tests -v` |

## Work done against the brief

1. **Catalogue validated and refreshed** from the official list; drift reviewed (one XML id). Version-pinned `/20261/` list compared once (identical) and not crawled (robots disallow). Per-page metadata audit with anchor extraction; page bodies are not stored.
2. **Inputs/outputs/config/dependencies per tool**: every entry has a contract combining public-doc headings and anchor labels, structural reference observations (anchor names and configuration element names from 292 shipped workflows), category dependency rules and, for implemented tools, the manifest and JSON Schema.
3. **Architecture** for engine, plugin system and editor: `docs/ARCHITECTURE.md` (ADR-001…010). The engine and plugin system are built; the editor boundary (JSON-RPC sidecar) is built and tested; the editor UI is not yet built.
4. **First production path**: typed CSV inputs → Data Cleansing → Unique → Filter → Formula → Join (with unjoined exceptions) → Summarize → Sort → CSV outputs + Browse, running on real files with no mocks.
5. **Determinism, logging, errors, schema validation, fixtures**: injected clock; deterministic ordering; JSONL events; run manifest with digests; stable error codes naming the node; workflow JSON Schema and per-tool config schemas; static schema inference; original fixtures and golden outputs.
6. **Tool-by-tool tracking**: `scripts/build_status.py` derives statuses from kernels, tests (mapped at run time), observations and conformance; CI fails if the ledger is stale.
7. **REA**: static provenance of the reference binaries recorded; process capture unavailable on Windows (documented). Behavioural evidence comes from the differential harness. See `docs/REA_RUNBOOK.md` §7 and `docs/AUTHORISATION.md`.
8. **Semantics learned from the reference** and adopted with citations (`docs/ENGINE_SEMANTICS.md`): case-insensitive string comparison, null-as-empty in string functions, `Round`/`Mod`/`ToNumber` behaviour, rounding on numeric→integer casts, key-ordered group outputs, null-key matching and ordering in Join, 15-significant-digit doubles.

## Blockers needing external action

| Blocker | Needed from owner |
|---|---|
| Authorisation details | Fill in reference, issuer and scope in `docs/AUTHORISATION.md`; required before any wider investigation (e.g. managed-code inspection for the 142 entries whose anchors are still unknown) |
| Reference build mismatch | Installed build is 2026.2.0.8, the pinned target is 2026.1.4.126. Provide a 2026.1.4.126 install, or approve re-pinning the target to 2026.2 |
| Licensed datasets / services | Sandbox credentials or data entitlements for Address, Demographic, Calgary, Behavior Analysis, Connectors, Connect and AI Tools (78 entries) |
| Predictive/ML runtimes | Decision on the R/Python runtimes to embed (licensing) before Predictive, Time Series, ML, Text Mining and CV kernels |
| AMP (E2) engine | All evidence is E1. Decide whether parity targets E1, E2 or both (E2 output ordering differs per vendor docs) |
| REA process capture | Needs a Linux/macOS host, which can't run the Windows reference; no action unless runtime tracing becomes necessary |
| Code signing / installer | Certificate and release process for the desktop build |

## Next wave (dependency order)

The remaining In/Out (Map Input), Preparation (Auto Field, Multi-Field Formula, Tile, Rank, Imputation, Random % Sample, Select Records, Data Cleanse Pro, Create Samples, Oversample, Multi-Field Binning), Join (Find Replace, Fuzzy Match, Join Multiple, Make Group), Parse (DateTime, XML Parse), Transform (Arrange, Make Columns, Weighted Average) and Developer (Field Info, JSON Parse, Dynamic Rename/Select/Replace, Blob, Message, Test, Block Until Done, Detour) tools. Each needs differential cases first. Also: the `.yxmd` importer, running the end-to-end example on the reference, and the editor UI.

## Reproduce

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m flowengine run examples/sales/monthly_sales.flow.json --clock 2026-01-15T09:30:00 --manifest run.json
python scripts/build_status.py --check
python conformance/harness.py        # only with an authorised reference install
```
