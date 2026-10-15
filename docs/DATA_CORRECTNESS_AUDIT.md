# Flowbench Data Type and Semantic Correctness Audit

Baseline: origin/main a3cca948d1eff821ea7460c41a90706298188fa3
Branch: audit/data-type-correctness
Reference: Alteryx Designer 2026.2.0.8; E1 and E2/AMP reported separately.

## Scope and ledger

Inspected Python types/casts, Table/schema propagation, Arrow and file/database boundaries, formula comparisons, processing kernels, macro and Python/R exchange, workflow import/export, React field configuration/results, Rust RPC transport, contracts and conformance evidence.

The generated ledger reports 323 catalogue entries across 321 names, 64 registered kernels (63 meeting the ledger's implemented rule; R remains in progress because its optional integration test is excluded from the cross-platform tool map), one Action adapter in progress, 258 not started, 32 partially reference-tested entries, zero fully verified entries and one failing reference case. The separate R-enabled acceptance job executes the R kernel; the generic status ledger remains environment-independent. All coverage counts below are evidence-derived, not palette counts.

## CSV and Filter reproduction

Synthetic 15-row Quantity values: 0, 1, 10, 09, 00123, -2, 2, 3, 5, 6, 11, 12, 20, 50, 100. Basic Filter: Quantity > "10".

| Observation | Result |
|---|---|
| Untyped CSV | InputData intentionally assigns v_wstring to delimited columns unless Fields declares types. |
| Text comparison | 9 True / 6 False; "2" > "10" is lexicographically true. Correct for a string field. |
| Declared types | Quantity:int32 and Sales:fixed_decimal(8,2) yield numeric comparison. Explicitly casting 00123 to integer yields 123. |
| Alteryx observation | Repository evidence records delimited Input Data as V_String size 254 with unquoted whitespace trimming. This does not establish numeric inference. Text Input auto-typing is separate. |
| Field size | 2^30-1 is the internal UNBOUNDED_TEXT sentinel, not observed length. Inspector now displays “Unbounded.” |

The owner’s exact CSV was not present; this synthetic fixture reproduces the reported count and symptom. No Filter-kernel defect is established for v_wstring. A real UI/engine risk existed: the basic Filter editor sends the threshold as text even for numeric fields and did not explain string ordering. Filter now binds basic comparison literals to the selected field type; numeric text uses Decimal for exact int64 thresholds. The Inspector explains text ordering and explicit typing via Input Data Fields or Select. Expression mode is unchanged.

## Confirmed defects and resolutions

The confirmed table contains one Critical exact-value Union corruption, five High-priority precision/schema/comparison/ordering defects, and one Medium field-aware Filter literal/configuration defect covering numeric and temporal fields. The reported 15-row string Filter outcome is separately diagnosed as intended `v_wstring` ordering, not as a wrong engine comparison.

| Component / source | Severity | Root cause and correction | Regression evidence | Parity |
|---|---|---|---|---|
| Union: types.py::unify_fields, tools/join.py | Critical | Rank-based promotion converted fixed_decimal(25,2) 9007199254740993.01 plus int64 9007199254740993 to Double, rounding both. Exact decimal/integer precision is now derived; unsafe decimal/float and int64/float promotion fail explicitly. | test_union_preserves_large_integer_and_decimal_exactly; test_union_rejects_unproven_lossy_numeric_promotions | New promotion unverified against vendor. Existing Union case passes. |
| FixedDecimal schema merge: types.py::unify_fields | High | Independent max precision/scale could shrink integral capacity. Now precision = maximum integral digits + maximum scale; fail above 50. | test_unify_exact_decimal_and_integer_without_binary_float | Independent policy; vendor case unavailable. |
| Arrow/Parquet: arrow.py::_arrow_type | High | Precision above 38 was forced into decimal128. Now decimal128 through 38 and decimal256 above. | test_arrow_decimal_precision_above_38_round_trips_exactly | Arrow roundtrip verified; vendor interop above 38 unverified. |
| Join: tools/join.py::Join.run | High | String/numeric family booleans let DATE-to-TIME keys through, silently yielding no matches. Now require exact type or same string/numeric family. | test_sort_rejects_nan_and_join_rejects_incompatible_temporal_keys | Existing E1/E2 joins pass; DATE/TIME vendor error unverified. |
| Sort: tools/preparation.py::Sort.run | High | NaN made comparator inconsistent. Sort now rejects NaN keys. | test_sort_rejects_nan_and_join_rejects_incompatible_temporal_keys | NaN behavior unverified; known locale discrepancy remains. |
| Casts: types.py::cast_value, _to_decimal | High | Non-finite Decimal and extreme values could escape as internal exceptions or finite Decimal overflow could become infinity. Now normalize failures as CastFailure and reject non-finite FixedDecimal. | test_fixed_decimal_nonfinite_and_extreme_cast_failures_are_explicit | New error behavior unverified. |
| Basic Filter literal binding: preparation.py, Inspector.tsx | Medium | Basic mode left numeric and temporal literals as strings and the UI did not explain lexical text ordering. Literals now bind to numeric, Boolean, Date, Time and DateTime field types; invalid typed values fail during validation. Inspector guidance distinguishes text ordering and recommends explicit conversion. | `test_filter_numeric_literal_int64_precision`; `test_basic_filter_temporal_literals_are_typed`; CSV UI/engine E2E | Formula/date E1/E2 cases pass; basic-mode vendor configuration remains unverified. |

## Type and conversion matrix

flowengine/types.py is authoritative. Types include Bool, Byte, Int16/32/64, Float/Double, FixedDecimal, String/WString/V_String/V_WString, Date/Time/DateTime and Blob. None represents null. Spatial geometry is not native.

| Conversion | Policy | Failure and precision |
|---|---|---|
| Null → any | Preserve null | Tool-specific behavior; Formula/Filter/Join null cases have E1/E2 evidence. |
| Same type | Identity or adapter representation | Invalid external payload errors; Arrow stores Flowbench type/size/scale metadata. |
| String → number/bool/date/time | Explicit Input Data Fields, Select or Formula | Strict grammar; error or configured null-with-warning. Leading-zero identifiers stay text until explicitly cast. |
| Integer widening | Union promotion | Exact in destination range; overflow fails. Numeric → integer rounds half away from zero per E1 cases probe-select-int-rounding and select-cast-rename-drop. |
| Integer/Bool + FixedDecimal | Exact Union FixedDecimal | Derived precision; error above 50. New rule is reference-unverified. |
| FixedDecimal + FixedDecimal | Precision = max integral digits + max scale | Error above 50; cast rounding is half-even and incompletely reference-tested. |
| FixedDecimal/Int64 + binary float | No implicit Union conversion | Explicit error prevents silent precision loss; Alteryx behavior unknown. Int32 + float may promote to Double. |
| Formula comparison: FixedDecimal + Double | Promote both operands to Double | Confirmed by `probe-fixed-decimal-double-comparison` (75/76 overall in both E1 and E2; this case passes): FixedDecimal 0.10 compares equal to the Double literal 0.1. This can round decimal precision, but it is pinned-reference behavior rather than a Flowbench-only discrepancy. |
| Float/Double | Binary floating | Finite integer/Decimal overflow checked. Sort rejects NaN; other NaN semantics unverified. |
| Scalar/date/blob → string | Explicit cast or heterogeneous Union-to-text | Truncation warns. ANSI code-page and rendering parity incomplete. |
| Date ↔ DateTime | Explicit cast or Union promotion | Strict ISO parsing; DateTime is naive; timezone/DST parity unverified. |
| Incompatible families | No generic cast; Join rejects unlike keys | Explicit error instead of successful all-unmatched Join. |

## Path assessment and unresolved risks

| Path | Findings |
|---|---|
| Types, Formula, Filter | Shared cast/comparison logic; string case, null semantics and mixed FixedDecimal/Double formula comparison have reference evidence. |
| Table/executor | Table operations preserve schema; constructor does not validate every cell against its Field, so adapters/kernels must uphold the invariant. |
| CSV/TSV | No inference by design; explicit schema available. Empty-as-null configurable; ragged rows error/pad. Alteryx width and whitespace differ. |
| JSON/JSONL | Homogeneous scalars infer; mixed/structured values become text; missing becomes null. Large numbers and nonstandard NaN JSON require more evidence. |
| Arrow/Parquet/spill | Metadata roundtrip and Decimal256 fix. Foreign timezone timestamps are stripped to naive DateTime. |
| YXDB | Coarse types; source widths and FixedDecimal precision unavailable, with diagnostic. |
| PostgreSQL/SQL Server | Decimal uses Decimal; zoned timestamps normalize UTC then become naive. Driver precision/money/temporal edges need fixtures. |
| Processing kernels | All registered kernels are inventoried below. Test presence does not prove all option/type combinations. |
| React UI/results | Displays engine schema; preview formatting does not mutate values. Other JS Number controls are unsafe for arbitrary int64. |
| Import/export | Imported CSV has no inferred schema unless configured. Unknown settings are not claimed lossless; unbounded sentinel may appear in some serialized metadata. |
| Python/R exchange | Typed headers preserve schema; headerless mixed numeric outputs can widen to Double; large integer plus float needs tests. |
| Rust/Tauri | RPC transports JSON; no separate numeric table conversion found. Native app interaction unavailable here. |

## Registered tool inventory

Generated from data/tool-status.json, manifests, test counts and verification ledger. “Reviewed” means type/schema source paths were inspected, not all options are proven.

| Catalogue ID | Tool | Kernel | Source | Type path reviewed | Independent tests (ledger) | Reference status |
|---|---|---|---|---|---:|---|
| in-out:browse | Browse | `core.browse` | `flowengine/tools/inout.py` | Input/output parsing, type declarations, null representation and format mappings. | 23 | not-tested |
| in-out:date-time-now | Date Time Now | `core.date_time_now` | `flowengine/tools/inout.py` | Input/output parsing, type declarations, null representation and format mappings. | 1 | not-tested |
| in-out:directory | Directory | `core.directory` | `flowengine/tools/inout.py` | Input/output parsing, type declarations, null representation and format mappings. | 1 | not-tested |
| in-out:input-data | Input Data | `core.input_data` | `flowengine/tools/inout.py` | Input/output parsing, type declarations, null representation and format mappings. | 15 | not-tested |
| in-out:output-data | Output Data | `core.output_data` | `flowengine/tools/inout.py` | Input/output parsing, type declarations, null representation and format mappings. | 9 | not-tested |
| in-out:text-input | Text Input | `core.text_input` | `flowengine/tools/inout.py` | Input/output parsing, type declarations, null representation and format mappings. | 118 | not-tested |
| preparation:auto-field | Auto Field | `core.auto_field` | `flowengine/tools/prep_extra.py` | Sampling, inference, fill/binning and schema propagation. | 1 | partial |
| preparation:create-samples | Create Samples | `core.create_samples` | `flowengine/tools/prep_extra.py` | Sampling, inference, fill/binning and schema propagation. | 1 | not-tested |
| preparation:data-cleanse-pro | Data Cleanse Pro | `core.data_cleanse_pro` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 3 | not-tested |
| preparation:data-cleansing | Data Cleansing | `core.data_cleansing` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 3 | not-tested |
| preparation:filter | Filter | `core.filter` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 24 | partial |
| preparation:formula | Formula | `core.formula` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 22 | partial |
| preparation:generate-rows | Generate Rows | `core.generate_rows` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 3 | not-tested |
| preparation:imputation | Imputation | `core.imputation` | `flowengine/tools/prep_extra.py` | Sampling, inference, fill/binning and schema propagation. | 1 | partial |
| preparation:multi-field-binning | Multi-Field Binning | `core.multi_field_binning` | `flowengine/tools/prep_extra.py` | Sampling, inference, fill/binning and schema propagation. | 1 | not-tested |
| preparation:multi-field-formula | Multi-Field Formula | `core.multi_field_formula` | `flowengine/tools/prep_extra.py` | Sampling, inference, fill/binning and schema propagation. | 1 | partial |
| preparation:multi-row-formula | Multi-Row Formula | `core.multi_row_formula` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 1 | partial |
| preparation:oversample-field | Oversample Field | `core.oversample_field` | `flowengine/tools/prep_extra.py` | Sampling, inference, fill/binning and schema propagation. | 1 | not-tested |
| preparation:random-sample | Random % Sample | `core.random_sample` | `flowengine/tools/prep_extra.py` | Sampling, inference, fill/binning and schema propagation. | 1 | not-tested |
| preparation:rank | Rank | `core.rank` | `flowengine/tools/prep_extra.py` | Sampling, inference, fill/binning and schema propagation. | 1 | not-tested |
| preparation:record-id | Record ID | `core.record_id` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 4 | partial |
| preparation:sample | Sample | `core.sample` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 2 | partial |
| preparation:select | Select | `core.select` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 8 | partial |
| preparation:select-records | Select Records | `core.select_records` | `flowengine/tools/prep_extra.py` | Sampling, inference, fill/binning and schema propagation. | 1 | partial |
| preparation:sort | Sort | `core.sort` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 14 | failed |
| preparation:tile | Tile | `core.tile` | `flowengine/tools/prep_extra.py` | Sampling, inference, fill/binning and schema propagation. | 1 | partial |
| preparation:unique | Unique | `core.unique` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys, ordering and null semantics. | 5 | partial |
| join:append-fields | Append Fields | `core.append_fields` | `flowengine/tools/join.py` | Join key compatibility, Union coercion, null matching and schema unification. | 1 | partial |
| join:find-replace | Find Replace | `core.find_replace` | `flowengine/tools/join_parse_extra.py` | Join-adjacent parsing and dynamic schema/type paths. | 1 | partial |
| join:fuzzy-match | Fuzzy Match | `core.fuzzy_match` | `flowengine/tools/fuzzy_match.py` | Fuzzy key generation, normalization, score output and value preservation. | 16 | not-tested |
| join:join | Join | `core.join` | `flowengine/tools/join.py` | Join key compatibility, Union coercion, null matching and schema unification. | 10 | partial |
| join:join-multiple | Join Multiple | `core.join_multiple` | `flowengine/tools/join_parse_extra.py` | Join-adjacent parsing and dynamic schema/type paths. | 1 | not-tested |
| join:make-group | Make Group | `core.make_group` | `flowengine/tools/join_parse_extra.py` | Join-adjacent parsing and dynamic schema/type paths. | 1 | partial |
| join:union | Union | `core.union` | `flowengine/tools/join.py` | Join key compatibility, Union coercion, null matching and schema unification. | 7 | partial |
| parse:datetime | DateTime | `core.datetime` | `flowengine/tools/join_parse_extra.py` | Join-adjacent parsing and dynamic schema/type paths. | 1 | partial |
| parse:regex | RegEx | `core.regex` | `flowengine/tools/parse.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| parse:text-to-columns | Text To Columns | `core.text_to_columns` | `flowengine/tools/parse.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| parse:xml-parse | XML Parse | `core.xml_parse` | `flowengine/tools/join_parse_extra.py` | Join-adjacent parsing and dynamic schema/type paths. | 1 | partial |
| transform:arrange | Arrange | `core.arrange` | `flowengine/tools/transform.py` | Aggregation and generated output schema/type propagation. | 2 | not-tested |
| transform:count-records | Count Records | `core.count_records` | `flowengine/tools/transform.py` | Aggregation and generated output schema/type propagation. | 7 | partial |
| transform:cross-tab | Cross Tab | `core.cross_tab` | `flowengine/tools/transform.py` | Aggregation and generated output schema/type propagation. | 1 | partial |
| transform:make-columns | Make Columns | `core.make_columns` | `flowengine/tools/join_parse_extra.py` | Join-adjacent parsing and dynamic schema/type paths. | 1 | partial |
| transform:running-total | Running Total | `core.running_total` | `flowengine/tools/transform.py` | Aggregation and generated output schema/type propagation. | 1 | partial |
| transform:summarize | Summarize | `core.summarize` | `flowengine/tools/transform.py` | Aggregation and generated output schema/type propagation. | 10 | partial |
| transform:transpose | Transpose | `core.transpose` | `flowengine/tools/transform.py` | Aggregation and generated output schema/type propagation. | 1 | partial |
| transform:weighted-average | Weighted Average | `core.weighted_average` | `flowengine/tools/join_parse_extra.py` | Join-adjacent parsing and dynamic schema/type paths. | 1 | partial |
| interface:macro-input | Macro Input | `flowengine.macro_input` | `flowengine/tools/macros.py` | Macro input/output schema and typed boundary propagation. | 13 | not-tested |
| interface:macro-output | Macro Output | `flowengine.macro_output` | `flowengine/tools/macros.py` | Macro input/output schema and typed boundary propagation. | 13 | not-tested |
| developer:base64-encoder | Base64 Encoder | `core.base64_encoder` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | not-tested |
| developer:blob-convert | Blob Convert | `core.blob_convert` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | not-tested |
| developer:block-until-done | Block Until Done | `core.block_until_done` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | not-tested |
| developer:detour | Detour | `core.detour` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | not-tested |
| developer:detour-end | Detour End | `core.detour_end` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | not-tested |
| developer:dynamic-rename | Dynamic Rename | `core.dynamic_rename` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | partial |
| developer:dynamic-select | Dynamic Select | `core.dynamic_select` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | partial |
| developer:expect-equals | Expect Equals | `core.expect_equals` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | not-tested |
| developer:field-info | Field Info | `core.field_info` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | partial |
| developer:json-parse | JSON Parse | `core.json_parse` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | partial |
| developer:message | Message | `core.message` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | not-tested |
| developer:python | Python | `core.python` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 4 | not-tested |
| developer:r | R | `core.r` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 0 | not-tested |
| developer:run-command | Run Command | `core.run_command` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | not-tested |
| developer:test | Test | `core.test` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | not-tested |
| developer:throttle | Throttle | `core.throttle` | `flowengine/tools/developer.py` | Runtime boundaries and cross-language type interchange. | 1 | not-tested |
| interface:action | Action | application adapter (not a kernel) | `flowengine/interop/yxmd.py` | Imported configuration preservation and isolated application execution; no tabular kernel. | 5 | In progress; reference unverified |

## Verification

Targeted regressions: eight test methods, included in pytest and unittest discovery. Browser E2E uses the real engine, not the installed-Tauri UI.

Full authorized differential rerun:

| Profile | Passed | Failure |
|---|---:|---|
| E1 2026.2.0.8 | 75/76 | probe-sort-locale-1033, known locale discrepancy |
| E2/AMP 2026.2.0.8 | 75/76 | probe-sort-locale-1033, known locale discrepancy |

No assertions were weakened. New Union/Arrow/error policies are not Alteryx parity claims.

## Native Windows acceptance and remaining risks

Windows-hosted Playwright reproduces Input Data → Filter → Browse with real CSV and engine, then declares Quantity:int32 and Sales:fixed_decimal(8,2) and checks the changed output. The installed native Tauri application was not controlled in this session; do not claim installed-app acceptance.

Additional unverified areas: authorized comparisons for decimal/float Union policies; Arrow timezone preservation; headerless Python/R mixed numeric inference; Summarize floating statistics and FixedDecimal sum limits; ANSI code pages; YXDB widths; JSON NaN/Infinity; SQL driver maximum precision. Many tools have one independent test only; generated statuses remain unchanged by this audit.

## Implemented tool audit inventory

| Catalogue ID | Tool | Kernel | Source | Type path reviewed | Independent tests | Reference status |
|---|---|---|---|---|---:|---|
| in-out:browse | Browse | `core.browse` | `flowengine/tools/inout.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 23 | not-tested |
| in-out:date-time-now | Date Time Now | `core.date_time_now` | `flowengine/tools/inout.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | not-tested |
| in-out:directory | Directory | `core.directory` | `flowengine/tools/inout.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | not-tested |
| in-out:input-data | Input Data | `core.input_data` | `flowengine/tools/inout.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 15 | not-tested |
| in-out:output-data | Output Data | `core.output_data` | `flowengine/tools/inout.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 9 | not-tested |
| in-out:text-input | Text Input | `core.text_input` | `flowengine/tools/inout.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 119 | not-tested |
| preparation:auto-field | Auto Field | `core.auto_field` | `flowengine/tools/prep_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| preparation:create-samples | Create Samples | `core.create_samples` | `flowengine/tools/prep_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | not-tested |
| preparation:data-cleanse-pro | Data Cleanse Pro | `core.data_cleanse_pro` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 3 | not-tested |
| preparation:data-cleansing | Data Cleansing | `core.data_cleansing` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 3 | not-tested |
| preparation:filter | Filter | `core.filter` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 24 | partial |
| preparation:formula | Formula | `core.formula` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 22 | partial |
| preparation:generate-rows | Generate Rows | `core.generate_rows` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 3 | not-tested |
| preparation:imputation | Imputation | `core.imputation` | `flowengine/tools/prep_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| preparation:multi-field-binning | Multi-Field Binning | `core.multi_field_binning` | `flowengine/tools/prep_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | not-tested |
| preparation:multi-field-formula | Multi-Field Formula | `core.multi_field_formula` | `flowengine/tools/prep_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| preparation:multi-row-formula | Multi-Row Formula | `core.multi_row_formula` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 1 | partial |
| preparation:oversample-field | Oversample Field | `core.oversample_field` | `flowengine/tools/prep_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | not-tested |
| preparation:random-sample | Random % Sample | `core.random_sample` | `flowengine/tools/prep_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | not-tested |
| preparation:rank | Rank | `core.rank` | `flowengine/tools/prep_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | not-tested |
| preparation:record-id | Record ID | `core.record_id` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 4 | partial |
| preparation:sample | Sample | `core.sample` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 2 | partial |
| preparation:select | Select | `core.select` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 8 | partial |
| preparation:select-records | Select Records | `core.select_records` | `flowengine/tools/prep_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| preparation:sort | Sort | `core.sort` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 14 | failed |
| preparation:tile | Tile | `core.tile` | `flowengine/tools/prep_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| preparation:unique | Unique | `core.unique` | `flowengine/tools/preparation.py` | Select casts, Filter comparisons, Sort keys and null ordering. | 5 | partial |
| join:append-fields | Append Fields | `core.append_fields` | `flowengine/tools/join.py` | Join/Union key compatibility, coercion, null matching and schema unification. | 1 | partial |
| join:find-replace | Find Replace | `core.find_replace` | `flowengine/tools/join_parse_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| join:fuzzy-match | Fuzzy Match | `core.fuzzy_match` | `flowengine/tools/fuzzy_match.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 16 | not-tested |
| join:join | Join | `core.join` | `flowengine/tools/join.py` | Join/Union key compatibility, coercion, null matching and schema unification. | 10 | partial |
| join:join-multiple | Join Multiple | `core.join_multiple` | `flowengine/tools/join_parse_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | not-tested |
| join:make-group | Make Group | `core.make_group` | `flowengine/tools/join_parse_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| join:union | Union | `core.union` | `flowengine/tools/join.py` | Join/Union key compatibility, coercion, null matching and schema unification. | 7 | partial |
| parse:datetime | DateTime | `core.datetime` | `flowengine/tools/join_parse_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| parse:regex | RegEx | `core.regex` | `flowengine/tools/parse.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| parse:text-to-columns | Text To Columns | `core.text_to_columns` | `flowengine/tools/parse.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| parse:xml-parse | XML Parse | `core.xml_parse` | `flowengine/tools/join_parse_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| transform:arrange | Arrange | `core.arrange` | `flowengine/tools/transform.py` | Aggregation, transpose/crosstab schemas and value-preserving transforms. | 2 | not-tested |
| transform:count-records | Count Records | `core.count_records` | `flowengine/tools/transform.py` | Aggregation, transpose/crosstab schemas and value-preserving transforms. | 7 | partial |
| transform:cross-tab | Cross Tab | `core.cross_tab` | `flowengine/tools/transform.py` | Aggregation, transpose/crosstab schemas and value-preserving transforms. | 1 | partial |
| transform:make-columns | Make Columns | `core.make_columns` | `flowengine/tools/join_parse_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| transform:running-total | Running Total | `core.running_total` | `flowengine/tools/transform.py` | Aggregation, transpose/crosstab schemas and value-preserving transforms. | 1 | partial |
| transform:summarize | Summarize | `core.summarize` | `flowengine/tools/transform.py` | Aggregation, transpose/crosstab schemas and value-preserving transforms. | 10 | partial |
| transform:transpose | Transpose | `core.transpose` | `flowengine/tools/transform.py` | Aggregation, transpose/crosstab schemas and value-preserving transforms. | 1 | partial |
| transform:weighted-average | Weighted Average | `core.weighted_average` | `flowengine/tools/join_parse_extra.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 1 | partial |
| interface:macro-input | Macro Input | `flowengine.macro_input` | `flowengine/tools/macros.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 13 | not-tested |
| interface:macro-output | Macro Output | `flowengine.macro_output` | `flowengine/tools/macros.py` | Tool-specific schema, cast, null and output metadata path reviewed. | 13 | not-tested |
| developer:base64-encoder | Base64 Encoder | `core.base64_encoder` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| developer:blob-convert | Blob Convert | `core.blob_convert` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| developer:block-until-done | Block Until Done | `core.block_until_done` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| developer:detour | Detour | `core.detour` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| developer:detour-end | Detour End | `core.detour_end` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| developer:dynamic-rename | Dynamic Rename | `core.dynamic_rename` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | partial |
| developer:dynamic-select | Dynamic Select | `core.dynamic_select` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | partial |
| developer:expect-equals | Expect Equals | `core.expect_equals` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| developer:field-info | Field Info | `core.field_info` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | partial |
| developer:json-parse | JSON Parse | `core.json_parse` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | partial |
| developer:message | Message | `core.message` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| developer:python | Python | `core.python` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 4 | not-tested |
| developer:r | R | `core.r` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| developer:run-command | Run Command | `core.run_command` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| developer:test | Test | `core.test` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| developer:throttle | Throttle | `core.throttle` | `flowengine/tools/developer.py` | Runtime boundary and type interchange for developer tools. | 1 | not-tested |
| interface:action | Action | application adapter (not a kernel) | `flowengine/interop/yxmd.py` | Imported action configuration preservation and isolated app execution; no tabular kernel. | 5 | In progress; reference unverified |


## Verification evidence

- Python: `python -m pytest -q -rs` — 213 passed, 12 skipped, 0 failed. The 12 skips require real PostgreSQL/SQL Server TLS fixtures; the acceptance orchestrator reports that environment as BLOCKED, not PASS.
- Browser desktop and real engine: `npx playwright test` — 15 passed, 0 failed. The new CSV test exercises the actual UI with a temporary CSV and the Python engine.
- TypeScript unit tests: `npm test` — 12 passed. Production build: `npm run build` — passed.
- Rust/Tauri: `cargo test --manifest-path desktop/src-tauri/Cargo.toml` — 3 passed, 0 failed (cargo invoked from the installed Rust toolchain).
- Differential reference: E1 75/76 and E2 75/76. The new mixed FixedDecimal/Double comparison probe passes in both profiles. The sole remaining failure in each is the recorded Sort locale collation case; it remains visible. The full acceptance holdout campaign reports existing workflow mismatches, itemised in its generated evidence report.
- Generated-ledger checks: `python scripts/build_status.py --check` and `python scripts/build_parity_roadmap.py --check` — passed both with and without R discoverable. The ledger reports 323 catalogue entries, 64 registered kernels (63 implemented, R and Action in progress), 32 partial reference records, 0 fully verified entries, and 78 external prerequisites.
- Native installed Tauri acceptance: not run. The available environment exposes browser Playwright and Rust tests but no native GUI automation surface. The Playwright test does not substitute for installed-app verification.
- Full acceptance orchestrator: 10 suites passed, 4 failed, 1 blocked, and 4 not run (acceptance run `20261015T023355Z`). The four failing suites are E1/E2 conformance (75/76 in each; the remaining Sort locale mismatch) and E1/E2 holdouts (15/31 workflows, 87/126 sinks in E1; 16/31 workflows, 88/126 sinks in E2). Database integration is blocked because OpenSSL is not on PATH for provisioning the PostgreSQL TLS fixture. R 4.6.0 was found through Flowbench's runtime resolver and its tool test passed. Spatial, connector contracts, performance and isolated Windows installer smoke were not run. Rust/Tauri passed in this run. Full JSON/Markdown evidence is in `acceptance-results-data-audit-final/`.

## Release risks and outstanding validation

The exact owner-provided CSV was unavailable. The reproduced behavior establishes that lexicographic comparison is correct when Quantity is explicitly `v_wstring`; it does not establish whether Alteryx would infer numeric types for that particular file. The repository’s reference evidence supports a delimited-input text schema unless configured. Decimal/float Union rejection, DATE/TIME Join incompatibility, NaN Sort rejection and new decimal precision rules have regression coverage but need explicit Alteryx E1/E2 observations before parity claims. Formula FixedDecimal/Double promotion is now reference-tested, but its precision loss is an observed compatibility behavior. JSON large-number behavior, timezones/DST, driver-specific decimal mappings, ANSI encodings, R/Python mixed output and many per-tool option/type combinations remain unverified. The native installed Windows UI was not operated in this session: the Windows Computer Use native pipe was unavailable. The actual temporary-CSV scenario passed through Playwright UI plus the real local engine, which is not a substitute for installed-Tauri verification.
