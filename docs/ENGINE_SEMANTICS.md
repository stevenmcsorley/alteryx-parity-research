# Engine semantics: evidence-backed rules and open policies

Every rule here is either **observed** on the reference engine by a differential case (id in brackets; results in `conformance/results/<build>/results.json`) or an **engine policy** that has not yet been compared. Reference: build 2026.2.0.8, original (E1) engine, `RunWithE2=False`.

## Observed and adopted (ADR-007)

| Area | Rule | Evidence |
|---|---|---|
| String comparison | `=`, `!=`, `<`, `<=`, `>`, `>=`, `IN` compare strings case-insensitively (`"a" < "B"`, `"é" = "É"`) | filter-string-eq-case, filter-not-in, probe-string-ordering |
| Contains/StartsWith/EndsWith | Optional 3rd argument is a case-insensitive flag, default 1; null text never matches | probe-contains-flag-and-codepoints, probe-string-ordering, formula-strings |
| Null in strings | `null + "x"` = `"x"`; `null + null` = null; `Length(null)` = 0; `FindString(null, …)` = -1; `Replace(null, …)` = `""`; `ToString(null)` = `""`; `Uppercase/Left/Trim(null)` = null | probe-null-string-functions, formula-null-concat, formula-strings |
| Null in arithmetic/comparison | Arithmetic propagates null; ordering comparisons with null are false; `null = null` is true | probe-null-arithmetic-bool |
| Division by zero | Null | formula-divide-by-zero |
| `Round(x, m)` | `floor(x/m + 0.5) * m` in binary floating point (`Round(-2.5,1) = -2`, `Round(1.45,0.1) = 1.4`) | formula-rounding |
| `Mod(a, b)` | Operands truncated to integers; sign follows dividend (`Mod(2.5,3) = 2`, `Mod(-7,3) = -1`) | formula-rounding |
| `ToNumber` | Invalid text → 0; null → 0 | formula-tonumber |
| Numeric → integer type | Rounds half away from zero (`2.5→3`, `-2.5→-3`); out of range → null (with warning) | probe-select-int-rounding, select-cast-rename-drop |
| Double rendering | 15 significant digits | formula-rounding, summarize-statistics |
| Sort | Ordinal (case-sensitive) ordering, nulls first ascending / last descending, stable | sort-string-case, sort-multikey-nulls |
| Unique | Key comparison case-sensitive; both outputs ordered by key (nulls first), stable within key | unique-case-null, probe-unique-order |
| Grouped outputs | Summarize, Cross Tab, Running Total and grouped Sample emit groups in ascending key order | summarize-*, crosstab-sum, running-total-grouped, sample-first-grouped |
| Summarize statistics | StdDev/Variance are sample (n−1); a single value gives 0 | summarize-statistics |
| Sample "1 of every N" | Takes the first record of each block of N | sample-every-nth |
| Join | String keys case-sensitive; null keys match each other; joined output ordered by key, right-record-major within a key; unjoined outputs ordered by key | join-many-to-many-nulls, join-string-case, probe-join-unjoined-order |
| RegEx match on null | Null result | regex-match |

## Observed and adopted in Milestone 2

| Area | Rule | Evidence |
|---|---|---|
| E2 (AMP) | Doubles render in shortest round-trip form; Join/Summarize/Cross Tab/Running Total/grouped Sample/Imputation output order is unspecified | e2 results; docs/COMPATIBILITY_PROFILES.md |
| Auto Field, Multi-Field Formula, Tile (equal records), Find Replace, Make Group, Dynamic Rename (first row), Dynamic Select, Select Records, Imputation (mean), Count Records, Weighted Average | Kernel output equals the reference on the recorded cases | wave-2 cases in conformance/cases_wave2.py |
| XML Parse | Child values include a field named after the element (its own text) | xml-parse-children |
| Make Columns | Column groups are named `Column_<k>_<field>` | make-columns-horizontal |
| Field Info | Type names/sizes use the reference vocabulary (Double, 8, ...); Scale null except FixedDecimal; a Source column exists | field-info-basic |
| JSON Parse | Booleans render 1/0; empty arrays/objects produce no record; trailing commas are accepted | json-parse-single, probe-json-false-null, holdout JSON_Parse |
| Weighted Average | A null value contributes nothing to the numerator but its weight counts in the denominator | weighted-average-macro |
| DateTime | `IsFrom=True` converts date/time to text; text input is read as an ISO date first; pattern tokens yyyy, MM, MMM, MMMM/Month, dd, d … | datetime-*, probe-datetime-format-0..5, holdout DateTime |
| Cross Tab | Header values become names with non-alphanumerics replaced by `_`; several methods give `<Method>_<header>` grouped by header; Sum/Avg convert text values to numbers | probe-crosstab-space-headers, crosstab-two-methods, holdout Cross_Tab/Transpose |
| Select | `OrderChanged=False` keeps the incoming field order | holdout Select |
| Expressions | A bare identifier (no brackets) is a field reference | holdout Generate_Rows |

## Known differences (not yet matched)

* **Sort with a non-zero locale** uses a culture-aware collation (digits as numbers, "a" < "a b" < "A" < "ab" < "a-b",
  lowercase words before capitalised ones) that neither the Windows `CompareStringEx` flag combinations tried nor
  a simple key reproduces. flowengine's `collation: locale` uses the platform collation and is documented as
  different; `probe-sort-locale-1033` fails, so Sort's verification status is `failed`.
* **CSV output drops spatial object fields** in the reference; flowengine has no spatial type yet (spatial objects
  from `.yxdb` arrive as blobs and are written as hex).
* **Text Input typing**: the reference auto-types Text Input columns; the importer infers int32/int64/double/text,
  which can differ.
* **Field provenance** (Field Info's Source column) is not tracked by flowengine.
* Remaining holdout differences (conformance/results/2026.2.0.8/e1-samples/results.json): Join output field
  selection/renaming in the Join tool, Imputation's separate-field/indicator options, Multi-Field Formula type
  changes, Find Replace on mixed content, XML Parse attribute/root handling, string normalisation workflows, and
  the trend workflow's unsupported tools.

## Engine policies not yet compared (open)

- Text → number/date grammar (strict: no thousands separators, ISO dates only); text with a fraction → integer type fails rather than rounds.
- Truncation of over-long strings to declared size (warning).
- `fixed_decimal` arithmetic and rounding (banker's rounding on cast).
- Date functions: `DateTimeAdd` month-end clamping; `DateTimeDiff` truncation; supported format specifiers.
- Exponent form for doubles ≥ 1e15.
- Text Input/Input Data typing: untyped delimited input is `v_wstring` of unbounded size. **Observed difference (not adopted):** the reference reads delimited text as `V_String` size 254 by default and trims unquoted whitespace on read — recorded in `data/reference-observations.json` notes and docs/CONFORMANCE.md.
- Output Data CSV: header row, `\n` line endings, minimal quoting, null written as empty.
- Union widening rules; Append Fields source limit default (16, engine safety policy).
- Text To Columns rows mode, quote-aware splitting; RegEx dialect is Python `re` (differences from the reference dialect not yet catalogued).
- Behaviour under the AMP/E2 engine (all evidence above is E1).
