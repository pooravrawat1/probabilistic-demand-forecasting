"""Stage 2: probability calibration on the calibration split only."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pickle
import platform
from typing import Any

import duckdb
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

from .build import (
    _git_state,
    _package_versions,
    _schema,
    _sha256,
    _sql_string,
)
from .metrics import METRIC_DEFINITIONS, evaluate
from .models import FAMILIES
from .selection import _load_examples, _write_predictions


CALIBRATION_FILES = (
    "calibration_predictions.parquet",
    "calibration_metrics.csv",
    "calibrators.json",
)


def _logit(p: np.ndarray) -> np.ndarray:
    clipped = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(clipped / (1 - clipped))


def _fit_platt(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    model = LogisticRegression(penalty=None, solver="lbfgs")
    model.fit(x.reshape(-1, 1), y)
    return float(model.coef_[0][0]), float(model.intercept_[0])


def _platt_predict(x: np.ndarray, slope: float, intercept: float) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-(slope * x + intercept)))


def calibrate_models(
    selection_dir: Path,
    examples_dir: Path,
    config: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Calibrate frozen models on calibration rows; write test-ready config."""
    record_path = selection_dir / "selection_record.json"
    examples_path = examples_dir / "modeling_examples.parquet"
    for path in (record_path, examples_path):
        if not path.is_file():
            raise FileNotFoundError(f"Input not found: {path}")
    project_root = (project_root or Path.cwd()).resolve()

    selection_record = json.loads(record_path.read_text(encoding="utf-8"))
    selection_id = selection_record["selection_id"]
    expected_examples = selection_record["data"]["modeling_examples_sha256"]
    actual_examples = _sha256(examples_path)
    if expected_examples != actual_examples:
        raise ValueError(
            "modeling_examples.parquet hash differs from the selection record"
        )

    budget_fraction = float(config["experiment"]["alert_budget_fraction"])
    folds = int(config["models"]["calibration_cv_folds"])
    strength = float(config["experiment"]["weekly_smoothing_strength"])
    training_end = config["study"]["training_end"]

    models: dict[str, Any] = {}
    for family in FAMILIES:
        pickle_path = selection_dir / "selected" / f"{family}.pkl"
        expected = selection_record["selected"][family]["pickle_sha256"]
        if _sha256(pickle_path) != expected:
            raise ValueError(
                f"Pickle for {family} does not match the selection record"
            )
        models[family] = pickle.loads(pickle_path.read_bytes())

    started = datetime.now(timezone.utc)
    connection = duckdb.connect()
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET preserve_insertion_order = true")
        # Only calibration rows are loaded in stage 2.
        data = _load_examples(connection, examples_path, ("calibration",))
    finally:
        connection.close()

    dates = data["target_dates"]
    iso_weeks = np.asarray(
        [f"{day.isocalendar().year}-W{day.isocalendar().week:02d}" for day in dates]
    )
    targets = data["targets"]
    calibrators: dict[str, Any] = {}
    predictions_by_model: dict[str, dict[str, Any]] = {}
    metrics_rows: list[dict[str, Any]] = []

    for family in FAMILIES:
        features = selection_record["selected_features"][family]
        x = np.column_stack([data["features"][name] for name in features])
        raw = models[family].predict_proba(x)[:, 1]
        logits = _logit(raw)

        oof = np.zeros(len(raw))
        splitter = GroupKFold(n_splits=folds)
        for train_idx, test_idx in splitter.split(logits, targets, iso_weeks):
            slope, intercept = _fit_platt(logits[train_idx], targets[train_idx])
            oof[test_idx] = _platt_predict(logits[test_idx], slope, intercept)
        slope, intercept = _fit_platt(logits, targets)
        calibrated = _platt_predict(logits, slope, intercept)

        eval_args = (targets, data["client_ids"], dates)
        raw_metrics = evaluate(targets, raw, *eval_args[1:], budget_fraction)
        oof_metrics = evaluate(targets, oof, *eval_args[1:], budget_fraction)
        in_sample = evaluate(targets, calibrated, *eval_args[1:], budget_fraction)
        use_calibration = oof_metrics["brier"] < raw_metrics["brier"]
        for kind, metrics in (
            ("raw", raw_metrics),
            ("platt_oof", oof_metrics),
            ("platt_in_sample", in_sample),
        ):
            metrics_rows.append(
                {"model_id": family, "probability_kind": kind, **metrics}
            )
        calibrators[family] = {
            "method": "platt",
            "slope": slope,
            "intercept": intercept,
            "raw_brier": raw_metrics["brier"],
            "oof_platt_brier": oof_metrics["brier"],
            "in_sample_platt_brier": in_sample["brier"],
            "use_calibration": use_calibration,
            "fold_definition": (
                f"{folds}-fold GroupKFold grouped by ISO year-week "
                "of target_date"
            ),
        }
        predictions_by_model[family] = {
            "probabilities": raw,
            "calibrated": calibrated,
            "client_ids": data["client_ids"],
            "target_dates": dates,
            "splits": ["calibration"] * len(dates),
            "targets": targets,
        }

    calibration_dir = selection_dir / "calibration"
    calibration_dir.mkdir(exist_ok=True)
    for filename in CALIBRATION_FILES:
        path = calibration_dir / filename
        if path.is_file():
            path.unlink()

    predictions_path = calibration_dir / "calibration_predictions.parquet"
    # Primary probability per model: calibrated if selected, else raw.
    primary = {
        family: (
            predictions_by_model[family]["calibrated"]
            if calibrators[family]["use_calibration"]
            else predictions_by_model[family]["probabilities"]
        )
        for family in FAMILIES
    }
    connection = duckdb.connect()
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET preserve_insertion_order = true")
        _write_predictions(
            connection,
            predictions_path,
            selection_id,
            {
                family: {
                    **predictions_by_model[family],
                    "rank_probabilities": primary[family],
                }
                for family in FAMILIES
            },
            budget_fraction,
        )
        schemas = {
            "calibration_predictions": _schema(connection, predictions_path)
        }
    finally:
        connection.close()

    metric_fields = [
        "model_id", "probability_kind", "n", "positives", "negatives",
        "prevalence", "average_precision", "brier", "alert_budget_fraction",
        "alert_count", "alert_true_positives", "alert_precision",
        "alert_recall", "cutoff_probability", "tied_at_cutoff_total",
        "tied_at_cutoff_alerted",
    ]
    metrics_path = calibration_dir / "calibration_metrics.csv"
    with metrics_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(metric_fields)
        for row in metrics_rows:
            writer.writerow([row[field] for field in metric_fields])

    finished = datetime.now(timezone.utc)
    calibrators_payload = {
        "selection_id": selection_id,
        "calibration_method": "platt",
        "models": calibrators,
    }
    (calibration_dir / "calibrators.json").write_text(
        json.dumps(calibrators_payload, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )

    test_ready_payload: dict[str, Any] = {
        "record_version": 1,
        "status": "ready_for_sign_off",
        "selection_id": selection_id,
        "data": {
            "modeling_examples_sha256": selection_record["data"][
                "modeling_examples_sha256"
            ],
            "examples_manifest_sha256": selection_record["data"][
                "examples_manifest_sha256"
            ],
            "upstream_inputs": selection_record["data"]["upstream_inputs"],
        },
        "split_boundaries": selection_record["split_boundaries"],
        "random_seed": selection_record["random_seed"],
        "feature_sets": {
            family: selection_record["selected_features"][family]
            for family in FAMILIES
        },
        "selected": selection_record["selected"],
        "calibration": {
            "method": "platt",
            "logit_clip": 1e-6,
            "cv_folds": folds,
            "fold_definition": (
                f"{folds}-fold GroupKFold grouped by ISO year-week of "
                "target_date"
            ),
            "decision": "use_calibration iff oof_platt_brier < raw_brier",
            "models": {
                family: {
                    "slope": calibrators[family]["slope"],
                    "intercept": calibrators[family]["intercept"],
                    "use_calibration": calibrators[family]["use_calibration"],
                    "primary_probability": (
                        "calibrated"
                        if calibrators[family]["use_calibration"]
                        else "raw"
                    ),
                }
                for family in FAMILIES
            },
        },
        "compared_methods": [
            {"model_id": "client_prevalence", "kind": "baseline"},
            {
                "model_id": "weekly_pattern",
                "kind": "baseline",
                "weekly_smoothing_strength": strength,
            },
            {
                "model_id": "logistic",
                "kind": "selected_model",
                "pickle_sha256": selection_record["selected"]["logistic"][
                    "pickle_sha256"
                ],
            },
            {
                "model_id": "hgb",
                "kind": "selected_model",
                "pickle_sha256": selection_record["selected"]["hgb"][
                    "pickle_sha256"
                ],
            },
        ],
        "metric_definitions": METRIC_DEFINITIONS,
        "alert_rule": METRIC_DEFINITIONS["alert_rule"],
        "alert_budget_fraction": budget_fraction,
        "tie_rule": "ascending (client_id, target_date)",
        "code_sha256": {
            name: _sha256(Path(__file__).parent / f"{name}.py")
            for name in (
                "models", "selection", "calibration", "metrics", "baselines",
                "experiment",
            )
        },
        "leakage_statement": (
            "test split (2014-07-01..2014-12-31) not loaded at any stage"
        ),
    }
    canonical = json.dumps(
        test_ready_payload, sort_keys=True, separators=(",", ":"), default=str
    )
    config_sha256 = hashlib.sha256(canonical.encode()).hexdigest()
    test_ready = {
        "config_sha256": config_sha256,
        **test_ready_payload,
        "sign_off": {"signed_by": None, "signed_at": None},
        "generated_at_utc": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "git": _git_state(project_root),
        "estimators": {
            "python": platform.python_version(),
            "dependencies": _package_versions(),
        },
        "outputs": {
            name: {
                "path": str((calibration_dir / filename).resolve()),
                "bytes": (calibration_dir / filename).stat().st_size,
                "sha256": _sha256(calibration_dir / filename),
                "schema": schemas.get(name),
            }
            for name, filename in (
                ("calibration_predictions", "calibration_predictions.parquet"),
                ("calibration_metrics", "calibration_metrics.csv"),
                ("calibrators", "calibrators.json"),
            )
        },
    }
    (selection_dir / "test_ready.json").write_text(
        json.dumps(test_ready, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return test_ready
