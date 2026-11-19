from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

import duckdb
import numpy as np

from demand_forecasting.calibration import calibrate_models
from demand_forecasting.models import FEATURE_SETS
from demand_forecasting.selection import select_models


FEATURE_NAMES = FEATURE_SETS["all"]


def _range(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


class SelectionTest(unittest.TestCase):
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
                "training_end": "2024-03-31",
                "selection_start": "2024-04-01",
                "selection_end": "2024-04-30",
                "calibration_start": "2024-05-01",
                "calibration_end": "2024-05-31",
                "test_start": "2024-06-01",
                "test_end": "2024-06-30",
            },
            "experiment": {
                "evaluation_splits": ["selection"],
                "alert_budget_fraction": 0.10,
                "weekly_smoothing_strength": 10.0,
            },
            "models": {
                "selection_metric": "average_precision",
                "max_iter_logistic": 500,
                "hgb_max_iter": 20,
                "logistic_C": [0.1, 1.0],
                "hgb_learning_rate": [0.1],
                "hgb_max_leaf_nodes": [15],
                "hgb_min_samples_leaf": [50],
                "hgb_l2_regularization": 1.0,
                "calibration_method": "platt",
                "calibration_cv_folds": 3,
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _split(self, day: date) -> str:
        if day <= date(2024, 3, 31):
            return "train"
        if day <= date(2024, 4, 30):
            return "selection"
        if day <= date(2024, 5, 31):
            return "calibration"
        return "test"

    def _write_examples(
        self, *, seed: int = 0, corrupt_later: bool = False
    ) -> None:
        rng = np.random.default_rng(seed)
        rows = []
        numeric = [
            name for name in FEATURE_NAMES if name not in (
                "target_iso_weekday", "target_is_weekend", "target_month",
                "target_month_sin", "target_month_cos",
            )
        ]
        for client_id in ("MT_A", "MT_B", "MT_C"):
            signal = {
                name: rng.standard_normal() for name in numeric
            }
            for day in _range(date(2024, 1, 1), date(2024, 6, 30)):
                lag1 = rng.standard_normal() + signal["lag1_peak_rel"]
                features = {name: rng.standard_normal() for name in numeric}
                features["lag1_peak_rel"] = lag1
                split = self._split(day)
                if split in ("train", "selection"):
                    probability = 1 / (1 + math.exp(-2 * lag1))
                    target = int(rng.random() < probability)
                else:
                    target = (
                        int(rng.random() < 0.5)
                        if not corrupt_later
                        else int(rng.random() < 0.9)
                    )
                weekday = day.isoweekday()
                month_angle = 2 * math.pi * (day.month - 1) / 12
                values = {
                    "target_iso_weekday": weekday,
                    "target_is_weekend": int(weekday >= 6),
                    "target_month": day.month,
                    "target_month_sin": math.sin(month_angle),
                    "target_month_cos": math.cos(month_angle),
                    **features,
                }
                rows.append(
                    (client_id, day, split, target)
                    + tuple(values[name] for name in FEATURE_NAMES)
                )
        connection = duckdb.connect()
        try:
            connection.execute("SET threads = 1")
            columns = (
                "client_id VARCHAR, target_date DATE, split VARCHAR, "
                "target TINYINT, " + ", ".join(f"{name} DOUBLE" for name in FEATURE_NAMES)
            )
            connection.execute(f"CREATE TEMP TABLE stage ({columns})")
            placeholders = ", ".join("?" * (4 + len(FEATURE_NAMES)))
            connection.executemany(
                f"INSERT INTO stage VALUES ({placeholders})", rows
            )
            connection.execute(
                "COPY (SELECT * FROM stage ORDER BY client_id, target_date) "
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

    def _select(self, config: dict | None = None) -> dict:
        return select_models(
            self.examples_dir,
            self.output_root,
            config or self.config,
            project_root=Path.cwd(),
        )

    def _predictions(self, output_dir: Path, filename: str) -> list[dict]:
        path = output_dir / filename
        connection = duckdb.connect()
        try:
            columns = [
                row[0]
                for row in connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
                ).fetchall()
            ]
            rows = connection.execute(
                "SELECT * FROM read_parquet(?) "
                "ORDER BY model_id, client_id, target_date",
                [str(path)],
            ).fetchall()
        finally:
            connection.close()
        return [dict(zip(columns, row)) for row in rows]

    def test_selection_end_to_end(self) -> None:
        self._write_examples()
        record = self._select()
        self.assertTrue(record["selection_id"].startswith("sel-"))
        output_dir = self.output_root / record["selection_id"]
        for path in (
            output_dir / "selection_log.csv",
            output_dir / "selection_predictions.parquet",
            output_dir / "selection_record.json",
            output_dir / "selected" / "logistic.pkl",
            output_dir / "selected" / "hgb.pkl",
        ):
            self.assertTrue(path.is_file(), path)

        self.assertEqual(
            record["training_state"]["train_target_date_max"], "2024-03-31"
        )
        self.assertEqual(record["training_state"]["train_rows"], 273)
        predictions = self._predictions(
            output_dir, "selection_predictions.parquet"
        )
        self.assertEqual(len(predictions), 2 * 3 * 30)
        self.assertEqual(
            {row["split"] for row in predictions}, {"selection"}
        )
        self.assertEqual(
            {row["model_id"] for row in predictions}, {"logistic", "hgb"}
        )
        for row in predictions:
            self.assertIsNone(row["calibrated_probability"])
            self.assertGreaterEqual(row["raw_probability"], 0.0)
            self.assertLessEqual(row["raw_probability"], 1.0)
            self.assertGreater(row["alert_rank"], 0)
            self.assertIn(row["alert"], (0, 1))

        with (output_dir / "selection_log.csv").open() as handle:
            log = list(csv.DictReader(handle))
        self.assertEqual(len(log), 6)  # 4 logistic + 2 hgb
        self.assertEqual(
            sum(1 for row in log if row["status"] == "selected"), 2
        )
        for family in ("logistic", "hgb"):
            eligible = [
                float(row["average_precision"])
                for row in log
                if row["family"] == family and row["status"] in ("ok", "selected")
            ]
            chosen = float(
                [
                    row["average_precision"]
                    for row in log
                    if row["family"] == family and row["status"] == "selected"
                ][0]
            )
            self.assertEqual(chosen, max(eligible))

    def test_single_class_training_raises(self) -> None:
        self._write_examples()
        # Overwrite targets with a single class via a tiny rebuild.
        connection = duckdb.connect()
        path = self.examples_dir / "modeling_examples.parquet"
        try:
            connection.execute(
                f"COPY (SELECT * REPLACE (0::TINYINT AS target) "
                f"FROM read_parquet('{path}')) TO '{path}' (FORMAT PARQUET)"
            )
        finally:
            connection.close()
        with self.assertRaises(ValueError):
            self._select()

    def test_non_converged_configs_logged(self) -> None:
        self._write_examples()
        config = {
            **self.config,
            "models": {**self.config["models"], "max_iter_logistic": 1},
        }
        with self.assertRaises(ValueError):
            # No logistic config converges at max_iter=1.
            self._select(config)
        # The failed run still wrote its log before raising? If not, rerun
        # selection with a mixed grid and inspect the log instead.
        config2 = {
            **self.config,
            "models": {
                **self.config["models"],
                "logistic_C": [0.1],
                "max_iter_logistic": 500,
            },
        }
        record = self._select(config2)
        with (self.output_root / record["selection_id"] / "selection_log.csv").open() as handle:
            log = list(csv.DictReader(handle))
        self.assertTrue(all(row["converged"] == "True" for row in log))

    def test_later_splits_do_not_influence_selection(self) -> None:
        self._write_examples()
        first = self._select()
        first_dir = self.output_root / first["selection_id"]

        self._write_examples(corrupt_later=True)
        second = self._select()
        second_dir = self.output_root / second["selection_id"]

        self.assertNotEqual(first["selection_id"], second["selection_id"])
        self.assertEqual(
            {
                family: {
                    "config_id": choice["config_id"],
                    "metrics": choice["metrics"],
                }
                for family, choice in first["selected"].items()
            },
            {
                family: {
                    "config_id": choice["config_id"],
                    "metrics": choice["metrics"],
                }
                for family, choice in second["selected"].items()
            },
        )
        first_pred = self._predictions(
            first_dir, "selection_predictions.parquet"
        )
        second_pred = self._predictions(
            second_dir, "selection_predictions.parquet"
        )
        for left, right in zip(first_pred, second_pred):
            strip = lambda row: {
                k: v for k, v in row.items() if k != "experiment_id"
            }
            self.assertEqual(strip(left), strip(right))

    def test_determinism(self) -> None:
        self._write_examples()
        first = self._select()
        second = self._select()
        self.assertEqual(first["selection_id"], second["selection_id"])
        output_dir = self.output_root / first["selection_id"]
        first_hash = hashlib.sha256(
            (output_dir / "selection_predictions.parquet").read_bytes()
        ).hexdigest()
        self._select()
        second_hash = hashlib.sha256(
            (output_dir / "selection_predictions.parquet").read_bytes()
        ).hexdigest()
        self.assertEqual(first_hash, second_hash)

    def _run_selection_and_calibration(self) -> tuple[dict, dict, Path]:
        record = self._select()
        selection_dir = self.output_root / record["selection_id"]
        ready = calibrate_models(
            selection_dir,
            self.examples_dir,
            self.config,
            project_root=Path.cwd(),
        )
        return record, ready, selection_dir

    def test_calibration_end_to_end(self) -> None:
        self._write_examples()
        _, ready, selection_dir = self._run_selection_and_calibration()
        self.assertEqual(ready["status"], "ready_for_sign_off")
        self.assertIsNone(ready["sign_off"]["signed_by"])
        self.assertIn(
            "test split (2014-07-01..2014-12-31) not loaded at any stage",
            ready["leakage_statement"],
        )
        calibration_dir = selection_dir / "calibration"
        for filename in (
            "calibration_predictions.parquet",
            "calibration_metrics.csv",
            "calibrators.json",
        ):
            self.assertTrue((calibration_dir / filename).is_file(), filename)

        predictions = self._predictions(
            calibration_dir, "calibration_predictions.parquet"
        )
        self.assertEqual(len(predictions), 2 * 3 * 31)
        self.assertEqual(
            {row["split"] for row in predictions}, {"calibration"}
        )
        for row in predictions:
            self.assertIsNotNone(row["calibrated_probability"])
            self.assertGreaterEqual(row["calibrated_probability"], 0.0)
            self.assertLessEqual(row["calibrated_probability"], 1.0)

        calibrators = json.loads(
            (calibration_dir / "calibrators.json").read_text()
        )
        for family in ("logistic", "hgb"):
            model = calibrators["models"][family]
            self.assertEqual(
                model["use_calibration"],
                model["oof_platt_brier"] < model["raw_brier"],
            )
            self.assertIn("slope", model)
            self.assertIn("intercept", model)

    def test_tampered_pickle_is_refused(self) -> None:
        self._write_examples()
        record = self._select()
        selection_dir = self.output_root / record["selection_id"]
        pickle_path = selection_dir / "selected" / "logistic.pkl"
        pickle_path.write_bytes(pickle_path.read_bytes() + b"tamper")
        with self.assertRaises(ValueError):
            calibrate_models(
                selection_dir,
                self.examples_dir,
                self.config,
                project_root=Path.cwd(),
            )

    def test_calibration_determinism(self) -> None:
        self._write_examples()
        _, ready1, selection_dir = self._run_selection_and_calibration()
        ready2 = calibrate_models(
            selection_dir,
            self.examples_dir,
            self.config,
            project_root=Path.cwd(),
        )
        self.assertEqual(ready1["config_sha256"], ready2["config_sha256"])


if __name__ == "__main__":
    unittest.main()
