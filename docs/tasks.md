# Tasks

**Status:** Planned. Check items only when implementation or evidence exists; observed values and findings are **TBD**.

## 1. Establish the source and reproducible workspace

- [ ] Add Python project metadata, locked or recorded dependency versions, seed/configuration files, and documented run commands.
- [ ] Implement source download or local-path ingestion while keeping the unchanged UCI file outside Git; record attribution, license, source identity, and actual dimensions.
- [ ] Audit timestamps, interval counts, daylight-saving cases, client coverage, zero runs, missing/nonnumeric/negative values, duplicates, and exceptional readings.
- [ ] Decide and document the timestamp-to-day, activation, valid-day, and confirmed-error rules before modeling; record any split-date changes before test inspection.
- [ ] Produce a data audit, data dictionary, quality flags, and counts/exclusion ledger for each transformation.

## 2. Build client-day data and targets

- [ ] Build reproducible interval and client-day Parquet tables with DuckDB access; verify kW and kWh calculations.
- [ ] Apply activation and validity rules; list clients excluded from the primary study and why.
- [ ] Freeze each eligible client's 90th-percentile threshold from valid 2012–2013 peaks, requiring at least 180 valid training days after activation.
- [ ] Build target labels from the frozen threshold and report actual prevalence by period.
- [ ] Add focused checks for timestamp assignment, daylight-saving days, threshold provenance, and nonoverlapping target-date splits.

## 3. Construct leakage-safe examples

- [ ] Implement lagged, rolling, high-use, and calendar features from data available by the forecast cutoff.
- [ ] Fit normalization and high-use rules on training data only; document missing-history handling.
- [ ] Record feature definitions and latest source dates; test that every feature predates its target.
- [ ] Save modeling examples and counts by split and exclusion reason.

## 4. Compare forecasts

- [ ] Implement client prevalence and weekly-pattern probability baselines on training data.
- [ ] Train logistic regression and a tree-based classifier on the same eligible examples and information.
- [ ] Select features and settings using January–March 2014; write an experiment record with seeds, parameters, split boundaries, and metric definitions.
- [ ] Calibrate probabilities and choose alert threshold/budget, if needed, using April–June 2014; retain uncalibrated forecasts.
- [ ] Verify identical evaluation keys across all methods, then evaluate once on July–December 2014.

## 5. Interpret and present results

- [ ] Produce a results table, prevalence and performance plots, calibration plot, and precision/recall at a 10% alert budget.
- [ ] Add paired comparisons and calendar-week uncertainty intervals where feasible.
- [ ] Cluster clients using normalized training-period profiles; report test results by client/cluster and assess cluster stability.
- [ ] Review extreme-day flags, representative correct and incorrect forecasts, model importance, and sensitivity to confirmed error removal.
- [ ] Write an IEEE-format report of at least four pages and prepare a 10-minute presentation covering question, data, methods, results, limits, and conclusion.
- [ ] From a clean checkout, follow the documented commands to rebuild the modeling data, comparisons, and figures; confirm every feature is available before its target day.

## Decisions to record before final test

| Decision | Current status |
| --- | --- |
| Source timestamp and daylight-saving day assignment | TBD after audit |
| Sustained-nonzero activation and valid-day criteria | TBD after audit |
| Missing-history and extreme-value treatment | TBD after audit |
| Any change to the planned date splits | TBD before test inspection |
| Exact PR-curve area estimator, calibration method, and tie rule | TBD in experiment record |
