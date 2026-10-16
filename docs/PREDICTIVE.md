# Flowbench predictive tools

Independent implementations of standard, documented methods (`flowengine/tools/predictive.py`,
`flowengine/modeling.py`) using statsmodels, scikit-learn and networkx. They are not the vendor's R
macros. Where an Alteryx tool relies on a specific R package, the method used here is named.

**Models** travel on a `model` anchor as a one-row table: a JSON description (family, target,
predictors, encoding, classes, training metrics, and the fitting spec for refitting) and a payload.
Coefficient models store plain parameters. scikit-learn models are stored with skops, whose loader
accepts only scikit-learn, numpy, scipy and builtin types. Loading a model can never execute code, as
pickle could. Categorical predictors are one-hot encoded with the training categories. Regression
models drop the first category as the baseline. Unseen categories encode as all-zero and are reported.
Records with missing values are excluded from fitting and counted.

| Group | Tools |
|---|---|
| Regression (statsmodels) | Linear (OLS/WLS: coefficients, t, p, R², adjusted R², F, RMSE, AIC, BIC), Logistic (binomial GLM, odds ratios, McFadden R², AUC; reports perfect separation), Count (Poisson or negative binomial, exposure offset, rate ratios), Gamma (log link) |
| Machine learning (scikit-learn) | Decision Tree (CART, tree rows, importance), Forest Model (random forest, OOB score), Boosted Model (gradient boosting, not gbm), Support Vector Machine (standardised, RBF/linear/poly), Neural Network (MLP), Spline Model (B-spline basis with ridge or logistic, not MARS) |
| Other models | Naive Bayes (Gaussian plus Laplace-smoothed categorical, stored as parameters) |
| Using models | Score (Score, or Score_<class> plus Prediction with an optional binary threshold), Model Coefficients, Stepwise (AIC/BIC; backward, forward or both), Nested Test (likelihood-ratio test of nested regressions) |
| Evaluation | Model Comparison (RMSE, MAE, R², MAPE / accuracy, macro F1, log loss, AUC, confusion), Cross-Validation (refits each model per fold; stratified, repeated, seeded), Lift Chart (cumulative gains and lift per bin, with a chart) |
| Diagnostics and tests | Test of Means (pairwise Welch or pooled t-tests with confidence intervals), Variance Inflation Factors |
| Survival | Survival Analysis (Kaplan–Meier with Greenwood log(−log) limits and a chart; Cox PH model with hazard ratios), Survival Score (linear predictor, relative risk, S(t) from the Breslow baseline) |
| Graphs | Network Analysis (degree, weighted degree, betweenness, closeness, PageRank, greedy-modularity communities, graph statistics, network diagram) |

Fitting is deterministic for a given `seed`. Example: `examples/predictive/fuel_models.flow.json`.
Tests: `tests/test_predictive.py`. It checks against numpy least squares, unpenalised scikit-learn,
scipy t-tests, statsmodels VIFs and LR tests, a hand-computed Kaplan–Meier, a perfect-ranking lift,
recovery of known Poisson, gamma and Cox effects, and refusal of models with untrusted types.

Not included: DataRobot tools (vendor service), the vendor's R report and interactive outputs, and
Alteryx Predictive XML import.
