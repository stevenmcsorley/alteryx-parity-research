# Codex continuation record

Updated: 2026-10-14

## Repository state

- Branch: `milestone-3-macro-interface-parity`
- Current continuation point: Arrange implementation is locally verified; commit/push is pending.
- PR: #4, https://github.com/stevenmcsorley/alteryx-parity-research/pull/4
- Do not merge automatically.

## Delivered in this work unit

- `transform:arrange`: registered independent kernel `core.arrange`; configuration supports selected key fields and ordered output columns generated from literals, source field values, source names, source descriptions, or nulls. Rows expand deterministically by input record and configured row order. Type/null/metadata behavior and invalid configuration have independent tests.
- Fixed coverage evidence test to use the live registry when determining unavailable tools, avoiding dependence on stale generated status.
- Regenerated catalogue status, test matrix, roadmap and coverage from test attribution.

## Verification

- `python -m unittest discover -s tests`: 179 run, 0 failures, 1 skipped (R unavailable locally).
- `python scripts/build_status.py --check`: pass; 323 entries, 63 registered kernels, 62 implemented, 2 in progress, 259 not started.
- `python scripts/build_parity_roadmap.py --check`: pass.
- `python scripts/validate_repository.py`: pass; 323 category/tool rows and 321 distinct names.
- E1/E2 ledger remains 74/75 each; 32 partial tool entries, zero fully reference-verified. Existing Sort locale comparison remains failed.
- Arrange has three independent attributed tests, no E1/E2 differential tests. Original Alteryx Arrange XML is not translated into the native kernel config.

## Current known limitations

- Action compatibility remains bounded to the already documented native Flowbench app adapter and existing imported XML subset; no new Action parity claim is made here.
- Arrange desktop configuration uses the generic schema inspector; matrix cells are configured through its JSON editor. Alteryx XML configuration translation and reference behavior remain unverified.
- Full acceptance baseline has existing conformance/holdout failures and blocked/not-run platform suites recorded in its generated report; do not call those passing.

## Exact next implementation action

Inspect the Fuzzy Match contract, reference fixtures and current Join kernels. Establish evidence-backed configuration and matching semantics before implementing `join:fuzzy-match`; implement only supported matching modes, add typed/null/Unicode/duplicate/tie/error tests, and leave undocumented variants explicit. Then continue through Developer and Interface entries in the generated roadmap.
