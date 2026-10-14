# Designer Parity Research

**Full-catalogue functional alternative to Alteryx Designer Desktop — evidence-first engineering repository.**

> Research baseline: 13 October 2026. Pinned reference release: **Designer 2026.1.4.126** (30 September 2026). The official `/current/` documentation is a moving target; contents need comparison with the actual pinned installation. This repository does **not** contain Alteryx software, private code, installation media, copied icons, customer datasets, credentials, licence circumvention instructions, or any completed tool implementations.

## What is here now (milestone 1, 13 October 2026)

- **Catalogue refreshed against the official tool list** (live sync 13 Oct 2026): 323 entries / 321 names / 29 categories, all 323 per-tool help URLs resolved, one XML identifier drift corrected (Survival Analysis). Metadata-only audit of all 321 unique help pages in `data/public-doc-audit.json`.
- **323 machine-readable tool contracts** (`tool-contracts/**/*.contract.json`) with anchors, configuration, dependencies, tests, conformance cases and open questions — all generated from evidence by `scripts/build_status.py`.
- **`flowengine`** — an original, typed workflow engine: JSON workflow format with schema validation, plugin registry with JSON-Schema-configured tool manifests, planner with static schema inference, deterministic executor with structured JSONL events, cancellation and reproducibility manifests, an independent formula language, a CLI and a JSON-RPC editor service.
- **26 tool kernels implemented and tested** (In/Out, Preparation, Join, Parse, Transform). Every other catalogue entry fails validation with `tool-unavailable` — nothing is stubbed.
- **Differential conformance harness** against an authorised local reference install (build 2026.2.0.8): **48/48 cases pass across 17 catalogue tools** after adopting the observed semantics, each cited in code (`docs/ENGINE_SEMANTICS.md`).
- **End-to-end example** `examples/sales/monthly_sales.flow.json` (two typed CSV inputs → cleanse → unique → filter → formula → join → summarise → sort → CSV outputs) with golden outputs and a reproducibility test.
- 98 unit/property/integration/golden/service tests; CI reproduces the status ledger.

See [`docs/COVERAGE.md`](docs/COVERAGE.md) for the separate metrics and [`docs/MILESTONE_1_REPORT.md`](docs/MILESTONE_1_REPORT.md) for the full report. `engine/independent_engine` is the superseded five-operator testbed, kept for history.

## Decisions

| Area | Decision |
|---|---|
| Target | Desktop visual data-analysis workflow product with full functional tool-catalogue coverage |
| Compatibility | Evaluate `.yxmd`, `.yxmc`, `.yxwz`, `.yxzp`, `.yxdb` independently; never claim compatibility before passing tests |
| Data model | Typed Arrow-like tabular batches, pluggable scalar expressions, explicit nulls, deterministic error contracts |
| Execution | Local engine first, supports batch + streams, DAG scheduling and explicit side-effect boundaries |
| Plugin model | Tool plugin manifests with typed input/output anchors, config schemas and separately testable kernels |
| Traceability | Evidence for every tool, specification, sample case, test result, assumptions and version-specific differences |
| Provenance | No copied commercial code, protected artwork, unauthorised analysis, or restricted access |
| Full catalogue | None silently removed; proprietary SaaS/data dependencies flagged and implemented using separately authorised adapters |

## Repository tour

- [`data/catalog.json`](data/catalog.json) — offline catalogue with manually transcribed public XML names and roles; missing per-tool help links
- [`data/catalog.csv`](data/catalog.csv) — same inventory in spreadsheet-friendly form
- [`tool-contracts/`](tool-contracts/) — 323 tool-specific research records
- [`docs/COVERAGE.md`](docs/COVERAGE.md) — actual implementation and research status
- [`docs/ACQUISITION_AND_DEPS.md`](docs/ACQUISITION_AND_DEPS.md) — all assets, dependencies, and licensed blockers
- [`docs/EXECUTION_PLAN_AND_GATES.md`](docs/EXECUTION_PLAN_AND_GATES.md) — full-scope programme and acceptance gates
- [`docs/READY_FOR_AGENT.md`](docs/READY_FOR_AGENT.md) — immediate Codex/Claude handoff
- [`engine/independent_engine/`](engine/independent_engine/) — independent five-operation DAG/CSV testbed (no vendor parity)
- [`data/tool-list-seed.txt`](data/tool-list-seed.txt) — offline listing by category
- [`data/test-matrix.json`](data/test-matrix.json) — work items for every tool entry
- [`docs/PRODUCT_REQUIREMENTS.md`](docs/PRODUCT_REQUIREMENTS.md) — full-scope requirements
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — proposed components and compatibility contracts
- [`docs/REA_RUNBOOK.md`](docs/REA_RUNBOOK.md) — installing REA and authorisation-based investigations
- [`docs/REFERENCE_TESTING.md`](docs/REFERENCE_TESTING.md) — behavioural conformance and evidence rubric
- [`docs/SOURCES.md`](docs/SOURCES.md) — official public references and retrieval status
- [`docs/ACCESS_AND_INPUTS.md`](docs/ACCESS_AND_INPUTS.md) — what the operator must legally provide
- [`docs/RIGHTS_AND_BOUNDARIES.md`](docs/RIGHTS_AND_BOUNDARIES.md) — restrictions, separation and review gates
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) — delivery programme (full scope preserved)

## Start here (Windows PowerShell)

```powershell
python -m pip install -r requirements-dev.txt
python scripts/validate_repository.py
python -m unittest discover -s tests -v
python -m flowengine validate examples/sales/monthly_sales.flow.json
python -m flowengine run examples/sales/monthly_sales.flow.json --log run.jsonl --manifest run.json
python -m flowengine tools
python scripts/build_status.py          # regenerate contracts + status ledger from evidence
python scripts/coverage_report.py

# Online, metadata only:
python scripts/sync_official_catalog.py
python scripts/audit_public_sources.py --max-pages 400 --cache-dir $env:TEMP\doc-cache

# With an authorised reference install (see docs/AUTHORISATION.md):
python conformance/reference/scan_reference_workflows.py
python conformance/harness.py
```

**Do not extend reference investigation beyond the scope recorded in [`docs/AUTHORISATION.md`](docs/AUTHORISATION.md).** REA is a research tool; it does not confer rights to use the target software or reproduce copyrighted implementation.

## Full parity is a contract, not a status

Track independent axes:

1. **Inventory coverage:** Every official category/tool entry exists in our matrix, including deprecated, optional and add-on entries.
2. **Design coverage:** Each tool has a versioned, tested contract describing configuration, input/output anchors, errors, supported types and determinism.
3. **Implementation coverage:** Each independently written kernel/adapter passes unit and integration checks.
4. **Observed compatibility:** A permitted, repeatable reference comparison has passed with recorded engine/build/configuration; vendor differences are acknowledged.
5. **Operational readiness:** Packaging, resilience, accessibility, security, CI, installer, diagnostics and performance satisfy release gates.

No single percentage can honestly represent all five.

## GitHub repository

Published at **https://github.com/stevenmcsorley/alteryx-parity-research**.

To check out this research foundation and run the baseline validation:

```powershell
git clone https://github.com/stevenmcsorley/alteryx-parity-research.git
cd alteryx-parity-research
python -m pip install -r requirements-dev.txt
python scripts/validate_repository.py
python -m unittest discover -s tests -v
```

The public repository intentionally excludes licensed software, binary dumps, protected assets, credentials, and unauthorised analysis. Research coverage does not establish Alteryx functional parity.
