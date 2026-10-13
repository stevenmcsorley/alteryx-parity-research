# Designer Parity Research

**Full-catalogue functional alternative to Alteryx Designer Desktop — evidence-first engineering repository.**

> Research baseline: 13 October 2026. Pinned reference release: **Designer 2026.1.4.126** (30 September 2026). The official `/current/` documentation is a moving target; contents need comparison with the actual pinned installation. This repository does **not** contain Alteryx software, private code, installation media, copied icons, customer datasets, credentials, licence circumvention instructions, or any completed tool implementations.

## What is here now

- **323 public category-tool occurrences / 321 distinct names across 29 categories**, with publicly listed XML identifiers and role fields manually transcribed from the official Designer Tools List. The duplicates are `Connect JSON Builder` and `Connect Output`, which appear in both Laboratory and Connect. These are **catalogue entries**, not implemented tools.
- A reproducible online parser to **cross-check XML identifiers and user roles and retrieve official per-tool help URLs** from the public tool list. Manual XML/role data are present offline, but the live detailed sync **has not been run** here because external network access is unavailable. The parser refuses partial downloads rather than silently shrinking the list.
- A **323-entry untested acceptance-test matrix**. Every entry has implementation=`not-started`, verification=`not-tested`.
- Full-system PRD, architecture boundaries, version and source register, REA runbook, authorisation boundary, and a reproducible independent-data fixture generator.
- **323 individual research briefs** in `tool-contracts/`, one for every category-tool occurrence, with source/rights/testing checklists and nothing misrepresented as implemented.
- `docs/ACQUISITION_AND_DEPS.md`, `docs/EXECUTION_PLAN_AND_GATES.md`, `docs/READY_FOR_AGENT.md`, and automatically generated `docs/COVERAGE.md`.
- **Original, limited working Python DAG engine foundation** (`engine/independent_engine`), supporting generic CSV input, filter, join, summary and CSV output; 12 tests in all. **This is not Alteryx parity.**
- Metadata-only public per-tool documentation auditor (`scripts/audit_public_sources.py`); needs internet and verified help URLs.
- Validation scripts and CI. The tools catalogue is research metadata, not application functionality.

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
# Prerequisites: Git, Python 3.11+; Node.js only for REA
python scripts/validate_repository.py
python scripts/generate_test_matrix.py
python scripts/build_fixture_pack.py
python scripts/materialize_tool_specs.py
python scripts/coverage_report.py
python -m unittest discover -s tests -v
python -m engine.independent_engine examples/customer_reconciliation.json

# When connected to the internet, independently refresh/check XML IDs, roles and documentation URLs:
python -m pip install -r requirements-dev.txt
python scripts/sync_official_catalog.py
python scripts/audit_public_sources.py --max-pages 323
python scripts/validate_repository.py

# When you have appropriate rights/authorisation and want to configure REA for your agent:
npx rea-agents setup
npx rea-agents doctor
```

The above workflow example outputs `examples/out/uk-customer-spend.csv`. The engine is an independently designed testbed with only five primitive operators and deliberately different semantics; it does **not** constitute Alteryx compatibility for any of the 323 catalogued tools.

**Do not proceed to restricted target binary inspection without reviewing [`docs/RIGHTS_AND_BOUNDARIES.md`](docs/RIGHTS_AND_BOUNDARIES.md).** REA is a research tool; it does not confer rights to use the target software or reproduce copyrighted implementation.

## Full parity is a contract, not a status

Track independent axes:

1. **Inventory coverage:** Every official category/tool entry exists in our matrix, including deprecated, optional and add-on entries.
2. **Design coverage:** Each tool has a versioned, tested contract describing configuration, input/output anchors, errors, supported types and determinism.
3. **Implementation coverage:** Each independently written kernel/adapter passes unit and integration checks.
4. **Observed compatibility:** A permitted, repeatable reference comparison has passed with recorded engine/build/configuration; vendor differences are acknowledged.
5. **Operational readiness:** Packaging, resilience, accessibility, security, CI, installer, diagnostics and performance satisfy release gates.

No single percentage can honestly represent all five.

## Publishing to GitHub

This package is a real Git working tree with initial commit. The current connector cannot create a new GitHub repository, so it is **not automatically published**. Create a blank, ideally private, repo named `alteryx-parity-research`, then push:

```powershell
git remote add origin https://github.com/YOUR_USERNAME/alteryx-parity-research.git
git branch -M main
git push -u origin main
```

Only push the **research files**. Keep licensed software, binary dumps, licensed sample assets and credentials outside Git.
