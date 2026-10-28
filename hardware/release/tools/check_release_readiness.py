"""Validate staged hardware release governance without inferring external evidence."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "hardware" / "release"
MECHANICAL_REPORT = "hardware/mechanical/generated/analysis.json"
MECHANICAL_SPEC = ROOT / "hardware/mechanical/design-spec.json"
MECHANICAL_LEDGER = ROOT / "hardware/mechanical/mass-ledger.csv"
MECHANICAL_GENERATOR = ROOT / "hardware/mechanical/tools/generate_artifacts.py"
UPSTREAM_REPORTS = {
    "pcb": "hardware/pcb/generated/release_readiness.json",
    "motor_driver": "hardware/motor_driver/generated/release-readiness.json",
    "procurement": "hardware/procurement/generated/procurement_report.json",
    "harness": "hardware/manufacturing/generated/harness_report.json",
    "qa": "hardware/qa/generated/qa_report.json",
    "validation": "hardware/validation/generated/validation_report.json",
}
CONTROLLED_STATUSES = {"PASS", "BLOCKED", "NOT_EXECUTED", "HOLD"}
CONTROLLED_EVIDENCE_BINDINGS = {
    "FILE",
    "ENGINEERING_PASS",
    "STATUS_READY",
    "EVT_READY",
    "PRODUCTION_READY",
    "PHYSICAL_EXECUTED",
}
EXPECTED_GATE_BINDINGS = {
    "REL-001": "ENGINEERING_PASS",
    "REL-001A": "FILE",
    "REL-002": "PRODUCTION_READY",
    "REL-003": "ENGINEERING_PASS",
    "REL-003A": "PRODUCTION_READY",
    "REL-004": "PRODUCTION_READY",
    "REL-005": "FILE",
    "REL-006A": "FILE",
    "REL-006B": "FILE",
    "REL-007A": "PRODUCTION_READY",
    "REL-007B": "PRODUCTION_READY",
    "REL-008": "PHYSICAL_EXECUTED",
    "REL-009": "FILE",
    "REL-010": "FILE",
    "REL-011": "FILE",
    "REL-012": "FILE",
    "REL-013": "FILE",
    "REL-014": "FILE",
    "REL-015": "FILE",
}
EXPECTED_CLOSURE_BINDINGS = {
    "HWC-AXIS-001": "FILE",
    "HWC-AXIS-002": "FILE",
    "HWC-PWR-001": "FILE",
    "HWC-PWR-002": "FILE",
    "HWC-SAF-001": "FILE",
    "HWC-SAF-002": "FILE",
    "HWC-PCB-001": "PRODUCTION_READY",
    "HWC-PCB-002": "FILE",
    "HWC-PCB-003": "PRODUCTION_READY",
    "HWC-HAR-001": "FILE",
    "HWC-HAR-002": "STATUS_READY",
    "HWC-MEC-001": "ENGINEERING_PASS",
    "HWC-MEC-002": "FILE",
    "HWC-VAL-001": "FILE",
    "HWC-VAL-002": "PHYSICAL_EXECUTED",
    "HWC-CMP-001": "FILE",
    "HWC-SYS-001": "FILE",
    "HWC-ARM-001": "FILE",
    "HWC-LIFT-001": "FILE",
    "HWC-DRIVE-001": "FILE",
    "HWC-WIRE-001": "FILE",
    "HWC-COST-001": "FILE",
    "HWC-ID-001": "FILE",
}
CONTROLLED_PRIORITIES = {"P0", "P1", "P2"}
CONTROLLED_DEPENDENCIES = {"REPOSITORY", "OWNER", "SUPPLIER", "PHYSICAL"}
CONTROLLED_CLOSURE_TYPES = {
    "ENGINEERING",
    "COMMERCIAL",
    "PHYSICAL",
    "SUPPLIER",
    "SAFETY",
    "COMPLIANCE",
}
REQUIRED_CLOSURE_DOMAINS = {
    "axes",
    "power",
    "safety",
    "pcb",
    "harness",
    "mechanical",
    "mobility",
    "system",
    "validation",
    "compliance",
    "cost",
}


def read_csv(name: str) -> list[dict[str, str]]:
    with (PACKAGE / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def resolve_repo_ref(reference: str) -> Path | None:
    """Resolve a repository-relative evidence path without allowing path escape."""
    if not reference or Path(reference).is_absolute():
        return None
    candidate = (ROOT / reference).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError:
        return None
    if candidate == ROOT.resolve():
        return None
    return candidate


def load_report(path: str) -> dict[str, Any]:
    resolved = resolve_repo_ref(path)
    if resolved is None:
        raise ValueError(f"evidence path escapes repository: {path!r}")
    return json.loads(resolved.read_text(encoding="utf-8"))


def validate_mechanical_evidence() -> dict[str, object]:
    """Require bound directional analytical evidence without treating it as physical proof."""
    report = load_report(MECHANICAL_REPORT)
    mass_model = report.get("mass_model")
    stability = report.get("stability")
    gates = stability.get("gates") if isinstance(stability, dict) else None
    cases = stability.get("cases") if isinstance(stability, dict) else None
    directions = stability.get("directions") if isinstance(stability, dict) else None
    source_validation: dict[str, object] = {"pass": False, "checks": {"validator_loaded": False}}
    try:
        generator_spec = importlib.util.spec_from_file_location(
            "workbench_release_mechanical_generator", MECHANICAL_GENERATOR
        )
        if generator_spec is not None and generator_spec.loader is not None:
            generator = importlib.util.module_from_spec(generator_spec)
            generator_spec.loader.exec_module(generator)
            with MECHANICAL_LEDGER.open(newline="", encoding="utf-8") as handle:
                ledger_rows = list(csv.DictReader(handle))
            mechanical_spec = json.loads(MECHANICAL_SPEC.read_text(encoding="utf-8"))
            source_validation = generator.validate_mass_model(mechanical_spec, ledger_rows)
    except (OSError, TypeError, ValueError, KeyError, ImportError):
        source_validation = {"pass": False, "checks": {"validator_loaded": False}}
    report_hash = mass_model.get("mass_model_sha256") if isinstance(mass_model, dict) else None
    source_hashes = mass_model.get("source_hashes", {}) if isinstance(mass_model, dict) else {}
    checks = {
        "report_declares_physical_validation_required": report.get("status") == "CONCEPT_PHYSICAL_VALIDATION_REQUIRED",
        "mass_model_is_bound_to_report": isinstance(mass_model, dict)
        and isinstance(mass_model.get("mass_model_sha256"), str)
        and len(mass_model["mass_model_sha256"]) == 64
        and mass_model.get("validation_status") == "CONCEPT_PHYSICAL_VALIDATION_REQUIRED",
        "mass_model_matches_authoritative_source": bool(source_validation.get("pass"))
        and report_hash == source_validation.get("mass_model_sha256")
        and isinstance(source_hashes, dict)
        and source_hashes.get("design_spec_sha256") == generator.sha256_file(MECHANICAL_SPEC),
        "directional_directions_are_complete": directions == ["+X", "-X", "+Y", "-Y"],
        "directional_cases_are_present": isinstance(cases, dict)
        and {"stowed", "raised", "payload", "shared_workspace", "emergency_stop", "stabilizer_deployed"} <= set(cases),
        "drive_gate_is_directional": isinstance(gates, dict)
        and isinstance(gates.get("drive"), dict)
        and set(gates["drive"].get("directional_minimums", {})) == set(directions or []),
        "stabilized_gate_is_directional": isinstance(gates, dict)
        and isinstance(gates.get("stabilized"), dict)
        and set(gates["stabilized"].get("directional_minimums", {})) == set(directions or []),
        "directional_gates_are_passing": isinstance(gates, dict)
        and all(
            isinstance(gates.get(name), dict) and gates[name].get("pass") is True for name in ("drive", "stabilized")
        ),
        "physical_release_is_not_inferred": report.get("status") != "RELEASE_READY_FOR_SIGNOFF",
    }
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "report": MECHANICAL_REPORT,
        "source_validation": source_validation,
    }


# The status strings each upstream package may report. A structural check used to
# accept any non-empty string, so `JUST_A_STRING` validated as an upstream approval
# (issue #446). Each package owns its own vocabulary; a new status has to be added
# here, which is where a reviewer will see it.
UPSTREAM_STATUSES: dict[str, frozenset[str]] = {
    "pcb": frozenset({"PRODUCTION_RELEASE_READY", "PRODUCTION_RELEASE_BLOCKED"}),
    "motor_driver": frozenset({"ORDER_RELEASE_READY", "ORDER_RELEASE_BLOCKED"}),
    "procurement": frozenset({"ORDER_RELEASE_READY", "ORDER_RELEASE_BLOCKED"}),
    "harness": frozenset({"HARNESS_RELEASE_READY", "HARNESS_RELEASE_BLOCKED"}),
    "qa": frozenset({"EXECUTION_REQUIRED", "EXECUTION_COMPLETE"}),
    "validation": frozenset({"PHYSICAL_EXECUTION_REQUIRED", "PHYSICAL_EXECUTION_COMPLETE"}),
}


def _status_is_ready(status: str) -> bool:
    return status.endswith("_READY") or status in {
        "ORDER_RELEASED",
        "HARNESS_RELEASED",
        "RELEASE_READY_FOR_SIGNOFF",
    }


def _stage_ready(report: dict[str, Any], stage: str) -> bool:
    """Read a staged readiness flag, falling back only for legacy reports."""
    stage_report = report.get(stage)
    if isinstance(stage_report, dict) and isinstance(stage_report.get("ready"), bool):
        return stage_report["ready"]
    if stage == "production_release":
        return isinstance(report.get("status"), str) and _status_is_ready(report["status"])
    explicit = report.get("order_release_ready")
    return isinstance(explicit, bool) and explicit


def _engineering_pass(report: dict[str, Any]) -> bool:
    """Return the report's engineering result, never its staged release status."""
    for field in ("engineering_package_pass", "engineering_pass"):
        value = report.get(field)
        if isinstance(value, bool):
            return value
    checks = report.get("checks")
    if isinstance(checks, dict) and checks and all(isinstance(value, bool) for value in checks.values()):
        return all(checks.values())
    value = report.get("pass")
    return isinstance(value, bool) and value


def _binding_observed_ready(binding: str, report: dict[str, Any]) -> bool:
    """Evaluate one explicit evidence binding against a JSON report."""
    if binding == "ENGINEERING_PASS":
        return _engineering_pass(report)
    if binding == "STATUS_READY":
        return isinstance(report.get("status"), str) and _status_is_ready(report["status"])
    if binding == "EVT_READY":
        return _stage_ready(report, "evt_prototype_order")
    if binding == "PRODUCTION_READY":
        return _stage_ready(report, "production_release")
    if binding == "PHYSICAL_EXECUTED":
        result = report.get("physical_results")
        return result in {"PASS", "PASSED", "VERIFIED", "COMPLETED"}
    return False


def validate_evidence_bindings(
    rows: list[dict[str, str]], id_field: str, expected_bindings: dict[str, str] | None = None
) -> dict[str, object]:
    """Cross-check PASS/non-PASS rows against the declared JSON evidence binding."""
    mismatches: list[dict[str, str]] = []
    invalid_bindings: list[str] = []
    binding_contract_mismatches: list[str] = []
    missing_reports: list[str] = []
    for row in rows:
        row_id = row.get(id_field, "")
        binding = row.get("evidence_binding", "")
        if binding not in CONTROLLED_EVIDENCE_BINDINGS:
            invalid_bindings.append(row_id)
            continue
        if expected_bindings is not None and binding != expected_bindings.get(row_id):
            binding_contract_mismatches.append(row_id)
            continue
        reference = row.get("evidence_ref", "").strip()
        if binding == "FILE":
            continue
        resolved = resolve_repo_ref(reference)
        if resolved is None or not resolved.is_file():
            missing_reports.append(row_id)
            continue
        try:
            report = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            missing_reports.append(row_id)
            continue
        if not isinstance(report, dict):
            missing_reports.append(row_id)
            continue
        observed_ready = _binding_observed_ready(binding, report)
        expected_ready = row.get("status") == "PASS"
        if observed_ready != expected_ready:
            mismatches.append(
                {
                    "id": row_id,
                    "evidence_ref": reference,
                    "binding": binding,
                    "row_status": row.get("status", ""),
                    "observed_ready": str(observed_ready).lower(),
                }
            )
    return {
        "pass": not invalid_bindings and not binding_contract_mismatches and not missing_reports and not mismatches,
        "invalid_bindings": invalid_bindings,
        "binding_contract_mismatches": binding_contract_mismatches,
        "missing_reports": missing_reports,
        "mismatches": mismatches,
    }


def binding_mismatches(
    rows: list[dict[str, str]],
    id_field: str,
    stage_flag: str | None = None,
    expected_bindings: dict[str, str] | None = None,
) -> list[str]:
    """Return rows whose bound report disagrees with their declared PASS state."""
    scoped_rows = rows if stage_flag is None else [row for row in rows if row.get(stage_flag) == "yes"]
    report = validate_evidence_bindings(scoped_rows, id_field, expected_bindings)
    return sorted(
        {
            *report["invalid_bindings"],
            *report["binding_contract_mismatches"],
            *report["missing_reports"],
            *(item["id"] for item in report["mismatches"]),
        }
    )


def _stage_is_consistent(stage: object, ready_status: str, blocked_status: str) -> bool:
    if not isinstance(stage, dict):
        return False
    ready = stage.get("ready")
    status = stage.get("status")
    blockers = stage.get("blockers")
    blocker_count = stage.get("blocker_count")
    checks = stage.get("checks")
    if not isinstance(ready, bool) or not isinstance(status, str):
        return False
    if not isinstance(blockers, list) or blocker_count != len(blockers):
        return False
    if checks is not None and (
        not isinstance(checks, dict) or not all(isinstance(value, bool) for value in checks.values())
    ):
        return False
    expected_status = ready_status if ready else blocked_status
    return status == expected_status and ready == (len(blockers) == 0)


def validate_upstream_report(report: dict[str, Any], package: str | None = None) -> dict[str, bool]:
    status = report.get("status")
    ready_status = isinstance(status, str) and _status_is_ready(status)
    blocker_count = report.get("blocker_count")
    blockers = report.get("blockers")
    count_consistent = (
        blocker_count is None
        or blockers is None
        or (isinstance(blocker_count, int) and isinstance(blockers, list) and blocker_count == len(blockers))
    )
    explicit_ready = report.get("order_release_ready")
    explicit_ready_consistent = explicit_ready is None or (
        isinstance(explicit_ready, bool) and explicit_ready == ready_status
    )
    physical_results = report.get("physical_results")
    physical_status_consistent = physical_results != "NOT_EXECUTED" or not ready_status
    evt_stage = report.get("evt_prototype_order")
    production_stage = report.get("production_release")
    staged_consistency = True
    if evt_stage is not None:
        staged_consistency = staged_consistency and _stage_is_consistent(
            evt_stage, "EVT_PROTOTYPE_ORDER_READY", "EVT_PROTOTYPE_ORDER_BLOCKED"
        )
    if production_stage is not None:
        staged_consistency = staged_consistency and _stage_is_consistent(
            production_stage, "PRODUCTION_RELEASE_READY", "PRODUCTION_RELEASE_BLOCKED"
        )
        if isinstance(production_stage, dict):
            staged_consistency = staged_consistency and status == production_stage.get("status")
    structural_pass_field = report.get("pass", report.get("engineering_package_pass"))
    known_statuses = UPSTREAM_STATUSES.get(package or "") if package else None
    return {
        "report_is_object": isinstance(report, dict),
        "status_is_nonempty_string": isinstance(status, str) and bool(status),
        "status_is_in_the_package_vocabulary": (known_statuses is None or status in known_statuses),
        # `pass: false` is a truthful report of an incomplete package, not a reason to
        # accept it as structurally valid evidence of completion (issue #446).
        "structural_pass_field_is_boolean": isinstance(structural_pass_field, bool),
        "structural_pass_field_is_true": structural_pass_field is True,
        "blocker_count_matches_blocker_list_when_present": count_consistent,
        "explicit_ready_matches_status_when_present": explicit_ready_consistent,
        "not_executed_physical_results_cannot_be_ready": physical_status_consistent,
        "staged_statuses_are_self_consistent": staged_consistency,
    }


def validate_upstream_reports() -> dict[str, object]:
    reports: dict[str, object] = {}
    for name, path in UPSTREAM_REPORTS.items():
        report = load_report(path)
        checks = validate_upstream_report(report, name)
        reports[name] = {
            "path": path,
            "status": report.get("status"),
            "checks": checks,
            "pass": all(checks.values()),
        }
    return reports


def _dependency_tokens(value: str) -> set[str]:
    return {token.strip() for token in value.split("+") if token.strip()}


def validate_closure_checklist(rows: list[dict[str, str]]) -> dict[str, object]:
    required_fields = {
        "closure_id",
        "domain",
        "priority",
        "owner",
        "dependency",
        "evidence_ref",
        "evidence_binding",
        "status",
        "closure_type",
        "evt_order_blocker",
        "production_release_blocker",
    }
    domains = {row.get("domain", "") for row in rows}
    evidence_exists = all(
        (resolved := resolve_repo_ref(row.get("evidence_ref", ""))) is not None and resolved.is_file() for row in rows
    )
    dependency_tokens = [_dependency_tokens(row.get("dependency", "")) for row in rows]
    evidence_bindings = validate_evidence_bindings(rows, "closure_id", EXPECTED_CLOSURE_BINDINGS)
    checks = {
        "required_fields_are_present": bool(rows) and all(required_fields <= set(row) for row in rows),
        "closure_ids_are_unique": len(rows) == len({row.get("closure_id") for row in rows}),
        "binding_contract_ids_are_complete": {row.get("closure_id") for row in rows} == set(EXPECTED_CLOSURE_BINDINGS),
        "required_domains_are_covered": REQUIRED_CLOSURE_DOMAINS <= domains,
        "priorities_are_controlled": all(row.get("priority") in CONTROLLED_PRIORITIES for row in rows),
        "owners_and_next_actions_are_present": all(
            str(row.get("owner") or "").strip() and str(row.get("next_action") or "").strip() for row in rows
        ),
        "dependencies_are_controlled": all(
            tokens and tokens <= CONTROLLED_DEPENDENCIES for tokens in dependency_tokens
        ),
        "evidence_references_exist": evidence_exists,
        "statuses_are_controlled": all(row.get("status") in CONTROLLED_STATUSES for row in rows),
        "evidence_bindings_match_statuses": evidence_bindings["pass"],
        "closure_types_are_controlled": all(row.get("closure_type") in CONTROLLED_CLOSURE_TYPES for row in rows),
        "stage_blocker_flags_are_explicit": all(
            row.get("evt_order_blocker") in {"yes", "no"} and row.get("production_release_blocker") in {"yes", "no"}
            for row in rows
        ),
        "evt_requirements_are_also_production_requirements": all(
            row.get("evt_order_blocker") != "yes" or row.get("production_release_blocker") == "yes" for row in rows
        ),
        "unexecuted_physical_work_does_not_block_prototype_order": all(
            not ("PHYSICAL" in tokens and row.get("status") != "PASS" and row.get("evt_order_blocker") == "yes")
            for row, tokens in zip(rows, dependency_tokens, strict=True)
        ),
        "unresolved_p0_items_block_prototype_order": all(
            row.get("status") == "PASS" or row.get("evt_order_blocker") == "yes"
            for row in rows
            if row.get("priority") == "P0"
        ),
    }
    evt_blockers = [row["closure_id"] for row in rows if row["evt_order_blocker"] == "yes" and row["status"] != "PASS"]
    evt_blockers.extend(binding_mismatches(rows, "closure_id", "evt_order_blocker", EXPECTED_CLOSURE_BINDINGS))
    production_blockers = [
        row["closure_id"] for row in rows if row["production_release_blocker"] == "yes" and row["status"] != "PASS"
    ]
    production_blockers.extend(
        binding_mismatches(rows, "closure_id", "production_release_blocker", EXPECTED_CLOSURE_BINDINGS)
    )
    evt_blockers = sorted(set(evt_blockers))
    production_blockers = sorted(set(production_blockers))
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "row_count": len(rows),
        "domains": sorted(domains),
        "evt_blockers": evt_blockers,
        "production_blockers": production_blockers,
        "evidence_bindings": evidence_bindings,
    }


def validate() -> dict[str, object]:
    rows = read_csv("evidence-register.csv")
    closure_rows = read_csv("hardware-closure-checklist.csv")
    upstream_reports = validate_upstream_reports()
    closure_report = validate_closure_checklist(closure_rows)
    mechanical_evidence = validate_mechanical_evidence()
    evidence_bindings = validate_evidence_bindings(rows, "gate_id", EXPECTED_GATE_BINDINGS)
    report_checks = {
        "gate_ids_are_unique": len(rows) == len({row["gate_id"] for row in rows}),
        "binding_contract_ids_are_complete": {row["gate_id"] for row in rows} == set(EXPECTED_GATE_BINDINGS),
        "all_gates_have_owner_and_next_action": all(row["owner"] and row["next_action"] for row in rows),
        "statuses_are_controlled": all(row["status"] in CONTROLLED_STATUSES for row in rows),
        "evidence_references_exist": all(
            (resolved := resolve_repo_ref(row["evidence_ref"])) is not None and resolved.exists() for row in rows
        ),
        "evidence_bindings_match_statuses": evidence_bindings["pass"],
        "stage_blockers_are_explicit": all(
            row["evt_order_blocker"] in {"yes", "no"} and row["production_release_blocker"] in {"yes", "no"}
            for row in rows
        ),
        "evt_gates_are_production_gates": all(
            row["evt_order_blocker"] != "yes" or row["production_release_blocker"] == "yes" for row in rows
        ),
        "not_executed_gates_do_not_block_evt_order": all(
            row["status"] != "NOT_EXECUTED" or row["evt_order_blocker"] == "no" for row in rows
        ),
        "upstream_reports_are_structurally_valid": all(
            item["pass"] for item in upstream_reports.values() if isinstance(item, dict)
        ),
        "master_closure_checklist_is_valid": closure_report["pass"],
        "mechanical_evidence_is_directional_and_bound": mechanical_evidence["pass"],
    }
    evt_blockers = [row["gate_id"] for row in rows if row["evt_order_blocker"] == "yes" and row["status"] != "PASS"]
    evt_blockers.extend(binding_mismatches(rows, "gate_id", "evt_order_blocker", EXPECTED_GATE_BINDINGS))
    production_blockers = [
        row["gate_id"] for row in rows if row["production_release_blocker"] == "yes" and row["status"] != "PASS"
    ]
    production_blockers.extend(
        binding_mismatches(rows, "gate_id", "production_release_blocker", EXPECTED_GATE_BINDINGS)
    )
    evt_blockers = sorted(set(evt_blockers))
    production_blockers = sorted(set(production_blockers))
    evt_ready = all(report_checks.values()) and not evt_blockers
    production_ready = all(report_checks.values()) and not production_blockers
    report = {
        "schema_version": 2,
        "package": "hardware-release-readiness",
        "checks": report_checks,
        "pass": all(report_checks.values()),
        "gate_count": len(rows),
        "evt_prototype_order": {
            "status": "EVT_PROTOTYPE_ORDER_READY" if evt_ready else "EVT_PROTOTYPE_ORDER_BLOCKED",
            "ready": evt_ready,
            "blocker_count": len(evt_blockers),
            "blockers": evt_blockers,
        },
        "production_release": {
            "status": "PRODUCTION_RELEASE_READY" if production_ready else "PRODUCTION_RELEASE_BLOCKED",
            "ready": production_ready,
            "blocker_count": len(production_blockers),
            "blockers": production_blockers,
        },
        "blocker_count": len(production_blockers),
        "blockers": production_blockers,
        "status": "PRODUCTION_RELEASE_READY" if production_ready else "PRODUCTION_RELEASE_BLOCKED",
        "legacy_status": "RELEASE_READY_FOR_SIGNOFF" if production_ready else "RELEASE_BLOCKED",
        "upstream_reports": upstream_reports,
        "closure_checklist": closure_report,
        "evidence_bindings": evidence_bindings,
        "mechanical_evidence": mechanical_evidence,
        "note": (
            "Validator PASS means the governance structure is internally consistent. EVT prototype ordering and "
            "production release are independent fail-closed stages; external evidence is never inferred."
        ),
    }
    output = PACKAGE / "generated" / "release_readiness_report.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=("evt", "production", "structure"),
        default="production",
        help="return success only when this release stage is ready; structure checks governance only",
    )
    args = parser.parse_args()
    result = validate()
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.stage == "structure":
        return 0 if result["pass"] else 1
    stage = result["evt_prototype_order"] if args.stage == "evt" else result["production_release"]
    return 0 if result["pass"] and stage["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
