# Agent execution brief — copy to Codex from repository root

You are taking over an original, production-oriented, full-scope workflow analytics platform project. The target parity reference is Alteryx Designer 2026.1.4.126. **All 323 catalogue occurrences and 29 tool categories remain in scope**; duplicate category placements may share kernels. Do not falsely claim that this repository already implements them.

## Mandatory first actions

1. Inspect `README.md`, `docs/ACQUISITION_AND_DEPS.md`, `docs/RIGHTS_AND_BOUNDARIES.md`, `docs/ARCHITECTURE.md`, `docs/PRODUCT_REQUIREMENTS.md`, and `docs/COVERAGE.md`.
2. Run `python scripts/validate_repository.py && python -m unittest discover -s tests -v`.
3. Run the original engine example `python -m engine.independent_engine examples/customer_reconciliation.json`; inspect `examples/out/uk-customer-spend.csv` (ignored in Git).
4. If internet is available, execute `python scripts/sync_official_catalog.py` and review **all diffs** before accepting the rolling public catalogue. Then `python scripts/audit_public_sources.py --max-pages 323` to capture help page URLs/titles/headings/hashes, **not the vendor page bodies**. Rematerialize only missing briefs.
5. Inventory sanctioned local reference material only after checking rights. Never assume a local installation or an expired trial implies permission to decompile, copy files or intercept restricted networks.

## Development requirements

- Every catalogued entry gets a verified-independent semantic contract with exact source references and accepted test cases.
- Build production-capable typed DAG executor and original UI. `engine/independent_engine` is a limited **non-conformant technical foundation** and may be replaced by Arrow/Polars/DataFusion/Rust implementation.
- No fake results, stub tools presented as working, missing nodes silently dropped, or unverified ``verified`` status changes.
- Work in catalogue waves driven by dependency edges, not by assumed profitability. Track external requirements explicitly.
- Add source provenance, observable run logs, deterministic tolerance policies, unit/property/integration/round-trip/benchmark tests and UI smoke tests for each implemented path.
- Where separately licensed APIs and datasets are required, implement real adapters against authorised sandbox accounts; mark blocked cases with reason until they can be tested.
- Treat installer, code signing, secrets, data egress, model execution, accessibility and resilience as production release requirements, not nice-to-haves.

## Reporting contract

In each development report, state actual metrics separately: public inventory, tool contracts specified, kernels implemented, conformance comparisons verified, external prerequisites blocked, entire-workflow verified. Refer to exact commits and test commands. A demonstration of five internal operators is not a claim of Alteryx compatibility.

First engineering task: pin and validate public source catalogue/rights; design the real typed plugin architecture and investigate genuine observed tool configurations; implement and test one compositional production path; keep the full 323-entry work queue intact.
