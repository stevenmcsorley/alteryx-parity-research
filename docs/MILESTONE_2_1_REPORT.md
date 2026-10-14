# Milestone 2.1 — database integration and desktop packaging

Status as of 14 October 2026: **in progress; owner-reported desktop run received, remaining manual evidence pending**.
Automated evidence and owner-reported evidence are stated separately below.

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

## Owner-reported manual evidence

The repository owner reports that the installed Windows Flowbench application connected to the PostgreSQL and
SQL Server fixture databases and successfully ran the configured analysis workflow. This is an owner-reported
manual result, not a Codex-observed UI session. No screenshots, exported-file validation, fixture mutation/rerun
result, or redacted run manifest were supplied with the report. Those substeps remain unverified. The workflow
connection/run report is recorded as received; the evidence bundle is still requested for review before the
manual acceptance gate can be closed.

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
- **Acceptance demo:** the owner reports connecting the installed UI to both databases and running the configured
  analysis workflow (see owner-reported evidence above). Codex did not independently operate the UI. Screenshots,
  validation of the PostgreSQL Customers + SQL Server Orders → Join → Filter → Formula → Summarize → Results Grid
  → CSV/Parquet exports, and a database record mutation/rerun were not supplied. The installer runs a separate deterministic local
  workflow smoke RPC, which does not prove this two-database UI flow. The computer-control service in this
  session returned no native app/window surfaces and its documented native launch API was unavailable, so
  browser-only or engine-only testing is not represented as acceptance evidence. **Milestone 2.1 remains
  incomplete** until the live desktop flow is run and reproducible evidence is captured.
- Added [`docs/MILESTONE_2_1_MANUAL_ACCEPTANCE.md`](MILESTONE_2_1_MANUAL_ACCEPTANCE.md), the Windows PowerShell
  fixture setup/mutation/restore procedure, and a credential-filtered run-manifest exporter for the repository
  owner to perform the remaining installed-UI check. These are instructions and utilities, not manual acceptance
  evidence. Their syntax, live-fixture read-only readiness checks, guarded mutation/restoration, and manifest
  secret-exclusion behavior were locally checked. Automated CI and integration evidence remain separately
  identified above.

The E1/E2 profiles and known Sort collation difference remain recorded in `docs/COVERAGE.md`. Current catalogue
implementation totals include the separately tracked Milestone 3 macro-boundary work.

## Latest delivery

- Installer fix: `88ad0cc819b4963cdbddf043919b5e874fd1af30`.
- PR #2 remains open and based on open PR #1: https://github.com/stevenmcsorley/alteryx-parity-research/pull/2.
- Latest base-branch CI at commit `83ed201`: [run 38527685675](https://github.com/stevenmcsorley/alteryx-parity-research/actions/runs/38527685675);
  all five jobs passed (engine, database integration, desktop, Windows desktop shell, and R runtime). The installer
  clean-runner evidence remains the earlier linked run `38460982270`.
- Milestone 3 work is tracked on the separate `milestone-3-macro-interface-parity` branch. The catalogue and
  evidence ledger are preserved; see the generated `docs/ALTERYX_PARITY_ROADMAP.md`.
