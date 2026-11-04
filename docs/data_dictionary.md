# Data dictionary

**Status:** Source audit outputs, analysis-ready interval, client-day, and
eligibility tables, and frozen client thresholds with target labels exist.
Features and predictions have not been built.

## Source and units

The source is [UCI ElectricityLoadDiagrams20112014](https://archive.ics.uci.edu/dataset/321/electricityloaddiagrams20112014.) by Artur Trindade ([DOI: 10.24432/C58C86](https://doi.org/10.24432/C58C86), CC BY 4.0). Its `LD2011_2014.txt` member is a semicolon-delimited wide table: an unlabeled timestamp column followed by `MT_001` through `MT_370`. Numeric text uses decimal commas. Timestamps are Portuguese local clock labels without a UTC offset. They label the **end** of each 15-minute interval; a `00:00:00` label is assigned to the preceding local date.

Each source value is power in **kilowatts (kW)**. A normal 15-minute interval's estimated energy is `kW × 0.25 hours`, measured in **kilowatt-hours (kWh)**. Daily peak and the future spike label use kW, not kWh. Daylight-saving dates are excluded from modeled days because the source keeps 96 labels while representing clock changes specially.

## Audit output files

All audit outputs are generated under ignored `data/audit/` by the `audit` command. They are rebuilt from the unchanged source and [configuration](../config/project.toml).

| File | Grain and key | Fields and meaning |
| --- | --- | --- |
| `summary.json` | One audit run | Source path, SHA-256, ZIP member identity, runtime/configuration, date coverage, counts at each stage, quality flag counts, eligibility counts, and small error samples. |
| `calendar_day_quality.csv` | One assigned local date | `interval_rows`, `unique_timestamps`, `duplicate_rows`, `off_grid_rows`, `dst_transition` (`spring`/`fall`/blank), pipe-separated `flags`. |
| `day_quality.csv` | `client_id` × `local_date` | Interval and numeric-value counts, `daily_peak_kw`, `source_valid`, `model_valid`, and pipe-separated `flags`. `source_valid` requires 96 unique, on-grid timestamps and 96 accepted values for that client; `model_valid` additionally excludes daylight-saving transition days. |
| `client_summary.csv` | One client | First/last positive date, `activation_date`, valid training days after activation, longest run of all-zero source-valid days, eligibility and reason, largest observed daily peak. |
| `exclusion_ledger.csv` | One excluded client | Client, exclusion reason, and valid training days after activation. Client-day exclusions remain visible in `day_quality.csv`. |
| `extreme_day_review.csv` | One flagged client-day | Peak kW and training-derived median, 99th percentile, and review cutoff. These are review candidates and remain in the data. |

Accepted numeric values are finite and nonnegative. Empty fields, other nonnumeric text, infinities/NaNs, and negative values get distinct counts; no such values were found in this source audit. A `zero_use_day` flag means all accepted readings for that client-day are zero. It does not automatically mean an error.

## Processed data outputs

The `build-data` command writes these ignored local files under
`data/processed/`. `manifest.json` records the exact schema, source and
configuration identities, dependency versions, counts, checksums, and audit
reconciliation for each run. `catalog.duckdb` exposes views named
`interval_readings`, `client_day`, and `client_eligibility` over the Parquet
files.

### `interval_readings.parquet`

Grain and key: one row per `client_id` × `source_timestamp`. The completed
source build has 51,894,720 rows.

| Field | Type | Meaning |
| --- | --- | --- |
| `client_id` | string | Source client identifier, `MT_001` through `MT_370`. |
| `source_timestamp` | timestamp | Original naive Portuguese local interval-end label. |
| `assigned_local_date` | date | Date receiving the interval; midnight is assigned to the prior day. |
| `kw` | double, nullable | Accepted finite, nonnegative power value; invalid values become null. |
| `numeric_valid` | boolean | Whether the source value was accepted into `kw`. |
| `duplicate_timestamp` | boolean | Whether the source timestamp appeared more than once. |
| `off_grid_timestamp` | boolean | Whether the timestamp was outside the configured 15-minute grid. |
| `dst_transition` | boolean | Whether `assigned_local_date` is a Portuguese clock-transition date. |
| `quality_flags` | string | Pipe-separated numeric, timestamp, and daylight-saving flags. |

### `client_day.parquet`

Grain and key: one row per `client_id` × `local_date`. The completed source
build has 540,570 rows, of which 537,610 are model-valid.

| Field | Type | Meaning |
| --- | --- | --- |
| `client_id` | string | Source client identifier. |
| `local_date` | date | Assigned local calendar date. |
| `interval_rows` | integer | Number of source interval rows assigned to the client-day. |
| `unique_timestamps` | integer | Distinct source timestamps assigned to the client-day. |
| `valid_values` | integer | Accepted numeric power values. |
| `positive_values` | integer | Accepted values strictly greater than zero. |
| `daily_peak_kw` | double | Maximum accepted 15-minute power reading in kW. |
| `mean_kw` | double | Mean accepted power reading in kW. |
| `energy_kwh_est` | double | Estimated daily energy, `sum(kw × 0.25 hours)`. |
| `source_valid` | boolean | Meets interval-count, uniqueness, grid, and numeric-validity rules. |
| `model_valid` | boolean | Source-valid and not an excluded daylight-saving transition. |
| `zero_use_day` | boolean | All accepted readings are zero. This is not automatically an error. |
| `dst_transition` | boolean | Date is one of the excluded clock-transition dates. |
| `activation_date` | date, nullable | First day of the earliest qualifying sustained-use window. |
| `post_activation` | boolean, nullable | Date is on or after activation; null when the client never activates. |
| `client_eligible` | boolean | Client has at least 180 model-valid training days after activation. |
| `exclusion_reason` | string | Empty for eligible clients; otherwise the fixed client-level reason. |
| `quality_flags` | string | Pipe-separated day-level quality and zero-use flags. |

### `client_eligibility.parquet`

Grain and key: one row per `client_id`. It contains 370 rows: 342 eligible
clients, 19 without sustained activation by the training end, and 9 with too
few valid training days after activation.

| Field | Type | Meaning |
| --- | --- | --- |
| `client_id` | string | Source client identifier. |
| `activation_date` | date, nullable | Fixed activation date derived without data after 2013-12-31. |
| `valid_training_days_after_activation` | integer | Model-valid 2012–2013 days on or after activation. |
| `eligible` | boolean | Meets the configured 180-day primary-study requirement. |
| `exclusion_reason` | string | Empty, `no_sustained_activation_by_training_end`, or `insufficient_valid_training_days`. |

## Label outputs

The `build-labels` command writes these ignored local files under
`data/labels/` from `data/processed/`. `labels_manifest.json` records input
paths and checksums, the processed manifest and source SHA-256, configuration
identity, dependency versions, the estimator definition, output schemas,
checksums, counts, excluded target-day counts by reason, split prevalence, and
the training-only provenance check. `catalog.duckdb` exposes views named
`client_thresholds` and `labeled_days` over the Parquet files.

### `client_thresholds.parquet`

Grain and key: one row per eligible `client_id`, ordered by `client_id`. Each
threshold is the nearest-rank 90th percentile (`k = ceil(0.9 × n)`, the k-th
smallest value) of the client's model-valid post-activation daily peaks dated
2012-01-01 through 2013-12-31.

| Field | Type | Meaning |
| --- | --- | --- |
| `client_id` | string | Source client identifier. |
| `threshold_kw` | double | Frozen 90th-percentile training daily peak in kW. |
| `estimator` | string | Always `nearest_rank`. |
| `percentile` | double | Configured percentile probability (0.9). |
| `rank_index` | integer | Nearest-rank index `k = ceil(percentile × contributing_days)`. |
| `contributing_days` | integer | Model-valid post-activation training days used; equals `valid_training_days_after_activation`. |
| `days_at_threshold` | integer | Contributing days whose peak equals the threshold. |
| `training_positive_days` | integer | Contributing days whose peak is strictly above the threshold. |
| `fit_start` / `fit_end` | date | Configured training bounds (2012-01-01 to 2013-12-31). |
| `first_fit_date` / `last_fit_date` | date | Actual earliest and latest contributing day. |
| `source_sha256` | string | SHA-256 of the unchanged source archive from the processed manifest. |
| `processed_manifest_sha256` | string | SHA-256 of the processed build manifest used as input. |
| `fitted_at_utc` | timestamp | Single run timestamp for this label build. |

### `labeled_days.parquet`

Grain and key: one row per eligible `client_id` × `target_date`, ordered by
`client_id`, `target_date`. Only model-valid post-activation days dated
2012-01-01 through 2014-12-31 appear; invalid and pre-activation days are
omitted rather than labeled negative, and dates before 2012 or after the test
end are excluded.

| Field | Type | Meaning |
| --- | --- | --- |
| `client_id` | string | Source client identifier. |
| `target_date` | date | Local date being labeled. |
| `split` | string | `train`, `selection`, `calibration`, or `test`, assigned from `target_date`. |
| `daily_peak_kw` | double | Maximum accepted 15-minute power reading in kW. |
| `threshold_kw` | double | Frozen client threshold in kW. |
| `target` | tinyint | 1 when `daily_peak_kw > threshold_kw` strictly, else 0. |

### Prevalence CSVs

Deterministic positive/negative counts over `labeled_days.parquet` with
`prevalence = positives / n` rounded to six decimals:

| File | Grouping columns |
| --- | --- |
| `prevalence_by_split.csv` | `split` (ordered train, selection, calibration, test) |
| `prevalence_by_client.csv` | `client_id`, `split` |
| `prevalence_by_month.csv` | `split`, `year`, `month` |
| `prevalence_by_weekday.csv` | `split`, `iso_weekday` (1 = Monday), `weekday_name` |

Each file then has `n`, `positives`, `negatives`, and `prevalence` columns.
Later-period prevalence is an observed result, not 10% by construction.

## Planned modeling table

| Table | Grain | Required fields |
| --- | --- | --- |
| `modeling_examples` | Client × target local date | `client_id`, `forecast_date`, `target_date`, `feature_cutoff`, chronological split, past-only features, frozen `threshold_kw`, and binary target. |

Features, modeling examples, and predictions are **TBD** until the remaining
roadmap milestones are implemented.
