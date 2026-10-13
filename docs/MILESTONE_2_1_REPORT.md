# Milestone 2.1 — database integration and desktop packaging

Status as of 13 October 2026: **in progress**. This is a report of implemented and verified work, not a claim that
the milestone acceptance workflow or installer is complete.

## Recovered local state

The checkout continued on `milestone-2-desktop-and-engine`, the head branch of open PR #2, whose base is the
open Milestone 1 PR #1. Claude's uncommitted database work included the PostgreSQL/SQL Server dialects,
credential-vault abstraction, connection persistence, Database Input/Output kernels, SQL pushdown translator,
and a Docker fixture launcher. The new kernels are Flowbench extensions (`catalog_ids=[]`), so the Alteryx
catalogue remains 323 entries and the coverage ledger remains evidence-derived.

## Codex changes in this continuation

- Added engine RPC operations to list, save and remove database connections; test a connection; list schemas,
  tables and columns; and preview bounded result sets. Passwords are submitted separately and delegated to the
  OS credential vault. Responses omit secrets.
- Corrected database preview to describe and execute the selected query, not the metadata probe query.
- Added real PostgreSQL 16 and SQL Server 2022 integration tests for metadata, preview, Unicode, cross-database
  joining, refresh-on-rerun, read-only SQL rejection and password-free connection persistence.
- Added pinned-major driver constraints and a CI job that provisions and removes isolated database fixtures.

## Verified evidence

- Catalogue: **323 category/tool entries**, 321 distinct names; unchanged from the baseline.
- Implemented catalogue kernels: **58**; full-reference-pass entries: **32**; reference-difference entries: **1**.
- Python: **151 tests passed, 1 skipped** on this machine; the skip is the existing R runtime test because R is
  not installed. The three new database tests used live local PostgreSQL 16 and SQL Server 2022 containers.
- Database exercise: connection test, metadata and preview succeeded; real PostgreSQL/SQL Server type matrices
  preserved nulls, Unicode, decimals and binary data; a 41-row PostgreSQL Customers table and
  120-row SQL Server Orders table were read and joined locally; a new SQL Server row was visible on the next run.
- TypeScript type-check: passed. Vitest: **10 passed**. Playwright: **9 passed**.
- Rust tests were not run because `cargo` is not installed in this environment.

## Remaining milestone work

- **Desktop connection manager and database-input authoring:** no connection UI or canvas configuration flow is
  wired yet. The engine RPC is available, but the required drag-and-configure experience is incomplete.
- **Pushdown:** dialect translation and planning code exists in Claude's work, but `run_workflow` does not invoke
  it. SQL execution pushdown, fallback diagnostics in run results, transfer-volume measurements and explain-plan
  inspection are therefore not claimed as delivered.
- **Integration coverage:** CI runs real-engine smoke and cross-database tests. Additional required cases remain:
  auth failures, TLS validation, SQL Server/PostgreSQL type matrices, large streaming, cancellation, timeouts,
  export correctness and write safeguards.
- **Installer:** current NSIS package still launches a system Python. Managed runtime bundling, clean-machine
  installation, installer/uninstaller checks and signing readiness remain outstanding.
- **R:** pinned R runtime/package environment and real R CI execution remain outstanding.
- **Acceptance demo:** the full PostgreSQL Customers + SQL Server Orders → Join → Filter → Formula → Summarize →
  Results Grid → CSV/Parquet workflow has not been demonstrated in the actual desktop app.

No compatibility or coverage claims were changed by this work. E1/E2 profiles and the known Sort collation
difference remain as recorded in `docs/COVERAGE.md`.
