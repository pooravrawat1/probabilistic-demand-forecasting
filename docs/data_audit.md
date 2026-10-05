# Source data audit

**Status:** Completed for the downloaded UCI archive. This is a source and eligibility audit, not a model result. Generated files are under ignored `data/audit/`.

## Provenance

- Dataset: [ElectricityLoadDiagrams20112014](https://archive.ics.uci.edu/dataset/321/electricityloaddiagrams20112014.) by Artur Trindade, [DOI: 10.24432/C58C86](https://doi.org/10.24432/C58C86), CC BY 4.0.
- Downloaded archive: `electricityloaddiagrams20112014.zip`, **261,335,609 bytes**; SHA-256 `f6c4d0e0df12ecdb9ea008dd6eef3518adb52c559d04a9bac2e1b81dcfc8d4e1`.
- Audited member: `LD2011_2014.txt`, **710,998,915 bytes**, ZIP CRC32 `db132fb7`. The archive remains unchanged in ignored `data/raw/`.
- Audit runtime: Python standard library; exact Python version, configuration, seed, and run time are recorded in `data/audit/summary.json`. Rules are versioned in [project.toml](../config/project.toml).

## Observed structure and counts

| Stage or check | Observed |
| --- | ---: |
| Client columns | 370 |
| Source timestamp rows | 140,256 |
| Client-interval values | 51,894,720 |
| First and last source labels | `2011-01-01 00:15:00` to `2015-01-01 00:00:00` |
| Assigned local dates | 2011-01-01 through 2014-12-31 (1,461 days) |
| Days with 96 assigned timestamp rows | 1,461 of 1,461 |
| Source-valid client-days | 540,570 |
| Client-days after excluding eight daylight-saving dates | 537,610 |
| Positive and zero client-interval values | 41,437,378 positive; 10,457,342 zero |
| Client-days with all-zero readings | 105,670 |
| Model-valid client-days in 2012–2013 | 268,990 |
| Pre-activation training client-days removed from eligibility counts | 26,010 |
| Post-activation training client-days | 242,980 |
| Post-activation training client-days from ineligible clients | 762 |
| Eligible training client-days | 242,218 |

There were **zero** duplicate timestamps, cadence gaps, off-grid timestamps, wrong-width rows, missing values, nonnumeric values, nonfinite values, and negative values. The longest all-zero run for one client is 1,294 source-valid days. These zero runs are why activation is based on sustained use rather than a first isolated positive reading.

The endpoint labels support the end-of-interval rule: assigning `00:00:00` to the preceding day yields 96 rows on every date, including the first and last days. The source's daylight-saving representation also yields 96 rows on each of the four spring and four fall transition dates. The UCI description says the spring 01:00–02:00 readings are zero, but the actual 01:00–01:45 labels include positive values (for example, 471 positive client-values on 2014-03-30). The audit therefore flags and excludes entire transition dates from modeling rather than interpreting those slots as ordinary intervals.

## Rules fixed before modeling

1. **Day assignment:** treat source timestamps as 15-minute interval end labels; `00:00:00` closes the prior local day. Preserve the original timestamp.
2. **Source-valid client-day:** require exactly 96 unique, on-grid timestamps for that day and 96 finite, numeric, nonnegative readings for the client. The source has no failures under this rule.
3. **Model-valid client-day:** additionally exclude the last Sunday in March and October in each year. This removes 2,960 client-days (8 dates × 370 clients). Do not treat source-filled spring zeros or fall aggregates as ordinary consumption.
4. **Activation:** use the first day of the earliest 14 consecutive model-valid calendar days with positive use on at least 10 of them, found using data no later than 2013-12-31. Earlier zeros are pre-activation history. Fully zero days after activation stay in the data.
5. **Primary-study eligibility:** require at least 180 model-valid days from activation through the 2012–2013 training period. This admits **342 clients**; **19** have no sustained activation by the training end and **9** have insufficient valid training days. The generated `data/audit/exclusion_ledger.csv` records each client and reason.
6. **Confirmed errors and extremes:** structural failures and invalid numeric values would invalidate affected days. None were found. Flag a client-day for review if its peak exceeds both twice its training-period median daily peak and 1.5 times its training-period 99th percentile; this produced **25 review candidates**. They remain included until a specific error is confirmed. This review threshold is not the 90th-percentile spike target.

The planned training, selection, calibration, and final-test dates remain unchanged. No final-test performance was evaluated. Feature missing-history handling, target thresholds, model scores, and conclusions remain **TBD**.
