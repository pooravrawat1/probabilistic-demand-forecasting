from __future__ import annotations

import unittest

from demand_forecasting.baselines import (
    fit_client_prevalence,
    fit_weekly_pattern,
    predict_client_prevalence,
    predict_weekly_pattern,
)


class BaselinesTest(unittest.TestCase):
    def test_client_prevalence(self) -> None:
        rates = fit_client_prevalence(
            ["A", "A", "A", "B"], [1, 0, 1, 0]
        )
        self.assertEqual(rates, {"A": 2 / 3, "B": 0.0})
        probabilities = predict_client_prevalence(["B", "A"], rates)
        self.assertEqual(list(probabilities), [0.0, 2 / 3])
        for value in probabilities:
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_weekly_pattern_smoothing(self) -> None:
        clients = ["A", "A", "A", "A", "B"]
        weekdays = [1, 1, 1, 2, 1]
        targets = [1, 0, 0, 0, 0]
        strength = 10.0
        table, prior = fit_weekly_pattern(clients, weekdays, targets, strength)

        self.assertEqual(prior, {"A": 0.25, "B": 0.0})
        # A weekday 1: (1 + 10*0.25) / (3 + 10)
        self.assertAlmostEqual(table[("A", 1)], 3.5 / 13)
        # A weekday 2: (0 + 10*0.25) / (1 + 10)
        self.assertAlmostEqual(table[("A", 2)], 2.5 / 11)
        self.assertAlmostEqual(table[("B", 1)], 0.0)

        # Unseen (client, weekday) falls back to the client rate.
        probabilities = predict_weekly_pattern(
            ["A", "A", "B"], [1, 7, 5], table, prior
        )
        self.assertAlmostEqual(probabilities[0], 3.5 / 13)
        self.assertAlmostEqual(probabilities[1], 0.25)
        self.assertAlmostEqual(probabilities[2], 0.0)
        for value in probabilities:
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_unknown_client_raises(self) -> None:
        rates = fit_client_prevalence(["A"], [1])
        with self.assertRaises(ValueError):
            predict_client_prevalence(["C"], rates)
        table, prior = fit_weekly_pattern(["A"], [1], [1], 10.0)
        with self.assertRaises(ValueError):
            predict_weekly_pattern(["C"], [1], table, prior)


if __name__ == "__main__":
    unittest.main()
