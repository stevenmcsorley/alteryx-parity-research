# Milestone 2 report

Reference: **Alteryx Designer 2026.2.0.8** (primary), E1 and E2 engines; 2026.1 kept as an inherited, unverified
profile. Scope remains all 323 catalogue entries. Nothing here claims parity.

## Metrics (separate axes, from data/tool-status.json and conformance results)

| Axis | Milestone 1 | Milestone 2 |
|---|---:|---:|
| Catalogue entries tracked / contracts | 323 / 323 | 323 / 323 |
| Kernels implemented (registered and passing tests) | 26 | **58** |
| Kernels registered without passing evidence (in-progress) | 0 | 1 (R: no R runtime installed) |
| Entries whose reference cases all pass ("partial") | 17 | **32** |
| Entries with a failing reference case | 0 | 1 (Sort: locale collation) |
| Synthetic differential cases, E1 | 48 / 48 | **74 / 75** |
| Synthetic differential cases, E2 | — | **74 / 75** |
| Holdout: shipped sample workflows, E1 (workflows / sinks) | — | **15 / 31, 87 / 126** |
| Holdout: shipped sample workflows, E2 | — | **16 / 31, 88 / 126** |
| Shipped sample workflows that import (validate cleanly) | — | 181 (32) |
| Fully verified tools | 0 | 0 (by rule) |
| Entries blocked by external prerequisites | 78 | 78 |
| Python tests | 98 | **147** (1 skipped: R not installed) |
| Desktop tests (Vitest / Playwright e2e / Rust) | — | **10 / 9 / 2** |

Implemented by category: Parse 4/4, Preparation 20/21, Transform 7/8, Join 6/7, In/Out 6/8, Developer 15/30 (+R in
progress). All other categories (Interface, Reporting, Spatial, Predictive, Time Series, Machine Learning, Text
Mining, Computer Vision, In-Database, Connectors, Address, Demographic, Calgary, Behavior, AI Tools, Connect,
Laboratory, Prescriptive, AB Testing, Predictive Grouping, Data Investigation, Documentation, SDK Examples) have no
kernels yet. They are catalogued, specified from public and structural evidence, and fail validation with
`tool-unavailable`.

## Delivered against the brief

1. **Authorisation:** `docs/AUTHORISATION.md` records the owner's telephone contact with Alteryx and the
   reported written authorisation. Investigation needing the scope written down (decompilation/Ghidra, runtime
   instrumentation) is held until the reference/scope fields are filled. Black-box differential execution and
   structural scans continued.
2. **Milestone 1 review:** all claims re-verified on `94ab2e3` (323 entries, 26 kernels, 98 tests, 48/48 cases,
   17 tools, reproducible outputs). PR #1 opened against `main`, not merged; no blocking defects found
   (non-blocking items listed in the PR).
3. **Reference version and modes:** 2026.2.0.8 primary; E1/E2 profiles with evidence (COMPATIBILITY_PROFILES.md).
4. **Engine:** typed columnar Table with Arrow interop (Parquet, Arrow IPC), streaming of linear segments in
   bounded batches, memory budget with disk spilling, batched parallel scheduling, cancellation, structured
   events, SQLite run history and provenance, deterministic outputs for any worker/batch configuration,
   versioned plugin schemas. Measurements are in BENCHMARKS.md (streaming cut peak memory 104 → 39 MB; threads
   give no CPU speed-up under the GIL; spilling does not lower the peak when one operation dominates).
5. **Desktop app:** Tauri + React + TypeScript, runnable Windows build and NSIS installer (DESKTOP.md). It covers
   the canvas, palette for all 323 entries, schema-driven configuration, run/cancel/rerun with live counts,
   results/fields/messages/performance/history, save/open, import/export, and a code-tool permission prompt.
6. **Tools:** 33 new kernels across Preparation, Join, Parse, Transform and Developer, including Python (isolated
   processes, named pinned environments) and Run Command; R is implemented but unverified.
7. **Python/R:** see RUNTIMES.md.
8. **Workflow compatibility:** `.yxmd/.yxmc/.yxwz/.yxzp` import with full preservation; `.yxmd` export for
   supported tools; read-only `.yxdb` input (INTEROP.md).
9. **Conformance:** E1/E2 modes, macros, probes for collation/dates/rounding/nulls/JSON/naming, and holdout runs
   of shipped sample workflows never used in development (CONFORMANCE.md). Assertions were never loosened.
   The one remaining synthetic failure is published, and Sort is marked `failed`.

## Defects found and fixed along the way

* Validation executed Generate Rows' loop for input-less sources (found by a UI cancellation test).
* Imputation's output type depended on the data (caught by the runtime schema-consistency check).
* Imported Text Input fields defaulted to size 254 and truncated values (found by holdout JSON Parse).
* Stale run results could be shown after loading another workflow (UI).
* One wrong inference was reverted: a Record ID reordering had been attributed to Record ID, but the reference
  tool has no grouping; the cause was an upstream locale Sort.

## Limitations and blockers

| Item | Status / needed |
|---|---|
| Authorisation scope | Owner to fill in the reference and covered activities in AUTHORISATION.md before decompilation or instrumentation work |
| R runtime | Install R (CRAN) on build and CI machines to verify the R tool |
| Embedded Python in the installer | The installer relies on a system Python with the requirements; an embedded runtime and code signing are needed for distribution |
| Sort locale collation | Culture-aware order not reproduced; needs more probes or authorised analysis |
| Spatial type | Not implemented; spatial tools, Map Input and spatial fields in data are unsupported |
| Licensed data / services | 78 entries (Address, Demographic, Calgary, Behavior, Connectors, Connect, AI Tools) need sandbox credentials or data entitlements |
| Macro and app runtime | Macros/apps import with their structure preserved but cannot execute yet (Interface category) |
| Performance | Pure-Python kernels (17–560k records/s single-threaded in this report); Arrow-backed kernels and external sort/aggregate are next |
| CI | The desktop e2e and Windows Rust jobs are new; their first runs are on this PR |

## Next

Interface tools with macro/app runtime (unlocks imported apps and macros), Spatial core (types, Create Points,
Distance, Buffer, Spatial Match), Data Investigation, Reporting, then Predictive on versioned runtimes; Join
select configuration, Text Input typing parity, the locale collation, and holdout coverage growth.
