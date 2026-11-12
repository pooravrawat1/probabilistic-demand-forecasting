from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

import duckdb

from demand_forecasting.examples import build_examples
from demand_forecasting.labels import build_labels


def _range(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


ACTIVATION = date(2011, 12, 26)


def _peak(day: date) -> float:
    return float((day - ACTIVATION).days + 1)


class ExamplesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.processed = self.root / "processed"
        self.processed.mkdir()
        self.labels = self.root / "labels"
        self.output = self.root / "examples"
        self.config = {
            "run": {"random_seed": 42},
            "study": {
                "training_start": "2011-12-26",
                "training_end": "2012-01-15",
                "minimum_valid_training_days": 5,
                "selection_start": "2012-01-16",
                "selection_end": "2012-01-31",
                "calibration_start": "2012-02-01",
                "calibration_end": "2012-02-15",
                "test_start": "2012-02-16",
                "test_end": "2012-02-29",
            },
            "labels": {
                "threshold_estimator": "nearest_rank",
                "threshold_percentile": 0.9,
            },
            "examples": {
                "history_window_days": 14,
                "min_history_valid_days": 12,
                "weekly_lag_max_weeks": 4,
                "high_use_percentile": 0.75,
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_processed(
        self,
        client_day_rows: list[tuple],
        eligibility_rows: list[tuple],
    ) -> None:
        connection = duckdb.connect()
        try:
            connection.execute("SET threads = 1")
            connection.execute(
                "CREATE TEMP TABLE client_day_stage ("
                "client_id VARCHAR, local_date DATE, daily_peak_kw DOUBLE, "
                "mean_kw DOUBLE, energy_kwh_est DOUBLE, "
                "model_valid BOOLEAN, post_activation BOOLEAN)"
            )
            connection.executemany(
                "INSERT INTO client_day_stage VALUES (?, ?, ?, ?, ?, ?, ?)",
                client_day_rows,
            )
            connection.execute(
                "CREATE TEMP TABLE eligibility_stage ("
                "client_id VARCHAR, activation_date DATE, "
                "valid_training_days_after_activation INTEGER, "
                "eligible BOOLEAN, exclusion_reason VARCHAR)"
            )
            connection.executemany(
                "INSERT INTO eligibility_stage VALUES (?, ?, ?, ?, ?)",
                eligibility_rows,
            )
            for table, filename in (
                ("client_day_stage", "client_day.parquet"),
                ("eligibility_stage", "client_eligibility.parquet"),
            ):
                connection.execute(
                    f"COPY (SELECT * FROM {table}) TO '{self.processed / filename}' "
                    "(FORMAT PARQUET)"
                )
        finally:
            connection.close()
        (self.processed / "manifest.json").write_text(
            json.dumps({"source": {"sha256": "fakesource"}}), encoding="utf-8"
        )

    def _default_fixture(self, *, late_peak_scale: float = 1.0) -> None:
        """Build the fixture; late_peak_scale multiplies peaks on/after 2012-01-16."""
        rows = []
        cutoff = date(2012, 1, 16)
        for day in _range(ACTIVATION, date(2012, 2, 29)):
            peak = _peak(day)
            if day >= cutoff:
                peak *= late_peak_scale
            rows.append(
                ("MT_A", day, peak, peak / 2, peak * 10, True, True)
            )
        # MT_C: lag1 and lag7 fallback (invalid t-1 and t-7 for target 01-16),
        # and mean_kw 0 (zero-median client still gets finite features).
        for day in _range(ACTIVATION, date(2012, 1, 16)):
            valid = day not in (date(2012, 1, 9), date(2012, 1, 15))
            rows.append(("MT_C", day, 8.0, 0.0, 80.0, valid, True))
        # MT_D: fewer than 12 valid days in every full window.
        for day in _range(ACTIVATION, date(2012, 1, 16)):
            valid = day not in (
                date(2012, 1, 2),
                date(2012, 1, 3),
                date(2012, 1, 4),
            )
            rows.append(("MT_D", day, 8.0, 4.0, 80.0, valid, True))
        # MT_E: all four weekly-lag candidates invalid for target 01-16.
        for day in _range(date(2011, 12, 19), date(2012, 1, 16)):
            post = day >= ACTIVATION
            valid = post and day not in (
                ACTIVATION,
                date(2012, 1, 2),
                date(2012, 1, 9),
            )
            rows.append(("MT_E", day, 8.0, 4.0, 80.0, valid, post))
        eligibility = [
            ("MT_A", ACTIVATION, 21, True, ""),
            ("MT_C", ACTIVATION, 19, True, ""),
            ("MT_D", ACTIVATION, 18, True, ""),
            ("MT_E", ACTIVATION, 18, True, ""),
        ]
        self._write_processed(rows, eligibility)

    def _build(self) -> dict:
        build_labels(
            self.processed, self.labels, self.config, project_root=Path.cwd()
        )
        return build_examples(
            self.processed,
            self.labels,
            self.output,
            self.config,
            project_root=Path.cwd(),
        )

    def _examples(self) -> dict[tuple, dict]:
        connection = duckdb.connect()
        try:
            columns = [
                row[0]
                for row in connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)",
                    [str(self.output / "modeling_examples.parquet")],
                ).fetchall()
            ]
            rows = connection.execute(
                "SELECT * FROM read_parquet(?) ORDER BY client_id, target_date",
                [str(self.output / "modeling_examples.parquet")],
            ).fetchall()
        finally:
            connection.close()
        return {
            (row[0], row[1]): dict(zip(columns, row)) for row in rows
        }

    def test_hand_calculated_features(self) -> None:
        self._default_fixture()
        self._build()
        examples = self._examples()

        row = examples[("MT_A", date(2012, 1, 16))]
        scale = 19.0
        self.assertEqual(row["forecast_date"], date(2012, 1, 15))
        self.assertEqual(row["feature_cutoff"], date(2012, 1, 15))
        self.assertEqual(row["split"], "selection")
        self.assertEqual(row["target"], 1)
        self.assertEqual(row["history_valid_days"], 14)
        self.assertEqual(row["history_first_date"], date(2012, 1, 2))
        self.assertEqual(row["history_last_date"], date(2012, 1, 15))
        self.assertAlmostEqual(row["lag1_peak_rel"], 21 / scale)
        self.assertAlmostEqual(row["lag1_mean_rel"], 10.5 / scale)
        self.assertAlmostEqual(row["lag1_energy_rel"], 210 / (scale * 24))
        self.assertEqual(row["lag1_age_days"], 1)
        self.assertEqual(row["lag1_is_fallback"], 0)
        self.assertAlmostEqual(row["lag7_peak_rel"], 15 / scale)
        self.assertAlmostEqual(row["lag7_energy_rel"], 150 / (scale * 24))
        self.assertEqual(row["lag7_age_days"], 7)
        self.assertEqual(row["lag7_is_fallback"], 0)
        self.assertAlmostEqual(row["roll3_peak_mean_rel"], 20 / scale)
        self.assertAlmostEqual(row["roll7_peak_mean_rel"], 18 / scale)
        self.assertAlmostEqual(row["roll14_peak_mean_rel"], 14.5 / scale)
        self.assertAlmostEqual(row["roll7_peak_max_rel"], 21 / scale)
        self.assertAlmostEqual(row["roll14_peak_max_rel"], 21 / scale)
        self.assertAlmostEqual(
            row["roll7_peak_std_rel"], math.sqrt(28 / 6) / scale
        )
        self.assertAlmostEqual(
            row["roll14_peak_std_rel"], math.sqrt(17.5) / scale
        )
        self.assertAlmostEqual(row["change_lag1_vs_roll7"], 3 / scale)
        self.assertAlmostEqual(row["change_lag1_vs_lag7"], 6 / scale)
        self.assertEqual(row["high_use_days_7"], 5)
        self.assertEqual(row["high_use_days_14"], 5)
        self.assertEqual(row["spike_days_14"], 2)
        self.assertEqual(row["target_iso_weekday"], 1)
        self.assertEqual(row["target_is_weekend"], 0)
        self.assertEqual(row["target_month"], 1)
        self.assertAlmostEqual(row["target_month_sin"], 0.0)
        self.assertAlmostEqual(row["target_month_cos"], 1.0)

        # Split is assigned from the target date, not the forecast date.
        self.assertEqual(row["split"], "selection")

    def test_lag_fallbacks_and_year_boundary(self) -> None:
        self._default_fixture()
        self._build()
        examples = self._examples()

        row = examples[("MT_C", date(2012, 1, 16))]
        self.assertEqual(row["history_valid_days"], 12)
        self.assertEqual(row["lag1_age_days"], 2)
        self.assertEqual(row["lag1_is_fallback"], 1)
        self.assertAlmostEqual(row["lag1_peak_rel"], 1.0)
        self.assertEqual(row["lag7_age_days"], 14)
        self.assertEqual(row["lag7_is_fallback"], 1)
        self.assertAlmostEqual(row["lag7_peak_rel"], 1.0)
        self.assertEqual(row["high_use_days_14"], 0)
        self.assertEqual(row["spike_days_14"], 0)
        self.assertEqual(row["target"], 0)
        # Zero-mean client still produces finite features.
        for name, value in row.items():
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value), name)

        # Window crossing Dec 31 -> Jan 1 computes correctly.
        boundary = examples[("MT_C", date(2012, 1, 7))]
        self.assertEqual(boundary["history_valid_days"], 12)
        self.assertEqual(boundary["lag1_age_days"], 1)
        self.assertEqual(boundary["lag7_age_days"], 7)
        self.assertEqual(boundary["history_last_date"], date(2012, 1, 6))

        # Leap day target computes a normal 14-day window.
        leap = examples[("MT_A", date(2012, 2, 29))]
        self.assertEqual(leap["history_valid_days"], 14)
        self.assertEqual(leap["lag1_age_days"], 1)
        self.assertEqual(leap["lag7_age_days"], 7)
        self.assertEqual(leap["split"], "test")

    def test_exclusions_and_counts(self) -> None:
        self._default_fixture()
        manifest = self._build()
        examples = self._examples()

        self.assertEqual(len(examples), 68)
        labeled_count = duckdb.connect().execute(
            "SELECT count(*) FROM read_parquet(?)",
            [str(self.labels / "labeled_days.parquet")],
        ).fetchone()[0]
        self.assertEqual(labeled_count, 124)
        self.assertEqual(manifest["counts"]["examples"], 68)
        self.assertEqual(manifest["counts"]["excluded"], 56)
        self.assertEqual(
            manifest["counts"]["excluded_by_reason"],
            {"insufficient_history": 55, "missing_weekly_lag": 1},
        )
        self.assertNotIn(("MT_D", date(2012, 1, 16)), examples)
        self.assertNotIn(("MT_E", date(2012, 1, 16)), examples)
        self.assertFalse(any(key[0] == "MT_D" for key in examples))

        lines = (
            (self.output / "example_exclusions.csv")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(
            lines,
            [
                "split,reason,n",
                "train,insufficient_history,54",
                "selection,insufficient_history,1",
                "selection,missing_weekly_lag,1",
            ],
        )
        self.assertEqual(
            manifest["counts"]["examples_by_split"]["train"]["n"], 22
        )
        self.assertEqual(
            manifest["counts"]["examples_by_split"]["test"]["n"], 14
        )
        self.assertTrue(manifest["lineage_check"]["passed"])

    def test_past_only_and_post_training_invariance(self) -> None:
        self._default_fixture()
        self._build()
        first = self._examples()[("MT_A", date(2012, 1, 16))]
        scaling = duckdb.connect().execute(
            "SELECT client_id, scale_kw, high_use_kw FROM read_parquet(?)",
            [str(self.output / "client_scaling.parquet")],
        ).fetchall()
        first_scaling = {row[0]: row[1:] for row in scaling}

        self._default_fixture(late_peak_scale=100.0)
        self._build()
        second = self._examples()[("MT_A", date(2012, 1, 16))]
        for name, value in first.items():
            if name != "target":
                self.assertEqual(value, second[name], name)
        scaling = duckdb.connect().execute(
            "SELECT client_id, scale_kw, high_use_kw FROM read_parquet(?)",
            [str(self.output / "client_scaling.parquet")],
        ).fetchall()
        self.assertEqual(first_scaling, {row[0]: row[1:] for row in scaling})

    def test_scaling_values(self) -> None:
        self._default_fixture()
        self._build()
        scaling = {
            row[0]: row[1:]
            for row in duckdb.connect().execute(
                "SELECT client_id, scale_kw, high_use_kw, contributing_days, "
                "last_fit_date FROM read_parquet(?) ORDER BY client_id",
                [str(self.output / "client_scaling.parquet")],
            ).fetchall()
        }
        # MT_A training peaks are 1..21: p75 k=16 -> 16, p90 k=19 -> 19.
        self.assertEqual(scaling["MT_A"], (19.0, 16.0, 21, date(2012, 1, 15)))
        self.assertEqual(scaling["MT_C"], (8.0, 8.0, 19, date(2012, 1, 14)))
        self.assertEqual(scaling["MT_E"], (8.0, 8.0, 18, date(2012, 1, 15)))

    def test_registry_and_determinism(self) -> None:
        self._default_fixture()
        self._build()
        registry_names = [
            line.split(",")[0]
            for line in (self.output / "feature_registry.csv")
            .read_text(encoding="utf-8")
            .splitlines()[1:]
        ]
        connection = duckdb.connect()
        try:
            parquet_columns = [
                row[0]
                for row in connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)",
                    [str(self.output / "modeling_examples.parquet")],
                ).fetchall()
            ]
        finally:
            connection.close()
        key_columns = {
            "client_id", "target_date", "forecast_date", "feature_cutoff",
            "split", "target", "history_first_date", "history_last_date",
        }
        self.assertEqual(
            registry_names,
            [name for name in parquet_columns if name not in key_columns],
        )
        first_hash = hashlib.sha256(
            (self.output / "modeling_examples.parquet").read_bytes()
        ).hexdigest()

        self._build()
        second_hash = hashlib.sha256(
            (self.output / "modeling_examples.parquet").read_bytes()
        ).hexdigest()
        self.assertEqual(first_hash, second_hash)


if __name__ == "__main__":
    unittest.main()
