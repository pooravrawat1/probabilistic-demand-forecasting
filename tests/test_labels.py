from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from demand_forecasting.labels import build_labels, percentile_nearest_rank


def _range(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


class LabelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.processed = self.root / "processed"
        self.processed.mkdir()
        self.output = self.root / "labels"
        self.config = {
            "run": {"random_seed": 42},
            "study": {
                "training_start": "2014-01-01",
                "training_end": "2014-01-10",
                "minimum_valid_training_days": 5,
                "selection_start": "2014-01-11",
                "selection_end": "2014-01-13",
                "calibration_start": "2014-01-14",
                "calibration_end": "2014-01-16",
                "test_start": "2014-01-17",
                "test_end": "2014-01-19",
            },
            "labels": {
                "threshold_estimator": "nearest_rank",
                "threshold_percentile": 0.9,
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
                "model_valid BOOLEAN, post_activation BOOLEAN)"
            )
            connection.executemany(
                "INSERT INTO client_day_stage VALUES (?, ?, ?, ?, ?)",
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

    def _default_fixture(self, *, post_training_peak: float = 120.0) -> None:
        rows = [("MT_A", date(2013, 12, 31), 50.0, True, True)]
        training_peaks = {
            date(2014, 1, 1): 10.0,
            date(2014, 1, 2): 20.0,
            date(2014, 1, 4): 40.0,
            date(2014, 1, 5): 50.0,
            date(2014, 1, 6): 60.0,
            date(2014, 1, 7): 70.0,
            date(2014, 1, 8): 80.0,
            date(2014, 1, 9): 90.0,
            date(2014, 1, 10): 100.0,
        }
        for day in _range(date(2014, 1, 1), date(2014, 1, 20)):
            if day == date(2014, 1, 3) or day == date(2014, 1, 15):
                rows.append(("MT_A", day, 999.0, False, True))
            elif day in training_peaks:
                rows.append(("MT_A", day, training_peaks[day], True, True))
            elif day == date(2014, 1, 11):
                rows.append(("MT_A", day, 100.0, True, True))
            elif day == date(2014, 1, 12):
                rows.append(("MT_A", day, 105.0, True, True))
            else:
                rows.append(("MT_A", day, post_training_peak, True, True))
        for day in _range(date(2014, 1, 1), date(2014, 1, 19)):
            rows.append(("MT_B", day, 7.0, True, True))
        for day in _range(date(2014, 1, 1), date(2014, 1, 19)):
            rows.append(
                ("MT_C", day, 5.0, True, day >= date(2014, 1, 5))
            )
        eligibility = [
            ("MT_A", date(2014, 1, 1), 9, True, ""),
            ("MT_B", date(2014, 1, 1), 3, False, "insufficient_valid_training_days"),
            ("MT_C", date(2014, 1, 5), 6, True, ""),
        ]
        self._write_processed(rows, eligibility)

    def _labeled_rows(self) -> list[tuple]:
        connection = duckdb.connect()
        try:
            return connection.execute(
                "SELECT client_id, target_date, split, daily_peak_kw, "
                "threshold_kw, target FROM read_parquet(?) "
                "ORDER BY client_id, target_date",
                [str(self.output / "labeled_days.parquet")],
            ).fetchall()
        finally:
            connection.close()

    def _threshold_rows(self) -> dict[str, tuple]:
        connection = duckdb.connect()
        try:
            rows = connection.execute(
                "SELECT client_id, threshold_kw, estimator, percentile, "
                "rank_index, contributing_days, days_at_threshold, "
                "training_positive_days, fit_start, fit_end, first_fit_date, "
                "last_fit_date FROM read_parquet(?) ORDER BY client_id",
                [str(self.output / "client_thresholds.parquet")],
            ).fetchall()
        finally:
            connection.close()
        return {row[0]: row for row in rows}

    def test_percentile_nearest_rank(self) -> None:
        self.assertEqual(percentile_nearest_rank(list(range(1, 11)), 0.9), 9)
        self.assertEqual(percentile_nearest_rank(list(range(1, 12)), 0.9), 10)
        self.assertEqual(percentile_nearest_rank([42.0], 0.9), 42.0)
        self.assertEqual(
            percentile_nearest_rank([5.0] * 9 + [7.0], 0.9), 5.0
        )
        self.assertEqual(percentile_nearest_rank([3.0, 1.0, 2.0], 1.0), 3.0)
        for values, probability in (
            ([], 0.9),
            ([1.0], 0.0),
            ([1.0], 1.5),
            ([1.0], -0.2),
        ):
            with self.assertRaises(ValueError):
                percentile_nearest_rank(values, probability)

    def test_thresholds_and_labels(self) -> None:
        self._default_fixture()
        manifest = build_labels(
            self.processed, self.output, self.config, project_root=Path.cwd()
        )

        thresholds = self._threshold_rows()
        self.assertEqual(sorted(thresholds), ["MT_A", "MT_C"])
        mt_a = thresholds["MT_A"]
        self.assertEqual(mt_a[1], 100.0)
        self.assertEqual(mt_a[2], "nearest_rank")
        self.assertEqual(mt_a[3], 0.9)
        self.assertEqual(mt_a[4], 9)
        self.assertEqual(mt_a[5], 9)
        self.assertEqual(mt_a[6], 1)
        self.assertEqual(mt_a[7], 0)
        self.assertEqual(
            (mt_a[8], mt_a[9]), (date(2014, 1, 1), date(2014, 1, 10))
        )
        self.assertEqual(
            (mt_a[10], mt_a[11]), (date(2014, 1, 1), date(2014, 1, 10))
        )
        mt_c = thresholds["MT_C"]
        self.assertEqual((mt_c[1], mt_c[4], mt_c[5], mt_c[6]), (5.0, 6, 6, 6))
        self.assertEqual(
            (mt_c[10], mt_c[11]), (date(2014, 1, 5), date(2014, 1, 10))
        )

        labeled = self._labeled_rows()
        keyed = {(row[0], row[1]): row for row in labeled}
        self.assertEqual(len(labeled), 32)
        # Strict `>`: a peak equal to the threshold is not positive.
        self.assertEqual(keyed[("MT_A", date(2014, 1, 11))][5], 0)
        self.assertEqual(keyed[("MT_A", date(2014, 1, 12))][5], 1)
        # Invalid target days are omitted rather than labeled 0.
        self.assertNotIn(("MT_A", date(2014, 1, 15)), keyed)
        # Days outside the split window are omitted.
        self.assertNotIn(("MT_A", date(2013, 12, 31)), keyed)
        self.assertNotIn(("MT_A", date(2014, 1, 20)), keyed)
        # Ineligible clients get no labeled rows.
        self.assertFalse(any(row[0] == "MT_B" for row in labeled))
        # Split assignment follows the target date bounds.
        expected_splits = {
            date(2014, 1, 1): "train",
            date(2014, 1, 10): "train",
            date(2014, 1, 11): "selection",
            date(2014, 1, 13): "selection",
            date(2014, 1, 14): "calibration",
            date(2014, 1, 16): "calibration",
            date(2014, 1, 17): "test",
            date(2014, 1, 19): "test",
        }
        for target_date, split in expected_splits.items():
            self.assertEqual(keyed[("MT_A", target_date)][2], split)

        self.assertEqual(manifest["counts"]["threshold_clients"], 2)
        self.assertEqual(manifest["counts"]["labeled_days"], 32)
        self.assertEqual(manifest["counts"]["positives"], 7)
        self.assertEqual(manifest["counts"]["negatives"], 25)
        self.assertEqual(
            manifest["excluded_target_days"],
            {
                "not_model_valid": 2,
                "pre_activation": 4,
                "ineligible_client_days": 19,
            },
        )
        self.assertTrue(manifest["provenance"]["passed"])
        self.assertEqual(
            manifest["prevalence_by_split"]["selection"],
            {"n": 6, "positives": 2, "negatives": 4, "prevalence": 0.333333},
        )

        csv_path = self.output / "prevalence_by_split.csv"
        total_n = sum(
            int(line.split(",")[1])
            for line in csv_path.read_text(encoding="utf-8").splitlines()[1:]
        )
        self.assertEqual(total_n, 32)
        client_n = 0
        for line in (
            (self.output / "prevalence_by_client.csv")
            .read_text(encoding="utf-8")
            .splitlines()[1:]
        ):
            client_n += int(line.split(",")[2])
        self.assertEqual(client_n, 32)

    def test_post_training_data_cannot_change_thresholds(self) -> None:
        self._default_fixture()
        build_labels(
            self.processed, self.output, self.config, project_root=Path.cwd()
        )
        first = {k: v[1] for k, v in self._threshold_rows().items()}

        self._default_fixture(post_training_peak=99999.0)
        build_labels(
            self.processed, self.output, self.config, project_root=Path.cwd()
        )
        second = {k: v[1] for k, v in self._threshold_rows().items()}
        self.assertEqual(first, second)

    def test_minimum_days_and_reconciliation(self) -> None:
        rows = [
            ("MT_D", day, 1.0, True, True)
            for day in _range(date(2014, 1, 6), date(2014, 1, 10))
        ]
        eligibility = [
            ("MT_D", date(2014, 1, 6), 5, True, ""),
        ]
        low_config = {
            **self.config,
            "study": {**self.config["study"], "minimum_valid_training_days": 6},
        }
        self._write_processed(rows, eligibility)
        with self.assertRaises(ValueError):
            build_labels(
                self.processed, self.output, low_config, project_root=Path.cwd()
            )

        self._write_processed(
            rows,
            [("MT_D", date(2014, 1, 6), 7, True, "")],
        )
        with self.assertRaises(ValueError):
            build_labels(
                self.processed, self.output, self.config, project_root=Path.cwd()
            )

    def test_deterministic_outputs(self) -> None:
        self._default_fixture()
        build_labels(
            self.processed, self.output, self.config, project_root=Path.cwd()
        )
        first_hash = hashlib.sha256(
            (self.output / "labeled_days.parquet").read_bytes()
        ).hexdigest()
        first_thresholds = {
            key: row[:12] for key, row in self._threshold_rows().items()
        }

        build_labels(
            self.processed, self.output, self.config, project_root=Path.cwd()
        )
        second_hash = hashlib.sha256(
            (self.output / "labeled_days.parquet").read_bytes()
        ).hexdigest()
        second_thresholds = {
            key: row[:12] for key, row in self._threshold_rows().items()
        }
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first_thresholds, second_thresholds)


if __name__ == "__main__":
    unittest.main()
