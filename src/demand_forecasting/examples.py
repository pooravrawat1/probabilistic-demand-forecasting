"""Build leakage-safe day-ahead modeling examples from frozen labels."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
from typing import Any

import duckdb

from . import __version__
from .build import (
    _git_state,
    _package_versions,
    _schema,
    _sha256,
    _sql_string,
)
from .labels import percentile_nearest_rank


OUTPUT_FILES = (
    "modeling_examples.parquet",
    "client_scaling.parquet",
    "feature_registry.csv",
    "example_counts_by_split.csv",
    "example_counts_by_client.csv",
    "example_exclusions.csv",
    "catalog.duckdb",
    "examples_manifest.json",
)

POLICY = (
    "valid history day = client_day row with model_valid AND post_activation; "
    "load features use only days on or before feature_cutoff = target_date - 1; "
    "rolling windows aggregate valid days in the last 14 days and require at "
    "least min_history_valid_days valid days; lag1 is the most recent valid "
    "day in the window; lag7 falls back to the most recent valid same-weekday "
    "day within weekly_lag_max_weeks; exclusions are counted, not imputed"
)

KEY_COLUMNS = (
    "client_id",
    "target_date",
    "forecast_date",
    "feature_cutoff",
    "split",
    "target",
    "history_first_date",
    "history_last_date",
)

# name, units, source_fields, lookback_days, fitting_requirement
FEATURE_REGISTRY = (
    ("history_valid_days", "days", "client_day.model_valid, post_activation", 14, "none"),
    ("lag1_peak_rel", "fraction of client threshold", "client_day.daily_peak_kw", 14, "client_thresholds.threshold_kw (training)"),
    ("lag1_mean_rel", "fraction of client threshold", "client_day.mean_kw", 14, "client_thresholds.threshold_kw (training)"),
    ("lag1_energy_rel", "fraction of client threshold-day", "client_day.energy_kwh_est", 14, "client_thresholds.threshold_kw (training)"),
    ("lag1_age_days", "days", "client_day.local_date", 14, "none"),
    ("lag1_is_fallback", "count", "client_day.local_date", 14, "none"),
    ("lag7_peak_rel", "fraction of client threshold", "client_day.daily_peak_kw", 28, "client_thresholds.threshold_kw (training)"),
    ("lag7_energy_rel", "fraction of client threshold-day", "client_day.energy_kwh_est", 28, "client_thresholds.threshold_kw (training)"),
    ("lag7_age_days", "days", "client_day.local_date", 28, "none"),
    ("lag7_is_fallback", "count", "client_day.local_date", 28, "none"),
    ("roll3_peak_mean_rel", "fraction of client threshold", "client_day.daily_peak_kw", 3, "client_thresholds.threshold_kw (training)"),
    ("roll7_peak_mean_rel", "fraction of client threshold", "client_day.daily_peak_kw", 7, "client_thresholds.threshold_kw (training)"),
    ("roll14_peak_mean_rel", "fraction of client threshold", "client_day.daily_peak_kw", 14, "client_thresholds.threshold_kw (training)"),
    ("roll7_peak_max_rel", "fraction of client threshold", "client_day.daily_peak_kw", 7, "client_thresholds.threshold_kw (training)"),
    ("roll14_peak_max_rel", "fraction of client threshold", "client_day.daily_peak_kw", 14, "client_thresholds.threshold_kw (training)"),
    ("roll7_peak_std_rel", "fraction of client threshold", "client_day.daily_peak_kw", 7, "client_thresholds.threshold_kw (training)"),
    ("roll14_peak_std_rel", "fraction of client threshold", "client_day.daily_peak_kw", 14, "client_thresholds.threshold_kw (training)"),
    ("change_lag1_vs_roll7", "fraction of client threshold", "client_day.daily_peak_kw", 14, "client_thresholds.threshold_kw (training)"),
    ("change_lag1_vs_lag7", "fraction of client threshold", "client_day.daily_peak_kw", 28, "client_thresholds.threshold_kw (training)"),
    ("high_use_days_7", "count", "client_day.daily_peak_kw", 7, "client_scaling.high_use_kw (training)"),
    ("high_use_days_14", "count", "client_day.daily_peak_kw", 14, "client_scaling.high_use_kw (training)"),
    ("spike_days_14", "count", "client_day.daily_peak_kw", 14, "client_thresholds.threshold_kw (training)"),
    ("target_iso_weekday", "calendar", "target_date", 0, "none"),
    ("target_is_weekend", "calendar", "target_date", 0, "none"),
    ("target_month", "calendar", "target_date", 0, "none"),
    ("target_month_sin", "calendar", "target_date", 0, "none"),
    ("target_month_cos", "calendar", "target_date", 0, "none"),
)

CALENDAR_FEATURES = {
    "target_iso_weekday",
    "target_is_weekend",
    "target_month",
    "target_month_sin",
    "target_month_cos",
}

FEATURE_COLUMNS = [name for name, *_ in FEATURE_REGISTRY]


def _remove_previous_outputs(output_dir: Path) -> None:
    for filename in OUTPUT_FILES:
        path = output_dir / filename
        if path.is_file():
            path.unlink()
    wal = output_dir / "catalog.duckdb.wal"
    if wal.is_file():
        wal.unlink()


def _fit_scaling(
    connection: duckdb.DuckDBPyConnection,
    client_day_path: Path,
    thresholds: dict[str, dict[str, Any]],
    training_start: str,
    training_end: str,
    percentile: float,
) -> list[tuple]:
    """Return per-client (client_id, high_use_kw, contributing_days, last_fit_date)."""
    rows = connection.execute(
        f"""
        SELECT client_day.client_id, client_day.local_date, client_day.daily_peak_kw
        FROM read_parquet({_sql_string(client_day_path)}) AS client_day
        JOIN read_parquet(
            {_sql_string(thresholds['path'])}
        ) AS thresholds USING (client_id)
        WHERE client_day.model_valid
            AND client_day.post_activation
            AND client_day.local_date BETWEEN DATE {_sql_string(training_start)}
                AND DATE {_sql_string(training_end)}
        ORDER BY client_day.client_id, client_day.local_date
        """
    ).fetchall()

    fitted: list[tuple] = []
    position = 0
    for client_id in sorted(thresholds["clients"]):
        peaks = []
        last_fit_date = None
        while position < len(rows) and rows[position][0] == client_id:
            peaks.append(rows[position][2])
            last_fit_date = rows[position][1]
            position += 1
        expected = thresholds["clients"][client_id]["contributing_days"]
        if len(peaks) != expected:
            raise ValueError(
                f"Client {client_id} has {len(peaks)} contributing training "
                f"days but client_thresholds recorded {expected}"
            )
        fitted.append(
            (
                client_id,
                percentile_nearest_rank(peaks, percentile),
                len(peaks),
                last_fit_date,
            )
        )
    return fitted


def _write_scaling(
    connection: duckdb.DuckDBPyConnection,
    scaling_path: Path,
    thresholds: dict[str, dict[str, Any]],
    fitted: list[tuple],
    percentile: float,
    training_start: str,
    training_end: str,
    thresholds_sha256: str,
    fitted_at: datetime,
) -> None:
    connection.execute("""
        CREATE TEMP TABLE scaling_stage (
            client_id VARCHAR,
            scale_kw DOUBLE,
            high_use_kw DOUBLE,
            high_use_percentile DOUBLE,
            contributing_days INTEGER,
            fit_start DATE,
            fit_end DATE,
            last_fit_date DATE,
            client_thresholds_sha256 VARCHAR,
            fitted_at_utc TIMESTAMP
        )
    """)
    rows = []
    for client_id, high_use_kw, contributing_days, last_fit_date in fitted:
        scale_kw = thresholds["clients"][client_id]["threshold_kw"]
        if scale_kw <= 0:
            raise ValueError(f"Client {client_id} has nonpositive scale_kw {scale_kw}")
        rows.append(
            (
                client_id,
                scale_kw,
                high_use_kw,
                percentile,
                contributing_days,
                training_start,
                training_end,
                last_fit_date,
                thresholds_sha256,
                fitted_at,
            )
        )
    connection.executemany(
        "INSERT INTO scaling_stage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    connection.execute(f"""
        COPY (
            SELECT * FROM scaling_stage ORDER BY client_id
        ) TO {_sql_string(scaling_path)} (
            FORMAT PARQUET,
            COMPRESSION ZSTD
        )
    """)


def build_examples(
    processed_dir: Path,
    labels_dir: Path,
    output_dir: Path,
    config: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Write modeling examples, client scaling, counts, and a manifest."""
    client_day_path = processed_dir / "client_day.parquet"
    labeled_path = labels_dir / "labeled_days.parquet"
    thresholds_path = labels_dir / "client_thresholds.parquet"
    labels_manifest_path = labels_dir / "labels_manifest.json"
    for path in (client_day_path, labeled_path, thresholds_path, labels_manifest_path):
        if not path.is_file():
            raise FileNotFoundError(f"Input not found: {path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_previous_outputs(output_dir)
    project_root = (project_root or Path.cwd()).resolve()

    examples_cfg = config["examples"]
    study_cfg = config["study"]
    history_window = int(examples_cfg["history_window_days"])
    min_history = int(examples_cfg["min_history_valid_days"])
    weekly_lag_weeks = int(examples_cfg["weekly_lag_max_weeks"])
    high_use_percentile = float(examples_cfg["high_use_percentile"])
    training_start = study_cfg["training_start"]
    training_end = study_cfg["training_end"]

    labels_manifest = json.loads(labels_manifest_path.read_text(encoding="utf-8"))
    input_hashes = {
        "client_day_parquet": {
            "path": str(client_day_path.resolve()),
            "sha256": _sha256(client_day_path),
        },
        "labeled_days_parquet": {
            "path": str(labeled_path.resolve()),
            "sha256": _sha256(labeled_path),
        },
        "client_thresholds_parquet": {
            "path": str(thresholds_path.resolve()),
            "sha256": _sha256(thresholds_path),
        },
        "labels_manifest": {
            "path": str(labels_manifest_path.resolve()),
            "sha256": _sha256(labels_manifest_path),
        },
        "source_sha256": labels_manifest["inputs"]["source_sha256"],
    }

    started = datetime.now(timezone.utc)
    fitted_at = started.replace(tzinfo=None)
    scaling_path = output_dir / "client_scaling.parquet"
    examples_path = output_dir / "modeling_examples.parquet"
    connection = duckdb.connect()
    try:
        # Stable row and aggregation order makes repeated runs byte-identical.
        connection.execute("SET threads = 1")
        connection.execute("SET preserve_insertion_order = true")

        threshold_rows = connection.execute(
            f"""
            SELECT client_id, threshold_kw, contributing_days
            FROM read_parquet({_sql_string(thresholds_path)})
            ORDER BY client_id
            """
        ).fetchall()
        thresholds = {
            "path": thresholds_path,
            "clients": {
                row[0]: {"threshold_kw": row[1], "contributing_days": row[2]}
                for row in threshold_rows
            },
        }
        fitted = _fit_scaling(
            connection,
            client_day_path,
            thresholds,
            training_start,
            training_end,
            high_use_percentile,
        )
        _write_scaling(
            connection,
            scaling_path,
            thresholds,
            fitted,
            high_use_percentile,
            training_start,
            training_end,
            input_hashes["client_thresholds_parquet"]["sha256"],
            fitted_at,
        )

        connection.execute(f"""
            CREATE TEMP TABLE history AS
            SELECT
                client_day.client_id,
                client_day.local_date,
                client_day.daily_peak_kw,
                client_day.mean_kw,
                client_day.energy_kwh_est
            FROM read_parquet({_sql_string(client_day_path)}) AS client_day
            JOIN read_parquet({_sql_string(scaling_path)}) AS scaling
                USING (client_id)
            WHERE client_day.model_valid AND client_day.post_activation
        """)
        connection.execute(f"""
            CREATE TEMP TABLE candidates AS
            SELECT
                labeled.client_id,
                labeled.target_date,
                (labeled.target_date - INTERVAL 1 DAY)::DATE AS forecast_date,
                (labeled.target_date - INTERVAL 1 DAY)::DATE AS feature_cutoff,
                labeled.split,
                labeled.target,
                labeled.daily_peak_kw AS target_peak_kw,
                thresholds.threshold_kw,
                scaling.scale_kw,
                scaling.high_use_kw
            FROM read_parquet({_sql_string(labeled_path)}) AS labeled
            JOIN read_parquet({_sql_string(thresholds_path)}) AS thresholds
                USING (client_id)
            JOIN read_parquet({_sql_string(scaling_path)}) AS scaling
                USING (client_id)
        """)

        lag7_dates = ", ".join(
            f"candidates.target_date - INTERVAL {7 * week} DAY"
            for week in range(1, weekly_lag_weeks + 1)
        )
        connection.execute(f"""
            CREATE TEMP TABLE fit AS
            SELECT
                candidates.*,
                count(history.local_date) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL {history_window} DAY
                )::INTEGER AS history_valid_days,
                min(history.local_date) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL {history_window} DAY
                ) AS window_first_date,
                max(history.local_date) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL {history_window} DAY
                ) AS lag1_date,
                max(history.local_date) FILTER (
                    WHERE history.local_date IN ({lag7_dates})
                ) AS lag7_date,
                avg(history.daily_peak_kw) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL 3 DAY
                ) AS roll3_peak_mean,
                avg(history.daily_peak_kw) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL 7 DAY
                ) AS roll7_peak_mean,
                avg(history.daily_peak_kw) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL {history_window} DAY
                ) AS roll14_peak_mean,
                max(history.daily_peak_kw) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL 7 DAY
                ) AS roll7_peak_max,
                max(history.daily_peak_kw) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL {history_window} DAY
                ) AS roll14_peak_max,
                stddev_samp(history.daily_peak_kw) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL 7 DAY
                ) AS roll7_peak_std,
                stddev_samp(history.daily_peak_kw) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL {history_window} DAY
                ) AS roll14_peak_std,
                count(history.local_date) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL 7 DAY
                        AND history.daily_peak_kw > candidates.high_use_kw
                )::INTEGER AS high_use_days_7,
                count(history.local_date) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL {history_window} DAY
                        AND history.daily_peak_kw > candidates.high_use_kw
                )::INTEGER AS high_use_days_14,
                count(history.local_date) FILTER (
                    WHERE history.local_date
                        >= candidates.target_date - INTERVAL {history_window} DAY
                        AND history.daily_peak_kw > candidates.threshold_kw
                )::INTEGER AS spike_days_14
            FROM candidates
            LEFT JOIN history
                ON history.client_id = candidates.client_id
                AND history.local_date BETWEEN
                    candidates.target_date - INTERVAL {7 * weekly_lag_weeks} DAY
                    AND candidates.feature_cutoff
            GROUP BY ALL
        """)

        month_angle = "(2 * pi() * (extract(month FROM target_date) - 1) / 12)"
        connection.execute(f"""
            COPY (
                SELECT
                    fit.client_id,
                    fit.target_date,
                    fit.forecast_date,
                    fit.feature_cutoff,
                    fit.split,
                    fit.target,
                    least(fit.window_first_date, fit.lag7_date) AS history_first_date,
                    fit.lag1_date AS history_last_date,
                    fit.history_valid_days,
                    lag1.daily_peak_kw / fit.scale_kw AS lag1_peak_rel,
                    lag1.mean_kw / fit.scale_kw AS lag1_mean_rel,
                    lag1.energy_kwh_est / (fit.scale_kw * 24) AS lag1_energy_rel,
                    date_diff('day', fit.lag1_date, fit.target_date)::INTEGER
                        AS lag1_age_days,
                    (date_diff('day', fit.lag1_date, fit.target_date) > 1)::TINYINT
                        AS lag1_is_fallback,
                    lag7.daily_peak_kw / fit.scale_kw AS lag7_peak_rel,
                    lag7.energy_kwh_est / (fit.scale_kw * 24) AS lag7_energy_rel,
                    date_diff('day', fit.lag7_date, fit.target_date)::INTEGER
                        AS lag7_age_days,
                    (date_diff('day', fit.lag7_date, fit.target_date) > 7)::TINYINT
                        AS lag7_is_fallback,
                    fit.roll3_peak_mean / fit.scale_kw AS roll3_peak_mean_rel,
                    fit.roll7_peak_mean / fit.scale_kw AS roll7_peak_mean_rel,
                    fit.roll14_peak_mean / fit.scale_kw AS roll14_peak_mean_rel,
                    fit.roll7_peak_max / fit.scale_kw AS roll7_peak_max_rel,
                    fit.roll14_peak_max / fit.scale_kw AS roll14_peak_max_rel,
                    coalesce(fit.roll7_peak_std, 0.0) / fit.scale_kw
                        AS roll7_peak_std_rel,
                    coalesce(fit.roll14_peak_std, 0.0) / fit.scale_kw
                        AS roll14_peak_std_rel,
                    (lag1.daily_peak_kw - fit.roll7_peak_mean) / fit.scale_kw
                        AS change_lag1_vs_roll7,
                    (lag1.daily_peak_kw - lag7.daily_peak_kw) / fit.scale_kw
                        AS change_lag1_vs_lag7,
                    fit.high_use_days_7,
                    fit.high_use_days_14,
                    fit.spike_days_14,
                    isodow(fit.target_date)::INTEGER AS target_iso_weekday,
                    (isodow(fit.target_date) >= 6)::TINYINT AS target_is_weekend,
                    extract(month FROM fit.target_date)::INTEGER AS target_month,
                    sin({month_angle}) AS target_month_sin,
                    cos({month_angle}) AS target_month_cos
                FROM fit
                JOIN history AS lag1
                    ON lag1.client_id = fit.client_id
                    AND lag1.local_date = fit.lag1_date
                JOIN history AS lag7
                    ON lag7.client_id = fit.client_id
                    AND lag7.local_date = fit.lag7_date
                WHERE fit.history_valid_days >= {min_history}
                ORDER BY fit.client_id, fit.target_date
            ) TO {_sql_string(examples_path)} (
                FORMAT PARQUET,
                COMPRESSION ZSTD,
                ROW_GROUP_SIZE 122880
            )
        """)

        # Reconciliation and exclusion accounting.
        labeled_count = connection.execute(
            f"SELECT count(*) FROM read_parquet({_sql_string(labeled_path)})"
        ).fetchone()[0]
        candidate_count = connection.execute(
            "SELECT count(*) FROM candidates"
        ).fetchone()[0]
        if candidate_count != labeled_count:
            raise ValueError(
                f"Candidates {candidate_count} != labeled days {labeled_count}"
            )
        example_count = connection.execute(
            f"SELECT count(*) FROM read_parquet({_sql_string(examples_path)})"
        ).fetchone()[0]
        excluded_count = connection.execute(
            f"SELECT count(*) FROM fit WHERE history_valid_days < {min_history}"
            " OR lag7_date IS NULL"
        ).fetchone()[0]
        if example_count + excluded_count != labeled_count:
            raise ValueError(
                f"Examples {example_count} + excluded {excluded_count} "
                f"!= labeled days {labeled_count}"
            )

        exclusions_path = output_dir / "example_exclusions.csv"
        connection.execute(f"""
            COPY (
                SELECT
                    split,
                    CASE
                        WHEN history_valid_days < {min_history}
                            THEN 'insufficient_history'
                        ELSE 'missing_weekly_lag'
                    END AS reason,
                    count(*)::INTEGER AS n
                FROM fit
                WHERE history_valid_days < {min_history} OR lag7_date IS NULL
                GROUP BY split, reason
                ORDER BY
                    CASE split WHEN 'train' THEN 1 WHEN 'selection' THEN 2
                        WHEN 'calibration' THEN 3 ELSE 4 END,
                    reason
            ) TO {_sql_string(exclusions_path)} (HEADER, DELIMITER ',')
        """)

        split_order = (
            "CASE split WHEN 'train' THEN 1 WHEN 'selection' THEN 2 "
            "WHEN 'calibration' THEN 3 ELSE 4 END"
        )
        prevalence_select = (
            "count(*)::INTEGER AS n, "
            "count(*) FILTER (WHERE target = 1)::INTEGER AS positives, "
            "count(*) FILTER (WHERE target = 0)::INTEGER AS negatives, "
            "round(count(*) FILTER (WHERE target = 1) / count(*), 6) AS prevalence "
            f"FROM read_parquet({_sql_string(examples_path)})"
        )
        connection.execute(f"""
            COPY (
                SELECT split, {prevalence_select}
                GROUP BY split ORDER BY {split_order}
            ) TO {_sql_string(output_dir / 'example_counts_by_split.csv')}
                (HEADER, DELIMITER ',')
        """)
        connection.execute(f"""
            COPY (
                SELECT client_id, split, {prevalence_select}
                GROUP BY client_id, split ORDER BY client_id, split
            ) TO {_sql_string(output_dir / 'example_counts_by_client.csv')}
                (HEADER, DELIMITER ',')
        """)

        # Feature registry and schema agreement.
        registry_path = output_dir / "feature_registry.csv"
        with registry_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "name",
                    "units",
                    "source_fields",
                    "lookback_days",
                    "fitting_requirement",
                    "latest_source_date",
                ]
            )
            for name, units, sources, lookback, fitting in FEATURE_REGISTRY:
                writer.writerow(
                    [
                        name,
                        units,
                        sources,
                        lookback,
                        fitting,
                        "target_date (calendar only)"
                        if name in CALENDAR_FEATURES
                        else "forecast_date",
                    ]
                )

        schemas = {
            "client_scaling": _schema(connection, scaling_path),
            "modeling_examples": _schema(connection, examples_path),
        }
        parquet_feature_names = [
            column["name"]
            for column in schemas["modeling_examples"]
            if column["name"] not in KEY_COLUMNS
        ]
        if parquet_feature_names != FEATURE_COLUMNS:
            raise ValueError(
                "Feature registry does not match parquet feature columns: "
                f"{parquet_feature_names} vs {FEATURE_COLUMNS}"
            )
        for name in ("feature_registry", "example_counts_by_split",
                     "example_counts_by_client", "example_exclusions"):
            schemas[name] = [
                {"name": row[0], "type": row[1], "nullable": row[2] == "YES"}
                for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_csv("
                    f"{_sql_string(output_dir / (name + '.csv'))}, header = true)"
                ).fetchall()
            ]

        # Leakage and null checks.
        lineage_row = connection.execute(f"""
            SELECT max(date_diff('day', feature_cutoff, history_last_date))
            FROM read_parquet({_sql_string(examples_path)})
        """).fetchone()
        max_history_gap = int(lineage_row[0]) if lineage_row[0] is not None else None
        if max_history_gap is not None and max_history_gap > 0:
            raise ValueError(
                f"history_last_date exceeds feature_cutoff by {max_history_gap} days"
            )
        null_checks = ", ".join(
            f"count(*) FILTER (WHERE {name} IS NULL)" for name in FEATURE_COLUMNS
        )
        null_counts = connection.execute(
            f"SELECT {null_checks} FROM read_parquet({_sql_string(examples_path)})"
        ).fetchone()
        null_features = {
            name: int(count)
            for name, count in zip(FEATURE_COLUMNS, null_counts)
            if count
        }
        if null_features:
            raise ValueError(f"NULL feature values found: {null_features}")

        counts_by_split = {
            split: {"n": n, "positives": pos, "negatives": neg, "prevalence": prev}
            for split, n, pos, neg, prev in connection.execute(
                f"""
                SELECT split, n, positives, negatives, prevalence
                FROM read_csv(
                    {_sql_string(output_dir / 'example_counts_by_split.csv')},
                    header = true
                )
                ORDER BY {split_order}
                """
            ).fetchall()
        }
        excluded_by_reason = {
            reason: int(n)
            for reason, n in connection.execute(
                f"""
                SELECT reason, sum(n)::BIGINT
                FROM read_csv(
                    {_sql_string(exclusions_path)}, header = true
                )
                GROUP BY reason ORDER BY reason
                """
            ).fetchall()
        }
    finally:
        connection.close()

    _create_example_catalog(output_dir)
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
            "examples_sha256": _sha256(Path(__file__)),
            **git_state,
            "random_seed": config.get("run", {}).get("random_seed"),
        },
        "policy": POLICY,
        "outputs": {
            name: {
                "path": str((output_dir / filename).resolve()),
                "bytes": (output_dir / filename).stat().st_size,
                "sha256": _sha256(output_dir / filename),
                "schema": schemas.get(name),
            }
            for name, filename in (
                ("modeling_examples", "modeling_examples.parquet"),
                ("client_scaling", "client_scaling.parquet"),
                ("feature_registry", "feature_registry.csv"),
                ("example_counts_by_split", "example_counts_by_split.csv"),
                ("example_counts_by_client", "example_counts_by_client.csv"),
                ("example_exclusions", "example_exclusions.csv"),
                ("catalog", "catalog.duckdb"),
            )
        },
        "counts": {
            "examples": int(example_count),
            "excluded": int(excluded_count),
            "excluded_by_reason": excluded_by_reason,
            "examples_by_split": counts_by_split,
        },
        "lineage_check": {
            "max_history_last_minus_cutoff_days": max_history_gap,
            "passed": max_history_gap is None or max_history_gap <= 0,
        },
    }
    (output_dir / "examples_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _create_example_catalog(output_dir: Path) -> None:
    catalog_path = output_dir / "catalog.duckdb"
    connection = duckdb.connect(str(catalog_path))
    try:
        for view, filename in (
            ("modeling_examples", "modeling_examples.parquet"),
            ("client_scaling", "client_scaling.parquet"),
        ):
            parquet_path = (output_dir / filename).resolve()
            connection.execute(
                f"CREATE OR REPLACE VIEW {view} AS "
                f"SELECT * FROM read_parquet({_sql_string(parquet_path)})"
            )
    finally:
        connection.close()
