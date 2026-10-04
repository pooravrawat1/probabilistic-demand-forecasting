# Design

**Status:** Proposed architecture. Dataset-specific rules marked **TBD** must be resolved from the audit before modeling.

## Data flow

```text
Unchanged UCI source (outside Git)
  → source validation and timestamp audit
  → interval readings
  → valid client-day summaries
  → training-only client rules and frozen spike thresholds
  → forecast examples with past-only features and target labels
  → baselines and models → calibration/alert selection → final evaluation
```

Use Python for a reproducible pipeline, Parquet for intermediate tables, and DuckDB for analytical queries. Keep raw data, generated tables, and model artifacts under ignored local directories such as `data/` and `artifacts/`. Commit code, small configuration, documentation, and report figures. Persist the input file identity, pipeline configuration, code revision, dependency versions, seeds, and row counts with each run.

## Source and day assignment

The source is expected to be a semicolon-delimited wide table with a timestamp and one column per client. Ingest it in bounded chunks or with DuckDB rather than requiring a single in-memory reshape. Verify delimiter, timestamp parsing, client columns, duplicate and missing timestamps, numeric conversion, and observed coverage. Preserve `source_timestamp` separately from `assigned_local_date`.

Before aggregation, inspect the source documentation and representative days to decide whether `00:00` belongs to the prior interval/day. Audit ordinary days and both daylight-saving transitions in Portuguese local time. Record the observed interval-count distribution and the chosen rule. The validity rule for incomplete days is **TBD**; apply it consistently across thresholds, labels, features, and eligibility. Retain quality flags and an exclusion ledger. Review extreme values against nearby readings before deciding whether they are errors.

## Logical tables and grain

| Table | Grain | Core fields |
| --- | --- | --- |
| `interval_readings` | Client × source interval | `client_id`, `source_timestamp`, `assigned_local_date`, `kw`, parse/quality flags. |
| `client_day` | Client × assigned local date | `client_id`, `local_date`, valid `reading_count`, `daily_peak_kw`, `mean_kw`, `energy_kwh_est`, activation/validity flags. |
| `modeling_examples` | Client × target local date | `client_id`, `forecast_date`, `target_date`, `feature_cutoff`, `split`, feature columns, `threshold_kw`, `target`. |

`energy_kwh_est` is the sum of valid 15-minute `kw × 0.25` contributions; the target is derived only from `daily_peak_kw`. Unique keys, nonnegative accepted power values, valid probabilities, and expected date relationships should be asserted during table construction. Preserve quality summaries before filtering rows.

## Fitting and feature lineage

Use 2011 only for historical context and warm-up. Determine the final activation rule and valid-day policy from the audit, then form the eligible client set using at least 180 valid **2012–2013** days after activation. Fit each eligible client's 90th-percentile peak threshold from that same training period. Fit all load scaling and high-use cutoffs from training data only. Save these fitted values with their provenance and reuse them unchanged in later splits.

Create one example for each eligible client and valid target date, with `forecast_date = target_date − 1 local day`. Its features can read only client-day records through the forecast date. Examples requiring unavailable lag or rolling history need a documented missing-feature policy; exclude or impute them consistently for all methods, with counts. Calendar features may describe the target date because that date is known at forecast time. For every feature, store or test its latest source date against `feature_cutoff`.

Split examples by target date using the periods in [requirements.md](requirements.md). If the audit requires different boundaries, record the decision before using the final test. Keep the final test unavailable to model fitting, feature selection, calibration, and alert-rule selection.

## Models and evaluation

1. Fit a prevalence forecast per client using training labels. Fit the weekly-pattern probability mapping from training examples using that client's corresponding-weekday lag; specify smoothing and missing-lag handling in the experiment record.
2. Train logistic regression and a histogram-gradient-boosting classifier (or another documented tree classifier) on the same feature and row set. Tune settings on January–March 2014. Log settings and seeds.
3. If needed, fit calibration and choose the alert rule on April–June 2014. Retain raw and calibrated probabilities for comparison. Freeze all choices before the July–December 2014 test.
4. Score every method on the same eligible test keys. Calculate precision–recall area (with the exact estimator named in the experiment record), Brier score, and precision/recall at the highest-risk 10% of client-days. Resolve ties deterministically and record the alert count. Plot reliability against observed event frequency, and report test size and prevalence.
5. Compare paired predictions on identical keys. Where data supports it, estimate uncertainty by resampling calendar-week blocks rather than individual rows. Report results by client and by training-derived client clusters. Derive clusters from normalized training-period daily profiles only, and describe selection and stability before using them for subgroup interpretation.

Create a separate analysis of flagged extreme days and the effect of excluding confirmed errors. Explain load drift, differences among clients, missing context for causes of peaks, and limits of the client-specific label. Report coefficient or permutation-importance summaries and representative forecast errors. Figures and conclusions remain **TBD**.

## Verification gates

- The documented timestamp rule produces audited day counts, including daylight-saving examples.
- No frozen threshold, scaler, high-use cutoff, cluster, or baseline parameter uses validation, calibration, or test outcomes.
- Each feature's latest source date is on or before its forecast date; each label uses its target date.
- Split target dates do not overlap, and every model receives exactly the same evaluation keys.
- A clean checkout with the source file and documented dependencies can rebuild the data, predictions, metrics, and report figures from recorded commands.
