from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from demand_forecasting.audit import audit_source
from demand_forecasting.build import build_data


class DataBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.txt"
        self.config = {
            "run": {"random_seed": 42},
            "source": {"member": "LD2011_2014.txt"},
            "audit": {
                "interval_minutes": 360,
                "expected_intervals_per_day": 4,
                "activation_window_days": 2,
                "activation_positive_days": 2,
                "exclude_dst_transition_days": True,
                "extreme_median_multiplier": 10,
                "extreme_p99_multiplier": 5,
            },
            "study": {
                "training_start": "2014-03-10",
                "training_end": "2014-03-31",
                "minimum_valid_training_days": 5,
            },
        }
        with self.source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(["", "MT_A", "MT_B", "MT_C"])
            for offset in range(21):
                day = date(2014, 3, 10) + timedelta(days=offset)
                for interval in range(1, 5):
                    timestamp = datetime.combine(day, datetime.min.time()) + timedelta(
                        hours=6 * interval
                    )
                    invalid_values = {
                        date(2014, 3, 15): "-1",
                        date(2014, 3, 16): "",
                        date(2014, 3, 17): "not-a-number",
                        date(2014, 3, 18): "NaN",
                    }
                    value_b = (
                        invalid_values[day]
                        if day in invalid_values and interval == 1
                        else "1,5"
                    )
                    writer.writerow(
                        [timestamp.isoformat(sep=" "), "2,0", value_b, "0"]
                    )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build(self) -> tuple[dict, Path]:
        audit_dir = self.root / "audit"
        output_dir = self.root / "processed"
        audit_source(self.source, audit_dir, self.config)
        manifest = build_data(
            self.source,
            output_dir,
            self.config,
            audit_dir=audit_dir,
            project_root=Path.cwd(),
        )
        return manifest, output_dir

    def test_builds_tables_and_reconciles_with_audit(self) -> None:
        manifest, output_dir = self._build()

        self.assertEqual(manifest["audit_reconciliation"]["status"], "matched")
        self.assertEqual(manifest["counts"]["interval_rows"], 252)
        self.assertEqual(manifest["counts"]["client_day_rows"], 63)
        self.assertEqual(manifest["counts"]["source_valid_client_days"], 59)
        self.assertEqual(manifest["counts"]["model_valid_client_days"], 56)
        self.assertEqual(manifest["counts"]["eligible_clients"], 2)
        self.assertEqual(manifest["counts"]["invalid_numeric_interval_values"], 4)
        self.assertEqual(manifest["counts"]["duplicate_interval_rows"], 0)
        self.assertEqual(
            manifest["exclusion_counts"],
            {"eligible": 2, "no_sustained_activation_by_training_end": 1},
        )
        for filename in (
            "interval_readings.parquet",
            "client_day.parquet",
            "client_eligibility.parquet",
            "catalog.duckdb",
            "manifest.json",
        ):
            self.assertTrue((output_dir / filename).is_file(), filename)

        saved_manifest = json.loads(
            (output_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved_manifest["status"], "complete")
        self.assertIn("schema", saved_manifest["outputs"]["client_day"])
        self.assertIn("builder_sha256", saved_manifest["run"])
        self.assertIn("git_dirty", saved_manifest["run"])

        first_hashes = {
            name: output["sha256"]
            for name, output in saved_manifest["outputs"].items()
        }
        second_manifest = build_data(
            self.source,
            output_dir,
            self.config,
            audit_dir=self.root / "audit",
            project_root=Path.cwd(),
        )
        self.assertEqual(
            first_hashes,
            {
                name: output["sha256"]
                for name, output in second_manifest["outputs"].items()
            },
        )

    def test_aggregates_units_flags_validity_and_activation(self) -> None:
        _, output_dir = self._build()
        connection = duckdb.connect()
        try:
            normal_day = connection.execute(
                """
                SELECT daily_peak_kw, mean_kw, energy_kwh_est, source_valid,
                       model_valid, activation_date, post_activation,
                       client_eligible
                FROM read_parquet(?)
                WHERE client_id = 'MT_A' AND local_date = DATE '2014-03-10'
                """,
                [str(output_dir / "client_day.parquet")],
            ).fetchone()
            self.assertEqual(normal_day, (2.0, 2.0, 48.0, True, True,
                                          date(2014, 3, 10), True, True))

            invalid_days = connection.execute(
                """
                SELECT local_date, valid_values, source_valid, model_valid,
                       quality_flags
                FROM read_parquet(?)
                WHERE client_id = 'MT_B'
                    AND local_date BETWEEN DATE '2014-03-15' AND DATE '2014-03-18'
                ORDER BY local_date
                """,
                [str(output_dir / "client_day.parquet")],
            ).fetchall()
            self.assertEqual(
                [row[1:4] for row in invalid_days],
                [(3, False, False)] * 4,
            )
            self.assertEqual(
                [row[4] for row in invalid_days],
                [
                    "negative_value",
                    "missing_value",
                    "nonnumeric_value",
                    "nonfinite_value",
                ],
            )

            dst_rows = connection.execute(
                """
                SELECT count(*), count(*) FILTER (WHERE model_valid)
                FROM read_parquet(?)
                WHERE local_date = DATE '2014-03-30'
                """,
                [str(output_dir / "client_day.parquet")],
            ).fetchone()
            self.assertEqual(dst_rows, (3, 0))

            eligibility = connection.execute(
                """
                SELECT client_id, eligible, exclusion_reason
                FROM read_parquet(?) ORDER BY client_id
                """,
                [str(output_dir / "client_eligibility.parquet")],
            ).fetchall()
            self.assertEqual(
                eligibility,
                [
                    ("MT_A", True, ""),
                    ("MT_B", True, ""),
                    ("MT_C", False, "no_sustained_activation_by_training_end"),
                ],
            )
        finally:
            connection.close()

    def test_catalog_exposes_all_tables_and_keys_are_unique(self) -> None:
        _, output_dir = self._build()
        connection = duckdb.connect(str(output_dir / "catalog.duckdb"), read_only=True)
        try:
            views = {
                row[0]
                for row in connection.execute("SHOW TABLES").fetchall()
            }
            self.assertEqual(
                views,
                {"interval_readings", "client_day", "client_eligibility"},
            )
            duplicate_intervals = connection.execute(
                """
                SELECT count(*) FROM (
                    SELECT client_id, source_timestamp, count(*) AS n
                    FROM interval_readings
                    GROUP BY client_id, source_timestamp
                    HAVING n != 1
                )
                """
            ).fetchone()[0]
            duplicate_days = connection.execute(
                """
                SELECT count(*) FROM (
                    SELECT client_id, local_date, count(*) AS n
                    FROM client_day
                    GROUP BY client_id, local_date
                    HAVING n != 1
                )
                """
            ).fetchone()[0]
            self.assertEqual(duplicate_intervals, 0)
            self.assertEqual(duplicate_days, 0)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
