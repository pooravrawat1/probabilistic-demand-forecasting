# Project roadmap

**Current status:** The source download, audit, quality rules, and primary-study
eligibility audit are complete. The next milestone is to turn the audited source
into tested interval and client-day tables. No modeling result or final-test
metric exists yet.

This is the project's ordered implementation plan. Check off an item only when
its code, focused tests, generated-output manifest, and documentation agree.

## Current state

Already available:

- [x] Reproducible source download and local ZIP/text ingestion.
- [x] Source identity, attribution, license, dimensions, and runtime metadata.
- [x] Timestamp, cadence, daylight-saving, numeric-value, zero-run, duplicate,
  client-coverage, and extreme-day audits.
- [x] Fixed day-assignment, activation, model-valid-day, and eligibility rules.
- [x] Generated audit tables and a client exclusion ledger under ignored
  `data/audit/`.
- [x] Unit tests for midnight assignment, daylight-saving dates, activation,
  invalid values, and idempotent source download.

The audit found 342 eligible clients. The chronological split remains:

| Target dates | Use |
| --- | --- |
| 2012-01-01 through 2013-12-31 | Fit thresholds, preprocessing, baselines, and models |
| 2014-01-01 through 2014-03-31 | Select features and model settings |
| 2014-04-01 through 2014-06-30 | Fit calibration and choose any alert rule |
| 2014-07-01 through 2014-12-31 | One final, untouched test evaluation |

## Milestone 1: Build analysis-ready data

This is the immediate priority. Finish this milestone before implementing
features or models.

- [ ] Add and pin the runtime dependencies needed for Parquet, DuckDB queries,
  modeling, plotting, and testing. Record the resolved environment alongside
  experiment artifacts.
- [ ] Add a `build-data` CLI command that reads the unchanged source and writes
  deterministic outputs under `data/processed/` without spreadsheet or notebook
  edits.
- [ ] Build `interval_readings` at client × source-interval grain with
  `client_id`, `source_timestamp`, `assigned_local_date`, `kw`, and applicable
  quality flags. Use a streaming or partitioned implementation; do not require
  all 51,894,720 long-form readings in memory at once.
- [ ] Build `client_day` at client × assigned-local-date grain with reading
  counts, peak kW, mean kW, estimated energy kWh, positive-use, source-valid,
  model-valid, activation, eligibility, and exclusion fields.
- [ ] Verify that daily energy is `sum(kw × 0.25 hours)`, while daily peak and
  the target threshold remain in kW.
- [ ] Persist a client eligibility table rather than relying only on a count;
  reconcile it with the audit result of 342 eligible clients and its 28 excluded
  clients.
- [ ] Write the processed tables as Parquet and expose stable DuckDB views or a
  catalog for inspection.
- [ ] Write a build manifest containing source hash, configuration hash or
  snapshot, code revision, Python/dependency versions, output schemas, row
  counts, exclusion counts, run time, and generated-file paths.
- [ ] Add a tiny synthetic fixture so data-building tests run without the full
  261 MB archive.
- [ ] Test unique keys, date coverage, midnight assignment, daylight-saving
  exclusion, invalid-reading behavior, activation boundaries, eligibility,
  aggregation arithmetic, and agreement between audit and processed counts.
- [ ] Update the data dictionary from “planned” to the actual schemas and add
  the exact rebuild command to the README.

**Milestone 1 is done when:** one documented command rebuilds both Parquet
tables and their manifest from the source, all data-contract tests pass, and the
published counts reconcile with the completed audit.

## Milestone 2: Freeze thresholds and target labels

- [ ] Implement one explicit percentile definition and record it. The audit
  currently has a nearest-rank helper, but the modeling threshold estimator has
  not been formally chosen.
- [ ] For each of the 342 eligible clients, calculate the 90th percentile of
  model-valid daily peaks from activated 2012–2013 training days only.
- [ ] Save a threshold table with client, threshold kW, estimator, fitting-date
  bounds, contributing-day count, source/build identity, and fitted timestamp.
- [ ] Add `target = 1` only when a valid target day's peak is strictly greater
  than the frozen client threshold; leave invalid target days out of modeled
  examples rather than assigning them a negative label.
- [ ] Report positive/negative counts and prevalence by split, client, month,
  and weekday. Treat later-period prevalence as an observed result, not as 10%
  by construction.
- [ ] Test the strict `>` boundary, percentile behavior for small/tied samples,
  training-only provenance, minimum 180-day rule, and target-date split edges.

**Milestone 2 is done when:** every eligible client has one reproducible frozen
threshold, every valid labeled day can be traced to it, and no post-2013 value
can influence a threshold.

## Milestone 3: Construct leakage-safe modeling examples

Resolve these decisions before inspecting final-test performance:

- [ ] Choose and document the missing-history policy. Recommended initial rule:
  require the full 14-day history for model examples, preserve genuine zero-use
  days, and report excluded rows; add imputation only if the resulting coverage
  is inadequate.
- [ ] Define the training-fitted recent “high-use” cutoff and any per-client
  scaling. Store fitted values and provenance just like target thresholds.
- [ ] Create a feature registry containing name, units, source fields, lookback
  window, fitting requirement, and latest permissible source date.

Then implement:

- [ ] Build one example for each eligible client and valid target date with
  `forecast_date = target_date - 1 day` and `feature_cutoff = forecast_date`.
- [ ] Add yesterday's peak, mean power, and energy; same-weekday values from one
  week earlier; 3-, 7-, and 14-day rolling peak summaries; variability and
  change; recent training-defined high-use counts; and known target-calendar
  fields.
- [ ] Assign splits from the target date, not the forecast date.
- [ ] Store feature-lineage evidence or derive it in tests so every load feature
  is proven to use data no later than its feature cutoff.
- [ ] Save `modeling_examples` as Parquet and record counts by split, client,
  target class, and exclusion reason.
- [ ] Test lag/rolling values on hand-calculated fixtures, year/month boundaries,
  leap day, daylight-saving gaps, unavailable history, past-only feature use,
  nonoverlapping splits, and deterministic output.

**Milestone 3 is done when:** one command rebuilds examples, every exclusion is
counted, and automated leakage tests prove that no target-day load or later data
enters any feature or fitted preprocessing rule.

## Milestone 4: Establish baselines and an experiment protocol

- [ ] Create a versioned experiment record containing data/build identity,
  seeds, feature set, parameter search, split boundaries, estimator versions,
  missing-history policy, and metric definitions.
- [ ] Specify metric details before the final test. Recommended defaults are
  average precision for PR-curve area, Brier score, and a stable descending-risk
  ranking with `(client_id, target_date)` as the deterministic tie-breaker for
  the top 10% alert budget.
- [ ] Implement a client-specific prevalence baseline fitted on training labels.
- [ ] Implement the weekly-pattern probability baseline, including smoothing
  and missing-week handling, fitted on training data only.
- [ ] Produce predictions with a common schema: experiment/model ID, client,
  target date, split, label, raw probability, optional calibrated probability,
  and alert rank/decision.
- [ ] Assert that all compared methods receive exactly the same evaluation keys
  and labels.
- [ ] Add unit tests for baseline fitting, smoothing, unseen/missing cases,
  probabilities in `[0, 1]`, deterministic ranking, and key equality.

**Milestone 4 is done when:** both baselines run end to end and produce scored,
reproducible validation predictions on identical rows.

## Milestone 5: Train and select models without touching the test set

- [ ] Implement logistic regression with training-fitted preprocessing.
- [ ] Implement the documented tree model (initially histogram gradient
  boosting unless evidence supports a different classifier) on the same feature
  and row set.
- [ ] Use January–March 2014 only to choose features and model settings. Log all
  tried configurations and the selection rule, including unsuccessful runs.
- [ ] Freeze the selected raw models and feature set before calibration.
- [ ] On April–June 2014 only, compare an explicitly named probability
  calibration method against uncalibrated probabilities and choose any alert
  threshold or fixed-budget rule.
- [ ] Check convergence, degenerate-client/class cases, probability bounds,
  serialization/reload equality, deterministic seeds, and absence of selection
  or calibration dates in training-fitted state.
- [ ] Write a signed-off “test-ready” configuration that fixes data identity,
  examples, features, models, calibration, metrics, tie rule, and alert budget.

**Milestone 5 is done when:** the selected pipeline can be reconstructed from
the experiment record and all choices are frozen without consulting July–December
2014 outcomes.

## Milestone 6: Run the final evaluation once

- [ ] Confirm the test-ready configuration and output directory are immutable or
  content-addressed before scoring the final period.
- [ ] Evaluate each frozen method once on July–December 2014 using identical
  client-day keys.
- [ ] Report test size, event prevalence, average precision, Brier score, and
  precision/recall for the highest-risk 10% of client-days, with exact alert
  counts and tie behavior.
- [ ] Produce PR, calibration/reliability, prevalence-over-time, and model
  comparison plots with labeled axes, units, sample sizes, and split names.
- [ ] Make paired method comparisons. Where sample size permits, compute
  uncertainty intervals by resampling calendar-week blocks rather than treating
  client-days as independent.
- [ ] Save predictions, metrics, plots, environment information, runtime, and
  checksums under a unique experiment artifact directory.
- [ ] Do not retune after seeing test results. Any later exploratory model must
  be labeled post-test analysis rather than a replacement final evaluation.

**Milestone 6 is done when:** the final numbers and figures are reproducible
from the frozen run and every method was scored on the same rows.

## Milestone 7: Interpretation and robustness

- [ ] Cluster clients using normalized training-period profiles only; document
  cluster-count selection and assess stability.
- [ ] Report test performance by client and training-derived cluster, including
  subgroup size and prevalence so small groups are not overinterpreted.
- [ ] Summarize logistic coefficients and tree permutation importance using only
  supported interpretations; do not treat feature importance as causality.
- [ ] Inspect representative correct alerts, missed spikes, and false alerts
  without changing the frozen evaluation pipeline.
- [ ] Review the 25 audit-flagged extreme client-days against neighboring
  readings, record any evidence-based confirmed errors, and run a clearly labeled
  sensitivity analysis with confirmed errors removed.
- [ ] Discuss drift, heterogeneity, missing contextual causes, the client-specific
  target definition, daylight-saving exclusions, dependence between observations,
  and limits on generalizing to unseen clients.

## Milestone 8: Final deliverables and reproducibility release

- [ ] Create `docs/experiment.md` with the frozen protocol and a run ledger.
- [ ] Create `docs/results.md` with final tables, plot references, uncertainty,
  robustness checks, and conclusions tied to the research questions.
- [ ] Write the IEEE-format report of at least four pages covering question,
  data, audit, methods, results, limits, and conclusion.
- [ ] Prepare a 10-minute presentation consistent with the report and final
  experiment artifacts.
- [ ] Add a single documented pipeline entry point (for example `reproduce`)
  that runs data construction, examples, frozen experiments, evaluation, and
  figures in dependency order.
- [ ] Add automated checks for formatting/linting, unit tests, synthetic
  end-to-end execution, and documentation links. Keep the full-data experiment
  as a documented local workflow if it is too large for continuous integration.
- [ ] From a clean checkout, recreate the environment, obtain the source, run
  the pipeline, and compare manifests/checksums and reported metrics.
- [ ] Update the README status and quick-start commands; verify that all links,
  tables, figures, and citations resolve.

**Project completion means:** another person can rebuild the modeling data,
rerun all comparisons, reproduce every reported figure and metric, and verify
that each feature and fitted rule was available before its target day.

## Decisions that must be frozen before final test

| Decision | Deadline | Current status |
| --- | --- | --- |
| Source timestamp and daylight-saving assignment | Complete before data build | Fixed in [data audit](data_audit.md) |
| Activation and valid-day criteria | Complete before data build | Fixed in [data audit](data_audit.md) |
| Percentile estimator for client thresholds | Before Milestone 2 labels | TBD |
| Missing-history policy | Before Milestone 3 examples | TBD; recommendation above |
| Training-derived high-use and scaling rules | Before Milestone 3 examples | TBD |
| Treatment of flagged extreme values | Before final protocol; sensitivity may follow | Flagged and retained unless confirmed erroneous |
| Feature set and model hyperparameters | End of selection period analysis | TBD |
| PR-area estimator and probability calibration method | Before calibration/final test | TBD; metric recommendation above |
| Alert budget/threshold and deterministic tie rule | End of calibration period analysis | 10% budget required for reporting; exact rule TBD |
| Cluster construction and stability rule | Before subgroup test analysis | TBD |
| Any date-split change | Before inspecting final test | No change planned |
