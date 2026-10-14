# Workflow document interoperability

Implementation: `flowengine/interop/yxmd.py` (independent implementation of the document structure as observed
structurally; docs/AUTHORISATION.md). CLI: `python -m flowengine import <file> -o out.flow.json`,
`python -m flowengine export <wf.flow.json> -o out.yxmd`. Service: `workflow.import`, `workflow.export`.

## Import

| Input | Support |
|---|---|
| `.yxmd` workflow | yes |
| `.yxmc` macro | imported as a workflow; macro inputs/outputs/questions preserved; macro runtime not implemented |
| `.yxwz` analytic app | imported as a workflow; interface tools preserved; app runtime not implemented |
| `.yxzp` package | main workflow imported; package members listed in the report |
| Format versions | read regardless of `yxmdVer`; the version is recorded in the report and document metadata |

Rules:

* **Nothing is dropped.** Tools without a converter keep their original identifier, so validation reports
  `tool-unavailable` naming the catalogue entry. Their original XML is kept in `metadata.import.nodes`.
* Converters translate the settings they understand. Anything else is reported as `partial` with a warning;
  a required setting that cannot be translated makes the node `unsupported`.
* Containers become editor containers (disable state kept) and text boxes become comments; node annotations
  and "disable tool" flags are imported.
* Imported workflows must pass normal validation before they run.

Converters exist for 34 tools (Text Input, Input/Output Data for delimited text and `.yxdb` input, Filter, Formula,
Select, Sort, Unique, Sample, Record ID, Summarize, Join, Union, Append Fields, Running Total, Transpose,
Cross Tab, Text To Columns, RegEx, Multi-Row Formula, Generate Rows, Browse, Auto Field, Multi-Field Formula,
Tile, Find Replace, Make Group, DateTime, XML Parse, Make Columns, Field Info, JSON Parse, Dynamic Rename,
Dynamic Select) and 4 macros (Count Records, Weighted Average, Select Records, Imputation).

Survey over the reference install's 181 shipped sample workflows (`data/import-survey.json`, counts only):
181/181 import, 3,094 nodes mapped, 279 partial, 714 unsupported, and 32 workflows validate cleanly after import.

## Export

`.yxmd` export re-emits the original XML for imported nodes whose configuration is unchanged, and generates XML
for native Text Input, Filter, Formula, Sort, Summarize, Unique and Browse nodes. It returns a list of nodes
and edges it cannot express. The CLI refuses to write a partial export; the service writes it only with
`allow_partial`.

## Data files

* `.yxdb` **input** uses the MIT-licensed third-party `yxdb` package (Tom Larsen). Its type model is coarse:
  text becomes `v_wstring`, integers `int64`, decimals `double`; original sizes, integer widths and fixed-decimal
  precision are not available, and spatial objects arrive as blobs.
* `.yxdb` output is not supported.
