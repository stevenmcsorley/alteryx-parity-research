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
- Added a desktop connection manager, drag-and-drop database extension tools, and a Database Input inspector for
  saved-connection selection, schema/table browsing, read-only SQL entry and live preview. Database writes now
  require a per-run confirmation in addition to the connection's write setting.
- Corrected database preview to describe and execute the selected query, not the metadata probe query.
- Added real PostgreSQL 16 and SQL Server 2022 integration tests for metadata, preview, Unicode, cross-database
  joining, refresh-on-rerun, read-only SQL rejection and password-free connection persistence.
- Wired the existing SQL planner into workflow execution, cancellation and manifests. PostgreSQL and SQL Server
  now execute eligible Filter, Select and Summarize chains as one live query; tests compare the pushed results
  with local execution. Cross-database joins and ordered Database Inputs stop pushdown with explicit reasons.
- Pushdown manifests/events record executed SQL, redacted bound parameter values, folded operations, duration,
  result rows, estimated transferred bytes and a reproducible output digest. Unsupported SQL failures return to
  local execution; connection and timeout failures remain run failures.
- Added same-database Join execution tests on PostgreSQL and SQL Server. Join folding requires explicit input
  ordering and only absorbs the `joined` output when every other output is unused. SQL Server text-key joins
  remain local because its string ordering does not match Flowbench's Unicode code-point ordering. Duplicate
  keys, null matching, case-distinct and Unicode keys, column collisions, branch fan-out, and multi-output
  fallback are compared against local execution.
- Added engine-side full-result Parquet staging for result outputs and Browse captures. The renderer continues to
  receive bounded previews; `run.export` streams full CSV from Parquet batches or copies typed Parquet directly.
  The results panel now offers CSV and Parquet file dialogs. Database Output now has a connection/table/mode
  editor and continues to require write-enabled connections plus per-run confirmation.
- Added real DB coverage for least-privilege writes, rollback after a failed batch, authentication failure,
  PostgreSQL certificate and hostname verification, credential rotation, query timeout, cancellation,
  terminated connections, 200,000-row batched reads, typed exports, and both-dialect join parity.
- Added a pinned CRAN R 4.6.0 CI job for the existing R kernel test; the job passed on commit `8696d2d`
  ([Actions run 38448953296](https://github.com/stevenmcsorley/alteryx-parity-research/actions/runs/38448953296)).
  R remains an explicit external runtime for end users and is not included in the Windows package.
- Added a Windows runtime staging script, pinned dependency manifest, managed CPython resource lookup, and a
  Windows CI installer smoke step for install/launch/reinstall/uninstall with system developer tools removed
  from `PATH`. Startup now checks `engine.info` and all 323 `catalog.list` records over the installed sidecar
  protocol; CI smoke mode also runs the shipped deterministic sales workflow through that installed engine.
  The installer artifact is retained by CI for local Tauri acceptance testing.
- Added pinned-major driver constraints and a CI job that provisions and removes isolated database fixtures.

## Verified evidence

- Catalogue: **323 category/tool entries**, 321 distinct names; unchanged from the baseline.
- Implemented catalogue kernels: **58**; full-reference-pass entries: **32**; reference-difference entries: **1**.
- Python: **147 tests passed, 1 skipped** on this machine; the skip is the existing R runtime test because R is
  not installed locally. The R test passed in the pinned R 4.6.0 CI job above.
- Database: **12 integration tests passed** against local PostgreSQL 16 and SQL Server 2022 fixtures. This
  includes pushed/local Filter → Select → Summarize parity on both engines, same-database Join parity where safe,
  cross-database local Join, reader-role write rejection, Database Output rollback, credential rotation, TLS
  verification, cancellation, timeout, connection loss, large batched reads and full typed CSV/Parquet export.
- Desktop TypeScript production build: passed; Vitest: **10 passed**. The actual Tauri desktop acceptance flow
  using two live database sources has not yet been demonstrated.
- Playwright: **11 passed** against the real engine, including database connection manager access and extension
  palette visibility; this is browser-driven UI verification and does not stand in for the Tauri live-database
  acceptance demonstration.
- Rust tests: **3 passed** in the Windows CI job (this workstation does not have `cargo`).

## Remaining milestone work

- **Pushdown:** local-vs-pushed Filter/Select/Summarize parity and same-database Join parity are verified for the
  documented fixtures. Join output branches, SQL Server text-key ordering, unordered sources and cross-database
  joins intentionally run locally. Native query-plan inspection and broader expression/type conformance remain
  open.
- **Integration coverage:** nine categories of failure/security/large-data cases are exercised in real databases,
  but driver-specific TLS verification on SQL Server, more adversarial cancellation/network cases and broader
  write permission matrices remain open.
- **Installer:** the clean-runner root cause was missing `venv` in CPython's embeddable distribution. The engine
  imports it during startup, so the sidecar exited before its health check. Commit `88ad0cc` stages the `venv`
  and `ensurepip` standard-library modules from exactly pinned CPython 3.12.10, adds `Lib` to the runtime path,
  validates imports, creates a venv, and invokes its provisioned pip before packaging. The full clean Windows
  job passed in [Actions run 38460982270](https://github.com/stevenmcsorley/alteryx-parity-research/actions/runs/38460982270):
  managed runtime staging, three Rust tests (including a real engine round trip), NSIS build, install with
  system runtimes removed from `PATH`, installed Tauri launch, `engine.info`, `catalog.list` count 323, the
  configured deterministic startup workflow, same-version reinstall, and uninstall. The installer artifact is
  available as [flowbench-windows-installer](https://github.com/stevenmcsorley/alteryx-parity-research/actions/runs/38460982270/artifacts/11846361313).
  I also installed that artifact locally and observed the installed executable start its bundled Python
  sidecar; its credential-free app-data log records `engine.info` and all 323 catalog entries. R is not bundled;
  user machines need a separately installed R 4.6.x runtime (CI pins 4.6.0).
- **Acceptance demo:** the full PostgreSQL Customers + SQL Server Orders → Join → Filter → Formula → Summarize
  → Results Grid → CSV/Parquet workflow, a database record mutation/rerun, and export validation have not been
  demonstrated by operating the actual Tauri desktop UI. The installer runs a separate deterministic local
  workflow smoke RPC, which does not prove this two-database UI flow. The computer-control service in this
  session returned no native app/window surfaces and its documented native launch API was unavailable, so
  browser-only or engine-only testing is not represented as acceptance evidence. **Milestone 2.1 remains
  incomplete** until the live desktop flow is run and reproducible evidence is captured.

No compatibility or coverage claims were changed by this work. E1/E2 profiles and the known Sort collation
difference remain as recorded in `docs/COVERAGE.md`.

## Latest delivery

- Branch head at this report update: `88ad0cc819b4963cdbddf043919b5e874fd1af30`.
- PR #2 remains open and based on open PR #1: https://github.com/stevenmcsorley/alteryx-parity-research/pull/2.
- Latest CI: [run 38460982270](https://github.com/stevenmcsorley/alteryx-parity-research/actions/runs/38460982270);
  all five jobs passed (engine, database integration, desktop, Windows desktop shell, and R runtime).
- No Milestone 3 work has started. Resume the existing 323-entry evidence ledger after the remaining actual-desktop
  acceptance gate passes.
