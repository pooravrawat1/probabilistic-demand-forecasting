"""Build analysis-ready Parquet tables from the unchanged UCI source."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
from typing import Any
import zipfile

import duckdb

from . import __version__


OUTPUT_FILES = (
    "interval_readings.parquet",
    "client_day.parquet",
    "client_eligibility.parquet",
    "catalog.duckdb",
    "manifest.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _source_text_path(source: Path, member: str, temporary_dir: Path) -> tuple[Path, dict[str, Any]]:
    if source.suffix.lower() != ".zip":
        return source, {
            "archive_member": "",
            "member_bytes": source.stat().st_size,
            "member_crc32": "",
        }

    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo(member)
        extracted = temporary_dir / Path(member).name
        with archive.open(info) as input_file, extracted.open("wb") as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
    return extracted, {
        "archive_member": member,
        "member_bytes": info.file_size,
        "member_crc32": f"{info.CRC:08x}",
    }


def _read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle, delimiter=";"))
    clients = header[1:]
    if len(header) < 2 or any(not client for client in clients):
        raise ValueError("Expected an unlabeled timestamp column and named client columns")
    if len(set(clients)) != len(clients):
        raise ValueError("Client column names must be unique")
    return clients


def _git_state(project_root: Path) -> dict[str, str | bool | None]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "git_revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "git_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def _schema(connection: duckdb.DuckDBPyConnection, parquet_path: Path) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"DESCRIBE SELECT * FROM read_parquet({_sql_string(parquet_path)})"
    ).fetchall()
    return [
        {"name": row[0], "type": row[1], "nullable": row[2] == "YES"}
        for row in rows
    ]


def _package_versions() -> dict[str, str]:
    packages = ("duckdb", "matplotlib", "numpy", "scikit-learn")
    versions: dict[str, str] = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def _audit_reconciliation(audit_dir: Path | None, counts: dict[str, int]) -> dict[str, Any]:
    if audit_dir is None or not (audit_dir / "summary.json").is_file():
        return {"status": "not_checked", "reason": "audit summary not supplied"}
    summary = json.loads((audit_dir / "summary.json").read_text(encoding="utf-8"))
    expected = {
        "interval_rows": summary["source"]["interval_values"],
        "client_day_rows": summary["source"]["client_day_rows"],
        "source_valid_client_days": summary["source"]["source_valid_client_days"],
        "model_valid_client_days": summary["source"]["model_valid_client_days"],
        "eligible_clients": summary["eligibility"]["eligible_clients"],
    }
    differences = {
        name: {"expected": value, "actual": counts[name]}
        for name, value in expected.items()
        if counts[name] != value
    }
    return {
        "status": "matched" if not differences else "mismatch",
        "audit_summary": str((audit_dir / "summary.json").resolve()),
        "expected": expected,
        "differences": differences,
    }


def _create_catalog(output_dir: Path) -> None:
    catalog_path = output_dir / "catalog.duckdb"
    connection = duckdb.connect(str(catalog_path))
    try:
        for view, filename in (
            ("interval_readings", "interval_readings.parquet"),
            ("client_day", "client_day.parquet"),
            ("client_eligibility", "client_eligibility.parquet"),
        ):
            parquet_path = (output_dir / filename).resolve()
            connection.execute(
                f"CREATE OR REPLACE VIEW {view} AS "
                f"SELECT * FROM read_parquet({_sql_string(parquet_path)})"
            )
    finally:
        connection.close()


def _remove_previous_outputs(output_dir: Path) -> None:
    for filename in OUTPUT_FILES:
        path = output_dir / filename
        if path.is_file():
            path.unlink()
    wal = output_dir / "catalog.duckdb.wal"
    if wal.is_file():
        wal.unlink()


def build_data(
    source: Path,
    output_dir: Path,
    config: dict[str, Any],
    *,
    audit_dir: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Create interval, client-day, and eligibility Parquet tables plus a catalog."""
    if not source.is_file():
        raise FileNotFoundError(f"Source not found: {source}")
    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_previous_outputs(output_dir)
    project_root = (project_root or Path.cwd()).resolve()

    audit_cfg = config["audit"]
    study_cfg = config["study"]
    expected_intervals = int(audit_cfg["expected_intervals_per_day"])
    interval_minutes = int(audit_cfg["interval_minutes"])
    activation_window = int(audit_cfg["activation_window_days"])
    activation_positive = int(audit_cfg["activation_positive_days"])
    exclude_dst = bool(audit_cfg["exclude_dst_transition_days"])
    training_start = study_cfg["training_start"]
    training_end = study_cfg["training_end"]
    minimum_training_days = int(study_cfg["minimum_valid_training_days"])

    source_hash = _sha256(source)
    started = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="demand-build-", dir=output_dir.parent) as temp_name:
        temp_dir = Path(temp_name)
        text_path, member_info = _source_text_path(source, config["source"]["member"], temp_dir)
        clients = _read_header(text_path)
        columns = ["source_timestamp_raw", *clients]
        column_map = ", ".join(
            f"{_sql_string(column)}: 'VARCHAR'" for column in columns
        )
        unpivot_columns = ", ".join(_sql_identifier(client) for client in clients)
        interval_path = output_dir / "interval_readings.parquet"
        client_day_path = output_dir / "client_day.parquet"
        eligibility_path = output_dir / "client_eligibility.parquet"
        build_database = temp_dir / "build.duckdb"
        connection = duckdb.connect(str(build_database))
        try:
            # Stable row and aggregation order makes repeated builds byte-identical.
            connection.execute("SET threads = 1")
            connection.execute("SET preserve_insertion_order = true")
            connection.execute("SET temp_directory = ?", [str(temp_dir / "duckdb-temp")])
            interval_sql = f"""
                COPY (
                    WITH wide AS (
                        SELECT *
                        FROM read_csv(
                            {_sql_string(text_path)},
                            delim = ';',
                            header = false,
                            skip = 1,
                            columns = {{{column_map}}},
                            nullstr = '__DEMAND_FORECAST_NULL__',
                            strict_mode = true
                        )
                    ),
                    long_values AS (
                        SELECT
                            try_strptime(source_timestamp_raw, '%Y-%m-%d %H:%M:%S') AS source_timestamp,
                            client_id,
                            raw_kw
                        FROM wide
                        UNPIVOT (raw_kw FOR client_id IN ({unpivot_columns}))
                    ),
                    parsed AS (
                        SELECT
                            client_id,
                            source_timestamp,
                            CASE
                                WHEN source_timestamp IS NULL THEN NULL
                                WHEN CAST(source_timestamp AS TIME) = TIME '00:00:00'
                                    THEN CAST(source_timestamp AS DATE) - INTERVAL 1 DAY
                                ELSE CAST(source_timestamp AS DATE)
                            END::DATE AS assigned_local_date,
                            raw_kw,
                            try_cast(replace(raw_kw, ',', '.') AS DOUBLE) AS parsed_kw,
                            count(*) OVER (PARTITION BY source_timestamp) > {len(clients)}
                                AS duplicate_timestamp
                        FROM long_values
                    ),
                    flagged AS (
                        SELECT
                            *,
                            source_timestamp IS NOT NULL AND (
                                extract(minute FROM source_timestamp)::INTEGER % {interval_minutes} != 0
                                OR extract(second FROM source_timestamp) != 0
                            ) AS off_grid_timestamp,
                            assigned_local_date IS NOT NULL
                                AND extract(month FROM assigned_local_date) IN (3, 10)
                                AND dayofweek(assigned_local_date) = 0
                                AND assigned_local_date + INTERVAL 7 DAY > last_day(assigned_local_date)
                                AS dst_transition,
                            CASE
                                WHEN raw_kw = '' THEN 'missing_value'
                                WHEN parsed_kw IS NULL THEN 'nonnumeric_value'
                                WHEN NOT isfinite(parsed_kw) THEN 'nonfinite_value'
                                WHEN parsed_kw < 0 THEN 'negative_value'
                                ELSE ''
                            END AS numeric_quality_flag
                        FROM parsed
                    )
                    SELECT
                        client_id,
                        source_timestamp,
                        assigned_local_date,
                        CASE
                            WHEN numeric_quality_flag = '' THEN parsed_kw
                            ELSE NULL
                        END AS kw,
                        numeric_quality_flag = '' AS numeric_valid,
                        duplicate_timestamp,
                        off_grid_timestamp,
                        dst_transition,
                        concat_ws(
                            '|',
                            nullif(numeric_quality_flag, ''),
                            CASE WHEN source_timestamp IS NULL THEN 'invalid_timestamp' END,
                            CASE WHEN duplicate_timestamp THEN 'duplicate_timestamp' END,
                            CASE WHEN off_grid_timestamp THEN 'off_grid_timestamp' END,
                            CASE WHEN dst_transition THEN 'dst_transition' END
                        ) AS quality_flags
                    FROM flagged
                ) TO {_sql_string(interval_path)} (
                    FORMAT PARQUET,
                    COMPRESSION ZSTD,
                    ROW_GROUP_SIZE 122880
                )
            """
            connection.execute(interval_sql)

            connection.execute(f"""
                CREATE TEMP TABLE day_stage AS
                WITH aggregated AS (
                    SELECT
                        client_id,
                        assigned_local_date AS local_date,
                        count(*)::INTEGER AS interval_rows,
                        count(DISTINCT source_timestamp)::INTEGER AS unique_timestamps,
                        count(kw)::INTEGER AS valid_values,
                        count(*) FILTER (WHERE kw > 0)::INTEGER AS positive_values,
                        max(kw) AS daily_peak_kw,
                        avg(kw) AS mean_kw,
                        sum(kw) * ({interval_minutes} / 60.0) AS energy_kwh_est,
                        bool_or(duplicate_timestamp) AS has_duplicate_timestamp,
                        bool_or(off_grid_timestamp) AS has_off_grid_timestamp,
                        bool_or(dst_transition) AS dst_transition,
                        bool_or(quality_flags LIKE '%missing_value%') AS has_missing_value,
                        bool_or(quality_flags LIKE '%nonnumeric_value%') AS has_nonnumeric_value,
                        bool_or(quality_flags LIKE '%nonfinite_value%') AS has_nonfinite_value,
                        bool_or(quality_flags LIKE '%negative_value%') AS has_negative_value
                    FROM read_parquet({_sql_string(interval_path)})
                    WHERE assigned_local_date IS NOT NULL
                    GROUP BY client_id, assigned_local_date
                ), validity AS (
                    SELECT
                        *,
                        interval_rows = {expected_intervals}
                            AND unique_timestamps = {expected_intervals}
                            AND valid_values = {expected_intervals}
                            AND NOT has_duplicate_timestamp
                            AND NOT has_off_grid_timestamp AS source_valid
                    FROM aggregated
                )
                SELECT
                    *,
                    source_valid AND ({str(not exclude_dst).upper()} OR NOT dst_transition)
                        AS model_valid,
                    valid_values = interval_rows AND positive_values = 0 AS zero_use_day
                FROM validity
            """)

            connection.execute(f"""
                CREATE TEMP TABLE activation AS
                WITH windows AS (
                    SELECT
                        client_id,
                        local_date,
                        min(local_date) OVER client_window AS window_start,
                        count(*) OVER client_window AS window_rows,
                        count(*) FILTER (WHERE model_valid) OVER client_window AS valid_days,
                        count(*) FILTER (WHERE model_valid AND positive_values > 0)
                            OVER client_window AS positive_days
                    FROM day_stage
                    WINDOW client_window AS (
                        PARTITION BY client_id ORDER BY local_date
                        ROWS BETWEEN {activation_window - 1} PRECEDING AND CURRENT ROW
                    )
                )
                SELECT client_id, min(window_start) AS activation_date
                FROM windows
                WHERE local_date <= DATE {_sql_string(training_end)}
                    AND window_rows = {activation_window}
                    AND date_diff('day', window_start, local_date) = {activation_window - 1}
                    AND valid_days = {activation_window}
                    AND positive_days >= {activation_positive}
                GROUP BY client_id
            """)

            connection.execute(f"""
                COPY (
                    WITH clients AS (
                        SELECT DISTINCT client_id FROM day_stage
                    ), training_counts AS (
                        SELECT
                            clients.client_id,
                            activation.activation_date,
                            count(*) FILTER (
                                WHERE day_stage.model_valid
                                    AND day_stage.local_date BETWEEN
                                        DATE {_sql_string(training_start)}
                                        AND DATE {_sql_string(training_end)}
                                    AND day_stage.local_date >= activation.activation_date
                            )::INTEGER AS valid_training_days_after_activation
                        FROM clients
                        LEFT JOIN activation USING (client_id)
                        LEFT JOIN day_stage USING (client_id)
                        GROUP BY clients.client_id, activation.activation_date
                    )
                    SELECT
                        client_id,
                        activation_date,
                        valid_training_days_after_activation,
                        valid_training_days_after_activation >= {minimum_training_days} AS eligible,
                        CASE
                            WHEN activation_date IS NULL
                                THEN 'no_sustained_activation_by_training_end'
                            WHEN valid_training_days_after_activation < {minimum_training_days}
                                THEN 'insufficient_valid_training_days'
                            ELSE ''
                        END AS exclusion_reason
                    FROM training_counts
                    ORDER BY client_id
                ) TO {_sql_string(eligibility_path)} (
                    FORMAT PARQUET,
                    COMPRESSION ZSTD
                )
            """)

            connection.execute(f"""
                COPY (
                    SELECT
                        day_stage.client_id,
                        day_stage.local_date,
                        day_stage.interval_rows,
                        day_stage.unique_timestamps,
                        day_stage.valid_values,
                        day_stage.positive_values,
                        day_stage.daily_peak_kw,
                        day_stage.mean_kw,
                        day_stage.energy_kwh_est,
                        day_stage.source_valid,
                        day_stage.model_valid,
                        day_stage.zero_use_day,
                        day_stage.dst_transition,
                        eligibility.activation_date,
                        day_stage.local_date >= eligibility.activation_date AS post_activation,
                        eligibility.eligible AS client_eligible,
                        eligibility.exclusion_reason,
                        concat_ws(
                            '|',
                            CASE WHEN day_stage.interval_rows != {expected_intervals}
                                THEN 'unexpected_interval_count' END,
                            CASE WHEN day_stage.has_duplicate_timestamp
                                THEN 'duplicate_timestamp' END,
                            CASE WHEN day_stage.has_off_grid_timestamp
                                THEN 'off_grid_timestamp' END,
                            CASE WHEN day_stage.has_missing_value THEN 'missing_value' END,
                            CASE WHEN day_stage.has_nonnumeric_value THEN 'nonnumeric_value' END,
                            CASE WHEN day_stage.has_nonfinite_value THEN 'nonfinite_value' END,
                            CASE WHEN day_stage.has_negative_value THEN 'negative_value' END,
                            CASE WHEN day_stage.dst_transition THEN 'dst_transition' END,
                            CASE WHEN day_stage.zero_use_day THEN 'zero_use_day' END
                        ) AS quality_flags
                    FROM day_stage
                    JOIN read_parquet({_sql_string(eligibility_path)}) AS eligibility
                        USING (client_id)
                    ORDER BY day_stage.client_id, day_stage.local_date
                ) TO {_sql_string(client_day_path)} (
                    FORMAT PARQUET,
                    COMPRESSION ZSTD,
                    ROW_GROUP_SIZE 122880
                )
            """)

            counts_row = connection.execute(f"""
                SELECT
                    (SELECT count(*) FROM read_parquet({_sql_string(interval_path)})),
                    (SELECT count(*) FROM read_parquet({_sql_string(client_day_path)})),
                    (SELECT count(*) FROM read_parquet({_sql_string(client_day_path)})
                        WHERE source_valid),
                    (SELECT count(*) FROM read_parquet({_sql_string(client_day_path)})
                        WHERE model_valid),
                    (SELECT count(*) FROM read_parquet({_sql_string(eligibility_path)})
                        WHERE eligible),
                    (SELECT count(*) FROM read_parquet({_sql_string(eligibility_path)})
                        WHERE NOT eligible),
                    (SELECT count(*) FROM read_parquet({_sql_string(interval_path)})
                        WHERE duplicate_timestamp),
                    (SELECT count(*) FROM read_parquet({_sql_string(interval_path)})
                        WHERE source_timestamp IS NULL),
                    (SELECT count(*) FROM read_parquet({_sql_string(interval_path)})
                        WHERE NOT numeric_valid),
                    (SELECT count(*) FROM read_parquet({_sql_string(client_day_path)})
                        WHERE zero_use_day)
            """).fetchone()
            assert counts_row is not None
            counts = dict(zip(
                (
                    "interval_rows",
                    "client_day_rows",
                    "source_valid_client_days",
                    "model_valid_client_days",
                    "eligible_clients",
                    "excluded_clients",
                    "duplicate_interval_rows",
                    "invalid_timestamp_interval_rows",
                    "invalid_numeric_interval_values",
                    "zero_use_client_days",
                ),
                map(int, counts_row),
            ))
            exclusion_counts = {
                reason or "eligible": int(count)
                for reason, count in connection.execute(f"""
                    SELECT exclusion_reason, count(*)
                    FROM read_parquet({_sql_string(eligibility_path)})
                    GROUP BY exclusion_reason
                    ORDER BY exclusion_reason
                """).fetchall()
            }
            coverage_row = connection.execute(f"""
                SELECT
                    min(source_timestamp), max(source_timestamp),
                    min(assigned_local_date), max(assigned_local_date)
                FROM read_parquet({_sql_string(interval_path)})
            """).fetchone()
            assert coverage_row is not None
            coverage = {
                "first_source_timestamp": coverage_row[0].isoformat(sep=" "),
                "last_source_timestamp": coverage_row[1].isoformat(sep=" "),
                "first_assigned_local_date": coverage_row[2].isoformat(),
                "last_assigned_local_date": coverage_row[3].isoformat(),
            }
            if counts["duplicate_interval_rows"]:
                raise ValueError(
                    "Interval key is not unique: source contains duplicate timestamps"
                )
            if counts["invalid_timestamp_interval_rows"]:
                raise ValueError("Cannot build keyed interval data from invalid timestamps")
            schemas = {
                "interval_readings": _schema(connection, interval_path),
                "client_day": _schema(connection, client_day_path),
                "client_eligibility": _schema(connection, eligibility_path),
            }
        finally:
            connection.close()

    _create_catalog(output_dir)
    finished = datetime.now(timezone.utc)
    config_snapshot = json.dumps(config, sort_keys=True, separators=(",", ":"))
    reconciliation = _audit_reconciliation(audit_dir, counts)
    git_state = _git_state(project_root)
    manifest: dict[str, Any] = {
        "status": "complete" if reconciliation["status"] != "mismatch" else "count_mismatch",
        "generated_at_utc": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "source": {
            "path": str(source.resolve()),
            "bytes": source.stat().st_size,
            "sha256": source_hash,
            "client_columns": len(clients),
            **member_info,
        },
        "run": {
            "package_version": __version__,
            "python": platform.python_version(),
            "dependencies": _package_versions(),
            "configuration": config,
            "configuration_sha256": hashlib.sha256(config_snapshot.encode()).hexdigest(),
            "builder_sha256": _sha256(Path(__file__)),
            **git_state,
            "random_seed": config.get("run", {}).get("random_seed"),
        },
        "outputs": {
            name: {
                "path": str((output_dir / filename).resolve()),
                "bytes": (output_dir / filename).stat().st_size,
                "sha256": _sha256(output_dir / filename),
                "schema": schemas.get(name),
            }
            for name, filename in (
                ("interval_readings", "interval_readings.parquet"),
                ("client_day", "client_day.parquet"),
                ("client_eligibility", "client_eligibility.parquet"),
                ("catalog", "catalog.duckdb"),
            )
        },
        "counts": counts,
        "exclusion_counts": exclusion_counts,
        "coverage": coverage,
        "audit_reconciliation": reconciliation,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if reconciliation["status"] == "mismatch":
        raise ValueError(f"Processed counts do not match audit: {reconciliation['differences']}")
    return manifest
