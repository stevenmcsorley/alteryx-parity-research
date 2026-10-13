# Differential conformance harness

`conformance/harness.py` runs each case in `conformance/cases.py` twice — in flowengine and in the authorised reference engine — and compares outputs.

```powershell
python conformance/harness.py                    # all cases; needs %LOCALAPPDATA%\Alteryx\bin\AlteryxEngineCmd.exe
python conformance/harness.py --only join probe-  # subset by case-id prefix (merges into the results file)
python scripts/build_status.py                   # fold results into contracts, ledger and catalogue statuses
python scripts/coverage_report.py
```

Without a reference engine the harness exits and statuses stay `not-tested`; CI never runs it.

## Case anatomy

A case declares: `case_id`, `catalog_id`, original `inputs` (typed fields + rows; `None` = null), the flowengine tool/config, the reference tool's plugin, entry point and configuration XML (authored by us from observed element names), the output-anchor mapping (ours → reference) and the comparison rule (`ordered`, optional numeric `tolerance`, or `expect: both-reject`).

Reference document per case: Text Input (every value prefixed with `~`; nulls as a sentinel) → Formula (strip prefix; sentinel → `Null()`) → Select (declared types) → tool under test → one CSV output per compared anchor (UTF-8, LF, header).

## Result record

`conformance/results/<build>/results.json`: build, engine mode, timestamp, fixed flowengine clock, documented limits, per-tool pass/fail summary, and per case: status, differences, config digest, output digests on both sides, reference errors/warnings and — for failures only — the observed reference output (values on our own synthetic inputs).

## Limits (recorded in every results file)

- CSV cannot distinguish null from empty string; probe cases use `IsNull()` fields where it matters.
- Types are compared only through rendered values.
- Observed while building the harness: the reference delimited-file reader trims unquoted whitespace and narrows text to a code page; its Text Input auto-types numeric-looking columns — hence the `~` prefix design.
- Evidence covers E1 and E2 (AMP) separately; see docs/COMPATIBILITY_PROFILES.md.

## Rules

Comparators are never loosened to make a failure pass. A failing case is either a harness defect (fixed in the harness, documented) or a semantic difference (fixed in the engine with a `[ref:<case>]` citation, plus a unit test).

## Milestone 2 additions

* `--mode e1|e2`: the reference document's `RunWithE2` property selects the engine; results live in
  `conformance/results/<build>/<mode>/results.json`, and the flowengine side runs with the matching compatibility
  profile. Order-insensitive comparison applies only to outputs whose order the active profile declares
  unspecified (values still exact).
* Macro tools: cases can target bundled reference macros (`macro_ref`) using the macro's own question names.
* `ignore_fields` (with a recorded reason) excludes a column only when it necessarily differs by harness
  construction (Field Info's Source column describes the upstream tool chain, which differs between the sides).
* **Holdout** (`conformance/reference/holdout_samples.py`): every shipped sample workflow that validates after
  import is run by both engines. Browse/Output tools are redirected, and a CSV sink is attached to every
  unconnected output anchor of an imported tool. Only pass/fail, digests and difference *locations* are stored.
  E1: 15/31 workflows, 87/126 sinks. E2: 16/31, 88/126.
* Assertions were never loosened. Every change made to pass a case was either a harness defect fix (documented
  in the commit history) or an engine change citing the case.
