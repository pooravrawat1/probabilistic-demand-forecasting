from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
import zipfile

from demand_forecasting.audit import activation_day, assigned_day, audit_source, DayRecord, dst_transition
from demand_forecasting.download import download_source


class AuditRulesTest(unittest.TestCase):
    def test_midnight_closes_prior_day_and_dst_dates(self) -> None:
        self.assertEqual(assigned_day(datetime(2014, 1, 2, 0, 0)), date(2014, 1, 1))
        self.assertEqual(assigned_day(datetime(2014, 1, 2, 0, 15)), date(2014, 1, 2))
        self.assertEqual(dst_transition(date(2014, 3, 30)), "spring")
        self.assertEqual(dst_transition(date(2014, 10, 26)), "fall")
        self.assertIsNone(dst_transition(date(2014, 3, 29)))

    def test_activation_requires_a_sustained_valid_window(self) -> None:
        start = date(2013, 1, 1)
        records = [DayRecord(start + timedelta(days=i), True, True, i < 9, 1.0)
                   for i in range(14)]
        self.assertIsNone(activation_day(records, 14, 10, date(2013, 12, 31)))
        records[-1].positive = True
        self.assertEqual(activation_day(records, 14, 10, date(2013, 12, 31)), start)
        records[7].model_valid = False
        self.assertIsNone(activation_day(records, 14, 10, date(2013, 12, 31)))

    def test_audit_keeps_dst_and_invalid_values_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle, delimiter=";")
                writer.writerow(["", "MT_001", "MT_002"])
                for day in (date(2014, 3, 29), date(2014, 3, 30), date(2014, 3, 31)):
                    for interval in range(1, 97):
                        stamp = datetime.combine(day, datetime.min.time()) + timedelta(minutes=15 * interval)
                        second = "-1" if day == date(2014, 3, 30) and interval == 1 else "2"
                        writer.writerow([stamp.isoformat(sep=" "), "1,5", second])
            config = {
                "run": {"random_seed": 42},
                "source": {"member": "LD2011_2014.txt"},
                "audit": {"interval_minutes": 15, "expected_intervals_per_day": 96,
                          "activation_window_days": 1, "activation_positive_days": 1,
                          "exclude_dst_transition_days": True,
                          "extreme_median_multiplier": 10, "extreme_p99_multiplier": 5},
                "study": {"training_start": "2014-03-29", "training_end": "2014-03-31",
                          "minimum_valid_training_days": 2},
            }
            summary = audit_source(source, root / "audit", config)
            self.assertEqual(summary["source"]["timestamp_rows"], 288)
            self.assertEqual(summary["source"]["last_assigned_day"], "2014-03-31")
            self.assertEqual(summary["source"]["negative_values"], 1)
            self.assertEqual(summary["source"]["model_valid_client_days"], 4)
            self.assertEqual(summary["eligibility"]["eligible_clients"], 2)
            with (root / "audit/day_quality.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            dst_rows = [row for row in rows if row["local_date"] == "2014-03-30"]
            self.assertEqual(len(dst_rows), 2)
            self.assertTrue(all(row["model_valid"] == "0" for row in dst_rows))
            self.assertIn("negative_value", dst_rows[1]["flags"])

    def test_download_preserves_source_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.zip"
            with zipfile.ZipFile(original, "w") as archive:
                archive.writestr("LD2011_2014.txt", "source bytes\n")
            destination = root / "raw" / "source.zip"
            first = download_source(original.as_uri(), destination)
            second = download_source(original.as_uri(), destination)
            self.assertEqual(destination.read_bytes(), original.read_bytes())
            self.assertFalse(first["already_present"])
            self.assertTrue(second["already_present"])
            self.assertEqual(first["sha256"], second["sha256"])


if __name__ == "__main__":
    unittest.main()
