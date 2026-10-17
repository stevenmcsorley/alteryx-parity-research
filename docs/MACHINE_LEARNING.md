# Machine Learning tools

Nine of the eleven Machine Learning catalogue entries are implemented as an independent scikit-learn pipeline.
They are not the vendor's Intelligence Suite, which builds on featuretools, woodwork and EvalML. **Machine
Learning Predict** and **Machine Learning Send** connect to the vendor's Alteryx One cloud service over OAuth,
so they stay unimplemented, like the DataRobot tools.

## How a pipeline travels

Feature Types, Transformation, Classification and Regression pass the records on with one extra text field,
`ML_Pipeline`. It holds the pipeline built so far as JSON (`flowbench.ml.v1`):

- the feature types
- the preparation steps
- the chosen algorithm and its parameters

Every record carries the same value. The stream can still be browsed, sampled or filtered before **Fit**, so a
Create Samples tool can sit between Classification and Fit. Fit outputs an ordinary Flowbench model
(`ModelType = ml_pipeline`). The model works with **Predict**, and also with Score, Model Comparison and
Cross-Validation, which refit it with the same pipeline.

```
Input ─► Feature Types ─► Transformation ─► Classification ─► Fit ─► Predict ◄─ new records
Input ─► Assisted Modeling ─► (model) ─► Predict
```

## Tools

| Tool | What it does |
|---|---|
| Feature Types | Autodetects or overrides Integer, Double, Boolean, Categorical, Datetime, NaturalLanguage, Index and Unknown. Values are unchanged. |
| Data Health | Requires at least 30 records. Scores each field on missing values, outliers (Tukey 1.5×IQR) and sparsity (share held by the most common value; it penalises above 90%). Optionally adds recommendations. Outputs scores, a report and the outlier records. |
| Build Features | Deep feature synthesis over related tables, with featuretools-style names: `YEAR(date)`, `COUNT(child)`, `SUM(child.x)`, `MEAN(sessions.SUM(transactions.amount))` and `parent.field`. Depth runs from 1 to 3. |
| Transformation | Sets data types (Numeric, Categorical, Boolean, ID), cleans up missing values (mean, median or mode from training, or a constant), excludes features, and one-hot encodes. Unknown categories at scoring are either ignored or an error. |
| Classification | Logistic regression, decision tree, random forest or gradient boosting, with the documented parameters and defaults. |
| Regression | Linear regression, decision tree or random forest. |
| Fit | Trains the pipeline. Outputs the model, training metrics and feature importance. |
| Predict | Adds `Probability_<class>` fields and `Prediction`, or `Prediction` alone for regression. |
| Assisted Modeling | Prepares the data automatically: drops ID-like fields, dates, free text and categories with more than 50 values, fills missing numbers with the median and categories with the mode, and one-hot encodes categories. It then cross-validates every candidate algorithm, refitting the preparation inside each fold, and retrains the best one on all records. Outputs the model, leaderboard, preparation steps and the pipeline, which can be edited and refitted. |

### Differences from the vendor tools

- **XGBoost** is offered as scikit-learn gradient-boosted trees, with the same learning-rate, depth, estimator,
  subsample and column-sample settings. XGBoost is not bundled.
- **Logistic regression** uses one-vs-rest with the liblinear solver (the vendor default) when there are more
  than two classes.
- **Linear regression** "Normalize" standardises the features first. This changes the coefficients, not the
  predictions.
- **Missing values** stop Fit with the list of affected fields, as the vendor documents. Categorical features
  that were not chosen for one-hot encoding are encoded automatically, with a warning.

## Example

[`examples/machine_learning/churn_model.flow.json`](../examples/machine_learning/churn_model.flow.json) runs
three steps on synthetic subscription data:

1. Checks the data's health.
2. Lets Assisted Modeling choose the best of four classifiers by cross-validated AUC.
3. Predicts churn for new subscribers.
