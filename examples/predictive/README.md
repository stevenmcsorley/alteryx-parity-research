# UK fuel price models

`fuel_models.flow.json` uses the UK weekly road fuel prices (see `../investigation/README.md` for the
source and licence). It splits the weeks 70/30 into estimation and validation samples, fits a logistic
regression and a random forest that classify weeks with petrol at 140p or more from diesel price and
fuel duty, compares them on the validation sample (Model Comparison and Lift Chart), and fits a
linear regression of the petrol price that Score applies to the validation weeks.
