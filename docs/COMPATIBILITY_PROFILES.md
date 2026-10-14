# Compatibility profiles

Primary reference: **Alteryx Designer 2026.2.0.8** (installed build). Every run records its profile in the run
manifest and history; kernels branch on a profile switch only where reference evidence shows the engines differ.
Implementation: `flowengine/profiles.py`.

| Profile | Build | Engine | Status | Differences from `ayx-2026.2-e1` |
|---|---|---|---|---|
| `ayx-2026.2-e1` (default) | 2026.2.0.8 | E1 (original) | evidence-backed | — (all `[ref:*]` rules in ENGINE_SEMANTICS.md were observed here) |
| `ayx-2026.2-e2` | 2026.2.0.8 | E2 (AMP, `RunWithE2=True`) | evidence-backed | doubles render shortest round-trip (17 digits) instead of 15 significant digits; output order of Join, Summarize, Cross Tab, Running Total, grouped Sample and the Imputation macro is not defined |
| `ayx-2026.1-e1` | 2026.1.4.126 | E1 | inherited-unverified | none known; no 2026.1 install available. The public 2026.1 and current tool lists were identical on 2026-10-13 |

## How E2 differences are handled

* **Number rendering** is a real output difference and is switched by `double_text` (`g15` vs `shortest`).
* **Unspecified order**: under E2 the reference returns rows of the listed tools in an order that follows no
  stable rule (observed on `e2/summarize-basic`, `e2/join-many-to-many-nulls`, `e2/probe-join-unjoined-order`,
  `e2/sample-first-grouped`, `e2/imputation-mean-macro`). flowengine still produces the E1 key order (a valid
  instance of an unspecified order, keeping its own runs deterministic). The harness compares those outputs as
  multisets under E2 only; values are still compared exactly. Ungrouped Sample stays order-checked.

## Evidence

| Suite | E1 | E2 |
|---|---|---|
| Synthetic differential cases (`conformance/cases*.py`) | 74 / 75 | 74 / 75 |
| Holdout: shipped sample workflows (workflows / sinks) | 15 / 31, 87 / 126 | 16 / 31, 88 / 126 |

The single failing synthetic case in both modes is `probe-sort-locale-1033` (see ENGINE_SEMANTICS.md, known
differences).
