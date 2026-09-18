"""Shared validation and aggregation for performance evidence."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

REQUIRED_TELEMETRY_FIELDS = {
    "timestamp",
    "level",
    "service",
    "source",
    "run_id",
    "sequence_no",
    "event",
    "message",
    "details",
}
ALLOWED_SOURCES = {"simulation", "hardware"}
MEMORY_UNITS = {
    "b": 1,
    "kb": 1000,
    "kib": 1024,
    "mb": 1000**2,
    "mib": 1024**2,
    "gb": 1000**3,
    "gib": 1024**3,
}


# Reviewed upper bounds. A value above these is not a slow robot, it is a broken
# producer or a fabricated record, and either way it must not become a percentile.
MAX_DURATION_MS = 24 * 60 * 60 * 1000.0
MAX_CPU_PERCENT = 100.0 * 1024
MAX_MEMORY_BYTES = 1 << 50  # 1 PiB, above any plausible container limit

# A failure is counted from the record itself, never inferred from a missing
# stage: a dropped producer looks like a quiet run, and a quiet run is not a
# passing one. Both an error level and an explicitly terminal failure event
# count, so a producer that logs a failure at INFO is still counted once.
FAILURE_LEVELS = frozenset({"ERROR", "CRITICAL"})
FAILURE_EVENTS = frozenset({"stage_failed", "fault", "policy_violation", "run_failed", "task_failed"})

# Identity fields must be non-empty strings so grouping keys stay hashable and
# comparable; `details` is the only free-form object and is validated per event.
_TELEMETRY_STRING_FIELDS = ("timestamp", "level", "service", "source", "run_id", "event", "message")
_TELEMETRY_FINITE_FIELDS = (("duration_ms", MAX_DURATION_MS),)


def _finite_bounded(value: object, label: str, maximum: float) -> float:
    """Return a finite, non-negative float no greater than `maximum`.

    `bool` is rejected explicitly: it is an `int` subclass, so `type(value) in
    (int, float)` already excludes it, but accepting it here would silently turn
    a flag into a measurement.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeError(f"{label} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise RuntimeError(f"{label} must be a finite number")
    if converted < 0:
        raise RuntimeError(f"{label} must not be negative")
    if converted > maximum:
        raise RuntimeError(f"{label} must not exceed {maximum}")
    return converted


def software_environment(commit: str | None = None) -> dict[str, str]:
    """Return the stable environment identity shared by software reports.

    `commit` is optional so existing callers keep the three-field identity; when
    supplied it must be a git object name, because a report that claims a
    revision it cannot name is not reproducible evidence.
    """
    environment = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
    }
    if commit is not None:
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,40}", commit):
            raise ValueError("commit must be a 7-40 character lowercase hex object name")
        environment["commit"] = commit
    return environment


def _failure_counts(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Count failures per source from the records themselves.

    A record counts once even when it carries both a failure level and a failure
    event, so one incident is not reported as two failures.
    """
    totals: dict[str, int] = defaultdict(int)
    failures: dict[str, int] = defaultdict(int)
    by_event: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for record in records:
        source = record["source"]
        totals[source] += 1
        level = record["level"]
        event = record["event"]
        failed = level in FAILURE_LEVELS or event in FAILURE_EVENTS
        if not failed:
            continue
        failures[source] += 1
        by_event[source][event] += 1
    return {
        source: {
            "total": totals[source],
            "failed": failures[source],
            "failure_rate": failures[source] / totals[source],
            "by_event": dict(sorted(by_event[source].items())),
        }
        for source in sorted(totals)
    }


def code_revision() -> str | None:
    """Return the current git object name, or None outside a git checkout.

    A missing revision is recorded as absent rather than invented, because a
    fabricated revision is worse evidence than an honest gap.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision if re.fullmatch(r"[0-9a-f]{7,40}", revision) else None


def write_json_report(path: Path, report: dict[str, Any]) -> None:
    """Serialize a report atomically and without non-standard JSON tokens.

    `allow_nan=False` turns a leaked NaN/Infinity into an error at write time
    instead of emitting a file that is not valid JSON. The rename means a report
    is either the previous complete file or the new complete file, never a
    half-written one left behind by a failure in the middle of serialization.
    """
    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    if not 0 < quantile <= 1:
        raise ValueError("quantile must be in (0, 1]")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hardware_log_hashes(paths: list[Path]) -> dict[str, str]:
    names = [path.name for path in paths]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise RuntimeError(f"hardware evidence log names must be unique: {', '.join(duplicates)}")
    return {path.name: file_sha256(path) for path in paths}


def _telemetry_paths(inputs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        if item.is_dir():
            paths.extend(sorted(item.glob("*.jsonl")))
        elif item.is_file():
            paths.append(item)
        else:
            raise RuntimeError(f"telemetry input does not exist: {item}")
    if not paths:
        raise RuntimeError("no telemetry JSONL files were found")
    return paths


def load_telemetry(inputs: list[Path]) -> tuple[list[dict[str, Any]], list[Path]]:
    records: list[dict[str, Any]] = []
    paths = _telemetry_paths(inputs)
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSON at {path}:{line_number}") from exc
            if not isinstance(record, dict) or not REQUIRED_TELEMETRY_FIELDS.issubset(record):
                raise RuntimeError(f"invalid telemetry fields at {path}:{line_number}")
            for field in _TELEMETRY_STRING_FIELDS:
                value = record[field]
                if not isinstance(value, str) or not value.strip():
                    raise RuntimeError(f"telemetry {field} must be a non-empty string at {path}:{line_number}")
            if record["source"] not in ALLOWED_SOURCES:
                raise RuntimeError(f"unknown telemetry source at {path}:{line_number}")
            if type(record["sequence_no"]) is not int or record["sequence_no"] < 0:
                raise RuntimeError(f"invalid telemetry sequence at {path}:{line_number}")
            if not isinstance(record["details"], dict):
                raise RuntimeError(f"telemetry details must be an object at {path}:{line_number}")
            # A non-finite duration would be serialized as bare NaN/Infinity,
            # which is not JSON and silently poisons every percentile it enters.
            for field, maximum in _TELEMETRY_FINITE_FIELDS:
                if field in record["details"] and record["details"][field] is not None:
                    _finite_bounded(record["details"][field], f"telemetry {field} at {path}:{line_number}", maximum)
            records.append(record)
    sequences: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for record in records:
        sequences[(record["source"], record["service"], record["run_id"])].append(record["sequence_no"])
    for key, values in sequences.items():
        if values != list(range(len(values))):
            raise RuntimeError(f"non-contiguous telemetry sequence for {key}")
    return records, paths


def validate_hardware_evidence(paths: list[Path], evidence_path: Path | None) -> dict[str, Any] | None:
    if evidence_path is None:
        return None
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    required = {"evidence_kind", "hardware_id", "operator", "captured_at", "logs"}
    if not isinstance(evidence, dict) or not required.issubset(evidence):
        raise RuntimeError("hardware evidence manifest is missing required fields")
    if evidence["evidence_kind"] != "operator_attested_real_hardware":
        raise RuntimeError("hardware evidence manifest has the wrong evidence_kind")
    expected = evidence["logs"]
    if not isinstance(expected, dict):
        raise RuntimeError("hardware evidence logs must map file names to SHA-256 digests")
    actual = hardware_log_hashes(paths)
    if actual != expected:
        raise RuntimeError("hardware evidence log hashes do not match the analyzed files")
    return evidence


def summarize_telemetry(
    records: list[dict[str, Any]],
    *,
    hardware_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stage_values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        if record["event"] != "stage_completed":
            continue
        stage = record["details"].get("stage")
        duration_ms = record["details"].get("duration_ms")
        if not isinstance(stage, str) or not stage:
            raise RuntimeError("stage_completed records require a non-empty stage")
        stage_values[record["source"]][stage].append(
            _finite_bounded(duration_ms, "stage_completed duration_ms", MAX_DURATION_MS)
        )
    if not stage_values:
        raise RuntimeError("telemetry contains no stage_completed samples")
    if "hardware" in stage_values and hardware_evidence is None:
        raise RuntimeError("hardware telemetry requires a hash-verified operator evidence manifest")
    sources: dict[str, Any] = {}
    for source, stages in sorted(stage_values.items()):
        sources[source] = {
            "stages": {
                stage: {
                    "samples": len(values),
                    "p50_ms": percentile(values, 0.50),
                    "p95_ms": percentile(values, 0.95),
                    "p99_ms": percentile(values, 0.99),
                    "max_ms": max(values),
                    "unit": "ms",
                }
                for stage, values in sorted(stages.items())
            },
            "failures": _failure_counts([record for record in records if record["source"] == source]),
        }
    return {
        "schema_version": 1,
        "generated_by": "tools/scripts/analyze_telemetry.py",
        "environment": software_environment(),
        "revision": code_revision(),
        "record_count": len(records),
        "sources": sources,
        "hardware_evidence": (
            {
                "verified": True,
                "hardware_id": hardware_evidence["hardware_id"],
                "operator": hardware_evidence["operator"],
                "captured_at": hardware_evidence["captured_at"],
            }
            if hardware_evidence
            else {"verified": False}
        ),
    }


def parse_memory_bytes(value: str) -> int:
    normalized = value.strip().replace(" ", "")
    split_at = next((index for index, char in enumerate(normalized) if char.isalpha()), len(normalized))
    number = normalized[:split_at]
    unit = normalized[split_at:].lower() or "b"
    if not number or unit not in MEMORY_UNITS:
        raise ValueError(f"unsupported memory value: {value}")
    try:
        magnitude = float(number)
    except ValueError:
        raise ValueError(f"unsupported memory value: {value}") from None
    if not math.isfinite(magnitude):
        raise ValueError(f"memory value must be finite: {value}")
    if magnitude < 0:
        raise ValueError(f"memory value must not be negative: {value}")
    bytes_value = round(magnitude * MEMORY_UNITS[unit])
    if bytes_value > MAX_MEMORY_BYTES:
        raise ValueError(f"memory value is above the {MAX_MEMORY_BYTES} byte bound: {value}")
    return bytes_value


def summarize_resource_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, float]]] = defaultdict(list)
    for sample in samples:
        name = sample.get("Name") or sample.get("Container")
        if not isinstance(name, str) or not name:
            raise RuntimeError("docker stats sample has no container name")
        try:
            raw_cpu = float(str(sample["CPUPerc"]).rstrip("%"))
            memory = parse_memory_bytes(str(sample["MemUsage"]).split("/")[0])
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"invalid docker stats sample for {name}") from exc
        cpu = _finite_bounded(raw_cpu, f"docker stats CPU percent for {name}", MAX_CPU_PERCENT)
        grouped[name].append({"cpu_percent": cpu, "memory_bytes": float(memory)})
    return {
        name: {
            "samples": len(values),
            "cpu_percent_p50": percentile([item["cpu_percent"] for item in values], 0.50),
            "cpu_percent_p95": percentile([item["cpu_percent"] for item in values], 0.95),
            "cpu_percent_max": max(item["cpu_percent"] for item in values),
            "memory_bytes_p50": percentile([item["memory_bytes"] for item in values], 0.50),
            "memory_bytes_p95": percentile([item["memory_bytes"] for item in values], 0.95),
            "memory_bytes_max": max(item["memory_bytes"] for item in values),
        }
        for name, values in sorted(grouped.items())
    }
