"""Training-only baseline probability models."""

from __future__ import annotations

from typing import Sequence

import numpy as np


def fit_client_prevalence(
    train_client_ids: Sequence[str],
    train_targets: Sequence[int],
) -> dict[str, float]:
    """Return each client's raw training positive rate."""
    rates: dict[str, list[int]] = {}
    for client_id, target in zip(train_client_ids, train_targets):
        rates.setdefault(client_id, []).append(int(target))
    return {
        client_id: sum(targets) / len(targets)
        for client_id, targets in rates.items()
    }


def predict_client_prevalence(
    client_ids: Sequence[str],
    rates: dict[str, float],
) -> np.ndarray:
    """Predict the fitted training positive rate for each client."""
    unknown = sorted({client_id for client_id in client_ids if client_id not in rates})
    if unknown:
        raise ValueError(f"No training rows for clients: {unknown[:5]}")
    return np.asarray([rates[client_id] for client_id in client_ids], dtype=float)


def fit_weekly_pattern(
    train_client_ids: Sequence[str],
    train_weekdays: Sequence[int],
    train_targets: Sequence[int],
    strength: float,
) -> tuple[dict[tuple[str, int], float], dict[str, float]]:
    """Fit per-client, per-ISO-weekday smoothed positive rates.

    Each (client, weekday) estimate is ``(positives + strength * client_rate) /
    (n + strength)``: the client's overall training rate shrunk toward by a
    pseudo-count of ``strength``. A (client, weekday) combination unseen in
    training falls back to the client's overall rate (missing-week handling).
    Returns ``(weekday_table, client_prior)``.
    """
    client_prior = fit_client_prevalence(train_client_ids, train_targets)
    counts: dict[tuple[str, int], list[int]] = {}
    for client_id, weekday, target in zip(
        train_client_ids, train_weekdays, train_targets
    ):
        key = (client_id, int(weekday))
        entry = counts.setdefault(key, [0, 0])
        entry[0] += 1
        entry[1] += int(target)
    weekday_table = {
        key: (positive + strength * client_prior[key[0]]) / (n + strength)
        for key, (n, positive) in counts.items()
    }
    return weekday_table, client_prior


def predict_weekly_pattern(
    client_ids: Sequence[str],
    weekdays: Sequence[int],
    weekday_table: dict[tuple[str, int], float],
    client_prior: dict[str, float],
) -> np.ndarray:
    """Predict smoothed per-client weekday rates with client-rate fallback."""
    unknown = sorted(
        {client_id for client_id in client_ids if client_id not in client_prior}
    )
    if unknown:
        raise ValueError(f"No training rows for clients: {unknown[:5]}")
    return np.asarray(
        [
            weekday_table.get((client_id, int(weekday)), client_prior[client_id])
            for client_id, weekday in zip(client_ids, weekdays)
        ],
        dtype=float,
    )
