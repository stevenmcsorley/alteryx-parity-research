# Flowbench Data Investigation tools

Independent kernels in `flowengine/tools/investigation.py` (Pearson Correlation is in `correlation.py`).
Statistics use numpy and scipy. Missing means null or NaN, and missing values are excluded from numeric
statistics. Standard deviation and variance are sample (n-1) statistics, as in Summarize. Every output
is a structured table with a static schema, so results feed downstream tools, the Browse panel and AI
inspection. The vendor's report and interactive (I/R) outputs are not reproduced.

| Tool | Kernel | Outputs |
|---|---|---|
| Basic Data Profile | `core.basic_data_profile` | `profile`: per field type, missing, completeness, distinct (with a cap), min/max, mean, median, standard deviation, variance, text lengths, top values. `overview`: records, fields, complete records. `sample_records` profiles the first N records. |
| Field Summary | `core.field_summary` | Per field: count, missing %, unique, min/max, mean, standard deviation, Q1/median/Q3 (linear interpolation), mode, shortest and longest text |
| Frequency Table | `core.frequency_table` | Per field and value: frequency, percent, cumulative frequency and percent. Nulls are `[Null]` unless excluded, and `max_categories` folds the rest into `(Other)`, so counts always reconcile. Value sorting is typed, so 2 sorts before 10. |
| Histogram | `core.histogram` | Equal-width bins from a fixed count or automatic rules (`auto`, `sturges`, `fd`, `sqrt`). Bins are [lower, upper), with the last bin closed. Each bin has count, relative frequency, density (integrates to 1) and cumulative count. Nulls, non-finite and out-of-range values are excluded and reported. |
| Spearman Correlation | `core.spearman_correlation` | Every field pair: rho with average ranks for ties, two-sided p-value and N. Missing values are dropped pairwise or listwise. Constant fields and N < 3 give null. |
| Contingency Table | `core.contingency_table` | `table`: observed and expected counts, row, column and total percentages, `(Total)` margins. `statistics`: Pearson chi-square, degrees of freedom, p-value, Cramér's V, minimum expected count (warns below 5), optional Yates correction for 2×2. |
| Distribution Analysis | `core.distribution_analysis` | `fits`: maximum-likelihood fits (normal, lognormal, exponential, gamma, Weibull, uniform) ranked by AIC, with parameters, log-likelihood, BIC, Kolmogorov-Smirnov statistic and approximate p-value, and Anderson-Darling for normal and exponential. Positive-only families use location 0 and are reported as not applicable for data ≤ 0. `moments`: N, mean, standard deviation, skewness, excess kurtosis, min and max. |

The desktop configures these through the standard Inspector, with field pickers for `field`, `fields`,
`row_field` and `column_field`. Flowbench AI discovers them through the tool catalogue.

`.yxmd` import is not mapped to these kernels because no reference mapping of the vendor settings has
been established. Plugin-based entries (for example Contingency Table) import as unsupported nodes with
diagnostics. Entries that Alteryx implements as R macros (for example `Predictive Tools\Histogram.yxmc`)
import as references to the vendor macro file. Flowbench does not ship that file, so such a node fails
validation until it is replaced by the native tool. In both cases the original XML is kept for export.

Worked example: `examples/investigation/fuel_investigation.flow.json` (UK weekly road fuel prices,
Open Government Licence). Tests: `tests/test_investigation.py`.
