# Subscription churn model

`churn_model.flow.json` uses `data/subscriptions.csv`: 300 synthetic subscribers, about 4% of them missing a
monthly spend. It runs Data Health with recommendations. Assisted Modeling then compares logistic regression,
decision tree, random forest and gradient boosting by 5-fold cross-validated AUC, and applies the best model to
`data/new_subscriptions.csv` with Predict.

The data was generated from a known logistic churn model: churn rises with spend and support tickets, falls
with tenure and is lowest on the premium plan. Logistic regression should therefore win.
