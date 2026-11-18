from __future__ import annotations

import math
from datetime import date, timedelta
import unittest

import numpy as np

from demand_forecasting.metrics import alert_ranking, evaluate


def _dates(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


class MetricsTest(unittest.TestCase):
    def test_hand_calculated_metrics(self) -> None:
        probabilities = [0.95, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]
        targets = [1, 0, 1, 0, 0, 1, 0, 1, 0, 0]
        clients = [f"MT_{i:03d}" for i in range(10)]
        dates = _dates(date(2024, 1, 1), 10)

        result = evaluate(targets, probabilities, clients, dates, 0.10)
        self.assertEqual(result["n"], 10)
        self.assertEqual(result["positives"], 4)
        self.assertEqual(result["prevalence"], 0.4)
        # AP = .25*1 + .25*(2/3) + .25*.5 + .25*.5
        self.assertAlmostEqual(result["average_precision"], 2 / 3)
        self.assertAlmostEqual(result["brier"], 0.26525)
        self.assertEqual(result["alert_count"], 1)
        self.assertEqual(result["alert_true_positives"], 1)
        self.assertEqual(result["alert_precision"], 1.0)
        self.assertEqual(result["alert_recall"], 0.25)
        self.assertEqual(result["cutoff_probability"], 0.95)
        self.assertEqual(result["tied_at_cutoff_total"], 1)
        self.assertEqual(result["tied_at_cutoff_alerted"], 1)

    def test_alert_count_uses_exact_ceiling(self) -> None:
        n = 11
        probabilities = [0.5] * n
        targets = [1] + [0] * (n - 1)
        clients = ["A"] * n
        dates = _dates(date(2024, 1, 1), n)
        result = evaluate(targets, probabilities, clients, dates, 0.10)
        self.assertEqual(result["alert_count"], 2)
        # All rows are tied at the cutoff.
        self.assertEqual(result["tied_at_cutoff_total"], 11)
        self.assertEqual(result["tied_at_cutoff_alerted"], 2)

    def test_tie_break_order_and_cutoff_ties(self) -> None:
        probabilities = [0.9, 0.5, 0.5, 0.5, 0.1]
        targets = [1, 1, 0, 0, 0]
        clients = ["C", "B", "A", "D", "E"]
        dates = [
            date(2024, 1, 1),
            date(2024, 1, 3),
            date(2024, 1, 2),
            date(2024, 1, 1),
            date(2024, 1, 1),
        ]
        ranks = alert_ranking(probabilities, clients, dates)
        # 0.9 first; 0.5 ties ordered by client then date: (A,1/2),(B,1/3),(D,1/1).
        self.assertEqual(list(ranks), [1, 3, 2, 4, 5])

        result = evaluate(targets, probabilities, clients, dates, 0.40)
        self.assertEqual(result["alert_count"], 2)
        self.assertEqual(result["cutoff_probability"], 0.5)
        self.assertEqual(result["tied_at_cutoff_total"], 3)
        self.assertEqual(result["tied_at_cutoff_alerted"], 1)
        self.assertEqual(result["alert_true_positives"], 1)
        self.assertEqual(result["alert_precision"], 0.5)
        self.assertEqual(result["alert_recall"], 0.5)

    def test_ranking_is_permutation_invariant(self) -> None:
        probabilities = [0.5, 0.9, 0.5, 0.3]
        clients = ["B", "A", "A", "C"]
        dates = [
            date(2024, 1, 1),
            date(2024, 1, 2),
            date(2024, 1, 1),
            date(2024, 1, 1),
        ]
        base = alert_ranking(probabilities, clients, dates)
        keyed = {
            (c, d): int(r)
            for c, d, r in zip(clients, dates, base)
        }
        order = [2, 0, 3, 1]
        shuffled = alert_ranking(
            [probabilities[i] for i in order],
            [clients[i] for i in order],
            [dates[i] for i in order],
        )
        for index, original in enumerate(order):
            self.assertEqual(
                int(shuffled[index]), keyed[(clients[original], dates[original])]
            )

    def test_input_validation(self) -> None:
        clients = ["A", "B"]
        dates = _dates(date(2024, 1, 1), 2)
        for probabilities in ([-0.1, 0.5], [1.1, 0.5], [math.nan, 0.5]):
            with self.assertRaises(ValueError):
                evaluate([0, 1], probabilities, clients, dates, 0.10)
        with self.assertRaises(ValueError):
            evaluate([0, 2], [0.5, 0.5], clients, dates, 0.10)


if __name__ == "__main__":
    unittest.main()
