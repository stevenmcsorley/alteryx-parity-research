# Flowbench full acceptance — 20261015T021327Z

Started: 2026-10-15T02:13:27.265578+00:00
Catalogue: 323 entries / 321 distinct names; reference E1/E2: ['2026.2.0.8/e1', '2026.2.0.8/e2']

## Suite results

| Suite | Status | Tests | Skipped | Cases/workflows passed | Seconds | Evidence |
|---|---|---:|---:|---:|---:|---|
| repository-integrity | **PASS** | — | 0 | — | 0.047 | PASS |
| status-ledger | **PASS** | — | 0 | — | 11.984 | tests: 212 run, 0 failed |
| parity-roadmap | **PASS** | — | 0 | — | 0.360 | Roadmap matches the catalogue and evidence ledger |
| python-engine-and-import-macro-security | **PASS** | 212 | 0 | — | 11.937 | OK |
| desktop-typescript-build | **PASS** | — | 0 | — | 3.250 | ✓ built in 1.11s |
| desktop-unit-tests | **PASS** | 12 | 0 | — | 1.641 |  |
| desktop-playwright | **PASS** | 15 | 0 | — | 24.109 |   15 passed (23.4s) |
| database-integration | **BLOCKED** | — | 0 | — | 0.000 | OpenSSL CLI is required by tests/integration/db_env.py to create the PostgreSQL TLS fixture |
| r-tool-runtime | **BLOCKED** | — | 0 | — | 0.000 | Pinned external R runtime is not installed |
| statistical-and-ml-engine-tests | **PASS** | 26 | 0 | — | 7.344 | OK |
| spatial-tool-tests | **NOT RUN** | — | 0 | — | 0.000 | No spatial implementation test module exists yet |
| connector-contract-tests | **NOT RUN** | — | 0 | — | 0.000 | No connector contract test module exists yet |
| rust-desktop-shell | **PASS** | — | 0 | — | 4.875 |  |
| conformance-e1 | **FAIL** | — | 0 | 75/76 cases | 16.765 | 75/76 cases passed against reference build 2026.2.0.8; wrote conformance\results\2026.2.0.8\e1\results.json |
| conformance-e1-holdout | **FAIL** | — | 0 | 15/31 workflows; 87/126 sinks | 21.000 | 15/31 sample workflows fully match; 87/126 sinks match (e1) |
| conformance-e2 | **FAIL** | — | 0 | 75/76 cases | 28.297 | 75/76 cases passed against reference build 2026.2.0.8; wrote conformance\results\2026.2.0.8\e2\results.json |
| conformance-e2-holdout | **FAIL** | — | 0 | 16/31 workflows; 88/126 sinks | 21.391 | 16/31 sample workflows fully match; 88/126 sinks match (e2) |
| performance | **NOT RUN** | — | 0 | — | 0.000 | Pass --include-performance to execute the 200k-row benchmark |
| windows-installer-clean-runner-smoke | **NOT RUN** | — | 0 | — | 0.000 | Installer changes the host installation state; run on isolated Windows CI with --include-installer |

Totals: {'BLOCKED': 2, 'FAIL': 4, 'NOT RUN': 4, 'PASS': 9, 'SKIP': 0}

## Catalogue matrix

| ID | Tool | Implementation | Reference | E1 | E2 | Tests | External blocker |
|---|---|---|---|---:|---:|---:|---|
| `in-out:auto-insights-uploader` | Auto Insights Uploader | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-out:browse` | Browse | implemented | not-tested | 0/0 | 0/0 | 23 | no |
| `in-out:date-time-now` | Date Time Now | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `in-out:directory` | Directory | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `in-out:input-data` | Input Data | implemented | not-tested | 0/0 | 0/0 | 15 | no |
| `in-out:map-input` | Map Input | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-out:output-data` | Output Data | implemented | not-tested | 0/0 | 0/0 | 9 | no |
| `in-out:text-input` | Text Input | implemented | not-tested | 0/0 | 0/0 | 119 | no |
| `preparation:auto-field` | Auto Field | implemented | partial | 1/1 | 1/1 | 1 | no |
| `preparation:create-samples` | Create Samples | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `preparation:data-cleanse-pro` | Data Cleanse Pro | implemented | not-tested | 0/0 | 0/0 | 3 | no |
| `preparation:data-cleansing` | Data Cleansing | implemented | not-tested | 0/0 | 0/0 | 3 | no |
| `preparation:filter` | Filter | implemented | partial | 5/5 | 5/5 | 24 | no |
| `preparation:formula` | Formula | implemented | partial | 13/13 | 13/13 | 22 | no |
| `preparation:generate-rows` | Generate Rows | implemented | not-tested | 0/0 | 0/0 | 3 | no |
| `preparation:imputation` | Imputation | implemented | partial | 1/1 | 1/1 | 1 | no |
| `preparation:multi-field-binning` | Multi-Field Binning | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `preparation:multi-field-formula` | Multi-Field Formula | implemented | partial | 1/1 | 1/1 | 1 | no |
| `preparation:multi-row-formula` | Multi-Row Formula | implemented | partial | 1/1 | 1/1 | 1 | no |
| `preparation:oversample-field` | Oversample Field | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `preparation:random-sample` | Random % Sample | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `preparation:rank` | Rank | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `preparation:record-id` | Record ID | implemented | partial | 1/1 | 1/1 | 4 | no |
| `preparation:sample` | Sample | implemented | partial | 6/6 | 6/6 | 2 | no |
| `preparation:select` | Select | implemented | partial | 2/2 | 2/2 | 8 | no |
| `preparation:select-records` | Select Records | implemented | partial | 1/1 | 1/1 | 1 | no |
| `preparation:sort` | Sort | implemented | failed | 2/3 | 2/3 | 14 | no |
| `preparation:tile` | Tile | implemented | partial | 1/1 | 1/1 | 1 | no |
| `preparation:unique` | Unique | implemented | partial | 2/2 | 2/2 | 5 | no |
| `join:append-fields` | Append Fields | implemented | partial | 1/1 | 1/1 | 1 | no |
| `join:find-replace` | Find Replace | implemented | partial | 1/1 | 1/1 | 1 | no |
| `join:fuzzy-match` | Fuzzy Match | implemented | not-tested | 0/0 | 0/0 | 16 | no |
| `join:join` | Join | implemented | partial | 4/4 | 4/4 | 10 | no |
| `join:join-multiple` | Join Multiple | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `join:make-group` | Make Group | implemented | partial | 1/1 | 1/1 | 1 | no |
| `join:union` | Union | implemented | partial | 1/1 | 1/1 | 7 | no |
| `parse:datetime` | DateTime | implemented | partial | 8/8 | 8/8 | 1 | no |
| `parse:regex` | RegEx | implemented | partial | 3/3 | 3/3 | 1 | no |
| `parse:text-to-columns` | Text To Columns | implemented | partial | 1/1 | 1/1 | 1 | no |
| `parse:xml-parse` | XML Parse | implemented | partial | 1/1 | 1/1 | 1 | no |
| `transform:arrange` | Arrange | implemented | not-tested | 0/0 | 0/0 | 2 | no |
| `transform:count-records` | Count Records | implemented | partial | 1/1 | 1/1 | 7 | no |
| `transform:cross-tab` | Cross Tab | implemented | partial | 3/3 | 3/3 | 1 | no |
| `transform:make-columns` | Make Columns | implemented | partial | 1/1 | 1/1 | 1 | no |
| `transform:running-total` | Running Total | implemented | partial | 1/1 | 1/1 | 1 | no |
| `transform:summarize` | Summarize | implemented | partial | 4/4 | 4/4 | 10 | no |
| `transform:transpose` | Transpose | implemented | partial | 1/1 | 1/1 | 1 | no |
| `transform:weighted-average` | Weighted Average | implemented | partial | 1/1 | 1/1 | 1 | no |
| `in-database:browse-in-db` | Browse In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:connect-in-db` | Connect In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:data-stream-in` | Data Stream In | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:data-stream-out` | Data Stream Out | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:dynamic-input-in-db` | Dynamic Input In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:dynamic-output-in-db` | Dynamic Output In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:filter-in-db` | Filter In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:formula-in-db` | Formula In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:join-in-db` | Join In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:macro-input-in-db` | Macro Input In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:macro-output-in-db` | Macro Output In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:sample-in-db` | Sample In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:select-in-db` | Select In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:summarize-in-db` | Summarize In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:union-in-db` | Union In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `in-database:write-data-in-db` | Write Data In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:email` | Email | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:image` | Image | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:interactive-chart` | Interactive Chart | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:layout` | Layout | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:map-legend-builder` | Map Legend Builder | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:map-legend-splitter` | Map Legend Splitter | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:overlay` | Overlay | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:render` | Render | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:report-footer` | Report Footer | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:report-header` | Report Header | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:report-map` | Report Map | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:report-text` | Report Text | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `reporting:table` | Table | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `documentation:comment` | Comment | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `documentation:explorer-box` | Explorer Box | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `documentation:tool-container` | Tool Container | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:buffer` | Buffer | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:create-points` | Create Points | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:distance` | Distance | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:find-nearest` | Find Nearest | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:generalize` | Generalize | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:heat-map` | Heat Map | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:make-grid` | Make Grid | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:non-overlap-drivetime` | Non Overlap Drivetime | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:poly-build` | Poly-Build | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:poly-split` | Poly-Split | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:smooth` | Smooth | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:spatial-info` | Spatial Info | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:spatial-match` | Spatial Match | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:spatial-process` | Spatial Process | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `spatial:trade-area` | Trade Area | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:action` | Action | in-progress | not-tested | 0/0 | 0/0 | 5 | no |
| `interface:check-box` | Check Box | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:condition` | Condition | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:control-parameter` | Control Parameter | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:date` | Date | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:dcm-connection` | DCM Connection | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:drop-down` | Drop Down | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:error-message` | Error Message | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:file-browse` | File Browse | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:folder-browse` | Folder Browse | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:list-box` | List Box | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:macro-input` | Macro Input | implemented | not-tested | 0/0 | 0/0 | 13 | no |
| `interface:macro-output` | Macro Output | implemented | not-tested | 0/0 | 0/0 | 13 | no |
| `interface:map` | Map | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:numeric-up-down` | Numeric Up Down | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:radio-button` | Radio Button | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:text-box` | Text Box | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `interface:tree` | Tree | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:association-analysis` | Association Analysis | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:basic-data-profile` | Basic Data Profile | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:contingency-table` | Contingency Table | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:distribution-analysis` | Distribution Analysis | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:field-summary` | Field Summary | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:frequency-table` | Frequency Table | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:heat-plot` | Heat Plot | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:histogram` | Histogram | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:importance-weights` | Importance Weights | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:pearson-correlation` | Pearson Correlation | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:plot-of-means` | Plot of Means | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:scatterplot` | Scatterplot | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:spearman-correlation` | Spearman Correlation | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `data-investigation:violin-plot` | Violin Plot | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:boosted-model` | Boosted Model | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:count-regression` | Count Regression | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:cross-validation` | Cross-Validation | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:datarobot-automodel` | DataRobot Automodel | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:datarobot-predict` | DataRobot Predict | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:decision-tree` | Decision Tree | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:forest-model` | Forest Model | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:gamma-regression` | Gamma Regression | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:lift-chart` | Lift Chart | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:linear-regression` | Linear Regression | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:logistic-regression` | Logistic Regression | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:model-coefficients` | Model Coefficients | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:model-comparison` | Model Comparison | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:naive-bayes-classifier` | Naive Bayes Classifier | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:nested-test` | Nested Test | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:network-analysis` | Network Analysis | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:neural-network` | Neural Network | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:score` | Score | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:spline-model` | Spline Model | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:stepwise` | Stepwise | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:support-vector-machine` | Support Vector Machine | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:survival-analysis` | Survival Analysis | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:survival-score` | Survival Score | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:test-of-means` | Test of Means | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive:variance-inflation-factors` | Variance Inflation Factors | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `ab-testing:ab-analysis` | AB Analysis | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `ab-testing:ab-controls` | AB Controls | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `ab-testing:ab-treatments` | AB Treatments | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `ab-testing:ab-trend` | AB Trend | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `time-series:arima` | ARIMA | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `time-series:ets` | ETS | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `time-series:ts-compare` | TS Compare | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `time-series:ts-covariate-forecast` | TS Covariate Forecast | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `time-series:ts-filler` | TS Filler | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `time-series:ts-forecast` | TS Forecast | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `time-series:ts-forecast-factory` | TS Forecast Factory | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `time-series:ts-model-factory` | TS Model Factory | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `time-series:ts-plot` | TS Plot | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive-grouping:append-cluster` | Append Cluster | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive-grouping:find-nearest-neighbors` | Find Nearest Neighbors | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive-grouping:k-centroids-cluster-analysis` | K-Centroids Cluster Analysis | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive-grouping:k-centroids-diagnostics` | K-Centroids Diagnostics | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive-grouping:mb-affinity` | MB Affinity | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive-grouping:mb-inspect` | MB Inspect | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive-grouping:mb-rules` | MB Rules | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive-grouping:multidimensional-scaling` | Multidimensional Scaling | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `predictive-grouping:principal-components` | Principal Components | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `prescriptive:optimization` | Optimization | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `prescriptive:simulation-sampling` | Simulation Sampling | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `prescriptive:simulation-scoring` | Simulation Scoring | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `prescriptive:simulation-summary` | Simulation Summary | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `connectors:azure-data-lake-file-input` | Azure Data Lake File Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:azure-data-lake-file-output` | Azure Data Lake File Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:adobe-analytics` | Adobe Analytics | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:amazon-s3-download` | Amazon S3 Download | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:amazon-s3-upload` | Amazon S3 Upload | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:anaplan-input` | Anaplan Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:anaplan-output` | Anaplan Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:automation-anywhere-360` | Automation Anywhere 360 | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:box-input` | Box Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:box-output` | Box Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:cognitive-services-text-analytics` | Cognitive Services Text Analytics | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:dataverse-input` | Dataverse Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:dataverse-output` | Dataverse Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:dropbox-input` | Dropbox Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:dropbox-output` | Dropbox Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:dynamics-crm-input` | Dynamics CRM Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:dynamics-crm-output` | Dynamics CRM Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:google-analytics` | Google Analytics | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:google-analytics-4-input` | Google Analytics 4 Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:google-bigquery-input` | Google BigQuery Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:google-bigquery-output` | Google BigQuery Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:google-drive-input` | Google Drive Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:google-drive-output` | Google Drive Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:google-sheets-input` | Google Sheets Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:google-sheets-output` | Google Sheets Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:marketo-append` | Marketo Append | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:marketo-input` | Marketo Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:marketo-output` | Marketo Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:microsoft-power-bi-output` | Microsoft Power BI Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:mongodb-input` | MongoDB Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:mongodb-output` | MongoDB Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:onedrive-input` | OneDrive Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:onedrive-output` | OneDrive Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:outlook-365-input` | Outlook 365 Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:power-automate` | Power Automate | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:publish-to-tableau-server` | Publish to Tableau Server | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:salesforce-input` | Salesforce Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:salesforce-output-new` | Salesforce Output (New) | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:servicenow-input` | ServiceNow Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:sharepoint-files-input` | SharePoint Files Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:sharepoint-files-output` | SharePoint Files Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:sharepoint-list-input` | SharePoint List Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:sharepoint-list-output` | SharePoint List Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connectors:tableau-output` | Tableau Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `address:cass` | CASS | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `address:parse-address` | Parse Address | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `address:reverse-geocoder` | Reverse Geocoder | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `address:street-geocoder` | Street Geocoder | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `address:us-geocoder` | US Geocoder | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `address:us-zip9-coder` | US ZIP9 Coder | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `demographic-analysis:allocate-append` | Allocate Append | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `demographic-analysis:allocate-input` | Allocate Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `demographic-analysis:allocate-metainfo` | Allocate Metainfo | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `demographic-analysis:allocate-report` | Allocate Report | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `behavior-analysis-deprecated:behavior-metainfo` | Behavior Metainfo | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `behavior-analysis-deprecated:cluster-code` | Cluster Code | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `behavior-analysis-deprecated:compare-behavior` | Compare Behavior | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `behavior-analysis-deprecated:create-profile` | Create Profile | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `behavior-analysis-deprecated:detail-fields` | Detail Fields | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `behavior-analysis-deprecated:profile-comparison-report` | Profile Comparison Report | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `behavior-analysis-deprecated:profile-detail-report` | Profile Detail Report | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `behavior-analysis-deprecated:profile-input` | Profile Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `behavior-analysis-deprecated:profile-output` | Profile Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `behavior-analysis-deprecated:profile-rank-report` | Profile Rank Report | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `calgary:calgary-input` | Calgary Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `calgary:calgary-join` | Calgary Join | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `calgary:calgary-cross-count` | Calgary Cross Count | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `calgary:calgary-cross-count-append` | Calgary Cross Count Append | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `calgary:calgary-loader` | Calgary Loader | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `developer:apache-spark-code` | Apache Spark Code | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:api-output` | API Output | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:base64-encoder` | Base64 Encoder | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `developer:blob-convert` | Blob Convert | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `developer:blob-input` | Blob Input | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:blob-output` | Blob Output | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:block-until-done` | Block Until Done | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `developer:control-container` | Control Container | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:detour` | Detour | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `developer:detour-end` | Detour End | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `developer:download` | Download | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:dynamic-input` | Dynamic Input | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:dynamic-rename` | Dynamic Rename | implemented | partial | 1/1 | 1/1 | 1 | no |
| `developer:dynamic-replace` | Dynamic Replace | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:dynamic-select` | Dynamic Select | implemented | partial | 1/1 | 1/1 | 1 | no |
| `developer:expect-equals` | Expect Equals | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `developer:field-info` | Field Info | implemented | partial | 1/1 | 1/1 | 1 | no |
| `developer:github-commit` | GitHub Commit | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:github-get-last-commit` | GitHub Get Last Commit | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:json-parse` | JSON Parse | implemented | partial | 2/2 | 2/2 | 1 | no |
| `developer:json-schema` | JSON Schema | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:license-api` | License API | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:message` | Message | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `developer:python` | Python | implemented | not-tested | 0/0 | 0/0 | 4 | no |
| `developer:r` | R | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `developer:run-command` | Run Command | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `developer:runner` | Runner | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:server-api` | Server API | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `developer:test` | Test | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `developer:throttle` | Throttle | implemented | not-tested | 0/0 | 0/0 | 1 | no |
| `laboratory:connect-json-builder` | Connect JSON Builder | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `laboratory:connect-output` | Connect Output | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `laboratory:json-build` | JSON Build | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `laboratory:jupyter-flow` | Jupyter Flow | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `laboratory:key-value-pair-extraction` | Key-Value Pair Extraction | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `laboratory:transpose-in-db` | Transpose In-DB | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `laboratory:visual-layout` | Visual Layout | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:assisted-modeling` | Assisted Modeling | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:build-features` | Build Features | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:classification` | Classification | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:data-health` | Data Health | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:feature-types` | Feature Types | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:fit` | Fit | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:machine-learning-predict` | Machine Learning Predict | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:machine-learning-send` | Machine Learning Send | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:predict` | Predict | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:regression` | Regression | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `machine-learning:transformation` | Transformation | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `text-mining:named-entity-recognition` | Named Entity Recognition | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `text-mining:part-of-speech-tagger` | Part-of-Speech Tagger | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `text-mining:sentiment-analysis` | Sentiment Analysis | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `text-mining:text-classification` | Text Classification | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `text-mining:text-pre-processing` | Text Pre-processing | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `text-mining:text-summary` | Text Summary | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `text-mining:topic-modeling` | Topic Modeling | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `text-mining:word-cloud` | Word Cloud | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `text-mining:zero-shot-text-classification` | Zero-shot Text Classification | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `computer-vision:barcode-tool` | Barcode Tool | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `computer-vision:image-input` | Image Input | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `computer-vision:image-output` | Image Output | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `computer-vision:image-processing` | Image Processing | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `computer-vision:image-profile` | Image Profile | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `computer-vision:image-recognition` | Image Recognition | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `computer-vision:image-template` | Image Template | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `computer-vision:image-to-text` | Image to Text | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `computer-vision:pdf-to-text` | PDF to Text | not-started | not-tested | 0/0 | 0/0 | 0 | no |
| `ai-tools:invoice-extractor` | Invoice Extractor | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `ai-tools:llm-override` | LLM Override | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `ai-tools:precision-match` | Precision Match | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `ai-tools:prompt` | Prompt | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `ai-tools:schema-fit` | Schema Fit | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `ai-tools:synthetic-input` | Synthetic Input | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connect:connect-json-builder` | Connect JSON Builder | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connect:connect-output` | Connect Output | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `connect:odbc-loader` | ODBC Loader | not-started | not-tested | 0/0 | 0/0 | 0 | yes |
| `sdk-examples:html-gui-library` | HTML - GUI Library | not-started | not-tested | 0/0 | 0/0 | 0 | no |
