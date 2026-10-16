# Grouping, time series, A/B testing and prescriptive tools

Independent implementations of standard methods. They are not the vendor's R macros. Models
(K-Centroids, ARIMA, ETS) are stored as plain JSON on a `model` anchor, so they are safe to share.

## Predictive Grouping (`flowengine/tools/grouping.py`)

| Tool | Method |
|---|---|
| K-Centroids Cluster Analysis | k-means (10 starts) or k-medians (L1, median centres) on optionally z-scored fields. Model plus a size, distance and centroid summary |
| Append Cluster | Nearest centre using the model's scaling and distance: Cluster and Distance |
| K-Centroids Diagnostics | Per k: within SS, silhouette, Calinski-Harabasz, Davies-Bouldin, bootstrap adjusted Rand stability, chart |
| Principal Components | Eigen-decomposition of the correlation or covariance matrix: scores, eigenvalues, variance explained, sign-fixed loadings |
| Multidimensional Scaling | Classical (Torgerson) MDS with goodness of fit and Kruskal stress-1 (up to 3,000 records) |
| Find Nearest Neighbors | KD-tree k-NN (Euclidean or Manhattan) within data or for query records |
| MB Rules / MB Inspect / MB Affinity | Apriori rules (support, confidence, lift, leverage, conviction), rule filtering and a chart, item-pair co-occurrence with Jaccard and lift |

## Time Series (`flowengine/tools/timeseries.py`, statsmodels)

ARIMA (seasonal SARIMAX; automatic AIC order search or a fixed order; optional covariates) and ETS
(error, trend, damping and seasonality chosen by AIC) produce models. Forecasts reapply the fitted
parameters. **TS Forecast** gives forecasts with 80% and 95% intervals, keeping weekly, monthly or
other spacing. **TS Covariate Forecast** uses future covariate records. **TS Compare** reports ME,
RMSE, MAE, MPE, MAPE and MASE on holdout data. **TS Plot** draws series and an STL decomposition.
**TS Filler** inserts missing periods filled with null, previous, linear-by-period or zero values.
**TS Model Factory** and **TS Forecast Factory** fit and forecast one model per group.

## A/B Testing (`flowengine/tools/abtest.py`)

**AB Trend** gives per-unit period totals: mean, trend, volatility and seasonality strength.
**AB Treatments** picks representative (closest to the stratum centre) or seeded random treatment
units. **AB Controls** matches each treatment with its nearest z-scored candidates, never treatment
units, optionally unique and stratified. **AB Analysis** computes a ratio difference-in-differences
lift per treatment, comparing test and comparison period growth against the mean control growth, and
gives the mean lift with a t confidence interval and p-value, plus a chart.

## Prescriptive (`flowengine/tools/prescriptive.py`)

**Optimization** solves linear and mixed-integer programmes with HiGHS (SciPy). It takes variables
(objective, bounds, continuous, integer or binary) and constraints (sense, RHS, one coefficient column
per variable), and returns values, constraint activity and slack, and status. Shadow prices and
reduced costs are given for LPs. **Simulation Sampling** draws seeded samples from parametric
distributions or bootstraps an input field. **Simulation Scoring** simulates model outcomes:
regression error from training RMSE, class draws from probabilities, or Poisson counts.
**Simulation Summary** reports moments, percentiles and exceedance probabilities, with a histogram.

Examples: `examples/timeseries/diesel_forecast.flow.json`. Tests: `tests/test_batch8.py`. It checks
against known answers: blob recovery (ARI = 1), silhouette at the true k, eigenvalues, exact MDS on
planar data, brute-force k-NN, the textbook basket rule (lift 1.25), AR(1) recovery, exact
trend-plus-season forecasts, covariate forecasts (y = 2x), period interpolation, STL
reconstruction, detection of a 10% A/B lift, the textbook LP (36, with duals 0, 1.5 and 1), a
knapsack, infeasibility, sampling moments and P(Z > 2).
