# Probabilistic Demand Forecasting

Research project to predict the probability that an electricity client will have an unusually high demand peak the next day, using readings available at the end of the current day.

**Status:** Source audit, analysis-ready data build, frozen client thresholds
with target labels, leakage-safe modeling examples, baseline experiments, and
model selection with calibration complete; the test-ready configuration is
pending sign-off and final results are TBD.

## Set up and reproduce the data

Requires Python 3.11 or newer. The audit implementation uses only the standard
library; the processed data build uses DuckDB for streaming transformation,
Parquet output, and analytical access.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/demand-forecast download
.venv/bin/demand-forecast audit
.venv/bin/demand-forecast build-data
.venv/bin/demand-forecast build-labels
.venv/bin/demand-forecast build-examples
.venv/bin/demand-forecast run-baselines
.venv/bin/demand-forecast select-models
.venv/bin/demand-forecast calibrate-models --selection-dir artifacts/experiments/<sel-id>
.venv/bin/python -m pytest -q
```

The download goes to ignored `data/raw/`, audit outputs to ignored
`data/audit/`, and processed outputs to ignored `data/processed/`. The build
produces:

- `interval_readings.parquet`: 51,894,720 client-interval readings.
- `client_day.parquet`: 540,570 client-day summaries.
- `client_eligibility.parquet`: 370 activation and eligibility records.
- `catalog.duckdb`: views over all three Parquet tables.
- `manifest.json`: source/configuration identity, dependency versions, schemas,
  checksums, counts, runtime, and audit reconciliation.

`build-labels` freezes each eligible client's nearest-rank 90th-percentile
training threshold and writes to ignored `data/labels/`:

- `client_thresholds.parquet`: 342 frozen thresholds with rank, contributing-day
  counts, fit bounds, and provenance.
- `labeled_days.parquet`: 366,364 eligible client target days with split and a
  strict `peak > threshold` target (train 242,218; selection 30,438;
  calibration 31,122; test 62,586).
- `prevalence_by_split.csv`, `prevalence_by_client.csv`,
  `prevalence_by_month.csv`, `prevalence_by_weekday.csv`: positive/negative
  counts and observed prevalence by group.
- `catalog.duckdb`: views over both Parquet tables.
- `labels_manifest.json`: input/configuration identity, estimator definition,
  schemas, checksums, counts, exclusion counts, and the training-only
  provenance check.

`build-examples` fits per-client scaling and high-use cutoffs and writes
leakage-safe day-ahead examples to ignored `data/examples/`:

- `modeling_examples.parquet`: 364,820 client target days with 27 past-only
  features (train 240,674; selection 30,438; calibration 31,122; test 62,586);
  1,544 train candidates were excluded for insufficient history.
- `client_scaling.parquet`: 342 frozen scale and high-use values with
  provenance.
- `feature_registry.csv`: name, units, source fields, lookback, fitting
  requirement, and latest permissible source date per feature.
- `example_counts_by_split.csv`, `example_counts_by_client.csv`,
  `example_exclusions.csv`: example and exclusion counts.
- `catalog.duckdb`: views over both Parquet tables.
- `examples_manifest.json`: input/configuration identity, missing-history
  policy, schemas, checksums, counts, and the feature-cutoff lineage check.

`run-baselines` fits the client-prevalence and weekly-pattern baselines on
training labels only and scores the selection split under the frozen protocol
(average precision, Brier score, top-10% alert budget with a deterministic
`(client_id, target_date)` tie-break). Each run writes a content-addressed
directory under ignored `artifacts/experiments/<experiment_id>/` containing
`predictions.parquet`, `metrics.json`/`metrics.csv`, and
`experiment_record.json`.

Selection-period baselines (Jan–Mar 2014, experiment `exp-10fa28cb34bb`;
30,438 client-days, 2,232 spikes, prevalence 7.33%, 3,044 alerts):

| Baseline | Average precision | Brier | Top-10% precision | Top-10% recall |
| --- | --- | --- | --- | --- |
| Client prevalence | 0.0625 | 0.0687 | 0.0818 | 0.1116 |
| Weekly pattern | 0.1299 | 0.0673 | 0.1541 | 0.2101 |

The client-prevalence baseline ranks below chance (prevalence 0.0733) because
training spike rates do not carry over to January–March. 89 client-days tie at
its alert cutoff, so the `(client_id, target_date)` tie-break decides 18 alerts.

`select-models` fits the configured logistic and histogram-gradient-boosting
grid on training rows only, scores every configuration on the selection split
(`selection_log.csv`), and freezes one winner per family under
`artifacts/experiments/<sel-id>/`. `calibrate-models` compares out-of-fold
Platt scaling against raw probabilities on the calibration split and writes
the frozen `test_ready.json`. Selection-period model results
(selection `sel-cd5b7e11cb77`; 30,438 client-days, 3,044 alerts):

| Method | Average precision | Brier | Top-10% precision | Top-10% recall |
| --- | --- | --- | --- | --- |
| Client prevalence (baseline) | 0.0625 | 0.0687 | 0.0818 | 0.1116 |
| Weekly pattern (baseline) | 0.1299 | 0.0673 | 0.1541 | 0.2101 |
| Logistic (`all`, C=1.0) | 0.6318 | 0.0406 | 0.5135 | 0.7003 |
| HGB (`all`, lr=0.1, leaves=31, min=200) | 0.6974 | 0.0363 | 0.5506 | 0.7509 |

On the calibration split (31,122 client-days, prevalence 4.61%), raw Brier was
0.0300 (logistic) and 0.0261 (HGB); out-of-fold Platt Brier was slightly worse
for both (0.0310 and 0.0265), so `use_calibration=false` and raw probabilities
remain primary. `test_ready.json` records all frozen choices and is pending
manual sign-off before the Milestone 6 test run.

The completed full build matches the audit counts, including 537,610
model-valid client-days and 342 eligible clients. To use an existing unchanged
archive or text file, pass `--source /path/to/file` to `audit` and `build-data`.
Use `--help` on either command for output-directory options.

Query the processed data with DuckDB, for example:

```sh
.venv/bin/python - <<'PY'
import duckdb

with duckdb.connect("data/processed/catalog.duckdb", read_only=True) as db:
    print(db.sql("SELECT * FROM client_eligibility WHERE NOT eligible"))
PY
```

## Documents

- [Requirements](docs/requirements.md), [design](docs/design.md), and the ordered [project roadmap](docs/tasks.md)
- [Data audit](docs/data_audit.md) and [data dictionary](docs/data_dictionary.md)

Source: [UCI Electricity Load Diagrams 2011–2014](https://archive.ics.uci.edu/dataset/321/electricityloaddiagrams20112014.) by Artur Trindade, [DOI: 10.24432/C58C86](https://doi.org/10.24432/C58C86), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The source file is not committed.
