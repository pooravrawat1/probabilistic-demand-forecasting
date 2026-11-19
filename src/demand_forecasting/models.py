"""Model families, feature sets, and hyperparameter grids.

Client identity is deliberately not a feature: a per-client training spike
rate would leak each training row's own label through the fitted weight.
"""

from __future__ import annotations

from typing import Any

from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .examples import FEATURE_COLUMNS


CALENDAR_FEATURES = (
    "target_iso_weekday",
    "target_is_weekend",
    "target_month",
    "target_month_sin",
    "target_month_cos",
)

FEATURE_SETS: dict[str, list[str]] = {
    "all": list(FEATURE_COLUMNS),
    "no_calendar": [
        name for name in FEATURE_COLUMNS if name not in CALENDAR_FEATURES
    ],
}

FEATURE_SET_ORDER = ("all", "no_calendar")
FAMILIES = ("logistic", "hgb")


def build_logistic(
    feature_set: str,
    features: list[str],
    c: float,
    max_iter: int,
    seed: int,
) -> Pipeline:
    """One-hot weekday (when present) + scaled numerics + L2 logistic.

    ``target_month`` is dropped implicitly: the ColumnTransformer keeps only
    the listed columns, and the month is already encoded by sin/cos.
    """
    transformers: list[tuple[str, Any, list[int]]] = []
    if "target_iso_weekday" in features:
        transformers.append(
            (
                "weekday",
                OneHotEncoder(
                    categories=[list(range(1, 8))], handle_unknown="error"
                ),
                [features.index("target_iso_weekday")],
            )
        )
    numeric = [
        index
        for index, name in enumerate(features)
        if name not in ("target_iso_weekday", "target_month")
    ]
    transformers.append(("scale", StandardScaler(), numeric))
    return Pipeline(
        [
            ("preprocess", ColumnTransformer(transformers)),
            (
                "model",
                LogisticRegression(
                    C=c,
                    max_iter=max_iter,
                    random_state=seed,
                ),
            ),
        ]
    )


def build_hgb(
    features: list[str],
    learning_rate: float,
    max_leaf_nodes: int,
    min_samples_leaf: int,
    l2_regularization: float,
    max_iter: int,
    seed: int,
) -> HistGradientBoostingClassifier:
    """Histogram gradient boosting on raw features (no preprocessing)."""
    return HistGradientBoostingClassifier(
        learning_rate=learning_rate,
        max_leaf_nodes=max_leaf_nodes,
        min_samples_leaf=min_samples_leaf,
        l2_regularization=l2_regularization,
        max_iter=max_iter,
        early_stopping=False,
        random_state=seed,
    )


def config_grid(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ordered grid of model configurations.

    Order: family (logistic, hgb), feature set (all, no_calendar), then the
    hyperparameters in the order they are listed in the configuration. This
    order is also the deterministic tie-break for selection.
    """
    models_cfg = config["models"]
    seed = int(config.get("run", {}).get("random_seed", 0))
    grid: list[dict[str, Any]] = []
    for feature_set in FEATURE_SET_ORDER:
        for c in models_cfg["logistic_C"]:
            grid.append(
                {
                    "config_id": f"logistic/{feature_set}/C={c}",
                    "family": "logistic",
                    "feature_set": feature_set,
                    "features": FEATURE_SETS[feature_set],
                    "params": {"C": c},
                    "builder": lambda c=c, feature_set=feature_set: build_logistic(
                        feature_set,
                        FEATURE_SETS[feature_set],
                        c,
                        int(models_cfg["max_iter_logistic"]),
                        seed,
                    ),
                }
            )
    for feature_set in FEATURE_SET_ORDER:
        for learning_rate in models_cfg["hgb_learning_rate"]:
            for max_leaf_nodes in models_cfg["hgb_max_leaf_nodes"]:
                for min_samples_leaf in models_cfg["hgb_min_samples_leaf"]:
                    grid.append(
                        {
                            "config_id": (
                                f"hgb/{feature_set}/lr={learning_rate},"
                                f"leaves={max_leaf_nodes},min={min_samples_leaf}"
                            ),
                            "family": "hgb",
                            "feature_set": feature_set,
                            "features": FEATURE_SETS[feature_set],
                            "params": {
                                "learning_rate": learning_rate,
                                "max_leaf_nodes": max_leaf_nodes,
                                "min_samples_leaf": min_samples_leaf,
                                "l2_regularization": models_cfg[
                                    "hgb_l2_regularization"
                                ],
                            },
                            "builder": (
                                lambda lr=learning_rate,
                                leaves=max_leaf_nodes,
                                ms=min_samples_leaf,
                                feature_set=feature_set: build_hgb(
                                    FEATURE_SETS[feature_set],
                                    lr,
                                    leaves,
                                    ms,
                                    models_cfg["hgb_l2_regularization"],
                                    int(models_cfg["hgb_max_iter"]),
                                    seed,
                                )
                            ),
                        }
                    )
    return grid


def choose_config(
    log_rows: list[dict[str, Any]], family: str, metric: str
) -> int | None:
    """Index of the eligible ``log_rows`` entry with the highest metric.

    Only rows with ``status == 'ok'`` (converged, fitted successfully) are
    eligible. Exact ties keep the earlier grid position. Returns None when no
    row of the family is eligible.
    """
    best_index = None
    best_value = None
    for index, row in enumerate(log_rows):
        if row["family"] != family or row["status"] != "ok":
            continue
        value = row.get(metric)
        if value is None:
            continue
        if best_value is None or value > best_value:
            best_index = index
            best_value = value
    return best_index
