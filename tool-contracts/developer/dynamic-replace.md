# Dynamic Replace

Independent Flowbench implementation: `core.dynamic_replace` (`developer:dynamic-replace`). It consumes data on D and ordered replacement rules on R, returning changed data on O and a replacement-count summary on S.

The native configuration selects rule columns for target field, predicate and replacement; replacements may be formula expressions. First matching rule per field and record wins. Output type behavior can retain the input type, infer conservatively from resulting values, or cast to a configured type (including fixed-decimal precision and scale). Unmatched field names can be allowed, warned or rejected.

The D/R/O/S anchors and ordered expression behavior are described by the [official Dynamic Replace documentation](https://help.alteryx.com/current/en/designer/tools/developer/dynamic-replace-tool.html). The independent native behavior has executable coverage in `tests/test_dynamic_replace.py`. **This tool is not reference verified.** Evidence limits: Alteryx XML configuration translation and differential parity are not implemented or verified; imported vendor configurations must not be treated as equivalent to the native Flowbench configuration.
