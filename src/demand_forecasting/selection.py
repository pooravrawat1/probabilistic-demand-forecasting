"""Grid search and model selection on train/selection splits only."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path
import pickle
import platform
import time
from typing import Any, Sequence
import warnings

import duckdb
import numpy as np
from sklearn.exceptions import ConvergenceWarning

from . import __version__
from .build import (
    _git_state,
    _package_versions,
    _schema,
    _sha256,
    _sql_string,
)
from .metrics import METRIC_DEFINITIONS, alert_ranking, evaluate
from .models import FAMILIES, FEATURE_SET_ORDER, FEATURE_SETS, choose_config, config_grid


OUTPUT_FILES = (
    "selection_log.csv",
    "selection_predictions.parquet",
    "selection_record.json",
)
SELECTION_RULE = (
    "per family, the eligible (converged) configuration with the highest "
    "selection-split selection_metric; exact ties keep the earlier grid order "
    "(feature set, then parameters in configuration order)"
)


def _load_examples(
    connection: duckdb.DuckDBPyConnection,
    examples_path: Path,
    splits: Sequence[str],
) -> dict[str, Any]:
    split_list = ", ".join(_sql_string(split) for split in splits)
    columns = [
        "client_id", "target_date", "split", "target", *FEATURE_SETS["all"]
    ]
    query = f"""
        SELECT {", ".join(columns)}
        FROM read_parquet({_sql_string(examples_path)})
        WHERE split IN ({split_list})
        ORDER BY client_id, target_date
    """
    frame = connection.execute(query).fetchnumpy()
    return {
        "client_ids": [str(value) for value in frame["client_id"]],
        "target_dates": np.asarray(frame["target_date"])
        .astype("datetime64[D]")
        .tolist(),
        "splits": [str(value) for value in frame["split"]],
        "targets": np.asarray(frame["target"], dtype=int),
        "features": {name: np.asarray(frame[name], dtype=float) for name in FEATURE_SETS["all"]},
    }


def _design_matrix(data: dict[str, Any], indexes: np.ndarray, features: list[str]) -> np.ndarray:
    return np.column_stack([data["features"][name][indexes] for name in features])


def _fit_config(
    entry: dict[str, Any],
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_eval: np.ndarray,
    eval_args: tuple,
    budget_fraction: float,
) -> tuple[dict[str, Any], Any, np.ndarray | None]:
    """Fit one grid entry; return (log_row, fitted_model, eval_probabilities)."""
    started = time.perf_counter()
    row: dict[str, Any] = {
        "config_id": entry["config_id"],
        "family": entry["family"],
        "feature_set": entry["feature_set"],
        "params": json.dumps(entry["params"], sort_keys=True),
        "converged": "",
        "n_iter": "",
        "fit_seconds": "",
        "status": "ok",
        "warning": "",
    }
    model = entry["builder"]()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            model.fit(x_train, y_train)
        row["warning"] = ";".join(
            str(item.message) for item in caught
            if issubclass(item.category, ConvergenceWarning)
        )
        converged = True
        n_iter = ""
        final = getattr(model, "named_steps", {}).get("model", model)
        if hasattr(final, "n_iter_"):
            n_iter = int(np.max(final.n_iter_))
            # The n_iter < max_iter convergence rule applies to the iterative
            # logistic solver; HGB always uses exactly max_iter iterations.
            if entry["family"] == "logistic":
                converged = n_iter < int(final.get_params()["max_iter"])
        row["converged"] = converged
        row["n_iter"] = n_iter
        if not converged:
            row["status"] = "not_converged"
    except Exception as error:  # fit failures stay in the log
        row["status"] = f"failed: {type(error).__name__}"
        model = None
    row["fit_seconds"] = round(time.perf_counter() - started, 3)

    probabilities = None
    if model is not None:
        probabilities = model.predict_proba(x_eval)[:, 1]
        if not np.isfinite(probabilities).all() or (probabilities < 0).any() or (
            probabilities > 1
        ).any():
            raise ValueError(
                f"Config {entry['config_id']} produced invalid probabilities"
            )
        if row["status"] == "ok":
            targets, clients, dates = eval_args
            row.update(
                evaluate(
                    targets,
                    probabilities,
                    clients,
                    dates,
                    budget_fraction,
                )
            )
    return row, model, probabilities


def _write_predictions(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
    selection_id: str,
    predictions_by_model: dict[str, dict[str, Any]],
    budget_fraction: float,
) -> None:
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
    for model_id in sorted(predictions_by_model):
        prediction = predictions_by_model[model_id]
        probabilities = prediction["probabilities"]
        rank_source = prediction.get(
            "rank_probabilities",
            prediction.get("calibrated", probabilities),
        )
        ranks = alert_ranking(
            rank_source, prediction["client_ids"], prediction["target_dates"]
        )
        alert_count = math.ceil(Fraction(str(budget_fraction)) * len(ranks))
        rows = [
            (
                selection_id,
                model_id,
                prediction["client_ids"][i],
                prediction["target_dates"][i],
                prediction["splits"][i],
                int(prediction["targets"][i]),
                float(prediction["probabilities"][i]),
                (
                    float(prediction["calibrated"][i])
                    if "calibrated" in prediction
                    else None
                ),
                int(ranks[i]),
                int(ranks[i] <= alert_count),
            )
            for i in range(len(ranks))
        ]
        connection.executemany(
            "INSERT INTO prediction_stage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    connection.execute(f"""
        COPY (
            SELECT * FROM prediction_stage
            ORDER BY model_id, split, client_id, target_date
        ) TO {_sql_string(path)} (
            FORMAT PARQUET,
            COMPRESSION ZSTD,
            ROW_GROUP_SIZE 122880
        )
    """)
    connection.execute("DROP TABLE prediction_stage")


def select_models(
    examples_dir: Path,
    output_root: Path,
    config: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Fit every grid config on train, score on selection, freeze winners."""
    examples_path = examples_dir / "modeling_examples.parquet"
    examples_manifest_path = examples_dir / "examples_manifest.json"
    for path in (examples_path, examples_manifest_path):
        if not path.is_file():
            raise FileNotFoundError(f"Input not found: {path}")
    project_root = (project_root or Path.cwd()).resolve()

    models_cfg = config["models"]
    experiment_cfg = config["experiment"]
    budget_fraction = float(experiment_cfg["alert_budget_fraction"])
    selection_metric = str(models_cfg["selection_metric"])
    seed = int(config.get("run", {}).get("random_seed", 0))
    training_end = config["study"]["training_end"]

    examples_manifest = json.loads(
        examples_manifest_path.read_text(encoding="utf-8")
    )
    examples_hash = _sha256(examples_path)

    started = datetime.now(timezone.utc)
    connection = duckdb.connect()
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET preserve_insertion_order = true")
        # Calibration and test rows are never loaded in stage 1.
        data = _load_examples(connection, examples_path, ("train", "selection"))
    finally:
        connection.close()

    is_train = np.asarray([s == "train" for s in data["splits"]])
    is_selection = np.asarray([s == "selection" for s in data["splits"]])
    y_train = data["targets"][is_train]
    if len(set(y_train.tolist())) < 2:
        raise ValueError("Training targets are single-class; cannot fit models")
    train_dates = [d for d, flag in zip(data["target_dates"], is_train) if flag]
    if str(max(train_dates)) > str(training_end):
        raise ValueError(
            f"Training state includes target_date {max(train_dates)} "
            f"after training_end {training_end}"
        )
    train_index = np.where(is_train)[0]
    eval_index = np.where(is_selection)[0]
    eval_targets = data["targets"][is_selection]
    eval_clients = [c for c, flag in zip(data["client_ids"], is_selection) if flag]
    eval_dates = [d for d, flag in zip(data["target_dates"], is_selection) if flag]

    grid = config_grid(config)
    log_rows: list[dict[str, Any]] = []
    fitted: dict[str, Any] = {}
    eval_probs: dict[str, np.ndarray] = {}
    for entry in grid:
        x_train = _design_matrix(data, train_index, entry["features"])
        x_eval = _design_matrix(data, eval_index, entry["features"])
        row, model, probabilities = _fit_config(
            entry,
            x_train,
            y_train,
            x_eval,
            (eval_targets, eval_clients, eval_dates),
            budget_fraction,
        )
        log_rows.append(row)
        if model is not None:
            fitted[entry["config_id"]] = model
            eval_probs[entry["config_id"]] = probabilities

    selected: dict[str, dict[str, Any]] = {}
    for family in FAMILIES:
        index = choose_config(log_rows, family, selection_metric)
        if index is None:
            raise ValueError(f"No eligible {family} configuration converged")
        log_rows[index]["status"] = "selected"
        selected[family] = {
            "config_id": log_rows[index]["config_id"],
            "feature_set": log_rows[index]["feature_set"],
            "features": FEATURE_SETS[log_rows[index]["feature_set"]],
            "params": json.loads(log_rows[index]["params"]),
            "metrics": {
                key: log_rows[index][key]
                for key in log_rows[index]
                if key
                not in (
                    "config_id",
                    "family",
                    "feature_set",
                    "params",
                    "converged",
                    "n_iter",
                    "fit_seconds",
                    "status",
                    "warning",
                )
            },
        }

    record: dict[str, Any] = {
        "record_version": 1,
        "data": {
            "modeling_examples_sha256": examples_hash,
            "examples_manifest_sha256": _sha256(examples_manifest_path),
            "upstream_inputs": examples_manifest["inputs"],
        },
        "random_seed": seed,
        "split_boundaries": {
            name: config["study"][f"{name}_{part}"]
            for name in ("training", "selection", "calibration", "test")
            for part in ("start", "end")
        },
        "feature_sets": FEATURE_SETS,
        "feature_set_order": FEATURE_SET_ORDER,
        "grid": [
            {"config_id": entry["config_id"], "family": entry["family"],
             "feature_set": entry["feature_set"], "params": entry["params"]}
            for entry in grid
        ],
        "selection_metric": selection_metric,
        "selection_rule": SELECTION_RULE,
        "alert_budget_fraction": budget_fraction,
        "metric_definitions": METRIC_DEFINITIONS,
        "note_client_identity": (
            "client identity is not a feature; per-client training spike "
            "rates would leak each training row's own label"
        ),
        "training_state": {
            "train_rows": int(is_train.sum()),
            "train_target_date_min": str(min(train_dates)),
            "train_target_date_max": str(max(train_dates)),
            "training_end": training_end,
            "train_positives": int(y_train.sum()),
            "train_negatives": int(len(y_train) - y_train.sum()),
        },
        "selected": {
            family: {k: v for k, v in choice.items() if k != "features"}
            for family, choice in selected.items()
        },
        "estimators": {
            "package_version": __version__,
            "python": platform.python_version(),
            "dependencies": _package_versions(),
        },
        "code_sha256": {
            name: _sha256(Path(__file__).parent / f"{name}.py")
            for name in ("models", "selection", "metrics", "experiment")
        },
    }
    selected_features = {
        family: selected[family]["features"] for family in FAMILIES
    }
    record["selected_features"] = selected_features
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    selection_id = "sel-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]

    output_dir = output_root / selection_id
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in OUTPUT_FILES:
        path = output_dir / filename
        if path.is_file():
            path.unlink()
    wal = output_dir / "catalog.duckdb.wal"
    if wal.is_file():
        wal.unlink()
    (output_dir / "selected").mkdir(exist_ok=True)
    (output_dir / "calibration").mkdir(exist_ok=True)

    # Pickle selected models, then verify reload equality on selection rows.
    pickle_sha: dict[str, str] = {}
    for family in FAMILIES:
        config_id = selected[family]["config_id"]
        model = fitted[config_id]
        pickle_path = output_dir / "selected" / f"{family}.pkl"
        pickle_path.write_bytes(pickle.dumps(model))
        digest = _sha256(pickle_path)
        pickle_sha[family] = digest
        reloaded = pickle.loads(pickle_path.read_bytes())
        features = selected[family]["features"]
        x_eval = _design_matrix(data, eval_index, features)
        if not np.array_equal(
            reloaded.predict_proba(x_eval)[:, 1], eval_probs[config_id]
        ):
            raise ValueError(f"Reloaded {family} model predictions differ")
    for family in FAMILIES:
        record["selected"][family]["pickle_sha256"] = pickle_sha[family]

    metric_fields = [
        "config_id", "family", "feature_set", "params", "converged", "n_iter",
        "fit_seconds", "status", "warning", "n", "positives", "negatives", "prevalence",
        "average_precision", "brier", "alert_budget_fraction", "alert_count",
        "alert_true_positives", "alert_precision", "alert_recall",
        "cutoff_probability", "tied_at_cutoff_total", "tied_at_cutoff_alerted",
    ]
    with (output_dir / "selection_log.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(metric_fields)
        for row in log_rows:
            writer.writerow([row.get(field, "") for field in metric_fields])

    predictions_path = output_dir / "selection_predictions.parquet"
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
                    "probabilities": eval_probs[selected[family]["config_id"]],
                    "client_ids": eval_clients,
                    "target_dates": eval_dates,
                    "splits": ["selection"] * len(eval_dates),
                    "targets": eval_targets,
                }
                for family in FAMILIES
            },
            budget_fraction,
        )
        schemas = {"selection_predictions": _schema(connection, predictions_path)}
    finally:
        connection.close()

    finished = datetime.now(timezone.utc)
    record["selection_id"] = selection_id
    record["generated_at_utc"] = finished.isoformat()
    record["duration_seconds"] = round((finished - started).total_seconds(), 3)
    record.update(_git_state(project_root))
    record["outputs"] = {
        name: {
            "path": str((output_dir / filename).resolve()),
            "bytes": (output_dir / filename).stat().st_size,
            "sha256": _sha256(output_dir / filename),
            "schema": schemas.get(name),
        }
        for name, filename in (
            ("selection_log", "selection_log.csv"),
            ("selection_predictions", "selection_predictions.parquet"),
        )
    }
    for family in FAMILIES:
        record["outputs"][f"selected_{family}_pickle"] = {
            "path": str((output_dir / "selected" / f"{family}.pkl").resolve()),
            "bytes": (output_dir / "selected" / f"{family}.pkl").stat().st_size,
            "sha256": pickle_sha[family],
        }
    (output_dir / "selection_record.json").write_text(
        json.dumps(record, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return record
