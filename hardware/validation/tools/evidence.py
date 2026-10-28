"""Register and verify hardware-validation evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from workbench_task_utils import exclusive_file_lock

REQUIRED_FIELDS = {
    "evidence_id",
    "scenario_id",
    "unit_id",
    "hardware_revision",
    "config_hash",
    "operator",
    "reviewer",
    "captured_at",
    "evidence_kind",
    "instrument_refs",
    "calibration_refs",
    "raw_files",
    "result",
}
RESULTS = {"PASS", "FAIL", "HOLD"}
EVIDENCE_KINDS = {"simulation", "bench", "physical"}

# A physical PASS is a measurement of a serialized unit. The register used to accept
# any non-empty file whose digest matched, so a file containing "not a measurement"
# plus a self-written instrument name was enough (issue #444). These are the fields
# that make the record checkable rather than self-asserted.
PHYSICAL_REQUIRED_FIELDS = {
    "instrument_serials",
    "calibration_due",
    "measurement_summary",
}
# Files a physical record has to carry, one per measurement it claims.
PHYSICAL_RAW_KINDS = {"capture", "log"}


class EvidenceError(ValueError):
    """Raised when evidence cannot be trusted."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_register(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"invalid evidence JSON at line {line_number}") from exc
        if not isinstance(record, dict):
            raise EvidenceError(f"evidence at line {line_number} must be an object")
        records.append(record)
    return records


def validate_record(
    record: dict,
    *,
    root: Path,
    scenarios: set[str],
    units: dict[str, tuple[str, str]],
) -> None:
    missing = REQUIRED_FIELDS - set(record)
    if missing:
        raise EvidenceError(f"evidence is missing fields: {sorted(missing)}")
    if record["scenario_id"] not in scenarios:
        raise EvidenceError(f"unknown scenario_id: {record['scenario_id']}")
    expected = units.get(record["unit_id"])
    if expected is None:
        raise EvidenceError(f"unknown unit_id: {record['unit_id']}")
    if (record["hardware_revision"], record["config_hash"]) != expected:
        raise EvidenceError(f"revision mismatch for {record['unit_id']}")
    if not isinstance(record["config_hash"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", record["config_hash"]):
        raise EvidenceError("config_hash must be a SHA-256 hex digest")
    if record["evidence_kind"] not in EVIDENCE_KINDS:
        raise EvidenceError("invalid evidence_kind")
    if record["result"] not in RESULTS:
        raise EvidenceError("invalid result")
    for field in ("evidence_id", "operator", "reviewer", "captured_at"):
        if not isinstance(record[field], str) or not record[field]:
            raise EvidenceError(f"invalid {field}")
    try:
        captured_at = datetime.fromisoformat(record["captured_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("captured_at must be an ISO-8601 timestamp") from exc
    if captured_at.utcoffset() != UTC.utcoffset(captured_at):
        raise EvidenceError("captured_at must be UTC")
    for field in ("instrument_refs", "calibration_refs"):
        if (
            not isinstance(record[field], list)
            or not record[field]
            or any(not isinstance(value, str) or not value for value in record[field])
        ):
            raise EvidenceError(f"invalid {field}")
    if not isinstance(record["raw_files"], dict) or not record["raw_files"]:
        raise EvidenceError("raw_files must not be empty")
    if record["evidence_kind"] == "physical":
        _validate_physical_record(record)
    root = root.resolve()
    for name, expected_hash in record["raw_files"].items():
        if not isinstance(name, str) or not isinstance(expected_hash, str):
            raise EvidenceError("raw_files must map paths to SHA-256 strings")
        path = (root / name).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise EvidenceError(f"raw evidence path escapes repository: {name}") from exc
        if not path.is_file():
            raise EvidenceError(f"raw evidence file is missing: {name}")
        if sha256(path) != expected_hash:
            raise EvidenceError(f"raw evidence hash mismatch: {name}")


def _validate_physical_record(record: dict) -> None:
    """Refuse a physical claim that carries no measurement metadata.

    ``instrument_refs`` and ``calibration_refs`` were free-form strings, so they
    proved nothing. A physical record now has to name the serial of every
    instrument it used, the calibration expiry that was in force, and a summary
    whose numbers the reviewer can check against the raw file.
    """

    missing = PHYSICAL_REQUIRED_FIELDS - set(record)
    if missing:
        raise EvidenceError(f"physical evidence is missing fields: {sorted(missing)}")

    serials = record["instrument_serials"]
    if (
        not isinstance(serials, list)
        or not serials
        or any(not isinstance(value, str) or not value.strip() for value in serials)
    ):
        raise EvidenceError("instrument_serials must list the serial of every instrument used")
    if len(set(serials)) != len(serials):
        raise EvidenceError("instrument_serials must not repeat a serial")

    # One serial per instrument reference: the record has to say which physical
    # instrument produced the numbers, not only which model was on the bench.
    if len(serials) != len(record["instrument_refs"]):
        raise EvidenceError("instrument_serials must name one serial per instrument_ref")

    due = record["calibration_due"]
    if not isinstance(due, dict) or set(due) != set(serials):
        raise EvidenceError("calibration_due must give an expiry for every instrument serial")
    captured_at = datetime.fromisoformat(record["captured_at"].replace("Z", "+00:00"))
    for serial, expiry in due.items():
        if not isinstance(expiry, str):
            raise EvidenceError(f"calibration_due[{serial}] must be an ISO-8601 date")
        try:
            due_at = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceError(f"calibration_due[{serial}] must be an ISO-8601 date") from exc
        if due_at.tzinfo is None:
            raise EvidenceError(f"calibration_due[{serial}] must state a timezone")
        if due_at <= captured_at:
            raise EvidenceError(f"calibration for {serial} had expired before the measurement was captured")

    summary = record["measurement_summary"]
    if not isinstance(summary, dict) or not summary:
        raise EvidenceError("measurement_summary must record what was measured")
    for name, value in summary.items():
        if not isinstance(name, str) or not name.strip():
            raise EvidenceError("measurement_summary keys must name a quantity")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise EvidenceError(f"measurement_summary[{name}] must be a number")

    kinds = record.get("raw_file_kinds")
    if not isinstance(kinds, dict) or set(kinds) != set(record["raw_files"]):
        raise EvidenceError("raw_file_kinds must classify every raw file")
    for name, kind in kinds.items():
        if kind not in PHYSICAL_RAW_KINDS:
            raise EvidenceError(f"raw_file_kinds[{name}] must be one of {sorted(PHYSICAL_RAW_KINDS)}")
    if "capture" not in set(kinds.values()):
        raise EvidenceError("a physical record must include at least one captured measurement")


def validate_register(
    path: Path,
    *,
    root: Path,
    scenarios: set[str],
    units: dict[str, tuple[str, str]],
) -> list[dict]:
    records = load_register(path)
    evidence_ids = [record.get("evidence_id") for record in records]
    if any(not isinstance(evidence_id, str) or not evidence_id for evidence_id in evidence_ids):
        raise EvidenceError("evidence_id must be a non-empty string")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise EvidenceError("duplicate evidence_id")
    for record in records:
        validate_record(record, root=root, scenarios=scenarios, units=units)
    return records


def register(
    path: Path,
    record: dict,
    *,
    root: Path,
    scenarios: set[str],
    units: dict[str, tuple[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with exclusive_file_lock(lock_path):
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o666)
        try:
            existing = validate_register(path, root=root, scenarios=scenarios, units=units)
            if record.get("evidence_id") in {item["evidence_id"] for item in existing}:
                raise EvidenceError("duplicate evidence_id")
            validate_record(record, root=root, scenarios=scenarios, units=units)
            payload = (json.dumps(record, allow_nan=False, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            original_size = os.fstat(descriptor).st_size
            try:
                if os.write(descriptor, payload) != len(payload):
                    raise OSError("short write while appending evidence")
                os.fsync(descriptor)
            except OSError:
                os.ftruncate(descriptor, original_size)
                os.fsync(descriptor)
                raise
        finally:
            os.close(descriptor)
