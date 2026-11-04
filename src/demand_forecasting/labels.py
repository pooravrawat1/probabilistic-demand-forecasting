"""Freeze client demand thresholds and build day-ahead target labels."""

from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import platform
from typing import Any, Sequence

import duckdb

from . import __version__
from .build import (
    _git_state,
    _package_versions,
    _schema,
    _sha256,
    _sql_string,
)


OUTPUT_FILES = (
    "client_thresholds.parquet",
    "labeled_days.parquet",
    "prevalence_by_split.csv",
    "prevalence_by_client.csv",
    "prevalence_by_month.csv",
    "prevalence_by_weekday.csv",
    "catalog.duckdb",
    "labels_manifest.json",
)

ESTIMATOR_DEFINITION = (
    "nearest-rank: k = ceil(p*n), threshold = k-th smallest model-valid "
    "post-activation training daily peak (kW); target = peak > threshold"
)


def nearest_rank_index(count: int, probability: float) -> int:
    """Return the exact nearest-rank index ``k = ceil(p * n)`` in ``1..n``."""
    if count < 1:
        raise ValueError("Cannot rank an empty sample")
    if not 0 < probability <= 1:
        raise ValueError("Probability must lie in (0, 1]")
    return math.ceil(Fraction(str(probability)) * count)


def percentile_nearest_rank(values: Sequence[float], probability: float) -> float:
    """Return the nearest-rank percentile of ``values`` sorted ascending."""
    if not values:
        raise ValueError("Cannot rank an empty sample")
    ordered = sorted(values)
    return ordered[nearest_rank_index(len(ordered), probability) - 1]


def _remove_previous_outputs(output_dir: Path) -> None:
    for filename in OUTPUT_FILES:
        path = output_dir / filename
        if path.is_file():
            path.unlink()
    wal = output_dir / "catalog.duckdb.wal"
    if wal.is_file():
        wal.unlink()


def _fit_thresholds(
    connection: duckdb.DuckDBPyConnection,
    client_day_path: Path,
    eligibility_path: Path,
    eligible: dict[str, dict[str, Any]],
    training_start: str,
    training_end: str,
    minimum_training_days: int,
    percentile: float,
    fitted_at: datetime,
    source_sha256: str,
    processed_manifest_sha256: str,
) -> tuple[list[tuple], Any]:
    rows = connection.execute(
        f"""
        SELECT client_day.client_id, client_day.local_date, client_day.daily_peak_kw
        FROM read_parquet({_sql_string(client_day_path)}) AS client_day
        JOIN read_parquet({_sql_string(eligibility_path)}) AS eligibility
            USING (client_id)
        WHERE eligibility.eligible
            AND client_day.model_valid
            AND client_day.post_activation
            AND client_day.local_date BETWEEN DATE {_sql_string(training_start)}
                AND DATE {_sql_string(training_end)}
        ORDER BY client_day.client_id, client_day.local_date
        """
    ).fetchall()

    fitted: list[tuple] = []
    max_fit_date = None
    position = 0
    for client_id in sorted(eligible):
        client_days = []
        while position < len(rows) and rows[position][0] == client_id:
            _, local_date, peak = rows[position]
            client_days.append((local_date, peak))
            position += 1
        expected = eligible[client_id]["valid_training_days_after_activation"]
        if len(client_days) < minimum_training_days:
            raise ValueError(
                f"Client {client_id} has {len(client_days)} contributing training "
                f"days, below the minimum of {minimum_training_days}"
            )
        if len(client_days) != expected:
            raise ValueError(
                f"Client {client_id} has {len(client_days)} contributing training "
                f"days but eligibility recorded {expected}"
            )
        peaks = [peak for _, peak in client_days]
        rank = nearest_rank_index(len(peaks), percentile)
        threshold = sorted(peaks)[rank - 1]
        fit_dates = [local_date for local_date, _ in client_days]
        if max_fit_date is None or max(fit_dates) > max_fit_date:
            max_fit_date = max(fit_dates)
        fitted.append(
            (
                client_id,
                threshold,
                "nearest_rank",
                percentile,
                rank,
                len(client_days),
                sum(1 for peak in peaks if peak == threshold),
                sum(1 for peak in peaks if peak > threshold),
                training_start,
                training_end,
                min(fit_dates),
                max(fit_dates),
                source_sha256,
                processed_manifest_sha256,
                fitted_at,
            )
        )
    return fitted, max_fit_date


def build_labels(
    processed_dir: Path,
    output_dir: Path,
    config: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Freeze per-client thresholds and write labeled target days."""
    client_day_path = processed_dir / "client_day.parquet"
    eligibility_path = processed_dir / "client_eligibility.parquet"
    processed_manifest_path = processed_dir / "manifest.json"
    for path in (client_day_path, eligibility_path, processed_manifest_path):
        if not path.is_file():
            raise FileNotFoundError(f"Processed input not found: {path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_previous_outputs(output_dir)
    project_root = (project_root or Path.cwd()).resolve()

    labels_cfg = config["labels"]
    study_cfg = config["study"]
    estimator = str(labels_cfg["threshold_estimator"])
    if estimator != "nearest_rank":
        raise ValueError(f"Unsupported threshold estimator: {estimator}")
    percentile = float(labels_cfg["threshold_percentile"])
    training_start = study_cfg["training_start"]
    training_end = study_cfg["training_end"]
    minimum_training_days = int(study_cfg["minimum_valid_training_days"])
    splits = (
        ("train", training_start, training_end),
        ("selection", study_cfg["selection_start"], study_cfg["selection_end"]),
        ("calibration", study_cfg["calibration_start"], study_cfg["calibration_end"]),
        ("test", study_cfg["test_start"], study_cfg["test_end"]),
    )
    test_end = study_cfg["test_end"]

    processed_manifest = json.loads(
        processed_manifest_path.read_text(encoding="utf-8")
    )
    source_sha256 = processed_manifest["source"]["sha256"]
    processed_manifest_sha256 = _sha256(processed_manifest_path)
    input_hashes = {
        "client_day_parquet": {
            "path": str(client_day_path.resolve()),
            "sha256": _sha256(client_day_path),
        },
        "client_eligibility_parquet": {
            "path": str(eligibility_path.resolve()),
            "sha256": _sha256(eligibility_path),
        },
        "processed_manifest": {
            "path": str(processed_manifest_path.resolve()),
            "sha256": processed_manifest_sha256,
        },
        "source_sha256": source_sha256,
    }

    started = datetime.now(timezone.utc)
    fitted_at = started.replace(tzinfo=None)
    thresholds_path = output_dir / "client_thresholds.parquet"
    labeled_path = output_dir / "labeled_days.parquet"
    connection = duckdb.connect()
    try:
        # Stable row and aggregation order makes repeated runs byte-identical.
        connection.execute("SET threads = 1")
        connection.execute("SET preserve_insertion_order = true")

        eligible = {
            row[0]: {
                "activation_date": row[1],
                "valid_training_days_after_activation": row[2],
            }
            for row in connection.execute(
                f"""
                SELECT client_id, activation_date, valid_training_days_after_activation
                FROM read_parquet({_sql_string(eligibility_path)})
                WHERE eligible
                ORDER BY client_id
                """
            ).fetchall()
        }
        fitted, max_fit_date = _fit_thresholds(
            connection,
            client_day_path,
            eligibility_path,
            eligible,
            training_start,
            training_end,
            minimum_training_days,
            percentile,
            fitted_at,
            source_sha256,
            processed_manifest_sha256,
        )
        if max_fit_date is not None and str(max_fit_date) > str(training_end):
            raise ValueError(
                f"Contributing day {max_fit_date} exceeds training_end {training_end}"
            )

        connection.execute("""
            CREATE TEMP TABLE threshold_stage (
                client_id VARCHAR,
                threshold_kw DOUBLE,
                estimator VARCHAR,
                percentile DOUBLE,
                rank_index INTEGER,
                contributing_days INTEGER,
                days_at_threshold INTEGER,
                training_positive_days INTEGER,
                fit_start DATE,
                fit_end DATE,
                first_fit_date DATE,
                last_fit_date DATE,
                source_sha256 VARCHAR,
                processed_manifest_sha256 VARCHAR,
                fitted_at_utc TIMESTAMP
            )
        """)
        connection.executemany(
            "INSERT INTO threshold_stage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            fitted,
        )
        connection.execute(f"""
            COPY (
                SELECT * FROM threshold_stage ORDER BY client_id
            ) TO {_sql_string(thresholds_path)} (
                FORMAT PARQUET,
                COMPRESSION ZSTD
            )
        """)

        split_case = "\n".join(
            f"WHEN local_date BETWEEN DATE {_sql_string(start)}"
            f" AND DATE {_sql_string(end)} THEN '{name}'"
            for name, start, end in splits
        )
        connection.execute(f"""
            COPY (
                SELECT
                    client_day.client_id,
                    client_day.local_date AS target_date,
                    CASE {split_case} END AS split,
                    client_day.daily_peak_kw,
                    thresholds.threshold_kw,
                    (client_day.daily_peak_kw > thresholds.threshold_kw)::TINYINT AS target
                FROM read_parquet({_sql_string(client_day_path)}) AS client_day
                JOIN read_parquet({_sql_string(thresholds_path)}) AS thresholds
                    USING (client_id)
                WHERE client_day.model_valid
                    AND client_day.post_activation
                    AND client_day.local_date BETWEEN DATE {_sql_string(training_start)}
                        AND DATE {_sql_string(test_end)}
                ORDER BY client_day.client_id, client_day.local_date
            ) TO {_sql_string(labeled_path)} (
                FORMAT PARQUET,
                COMPRESSION ZSTD,
                ROW_GROUP_SIZE 122880
            )
        """)

        prevalence_files = {
            "prevalence_by_split": (
                "SELECT split, count(*)::INTEGER AS n, "
                "count(*) FILTER (WHERE target = 1)::INTEGER AS positives, "
                "count(*) FILTER (WHERE target = 0)::INTEGER AS negatives, "
                "round(count(*) FILTER (WHERE target = 1) / count(*), 6) AS prevalence "
                f"FROM read_parquet({_sql_string(labeled_path)}) "
                "GROUP BY split "
                "ORDER BY CASE split WHEN 'train' THEN 1 WHEN 'selection' THEN 2 "
                "WHEN 'calibration' THEN 3 ELSE 4 END"
            ),
            "prevalence_by_client": (
                "SELECT client_id, split, count(*)::INTEGER AS n, "
                "count(*) FILTER (WHERE target = 1)::INTEGER AS positives, "
                "count(*) FILTER (WHERE target = 0)::INTEGER AS negatives, "
                "round(count(*) FILTER (WHERE target = 1) / count(*), 6) AS prevalence "
                f"FROM read_parquet({_sql_string(labeled_path)}) "
                "GROUP BY client_id, split ORDER BY client_id, split"
            ),
            "prevalence_by_month": (
                "SELECT split, extract(year FROM target_date)::INTEGER AS year, "
                "extract(month FROM target_date)::INTEGER AS month, "
                "count(*)::INTEGER AS n, "
                "count(*) FILTER (WHERE target = 1)::INTEGER AS positives, "
                "count(*) FILTER (WHERE target = 0)::INTEGER AS negatives, "
                "round(count(*) FILTER (WHERE target = 1) / count(*), 6) AS prevalence "
                f"FROM read_parquet({_sql_string(labeled_path)}) "
                "GROUP BY split, year, month ORDER BY split, year, month"
            ),
            "prevalence_by_weekday": (
                "SELECT split, isodow(target_date)::INTEGER AS iso_weekday, "
                "dayname(target_date) AS weekday_name, "
                "count(*)::INTEGER AS n, "
                "count(*) FILTER (WHERE target = 1)::INTEGER AS positives, "
                "count(*) FILTER (WHERE target = 0)::INTEGER AS negatives, "
                "round(count(*) FILTER (WHERE target = 1) / count(*), 6) AS prevalence "
                f"FROM read_parquet({_sql_string(labeled_path)}) "
                "GROUP BY split, iso_weekday, weekday_name "
                "ORDER BY split, iso_weekday"
            ),
        }
        for name, query in prevalence_files.items():
            csv_path = output_dir / f"{name}.csv"
            connection.execute(
                f"COPY ({query}) TO {_sql_string(csv_path)} (HEADER, DELIMITER ',')"
            )

        counts_row = connection.execute(f"""
            SELECT
                count(*),
                count(*) FILTER (WHERE target = 1),
                count(*) FILTER (WHERE target = 0)
            FROM read_parquet({_sql_string(labeled_path)})
        """).fetchone()
        assert counts_row is not None
        labeled_days, positives, negatives = map(int, counts_row)

        prevalence_by_split = {
            split: {"n": n, "positives": pos, "negatives": neg, "prevalence": prev}
            for split, n, pos, neg, prev in connection.execute(
                f"""
                SELECT split, n, positives, negatives, prevalence
                FROM read_csv(
                    {_sql_string(output_dir / 'prevalence_by_split.csv')},
                    header = true
                )
                ORDER BY CASE split WHEN 'train' THEN 1 WHEN 'selection' THEN 2
                    WHEN 'calibration' THEN 3 ELSE 4 END
                """
            ).fetchall()
        }

        excluded_row = connection.execute(f"""
            SELECT
                count(*) FILTER (
                    WHERE eligibility.eligible
                        AND client_day.post_activation
                        AND NOT client_day.model_valid
                ),
                count(*) FILTER (
                    WHERE eligibility.eligible
                        AND NOT coalesce(client_day.post_activation, false)
                ),
                count(*) FILTER (WHERE NOT eligibility.eligible)
            FROM read_parquet({_sql_string(client_day_path)}) AS client_day
            JOIN read_parquet({_sql_string(eligibility_path)}) AS eligibility
                USING (client_id)
            WHERE client_day.local_date BETWEEN DATE {_sql_string(training_start)}
                AND DATE {_sql_string(test_end)}
        """).fetchone()
        assert excluded_row is not None
        excluded_days = {
            "not_model_valid": int(excluded_row[0]),
            "pre_activation": int(excluded_row[1]),
            "ineligible_client_days": int(excluded_row[2]),
        }

        schemas = {
            "client_thresholds": _schema(connection, thresholds_path),
            "labeled_days": _schema(connection, labeled_path),
        }
        for name in prevalence_files:
            schemas[name] = [
                {"name": row[0], "type": row[1], "nullable": row[2] == "YES"}
                for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_csv("
                    f"{_sql_string(output_dir / (name + '.csv'))}, header = true)"
                ).fetchall()
            ]
    finally:
        connection.close()

    _create_label_catalog(output_dir)
    finished = datetime.now(timezone.utc)
    config_snapshot = json.dumps(config, sort_keys=True, separators=(",", ":"))
    git_state = _git_state(project_root)
    manifest: dict[str, Any] = {
        "status": "complete",
        "generated_at_utc": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "inputs": input_hashes,
        "run": {
            "package_version": __version__,
            "python": platform.python_version(),
            "dependencies": _package_versions(),
            "configuration": config,
            "configuration_sha256": hashlib.sha256(config_snapshot.encode()).hexdigest(),
            "labels_sha256": _sha256(Path(__file__)),
            **git_state,
            "random_seed": config.get("run", {}).get("random_seed"),
        },
        "estimator": {
            "name": "nearest_rank",
            "probability": percentile,
            "definition": ESTIMATOR_DEFINITION,
        },
        "outputs": {
            name: {
                "path": str((output_dir / filename).resolve()),
                "bytes": (output_dir / filename).stat().st_size,
                "sha256": _sha256(output_dir / filename),
                "schema": schemas.get(name),
            }
            for name, filename in (
                ("client_thresholds", "client_thresholds.parquet"),
                ("labeled_days", "labeled_days.parquet"),
                ("prevalence_by_split", "prevalence_by_split.csv"),
                ("prevalence_by_client", "prevalence_by_client.csv"),
                ("prevalence_by_month", "prevalence_by_month.csv"),
                ("prevalence_by_weekday", "prevalence_by_weekday.csv"),
                ("catalog", "catalog.duckdb"),
            )
        },
        "counts": {
            "threshold_clients": len(fitted),
            "labeled_days": labeled_days,
            "positives": positives,
            "negatives": negatives,
        },
        "excluded_target_days": excluded_days,
        "prevalence_by_split": prevalence_by_split,
        "provenance": {
            "max_fit_date": max_fit_date.isoformat() if max_fit_date else None,
            "training_end": training_end,
            "passed": max_fit_date is None or str(max_fit_date) <= str(training_end),
        },
    }
    (output_dir / "labels_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _create_label_catalog(output_dir: Path) -> None:
    catalog_path = output_dir / "catalog.duckdb"
    connection = duckdb.connect(str(catalog_path))
    try:
        for view, filename in (
            ("client_thresholds", "client_thresholds.parquet"),
            ("labeled_days", "labeled_days.parquet"),
        ):
            parquet_path = (output_dir / filename).resolve()
            connection.execute(
                f"CREATE OR REPLACE VIEW {view} AS "
                f"SELECT * FROM read_parquet({_sql_string(parquet_path)})"
            )
    finally:
        connection.close()
