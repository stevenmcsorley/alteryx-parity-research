# UK fuel prices investigation

`fuel_investigation.flow.json` loads 459 weekly UK road fuel price observations (January 2018 to
October 2026), derives `Year`, `PriceBand` and `DutyPeriod`, and runs Basic Data Profile, Field
Summary, Frequency Table, Histogram, Spearman Correlation, Contingency Table and Distribution Analysis.
Each tool feeds a Browse node, so its result appears in the desktop results panel. Open the workflow in
Flowbench and press Run, or use the CLI:

```sh
python -m flowengine run examples/investigation/fuel_investigation.flow.json
```

Data: Department for Energy Security and Net Zero, *Weekly road fuel prices*
(https://www.gov.uk/government/statistics/weekly-road-fuel-prices). Crown copyright, Open Government
Licence v3.0 (https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/). Column names
are simplified; values and row order are unchanged. `data/weekly_fuel_prices.source.json` records the
download URL, time and SHA-256.
