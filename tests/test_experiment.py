from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from demand_forecasting.experiment import assert_identical_keys, run_baselines


def _range(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


class ExperimentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.examples_dir = self.root / "examples"
        self.examples_dir.mkdir()
        self.output_root = self.root / "experiments"
        self.config = {
            "run": {"random_seed": 42},
            "study": {
                "training_start": "2024-01-01",
                "training_end": "2024-01-10",
                "selection_start": "2024-01-11",
                "selection_end": "2024-01-14",
                "calibration_start": "2024-01-15",
                "calibration_end": "2024-01-15",
                "test_start": "2024-01-16",
                "test_end": "2024-01-16",
            },
            "experiment": {
                "evaluation_splits": ["selection"],
                "alert_budget_fraction": 0.10,
                "weekly_smoothing_strength": 10.0,
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_examples(self, *, test_target: int = 1) -> None:
        rows = []
        train_targets = {"MT_A": [1, 0, 1, 0, 0, 1, 0, 0, 1, 0],
                         "MT_B": [0] * 10}
        selection_targets = {"MT_A": [1, 0, 1, 0], "MT_B": [0, 1, 0, 0]}
        for client_id, targets in train_targets.items():
            for index, target in enumerate(targets):
                day = date(2024, 1, 1) + timedelta(days=index)
                rows.append(
                    (client_id, day, "train", target, day.isoweekday())
                )
        for client_id, targets in selection_targets.items():
            for index, target in enumerate(targets):
                day = date(2024, 1, 11) + timedelta(days=index)
                rows.append(
                    (client_id, day, "selection", target, day.isoweekday())
                )
        for client_id in ("MT_A", "MT_B"):
            for day in _range(date(2024, 1, 16), date(2024, 1, 16)):
                rows.append(
                    (client_id, day, "test", test_target, day.isoweekday())
                )
        connection = duckdb.connect()
        try:
            connection.execute("SET threads = 1")
            connection.execute(
                "CREATE TEMP TABLE stage ("
                "client_id VARCHAR, target_date DATE, split VARCHAR, "
                "target TINYINT, target_iso_weekday INTEGER)"
            )
            connection.executemany(
                "INSERT INTO stage VALUES (?, ?, ?, ?, ?)", rows
            )
            connection.execute(
                f"COPY (SELECT * FROM stage ORDER BY client_id, target_date) "
                f"TO '{self.examples_dir / 'modeling_examples.parquet'}' "
                "(FORMAT PARQUET)"
            )
        finally:
            connection.close()
        (self.examples_dir / "examples_manifest.json").write_text(
            json.dumps(
                {
                    "policy": "test policy text",
                    "inputs": {
                        "source_sha256": "fakesource",
                        "client_day_parquet": {"sha256": "fake"},
                    },
                }
            ),
            encoding="utf-8",
        )

    def _predictions(self, output_dir: Path) -> list[dict]:
        connection = duckdb.connect()
        try:
            columns = [
                row[0]
                for row in connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)",
                    [str(output_dir / "predictions.parquet")],
                ).fetchall()
            ]
            rows = connection.execute(
                "SELECT * FROM read_parquet(?) "
                "ORDER BY model_id, client_id, target_date",
                [str(output_dir / "predictions.parquet")],
            ).fetchall()
        finally:
            connection.close()
        return [dict(zip(columns, row)) for row in rows]

    def test_run_baselines_end_to_end(self) -> None:
        self._write_examples()
        record = run_baselines(
            self.examples_dir, self.output_root, self.config,
            project_root=Path.cwd(),
        )
        self.assertTrue(record["experiment_id"].startswith("exp-"))
        output_dir = self.output_root / record["experiment_id"]
        for filename in (
            "predictions.parquet",
            "metrics.json",
            "metrics.csv",
            "experiment_record.json",
        ):
            self.assertTrue((output_dir / filename).is_file(), filename)

        predictions = self._predictions(output_dir)
        self.assertEqual(len(predictions), 16)
        self.assertEqual({row["split"] for row in predictions}, {"selection"})
        keys = {
            (row["model_id"], row["client_id"], row["target_date"], row["target"])
            for row in predictions
        }
        client_keys = {
            (client, day, target)
            for model, client, day, target in keys
        }
        self.assertEqual(len(client_keys), 8)
        for row in predictions:
            self.assertIsNone(row["calibrated_probability"])
            self.assertGreaterEqual(row["raw_probability"], 0.0)
            self.assertLessEqual(row["raw_probability"], 1.0)
            self.assertGreaterEqual(row["alert_rank"], 1)
            self.assertIn(row["alert"], (0, 1))

        # client_prevalence probabilities equal the training positive rates.
        client_prev = {
            row["client_id"]: row["raw_probability"]
            for row in predictions
            if row["model_id"] == "client_prevalence"
        }
        self.assertEqual(client_prev["MT_A"], 0.4)
        self.assertEqual(client_prev["MT_B"], 0.0)

        metrics = {row["model_id"]: row for row in record["metrics"]}
        self.assertEqual(metrics["client_prevalence"]["n"], 8)
        self.assertEqual(metrics["client_prevalence"]["positives"], 3)
        self.assertEqual(metrics["client_prevalence"]["alert_count"], 1)
        self.assertEqual(
            metrics["client_prevalence"]["tied_at_cutoff_total"], 4
        )
        self.assertEqual(
            metrics["client_prevalence"]["tied_at_cutoff_alerted"], 1
        )

    def test_test_targets_never_influence_outputs(self) -> None:
        self._write_examples(test_target=1)
        first = run_baselines(
            self.examples_dir, self.output_root, self.config,
            project_root=Path.cwd(),
        )
        self._write_examples(test_target=0)
        second = run_baselines(
            self.examples_dir, self.output_root, self.config,
            project_root=Path.cwd(),
        )
        # experiment_id includes the examples hash, which changes; the
        # predictions, metrics, and alert decisions must not.
        self.assertNotEqual(first["experiment_id"], second["experiment_id"])
        self.assertEqual(first["metrics"], second["metrics"])
        first_predictions = self._predictions(
            self.output_root / first["experiment_id"]
        )
        second_predictions = self._predictions(
            self.output_root / second["experiment_id"]
        )
        for left, right in zip(first_predictions, second_predictions):
            left = {k: v for k, v in left.items() if k != "experiment_id"}
            right = {k: v for k, v in right.items() if k != "experiment_id"}
            self.assertEqual(left, right)

    def test_test_split_is_rejected(self) -> None:
        self._write_examples()
        config = {
            **self.config,
            "experiment": {
                **self.config["experiment"],
                "evaluation_splits": ["selection", "test"],
            },
        }
        with self.assertRaises(ValueError):
            run_baselines(
                self.examples_dir, self.output_root, config,
                project_root=Path.cwd(),
            )

    def test_assert_identical_keys(self) -> None:
        base = {
            "client_ids": ["A", "B"],
            "target_dates": [date(2024, 1, 1), date(2024, 1, 2)],
            "splits": ["selection", "selection"],
            "targets": [0, 1],
        }
        assert_identical_keys({"m1": dict(base), "m2": dict(base)})
        with self.assertRaises(ValueError):
            assert_identical_keys(
                {
                    "m1": dict(base),
                    "m2": {
                        **base,
                        "client_ids": ["A", "C"],
                    },
                }
            )
        with self.assertRaises(ValueError):
            assert_identical_keys(
                {"m1": dict(base), "m2": {**base, "targets": [1, 1]}}
            )

    def test_deterministic_experiment(self) -> None:
        self._write_examples()
        first = run_baselines(
            self.examples_dir, self.output_root, self.config,
            project_root=Path.cwd(),
        )
        second = run_baselines(
            self.examples_dir, self.output_root, self.config,
            project_root=Path.cwd(),
        )
        self.assertEqual(first["experiment_id"], second["experiment_id"])
        output_dir = self.output_root / first["experiment_id"]
        first_hash = hashlib.sha256(
            (output_dir / "predictions.parquet").read_bytes()
        ).hexdigest()
        run_baselines(
            self.examples_dir, self.output_root, self.config,
            project_root=Path.cwd(),
        )
        second_hash = hashlib.sha256(
            (output_dir / "predictions.parquet").read_bytes()
        ).hexdigest()
        self.assertEqual(first_hash, second_hash)


if __name__ == "__main__":
    unittest.main()
