"""Deterministic evaluation metrics and the frozen alert rule."""

from __future__ import annotations

from fractions import Fraction
import math
from typing import Any, Sequence

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss


METRIC_DEFINITIONS: dict[str, str] = {
    "average_precision": (
        "scikit-learn average_precision_score: step-wise precision-recall "
        "area, sum over recall steps of (recall_k - recall_(k-1)) * precision_k"
    ),
    "brier": (
        "scikit-learn brier_score_loss: mean squared error between predicted "
        "probabilities and binary targets"
    ),
    "alert_rule": (
        "global top ceil(alert_budget_fraction * n) client-days per split by "
        "descending probability; ties broken by ascending (client_id, "
        "target_date)"
    ),
    "alert_precision": "true positives inside the alert budget / alert_count",
    "alert_recall": "true positives inside the alert budget / positives",
    "tied_at_cutoff": (
        "rows whose probability equals the cutoff_probability; "
        "tied_at_cutoff_alerted counts those inside the budget"
    ),
}


def alert_ranking(
    probabilities: Sequence[float],
    client_ids: Sequence[str],
    target_dates: Sequence[Any],
) -> np.ndarray:
    """Return 1-based ranks: descending probability, ties by client/date."""
    n = len(probabilities)
    if not (len(client_ids) == len(target_dates) == n):
        raise ValueError("Ranking inputs must have equal length")
    if n == 0:
        return np.array([], dtype=np.int64)
    order = np.lexsort(
        (
            np.asarray(target_dates, dtype=str),
            np.asarray(client_ids, dtype=str),
            -np.asarray(probabilities, dtype=float),
        )
    )
    ranks = np.empty(n, dtype=np.int64)
    ranks[order] = np.arange(1, n + 1)
    return ranks


def evaluate(
    targets: Sequence[int],
    probabilities: Sequence[float],
    client_ids: Sequence[str],
    target_dates: Sequence[Any],
    budget_fraction: float,
) -> dict[str, Any]:
    """Score one model on one split under the frozen protocol."""
    y = np.asarray(targets, dtype=float)
    p = np.asarray(probabilities, dtype=float)
    n = len(y)
    if not (len(p) == len(client_ids) == len(target_dates) == n):
        raise ValueError("Evaluation inputs must have equal length")
    if n == 0:
        raise ValueError("Cannot evaluate an empty split")
    if not np.isfinite(p).all() or (p < 0).any() or (p > 1).any():
        raise ValueError("Probabilities must be finite and within [0, 1]")
    if not ((y == 0) | (y == 1)).all():
        raise ValueError("Targets must be 0 or 1")
    if not 0 < budget_fraction <= 1:
        raise ValueError("Alert budget fraction must lie in (0, 1]")

    ranks = alert_ranking(p, client_ids, target_dates)
    alert_count = math.ceil(Fraction(str(budget_fraction)) * n)
    alerts = ranks <= alert_count
    cutoff_probability = float(p[np.where(ranks == alert_count)][0])
    tied_at_cutoff = p == cutoff_probability

    positives = int(y.sum())
    alert_true_positives = int(np.logical_and(alerts, y == 1).sum())
    return {
        "n": n,
        "positives": positives,
        "negatives": n - positives,
        "prevalence": round(positives / n, 6),
        "average_precision": float(average_precision_score(y, p)),
        "brier": float(brier_score_loss(y, p)),
        "alert_budget_fraction": float(budget_fraction),
        "alert_count": alert_count,
        "alert_true_positives": alert_true_positives,
        "alert_precision": alert_true_positives / alert_count,
        "alert_recall": alert_true_positives / positives if positives else 0.0,
        "cutoff_probability": cutoff_probability,
        "tied_at_cutoff_total": int(tied_at_cutoff.sum()),
        "tied_at_cutoff_alerted": int(np.logical_and(tied_at_cutoff, alerts).sum()),
    }
