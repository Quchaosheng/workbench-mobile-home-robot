"""Check that BSP readiness remains evidence-bound and fail closed.

A gate may only claim ``PASS`` through a committed bring-up record that binds
the gate id, the board and the source revision to a raw capture whose declared
sha256 recomputes. A file existing is never evidence on its own.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "bsp/validation/bringup-plan.yaml"
EXPECTED_GATE_IDS = {
    "BSP-GATE-ARCH",
    "BSP-GATE-SELECTION",
    "BSP-GATE-CARRIER",
    "BSP-GATE-MCU",
    "BSP-GATE-SAFETY",
    "BSP-GATE-IMAGE",
    "BSP-GATE-CAN",
    "BSP-GATE-VALIDATION",
    "BSP-GATE-CAMERA",
}
BLOCKED_STATUS = "REPOSITORY_BASELINE_READY_PHYSICAL_BRINGUP_BLOCKED"
READY_STATUS = "PHYSICAL_BRINGUP_EVIDENCE_COMPLETE"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
UNRESOLVED_PREFIXES = ("TBD_", "NOT_BUILT")
PLACEHOLDERS = {"UNKNOWN", "NONE", "N/A", "PLACEHOLDER", "NOT_APPLICABLE"}


def validate_readiness(readiness: object, root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    if not isinstance(readiness, dict):
        return ["BSP readiness must be a mapping"]
    gates = readiness.get("gates", [])
    allowed = set(readiness.get("allowed_statuses", []))
    if not isinstance(gates, list) or any(not isinstance(gate, dict) for gate in gates):
        return ["BSP readiness gates must be a list of mappings"]
    gate_ids = [gate.get("id") for gate in gates]
    if len(gates) != len(EXPECTED_GATE_IDS) or set(gate_ids) != EXPECTED_GATE_IDS:
        errors.append("BSP readiness must contain the exact nine controlled gates")
    if any(gate.get("status") not in allowed for gate in gates):
        errors.append("BSP readiness contains an uncontrolled status")
    if any(not gate.get("owner") or not gate.get("requirement") for gate in gates):
        errors.append("every BSP gate requires an owner and requirement")
    contract = _evidence_contract()
    if contract is None:
        errors.append("bring-up plan must declare the controlled evidence fields")
    evidence_errors: list[str] = []
    for gate in gates:
        evidence = gate.get("evidence")
        if gate.get("status") == "PASS":
            evidence_errors.extend(_gate_evidence_errors(gate, root, contract))
        elif evidence == "NOT_ATTACHED" and gate.get("status") not in {"BLOCKED", "NOT_EXECUTED"}:
            errors.append(f"{gate.get('id')} lacks evidence but is not blocked")
    errors.extend(evidence_errors)
    all_pass = (
        bool(gates)
        and contract is not None
        and not evidence_errors
        and all(gate.get("status") == "PASS" for gate in gates)
    )
    release_ready = readiness.get("physical_release_ready")
    expected_status = READY_STATUS if all_pass else BLOCKED_STATUS
    if release_ready is not all_pass:
        errors.append("physical_release_ready must equal the all-gates-PASS result")
    if readiness.get("status") != expected_status:
        errors.append(f"BSP package status must be {expected_status}")
    return errors


def _repository_evidence_path(root: Path, evidence: object) -> Path | None:
    if not isinstance(evidence, str) or not evidence or evidence == "NOT_ATTACHED":
        return None
    relative = Path(evidence)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        return None
    root = root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


def _evidence_contract() -> tuple[list[str], list[str], str] | None:
    """Read the record fields, allowed results and board target the plan declares.

    The plan is read from this repository, not from ``root``: the schema is a
    property of the BSP package, while ``root`` only bounds where evidence lives.
    """
    try:
        plan = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(plan, dict) or not isinstance(plan.get("target"), str) or not plan["target"]:
        return None
    evidence = plan.get("evidence")
    if not isinstance(evidence, dict):
        return None
    required = evidence.get("required_fields")
    binding = evidence.get("binding_fields")
    allowed_results = evidence.get("allowed_results")
    for declared in (required, binding, allowed_results):
        if not isinstance(declared, list) or not declared:
            return None
    fields = [*required, *binding]
    if any(not isinstance(field, str) or not field for field in fields):
        return None
    if any(not isinstance(result, str) for result in allowed_results):
        return None
    return fields, allowed_results, plan["target"]


def _gate_evidence_errors(gate: dict, root: Path, contract: tuple[list[str], list[str], str] | None) -> list[str]:
    gate_id = gate.get("id")
    if contract is None:
        return [f"{gate_id} claims PASS without a controlled evidence contract"]
    fields, allowed_results, target = contract
    path = _repository_evidence_path(root, gate.get("evidence"))
    if path is None:
        return [f"{gate_id} claims PASS without repository evidence"]
    try:
        record = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return [f"{gate_id} evidence must be a structured YAML record"]
    if not isinstance(record, dict):
        return [f"{gate_id} evidence must be a structured YAML record"]
    missing = [field for field in fields if field not in record]
    if missing:
        return [f"{gate_id} evidence is missing fields: {', '.join(missing)}"]
    errors: list[str] = []
    if record["gate"] != gate_id:
        errors.append(f"{gate_id} evidence is bound to gate {record['gate']!r}, not {gate_id}")
    if record["board_model"] != target:
        errors.append(f"{gate_id} evidence board_model does not match the bring-up plan target")
    for field in ("board_revision", "kernel_version"):
        if _unresolved(record[field]):
            errors.append(f"{gate_id} evidence {field} is blank or unresolved")
    if not _firmware_versions(record["firmware_versions"]):
        errors.append(f"{gate_id} evidence firmware_versions must be a non-empty list or mapping")
    if not _utc_timestamp(record["timestamp_utc"]):
        errors.append(f"{gate_id} evidence timestamp_utc must be an ISO-8601 UTC timestamp")
    if not isinstance(record["source_revision"], str) or not COMMIT.fullmatch(record["source_revision"]):
        errors.append(f"{gate_id} evidence source_revision must be a full commit hash")
    result = record["result"]
    if result not in allowed_results:
        errors.append(f"{gate_id} evidence result must be one of {', '.join(allowed_results)}")
    elif result != "PASS":
        errors.append(f"{gate_id} evidence result {result} cannot close a PASS gate")
    capture = _repository_evidence_path(root, record["raw_capture"])
    if capture is None:
        errors.append(f"{gate_id} raw capture must reference a repository file")
    digest = record["raw_capture_hash"]
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        errors.append(f"{gate_id} evidence raw_capture_hash must be a lowercase sha256 digest")
    elif capture is not None and _sha256(capture) != digest:
        errors.append(f"{gate_id} raw capture digest does not match raw_capture_hash")
    return errors


def _unresolved(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return True
    text = value.strip()
    return text.upper() in PLACEHOLDERS or text.startswith(UNRESOLVED_PREFIXES)


def _firmware_versions(value: object) -> bool:
    if isinstance(value, dict):
        versions = list(value.values())
    elif isinstance(value, list):
        versions = value
    else:
        return False
    return bool(versions) and all(not _unresolved(version) for version in versions)


def _utc_timestamp(value: object) -> bool:
    if _unresolved(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() == UTC.utcoffset(parsed)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check() -> list[str]:
    readiness = yaml.safe_load((ROOT / "bsp/readiness.yaml").read_text(encoding="utf-8"))
    return validate_readiness(readiness)


if __name__ == "__main__":
    failures = check()
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        raise SystemExit(1)
    print("BSP readiness validation passed")
