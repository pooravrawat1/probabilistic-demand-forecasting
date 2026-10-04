# Requirements

**Status:** Proposed. This document restates the supplied PRD as implementable requirements; no data audit or experiment has been completed.

## Goal and scope

At the end of local day **d**, issue a probability that client **c** will have an unusually high 15-minute demand reading on local day **d + 1**. The primary question is whether recent consumption predicts these events better than historical and weekly-pattern baselines. A secondary question asks whether performance differs between clients with regular and irregular use.

The primary study evaluates **future days for clients observed during training**. It does not establish performance for previously unseen clients. The input is the [UCI Electricity Load Diagrams 2011–2014 dataset](https://archive.ics.uci.edu/dataset/321/electricityloaddiagrams20112014.) by Artur Trindade ([DOI: 10.24432/C58C86](https://doi.org/10.24432/C58C86), CC BY 4.0). The PRD describes 370 clients and 15-minute readings; actual dimensions, date coverage, and file structure are **TBD pending ingestion**.

## Forecast and target

| Item | Requirement |
| --- | --- |
| Prediction unit | One eligible client and one target local day. |
| Forecast cutoff | After the final interval assigned to day d is recorded; before any interval assigned to day d + 1 is used. |
| Daily peak | Maximum **valid** 15-minute power reading assigned to a client-day, measured in **kW**. |
| Client threshold | 90th percentile of that client's valid daily peaks in the **2012–2013 training period**. Compute once from training data and freeze it for all splits. |
| Positive label | Target-day peak is **strictly greater** than the frozen client threshold. Otherwise the label is 0, provided the target day is valid. |
| Output | Probability in [0, 1]; an optional alert decision uses a rule chosen before final test evaluation. |

The timestamp-to-local-day rule is **TBD after inspecting the source convention**, especially whether a `00:00` record closes the preceding day. The audit must establish expected interval counts for ordinary and daylight-saving days before defining valid days. Label prevalence in each split must be measured; the training percentile does not guarantee 10% prevalence in later periods.

## Data and eligibility

1. Keep the original download unchanged outside Git. A repeatable Python pipeline must download or locate it, verify its structure, and produce processed data without spreadsheet edits.
2. Audit duplicate timestamps, missing or nonnumeric values, negative values, unexpected interval counts, long zero runs, daylight-saving days, and exceptional readings. Flag plausible extreme loads for review rather than automatically removing them.
3. Establish a sustained-nonzero activation rule from the data audit. Ignore zero-filled pre-activation history for eligibility and training statistics. The final rule and valid-day criteria are **TBD** and must be recorded before modeling.
4. Include a client in the primary study only if it has at least **180 valid training days after activation**. Record each excluded client and reason. Use identical eligible client-day examples for every compared method.
5. Distinguish power (**kW**) from estimated energy (**kWh**): each valid 15-minute interval contributes `kW × 0.25 hours` to the daily energy estimate. The spike target uses peak kW.
6. Store row counts, invalid counts, and exclusion counts after each transformation. Document source attribution, units, schemas, and quality decisions in a data audit and data dictionary.

## Chronological study plan

| Period | Purpose |
| --- | --- |
| 2011 | Historical warm-up and data audit. |
| 2012–2013 | Train models and fit client thresholds, normalization, and other training-derived rules. |
| January–March 2014 | Select features and model settings. |
| April–June 2014 | Fit probability calibration and select the alert threshold or budget, if used. |
| July–December 2014 | Untouched final test. |

Assign splits by **target date**, not forecast date. Any adjustment prompted by insufficient eligible clients or incomplete periods must be documented before inspecting final test performance. Chronological split boundaries must not overlap.

## Features and comparisons

Features may use readings through day d and target-day calendar information only. Start with yesterday's peak, mean power, and estimated energy; same-weekday values from one week earlier; 3-, 7-, and 14-day rolling peak averages; rolling variability and change; recent high-use counts using a training-defined level; and target-day weekday, month, and weekend status. Record each feature's source, window, and latest permissible timestamp. Fit any client scaling or normalization on training data only.

Compare a client-specific training prevalence baseline, a weekly-pattern probability rule fitted on training data, logistic regression, and a tree-based classifier on the same examples. The two models use the same available information. Preserve uncalibrated probabilities and fit any calibration without final test data. More complex sequence methods come only after the baseline study works.

## Evaluation and deliverables

**Primary metrics:** area under the precision–recall curve, precision and recall for the highest-risk 10% of client-days, Brier score, and a calibration plot. Report test size, spike prevalence, paired comparisons on identical rows, client or cluster subgroup results, and time-block confidence intervals where feasible. Interpret coefficients or permutation importance and representative successes and failures. A baseline win remains a valid result.

The repository must eventually provide a documented command sequence for ingestion, feature construction, training, evaluation, and figure generation; fixed random seeds where applicable; recorded configuration and dependency versions; and focused checks for day assignment, daylight-saving cases, training-only fitting, past-only features, split boundaries, and identical evaluation rows.

Final deliverables are a reproducible repository, data audit and dictionary, experiment record, results table and labeled plots, an **IEEE-format report of at least four pages**, and a **10-minute presentation**. Observed counts, scores, clusters, and conclusions remain **TBD** until verified. Completion means another person can rebuild the modeling table, rerun comparisons, reproduce figures, and verify that each feature predates its target day.
