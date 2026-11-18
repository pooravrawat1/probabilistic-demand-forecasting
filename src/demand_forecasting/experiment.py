"""Versioned baseline experiment runner and protocol record."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import platform
from typing import Any

import duckdb
import numpy as np

from . import __version__
from .baselines import (
    fit_client_prevalence,
    fit_weekly_pattern,
    predict_client_prevalence,
    predict_weekly_pattern,
)
from .build import (
    _git_state,
    _package_versions,
    _schema,
    _sha256,
    _sql_string,
)
from .metrics import METRIC_DEFINITIONS, alert_ranking, evaluate


RECORD_VERSION = 1
BASELINE_FEATURES = ["client_id", "target_iso_weekday"]
MODEL_IDS = ("client_prevalence", "weekly_pattern")
OUTPUT_FILES = (
    "predictions.parquet",
    "metrics.json",
    "metrics.csv",
    "experiment_record.json",
)


def assert_identical_keys(predictions_by_model: dict[str, dict[str, Any]]) -> None:
    """Raise unless every model scored the same labeled rows."""
    reference_model = sorted(predictions_by_model)[0]
    reference = predictions_by_model[reference_model]
    for model_id in sorted(predictions_by_model)[1:]:
        other = predictions_by_model[model_id]
        keys = set(zip(other["client_ids"], other["target_dates"], other["splits"]))
        reference_keys = set(
            zip(reference["client_ids"], reference["target_dates"], reference["splits"])
        )
        if keys != reference_keys:
            raise ValueError(
                f"Model {model_id} evaluation keys differ from {reference_model}"
            )
        labels = {
            (client_id, target_date): int(target)
            for client_id, target_date, target in zip(
                other["client_ids"], other["target_dates"], other["targets"]
            )
        }
        for client_id, target_date, target in zip(
            reference["client_ids"], reference["target_dates"], reference["targets"]
        ):
            if labels[(client_id, target_date)] != int(target):
                raise ValueError(
                    f"Model {model_id} labels differ from {reference_model} "
                    f"on ({client_id}, {target_date})"
                )


def _experiment_id(record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return "exp-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


def run_baselines(
    examples_dir: Path,
    output_root: Path,
    config: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Fit baselines on train, score the configured evaluation splits."""
    examples_path = examples_dir / "modeling_examples.parquet"
    examples_manifest_path = examples_dir / "examples_manifest.json"
    for path in (examples_path, examples_manifest_path):
        if not path.is_file():
            raise FileNotFoundError(f"Input not found: {path}")
    project_root = (project_root or Path.cwd()).resolve()

    experiment_cfg = config["experiment"]
    evaluation_splits = list(experiment_cfg["evaluation_splits"])
    if "test" in evaluation_splits:
        raise ValueError("The test split may not be evaluated before Milestone 6")
    budget_fraction = float(experiment_cfg["alert_budget_fraction"])
    strength = float(experiment_cfg["weekly_smoothing_strength"])
    study_cfg = config["study"]

    examples_manifest = json.loads(
        examples_manifest_path.read_text(encoding="utf-8")
    )

    started = datetime.now(timezone.utc)
    connection = duckdb.connect()
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET preserve_insertion_order = true")
        split_list = ", ".join(_sql_string(split) for split in evaluation_splits)
        # The test split's rows and targets are never loaded.
        rows = connection.execute(f"""
            SELECT client_id, target_date, split, target, target_iso_weekday
            FROM read_parquet({_sql_string(examples_path)})
            WHERE split IN ('train', {split_list})
            ORDER BY client_id, target_date
        """).fetchall()
    finally:
        connection.close()

    train_rows = [row for row in rows if row[2] == "train"]
    evaluation_rows = [row for row in rows if row[2] != "train"]
    if not train_rows:
        raise ValueError("No training examples available to fit baselines")

    train_clients = [row[0] for row in train_rows]
    train_weekdays = [row[4] for row in train_rows]
    train_targets = [row[3] for row in train_rows]
    client_rates = fit_client_prevalence(train_clients, train_targets)
    weekday_table, client_prior = fit_weekly_pattern(
        train_clients, train_weekdays, train_targets, strength
    )

    eval_clients = [row[0] for row in evaluation_rows]
    eval_dates = [row[1] for row in evaluation_rows]
    eval_splits = [row[2] for row in evaluation_rows]
    eval_targets = [row[3] for row in evaluation_rows]
    eval_weekdays = [row[4] for row in evaluation_rows]

    predictions_by_model: dict[str, dict[str, Any]] = {
        "client_prevalence": {
            "probabilities": predict_client_prevalence(eval_clients, client_rates),
            "client_ids": eval_clients,
            "target_dates": eval_dates,
            "splits": eval_splits,
            "targets": eval_targets,
        },
        "weekly_pattern": {
            "probabilities": predict_weekly_pattern(
                eval_clients, eval_weekdays, weekday_table, client_prior
            ),
            "client_ids": eval_clients,
            "target_dates": eval_dates,
            "splits": eval_splits,
            "targets": eval_targets,
        },
    }
    assert_identical_keys(predictions_by_model)

    metrics_rows: list[dict[str, Any]] = []
    for model_id in MODEL_IDS:
        prediction = predictions_by_model[model_id]
        for split in evaluation_splits:
            mask = np.asarray([s == split for s in prediction["splits"]])
            if not mask.any():
                continue
            result = evaluate(
                np.asarray(prediction["targets"])[mask],
                np.asarray(prediction["probabilities"])[mask],
                np.asarray(prediction["client_ids"], dtype=object)[mask],
                np.asarray(prediction["target_dates"], dtype=object)[mask],
                budget_fraction,
            )
            metrics_rows.append({"model_id": model_id, "split": split, **result})

    record: dict[str, Any] = {
        "record_version": RECORD_VERSION,
        "data": {
            "modeling_examples_sha256": _sha256(examples_path),
            "examples_manifest_sha256": _sha256(examples_manifest_path),
            "upstream_inputs": examples_manifest["inputs"],
        },
        "split_boundaries": {
            name: study_cfg[f"{name}_{part}"]
            for name in ("training", "selection", "calibration", "test")
            for part in ("start", "end")
        },
        "random_seed": config.get("run", {}).get("random_seed"),
        "missing_history_policy": examples_manifest["policy"],
        "feature_set": BASELINE_FEATURES,
        "parameter_search": f"none: fixed smoothing strength {strength}",
        "models": [
            {
                "model_id": "client_prevalence",
                "description": "per-client training positive rate",
            },
            {
                "model_id": "weekly_pattern",
                "description": (
                    "per-client per-ISO-weekday smoothed positive rate; "
                    "unseen (client, weekday) falls back to the client rate"
                ),
                "weekly_smoothing_strength": strength,
            },
        ],
        "estimators": {
            "package_version": __version__,
            "python": platform.python_version(),
            "dependencies": _package_versions(),
        },
        "metric_definitions": METRIC_DEFINITIONS,
        "alert_budget_fraction": budget_fraction,
        "evaluation_splits": evaluation_splits,
        "code_sha256": {
            "metrics": _sha256(Path(__file__).parent / "metrics.py"),
            "baselines": _sha256(Path(__file__).parent / "baselines.py"),
            "experiment": _sha256(Path(__file__)),
        },
    }
    experiment_id = _experiment_id(record)

    output_dir = output_root / experiment_id
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in OUTPUT_FILES:
        path = output_dir / filename
        if path.is_file():
            path.unlink()

    predictions_path = output_dir / "predictions.parquet"
    connection = duckdb.connect()
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET preserve_insertion_order = true")
        connection.execute("""
            CREATE TEMP TABLE prediction_stage (
                experiment_id VARCHAR,
                model_id VARCHAR,
                client_id VARCHAR,
                target_date DATE,
                split VARCHAR,
                target TINYINT,
                raw_probability DOUBLE,
                calibrated_probability DOUBLE,
                alert_rank INTEGER,
                alert TINYINT
            )
        """)
        for model_id in MODEL_IDS:
            prediction = predictions_by_model[model_id]
            ranks = alert_ranking(
                prediction["probabilities"],
                prediction["client_ids"],
                prediction["target_dates"],
            )
            # Ranks and alerts are per model × split.
            split_indexes: dict[str, list[int]] = {}
            for index, split in enumerate(prediction["splits"]):
                split_indexes.setdefault(split, []).append(index)
            alert = np.zeros(len(prediction["splits"]), dtype=int)
            global_rank = np.zeros(len(prediction["splits"]), dtype=int)
            for split, indexes in split_indexes.items():
                order = np.argsort(ranks[indexes], kind="stable")
                for position, original in enumerate(np.asarray(indexes)[order]):
                    global_rank[original] = position + 1
                alert_count = math.ceil(
                    Fraction(str(budget_fraction)) * len(indexes)
                )
                for original in np.asarray(indexes)[order][:alert_count]:
                    alert[original] = 1
            rows_to_insert = [
                (
                    experiment_id,
                    model_id,
                    prediction["client_ids"][i],
                    prediction["target_dates"][i],
                    prediction["splits"][i],
                    int(prediction["targets"][i]),
                    float(prediction["probabilities"][i]),
                    None,
                    int(global_rank[i]),
                    int(alert[i]),
                )
                for i in range(len(prediction["splits"]))
            ]
            connection.executemany(
                "INSERT INTO prediction_stage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows_to_insert,
            )
        connection.execute(f"""
            COPY (
                SELECT * FROM prediction_stage
                ORDER BY model_id, split, client_id, target_date
            ) TO {_sql_string(predictions_path)} (
                FORMAT PARQUET,
                COMPRESSION ZSTD,
                ROW_GROUP_SIZE 122880
            )
        """)
        schemas = {"predictions": _schema(connection, predictions_path)}
    finally:
        connection.close()

    (output_dir / "metrics.json").write_text(
        json.dumps(metrics_rows, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    metric_fields = [
        "model_id", "split", "n", "positives", "negatives", "prevalence",
        "average_precision", "brier", "alert_budget_fraction", "alert_count",
        "alert_true_positives", "alert_precision", "alert_recall",
        "cutoff_probability", "tied_at_cutoff_total", "tied_at_cutoff_alerted",
    ]
    with (output_dir / "metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(metric_fields)
        for row in metrics_rows:
            writer.writerow([row[field] for field in metric_fields])

    finished = datetime.now(timezone.utc)
    git_state = _git_state(project_root)
    experiment_record = {
        "experiment_id": experiment_id,
        **record,
        "generated_at_utc": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        **git_state,
        "metrics": metrics_rows,
        "outputs": {
            name: {
                "path": str((output_dir / filename).resolve()),
                "bytes": (output_dir / filename).stat().st_size,
                "sha256": _sha256(output_dir / filename),
                "schema": schemas.get(name),
            }
            for name, filename in (
                ("predictions", "predictions.parquet"),
                ("metrics_json", "metrics.json"),
                ("metrics_csv", "metrics.csv"),
            )
        },
    }
    (output_dir / "experiment_record.json").write_text(
        json.dumps(experiment_record, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return experiment_record
