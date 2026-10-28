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


class EvidenceError(ValueError):
    """Raised when evidence cannot be trusted."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# The register is the authoritative hardware-validation evidence index, and it is
# read on every append. Without a bound, a large register exhausts the validator's
# memory; without strict parsing, a duplicate key silently selects the last value
# and a non-finite number survives into the report (issue #454).
MAX_REGISTER_BYTES = 8 * 1024 * 1024
MAX_REGISTER_LINES = 10_000
MAX_REGISTER_LINE_BYTES = 64 * 1024


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise EvidenceError(f"duplicate key in evidence record: {key!r}")
        seen[key] = value
    return seen


def _reject_non_finite(token: str) -> object:
    raise EvidenceError(f"non-finite number in evidence record: {token}")


def _parse_evidence_line(line: str, line_number: int) -> dict:
    try:
        record = json.loads(
            line,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except EvidenceError as exc:
        raise EvidenceError(f"line {line_number}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"invalid evidence JSON at line {line_number}") from exc
    if not isinstance(record, dict):
        raise EvidenceError(f"evidence at line {line_number} must be an object")
    return record


def load_register(path: Path) -> list[dict]:
    if not path.exists():
        return []

    size = path.stat().st_size
    if size > MAX_REGISTER_BYTES:
        raise EvidenceError(f"evidence register is {size} bytes, over the {MAX_REGISTER_BYTES}-byte limit")

    # Read as bytes and decode once, so the byte limit is checked before the whole
    # file becomes a str and the line count is counted on what was actually read.
    raw = path.read_bytes()
    if len(raw) > MAX_REGISTER_BYTES:
        raise EvidenceError(
            f"evidence register grew to {len(raw)} bytes while being read, over the {MAX_REGISTER_BYTES}-byte limit"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("evidence register is not valid UTF-8") from exc

    lines = text.splitlines()
    if len(lines) > MAX_REGISTER_LINES:
        raise EvidenceError(f"evidence register has {len(lines)} lines, over the {MAX_REGISTER_LINES}-line limit")

    records = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_REGISTER_LINE_BYTES:
            raise EvidenceError(f"evidence at line {line_number} is over the {MAX_REGISTER_LINE_BYTES}-byte line limit")
        records.append(_parse_evidence_line(line, line_number))
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
