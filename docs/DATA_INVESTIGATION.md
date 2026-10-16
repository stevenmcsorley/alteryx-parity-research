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

| Association Analysis | `core.association_analysis` | Pairwise measure suited to the field types: Pearson r for numeric pairs, Cramér's V (chi-square p-value) for categorical pairs, and correlation ratio η (one-way ANOVA p-value) for mixed pairs. Includes strength (0 to 1) and N, an optional target field, and a strength heat-map chart. |
| Heat Plot | `core.heat_plot` | Cells over equal-width numeric bins or categories: count, sum, mean or median of a value field, with empty cells left null. Palettes are viridis, blues, reds and diverging. Outputs the grid table and a chart. |
| Importance Weights | `core.importance_weights` | Mutual information (bits), gain ratio or symmetrical uncertainty of each predictor for a target. Numeric fields are cut into equal-frequency bins. Weights are ranked and normalised, with a bar chart. This is a documented information-theory method, not the vendor macro algorithm. |
| Plot of Means | `core.plot_of_means` | Per group: N, mean, standard deviation, standard error and a Student-t confidence interval. Also a one-way ANOVA (F, degrees of freedom, p) and a chart of means with interval bars. |
| Scatterplot | `core.scatterplot` | Chart of y against x with optional colour groups and per-group least-squares trend (slope, intercept, R², p). Above `max_points`, the chart is evenly thinned, but trends use every record. |
| Violin Plot | `core.violin_plot` | Gaussian KDE per group (Scott, Silverman or fixed bandwidth). Outputs a summary (N, min, quartiles, max, bandwidth), the density table and violins with quartile boxes. |
| Histogram (chart) | `core.histogram` | Also emits a rendered histogram on its `chart` anchor. |

**Charts.** Graphical tools return their numbers as tables plus a `chart` anchor holding a rendered SVG
(`flowengine/charts.py`). The desktop results panel shows the chart as an image, through a data URL,
so it never runs script. It can be downloaded as SVG, and Reporting reuses the same drawing
operations for PDF. All chart text is XML-escaped.

The desktop configures these through the standard Inspector, with field pickers for `field`, `fields`,
`row_field` and `column_field`. Flowbench AI discovers them through the tool catalogue.

`.yxmd` import is not mapped to these kernels because no reference mapping of the vendor settings has
been established. Plugin-based entries (for example Contingency Table) import as unsupported nodes with
diagnostics. Entries that Alteryx implements as R macros (for example `Predictive Tools\Histogram.yxmc`)
import as references to the vendor macro file. Flowbench does not ship that file, so such a node fails
validation until it is replaced by the native tool. In both cases the original XML is kept for export.

Worked examples (UK weekly road fuel prices, Open Government Licence):
`examples/investigation/fuel_investigation.flow.json` (tables) and `examples/investigation/fuel_charts.flow.json`
(every graphical tool, one Browse per chart). Tests: `tests/test_investigation.py`, `tests/test_investigation_plots.py`.
