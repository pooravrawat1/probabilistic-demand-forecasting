# Data dictionary

**Status:** Source audit outputs exist. The modeling tables below are planned and have not been built.

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

## Planned modeling tables

| Table | Grain | Required fields |
| --- | --- | --- |
| `interval_readings` | Client × source timestamp | `client_id`, `source_timestamp`, `assigned_local_date`, `kw`, numeric and source-quality flags. |
| `client_day` | Client × local date | `client_id`, `local_date`, reading count, `daily_peak_kw`, `mean_kw`, `energy_kwh_est`, activation and validity flags. |
| `modeling_examples` | Client × target local date | `client_id`, `forecast_date`, `target_date`, `feature_cutoff`, chronological split, past-only features, frozen `threshold_kw`, and binary target. |

The spike threshold, labels, modeled energy, features, and predictions are **TBD** until those tables are implemented.
