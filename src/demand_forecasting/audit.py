"""Stream the UCI wide table and write a source-level quality audit."""

from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import platform
from statistics import median
from typing import Iterator, TextIO
import zipfile

from . import __version__


@dataclass(slots=True)
class DayRecord:
    day: date
    source_valid: bool
    model_valid: bool
    positive: bool
    peak_kw: float | None


def assigned_day(timestamp: datetime) -> date:
    """UCI labels the end of an interval; midnight closes the prior day."""
    if timestamp.hour == 0 and timestamp.minute == 0 and timestamp.second == 0:
        return timestamp.date() - timedelta(days=1)
    return timestamp.date()


def dst_transition(day: date) -> str | None:
    if day.month not in (3, 10):
        return None
    last = date(day.year, day.month + 1, 1) - timedelta(days=1)
    last_sunday = last - timedelta(days=(last.weekday() + 1) % 7)
    if day != last_sunday:
        return None
    return "spring" if day.month == 3 else "fall"


def activation_day(
    records: list[DayRecord], window_days: int, positive_days: int, training_end: date
) -> date | None:
    window: deque[DayRecord] = deque(maxlen=window_days)
    for record in records:
        if record.day > training_end:
            break
        window.append(record)
        if (len(window) == window_days
                and (window[-1].day - window[0].day).days == window_days - 1
                and all(item.model_valid for item in window)
                and sum(item.positive for item in window) >= positive_days):
            return window[0].day
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _source_text(path: Path, member: str) -> Iterator[tuple[TextIO, dict[str, int | str]]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(member)
            with archive.open(info) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                yield text, {"archive_member": member, "member_bytes": info.file_size, "member_crc32": f"{info.CRC:08x}"}
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as text:
            yield text, {"archive_member": "", "member_bytes": path.stat().st_size, "member_crc32": ""}


def _percentile_nearest_rank(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(probability * len(ordered)) - 1)]


def audit_source(source: Path, output_dir: Path, config: dict) -> dict:
    """Audit source structure and quality without writing a modeled dataset."""
    if not source.is_file():
        raise FileNotFoundError(f"Source not found: {source}")
    audit_cfg = config["audit"]
    study = config["study"]
    expected_per_day = int(audit_cfg["expected_intervals_per_day"])
    interval_minutes = int(audit_cfg["interval_minutes"])
    training_start = date.fromisoformat(study["training_start"])
    training_end = date.fromisoformat(study["training_end"])
    output_dir.mkdir(parents=True, exist_ok=True)

    source_counts: Counter[str] = Counter()
    quality_counts: Counter[str] = Counter()
    day_count_distribution: Counter[int] = Counter()
    error_samples: list[str] = []
    cadence_samples: list[str] = []
    dst_hour_values: dict[str, Counter[str]] = {}
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    previous_ts: datetime | None = None
    seen_timestamps: set[datetime] = set()
    current_day: date | None = None
    day_rows = 0
    day_duplicates = 0
    day_offgrid = 0
    records: dict[str, list[DayRecord]] = {}

    with _source_text(source, config["source"]["member"]) as (handle, member_info), \
            (output_dir / "day_quality.csv").open("w", newline="", encoding="utf-8") as quality_file, \
            (output_dir / "calendar_day_quality.csv").open("w", newline="", encoding="utf-8") as calendar_file:
        reader = csv.reader(handle, delimiter=";")
        header = next(reader)
        if not header or len(header) < 2:
            raise ValueError("Expected timestamp column and at least one client column")
        clients = header[1:]
        if len(set(clients)) != len(clients) or any(not client for client in clients):
            raise ValueError("Client column names must be unique and nonempty")
        records = {client: [] for client in clients}
        client_count = len(clients)
        quality_writer = csv.writer(quality_file)
        calendar_writer = csv.writer(calendar_file)
        quality_writer.writerow(["client_id", "local_date", "interval_rows", "valid_values", "positive_values", "missing_values", "nonnumeric_values", "nonfinite_values", "negative_values", "daily_peak_kw", "source_valid", "model_valid", "flags"])
        calendar_writer.writerow(["local_date", "interval_rows", "unique_timestamps", "duplicate_rows", "off_grid_rows", "dst_transition", "flags"])

        valid_values = [0] * client_count
        positive_values = [0] * client_count
        missing_values = [0] * client_count
        nonnumeric_values = [0] * client_count
        nonfinite_values = [0] * client_count
        negative_values = [0] * client_count
        peaks = [0.0] * client_count

        def flush_day() -> None:
            nonlocal day_rows, day_duplicates, day_offgrid
            if current_day is None:
                return
            transition = dst_transition(current_day)
            calendar_flags: list[str] = []
            if day_rows != expected_per_day:
                calendar_flags.append("unexpected_interval_count")
            if day_duplicates:
                calendar_flags.append("duplicate_timestamp")
            if day_offgrid:
                calendar_flags.append("off_grid_timestamp")
            if transition:
                calendar_flags.append(f"{transition}_dst")
            calendar_writer.writerow([current_day.isoformat(), day_rows, day_rows - day_duplicates, day_duplicates, day_offgrid, transition or "", "|".join(calendar_flags)])
            source_counts["calendar_days"] += 1
            day_count_distribution[day_rows] += 1
            for flag in calendar_flags:
                quality_counts[f"calendar:{flag}"] += 1

            complete_day = day_rows == expected_per_day and day_duplicates == 0 and day_offgrid == 0
            for index, client in enumerate(clients):
                flags = list(calendar_flags)
                if missing_values[index]:
                    flags.append("missing_value")
                if nonnumeric_values[index]:
                    flags.append("nonnumeric_value")
                if nonfinite_values[index]:
                    flags.append("nonfinite_value")
                if negative_values[index]:
                    flags.append("negative_value")
                if valid_values[index] == day_rows and positive_values[index] == 0:
                    flags.append("zero_use_day")
                source_valid = complete_day and valid_values[index] == expected_per_day
                model_valid = source_valid and (not transition or not audit_cfg["exclude_dst_transition_days"])
                peak = peaks[index] if valid_values[index] else None
                quality_writer.writerow([client, current_day.isoformat(), day_rows, valid_values[index], positive_values[index], missing_values[index], nonnumeric_values[index], nonfinite_values[index], negative_values[index], "" if peak is None else peak, int(source_valid), int(model_valid), "|".join(flags)])
                records[client].append(DayRecord(current_day, source_valid, model_valid, positive_values[index] > 0, peak))
                source_counts["client_day_rows"] += 1
                source_counts["source_valid_client_days"] += source_valid
                source_counts["model_valid_client_days"] += model_valid
                for flag in flags:
                    quality_counts[f"client_day:{flag}"] += 1

        for line_number, row in enumerate(reader, start=2):
            source_counts["physical_data_rows"] += 1
            if len(row) != client_count + 1:
                source_counts["wrong_width_rows"] += 1
                if len(error_samples) < 20:
                    error_samples.append(f"line {line_number}: {len(row)} columns, expected {client_count + 1}")
                continue
            try:
                timestamp = datetime.fromisoformat(row[0])
            except ValueError:
                source_counts["invalid_timestamp_rows"] += 1
                if len(error_samples) < 20:
                    error_samples.append(f"line {line_number}: invalid timestamp {row[0]!r}")
                continue
            day = assigned_day(timestamp)
            if current_day is not None and day < current_day:
                raise ValueError(f"Source is not sorted by assigned local day at line {line_number}")
            if current_day != day:
                flush_day()
                current_day = day
                day_rows = day_duplicates = day_offgrid = 0
                valid_values = [0] * client_count
                positive_values = [0] * client_count
                missing_values = [0] * client_count
                nonnumeric_values = [0] * client_count
                nonfinite_values = [0] * client_count
                negative_values = [0] * client_count
                peaks = [0.0] * client_count
            day_rows += 1
            source_counts["timestamp_rows"] += 1
            if timestamp in seen_timestamps:
                source_counts["duplicate_timestamp_rows"] += 1
                day_duplicates += 1
            seen_timestamps.add(timestamp)
            if timestamp.minute % interval_minutes or timestamp.second or timestamp.microsecond:
                source_counts["off_grid_timestamp_rows"] += 1
                day_offgrid += 1
            if previous_ts is not None:
                delta_minutes = (timestamp - previous_ts).total_seconds() / 60
                if delta_minutes != interval_minutes:
                    source_counts["cadence_anomalies"] += 1
                    if delta_minutes > interval_minutes and delta_minutes % interval_minutes == 0:
                        source_counts["missing_timestamp_slots"] += int(delta_minutes / interval_minutes) - 1
                    if len(cadence_samples) < 20:
                        cadence_samples.append(f"{previous_ts.isoformat(sep=' ')} -> {timestamp.isoformat(sep=' ')} ({delta_minutes:g} minutes)")
            previous_ts = timestamp
            if first_ts is None:
                first_ts = timestamp
            last_ts = timestamp

            is_dst_hour = dst_transition(timestamp.date()) is not None and timestamp.hour == 1
            if is_dst_hour:
                dst_hour_values.setdefault(timestamp.date().isoformat(), Counter())["rows"] += 1
            for index, raw_value in enumerate(row[1:]):
                source_counts["interval_values"] += 1
                if raw_value == "":
                    missing_values[index] += 1
                    source_counts["missing_values"] += 1
                    continue
                try:
                    value = float(raw_value.replace(",", "."))
                except ValueError:
                    nonnumeric_values[index] += 1
                    source_counts["nonnumeric_values"] += 1
                    continue
                if not math.isfinite(value):
                    nonfinite_values[index] += 1
                    source_counts["nonfinite_values"] += 1
                    continue
                if value < 0:
                    negative_values[index] += 1
                    source_counts["negative_values"] += 1
                    continue
                valid_values[index] += 1
                source_counts["accepted_values"] += 1
                if value > 0:
                    positive_values[index] += 1
                    source_counts["positive_values"] += 1
                    if value > peaks[index]:
                        peaks[index] = value
                    if is_dst_hour:
                        dst_hour_values[timestamp.date().isoformat()]["positive_values"] += 1
                else:
                    source_counts["zero_values"] += 1
        flush_day()

    if not source_counts["timestamp_rows"]:
        raise ValueError("Source contains no parseable timestamp rows")

    exclusion_counts: Counter[str] = Counter()
    eligible_training_days = 0
    all_valid_training_days = 0
    postactivation_training_days = 0
    with (output_dir / "client_summary.csv").open("w", newline="", encoding="utf-8") as client_file, \
            (output_dir / "exclusion_ledger.csv").open("w", newline="", encoding="utf-8") as exclusion_file, \
            (output_dir / "extreme_day_review.csv").open("w", newline="", encoding="utf-8") as extreme_file:
        client_writer = csv.writer(client_file)
        exclusion_writer = csv.writer(exclusion_file)
        extreme_writer = csv.writer(extreme_file)
        client_writer.writerow(["client_id", "first_positive_date", "last_positive_date", "activation_date", "valid_training_days_after_activation", "longest_zero_day_run", "eligible", "exclusion_reason", "largest_daily_peak_kw"])
        exclusion_writer.writerow(["client_id", "reason", "valid_training_days_after_activation"])
        extreme_writer.writerow(["client_id", "local_date", "daily_peak_kw", "training_peak_median_kw", "training_peak_p99_kw", "review_cutoff_kw"])
        for client, client_records in records.items():
            positive_dates = [record.day for record in client_records if record.positive]
            activated = activation_day(
                client_records,
                int(audit_cfg["activation_window_days"]),
                int(audit_cfg["activation_positive_days"]),
                training_end,
            )
            all_valid_training_days += sum(training_start <= record.day <= training_end and record.model_valid
                                           for record in client_records)
            training_records = [record for record in client_records
                                if activated is not None and activated <= record.day
                                and training_start <= record.day <= training_end and record.model_valid]
            valid_training_days = len(training_records)
            postactivation_training_days += valid_training_days
            eligible = valid_training_days >= int(study["minimum_valid_training_days"])
            reason = "" if eligible else ("no_sustained_activation_by_training_end" if activated is None else "insufficient_valid_training_days")
            exclusion_counts[reason or "eligible"] += 1
            if not eligible:
                exclusion_writer.writerow([client, reason, valid_training_days])
            else:
                eligible_training_days += valid_training_days
            zero_run = longest_run = 0
            for record in client_records:
                zero_run = zero_run + 1 if record.source_valid and not record.positive else 0
                longest_run = max(longest_run, zero_run)
            largest_peak = max((record.peak_kw or 0 for record in client_records), default=0)
            client_writer.writerow([client, positive_dates[0].isoformat() if positive_dates else "", positive_dates[-1].isoformat() if positive_dates else "", activated.isoformat() if activated else "", valid_training_days, longest_run, int(eligible), reason, largest_peak])
            train_peaks = [record.peak_kw for record in training_records if record.peak_kw is not None and record.peak_kw > 0]
            if not train_peaks:
                continue
            train_median = median(train_peaks)
            train_p99 = _percentile_nearest_rank(train_peaks, 0.99)
            review_cutoff = max(float(audit_cfg["extreme_median_multiplier"]) * train_median,
                                float(audit_cfg["extreme_p99_multiplier"]) * train_p99)
            for record in client_records:
                if activated is not None and record.day >= activated and record.peak_kw is not None and record.peak_kw > review_cutoff:
                    extreme_writer.writerow([client, record.day.isoformat(), record.peak_kw, train_median, train_p99, review_cutoff])
                    source_counts["extreme_days_for_review"] += 1

    summary = {
        "status": "source_audit_complete; model construction not started",
        "run": {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "python": platform.python_version(), "package_version": __version__,
                "random_seed": config["run"]["random_seed"], "config": config},
        "source": {"path": str(source.resolve()), "sha256": _sha256(source), "bytes": source.stat().st_size,
                   **member_info, "client_columns": len(records),
                   "first_timestamp": first_ts.isoformat(sep=" ") if first_ts else None,
                   "last_timestamp": last_ts.isoformat(sep=" ") if last_ts else None,
                   "first_assigned_day": assigned_day(first_ts).isoformat() if first_ts else None,
                   "last_assigned_day": assigned_day(last_ts).isoformat() if last_ts else None,
                   **dict(source_counts)},
        "daily_interval_count_distribution": {str(key): value for key, value in sorted(day_count_distribution.items())},
        "dst_hour_observations": {key: dict(value) for key, value in sorted(dst_hour_values.items())},
        "quality_flag_counts": dict(sorted(quality_counts.items())),
        "eligibility": {"eligible_clients": exclusion_counts["eligible"],
                        "all_valid_training_client_days": all_valid_training_days,
                        "preactivation_training_client_days": all_valid_training_days - postactivation_training_days,
                        "postactivation_training_client_days": postactivation_training_days,
                        "ineligible_postactivation_training_client_days": postactivation_training_days - eligible_training_days,
                        "eligible_training_client_days": eligible_training_days,
                        "exclusion_counts": dict(exclusion_counts)},
        "error_samples": error_samples,
        "cadence_samples": cadence_samples,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary
