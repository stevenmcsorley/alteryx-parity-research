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
- Evidence is for the E1 engine only.

## Rules

Comparators are never loosened to make a failure pass. A failing case is either a harness defect (fixed in the harness, documented) or a semantic difference (fixed in the engine with a `[ref:<case>]` citation, plus a unit test).
