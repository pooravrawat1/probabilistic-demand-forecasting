from __future__ import annotations

import unittest

import numpy as np
from sklearn.model_selection import GroupKFold

from demand_forecasting.calibration import _fit_platt, _logit, _platt_predict
from demand_forecasting.models import (
    CALENDAR_FEATURES,
    FEATURE_SETS,
    build_logistic,
    choose_config,
)


class ModelsTest(unittest.TestCase):
    def test_feature_sets(self) -> None:
        self.assertEqual(len(FEATURE_SETS["all"]), 27)
        self.assertEqual(
            len(FEATURE_SETS["no_calendar"]),
            27 - len(CALENDAR_FEATURES),
        )
        for name in CALENDAR_FEATURES:
            self.assertNotIn(name, FEATURE_SETS["no_calendar"])
        self.assertNotIn("client_id", FEATURE_SETS["all"])

    def test_logistic_drops_target_month(self) -> None:
        features = FEATURE_SETS["all"]
        pipeline = build_logistic("all", features, 1.0, 100, 42)
        transformers = pipeline.named_steps["preprocess"].transformers
        used_indexes = {
            index
            for _, _, indexes in transformers
            for index in indexes
        }
        self.assertNotIn(features.index("target_month"), used_indexes)
        self.assertIn(features.index("target_iso_weekday"), used_indexes)

    def test_choose_config_rule(self) -> None:
        rows = [
            {"family": "logistic", "status": "ok", "average_precision": 0.5},
            {"family": "logistic", "status": "not_converged",
             "average_precision": 0.99},
            {"family": "logistic", "status": "ok", "average_precision": 0.5},
            {"family": "logistic", "status": "ok", "average_precision": 0.4},
            {"family": "hgb", "status": "ok", "average_precision": 0.6},
        ]
        # Non-converged config is ineligible; exact tie keeps earlier order.
        self.assertEqual(choose_config(rows, "logistic", "average_precision"), 0)
        self.assertEqual(choose_config(rows, "hgb", "average_precision"), 4)

    def test_platt_recovers_identity(self) -> None:
        rng = np.random.default_rng(0)
        logits = rng.standard_normal(20000)
        probabilities = 1.0 / (1.0 + np.exp(-logits))
        targets = (rng.random(20000) < probabilities).astype(int)
        slope, intercept = _fit_platt(_logit(probabilities), targets)
        self.assertAlmostEqual(slope, 1.0, delta=0.05)
        self.assertAlmostEqual(intercept, 0.0, delta=0.05)

    def test_platt_out_of_fold_predictions(self) -> None:
        # A deliberately miscalibrated model (probabilities pushed to the
        # edges) should improve under out-of-fold Platt.
        rng = np.random.default_rng(1)
        logits = rng.standard_normal(4000)
        true_p = 1.0 / (1.0 + np.exp(-logits))
        targets = (rng.random(4000) < true_p).astype(int)
        raw = np.clip(1.0 / (1.0 + np.exp(-3 * logits)), 1e-6, 1 - 1e-6)
        x = _logit(raw)
        groups = np.arange(4000) // 40
        oof = np.zeros(4000)
        for train_idx, test_idx in GroupKFold(n_splits=5).split(x, targets, groups):
            slope, intercept = _fit_platt(x[train_idx], targets[train_idx])
            oof[test_idx] = _platt_predict(x[test_idx], slope, intercept)
        raw_brier = float(np.mean((raw - targets) ** 2))
        oof_brier = float(np.mean((oof - targets) ** 2))
        self.assertLess(oof_brier, raw_brier)


if __name__ == "__main__":
    unittest.main()
