# Designer Parity Research

**Full-catalogue functional alternative to Alteryx Designer Desktop — evidence-first engineering repository.**

> Research baseline: 13 October 2026. Pinned reference release: **Designer 2026.1.4.126** (30 September 2026). The official `/current/` documentation is a moving target; contents need comparison with the actual pinned installation. This repository does **not** contain Alteryx software, private code, installation media, copied icons, customer datasets, credentials, licence circumvention instructions, or any completed tool implementations.

## What is here now (Milestone 2)

- **Flowbench desktop app** (`desktop/`): Tauri + React + TypeScript. Workflow canvas, palette for all 323
  catalogue entries with evidence-based status, schema-driven configuration, live runs, results, history,
  `.yxmd` import/export. See [docs/DESKTOP.md](docs/DESKTOP.md).
- **flowengine**: typed columnar engine with Arrow/Parquet, streaming, spilling, parallel scheduling, run history,
  E1/E2 compatibility profiles, isolated Python/R/command runtimes and a JSON-RPC service.
- **74 catalogue kernels implemented and passing independent tests** (including GitHub Commit and Get Last Commit, Download, Runner, Dynamic Input, Control Container, Pearson correlation and sample covariance);
  every other entry fails validation with `tool-unavailable`, and nothing is stubbed.
- **Conformance against Alteryx Designer 2026.2.0.8**: 74/75 differential cases in both E1 and E2; holdout runs
  of the install's shipped sample workflows match on 15–16 of 31 workflows (87–88 of 126 outputs). 32 entries
  pass all their reference cases; 0 are claimed fully verified.
- 323 machine-readable contracts and an evidence ledger reproduced in CI.

Reports: [docs/COVERAGE.md](docs/COVERAGE.md), [docs/MILESTONE_2_REPORT.md](docs/MILESTONE_2_REPORT.md),
[docs/MILESTONE_1_REPORT.md](docs/MILESTONE_1_REPORT.md). Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/ENGINE_SEMANTICS.md](docs/ENGINE_SEMANTICS.md), [docs/COMPATIBILITY_PROFILES.md](docs/COMPATIBILITY_PROFILES.md),
[docs/INTEROP.md](docs/INTEROP.md), [docs/RUNTIMES.md](docs/RUNTIMES.md), [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

Linux/macOS desktop packaging and native verification: [docs/CROSS_PLATFORM_UPDATE.md](docs/CROSS_PLATFORM_UPDATE.md).

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
python -m flowengine import tests/fixtures/yxmd/regional_sales.yxmd -o imported.flow.json
python scripts/build_status.py          # regenerate contracts + status ledger from evidence
python scripts/full_acceptance.py       # acceptance suites; records blocked and not-run gates explicitly

# Desktop app (see docs/DESKTOP.md)
cd desktop; npm ci; npx tauri dev
python scripts/coverage_report.py
python scripts/build_parity_roadmap.py --check

# Online, metadata only:
python scripts/sync_official_catalog.py
python scripts/audit_public_sources.py --max-pages 400 --cache-dir $env:TEMP\doc-cache

# With an authorised reference install (see docs/AUTHORISATION.md):
python conformance/reference/scan_reference_workflows.py
python conformance/harness.py --mode e1      # and --mode e2
python conformance/reference/holdout_samples.py --mode e1
```

**Do not extend reference investigation beyond the scope recorded in [`docs/AUTHORISATION.md`](docs/AUTHORISATION.md).** REA is a research tool; it does not confer rights to use the target software or reproduce copyrighted implementation.

## Full parity is a contract, not a status

[`docs/ALTERYX_PARITY_ROADMAP.md`](docs/ALTERYX_PARITY_ROADMAP.md) is the authoritative full-catalogue work queue. It
is generated from the catalogue, per-tool contracts, and evidence-derived implementation/conformance ledger; rebuild
it with `python scripts/build_parity_roadmap.py` after refreshing tool status.

[`docs/FULL_ACCEPTANCE.md`](docs/FULL_ACCEPTANCE.md) describes the end-to-end verification entrypoint, report formats,
optional isolated installer/performance runs, and how missing external prerequisites are recorded.

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
