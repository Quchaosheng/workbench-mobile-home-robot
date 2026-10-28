"""Validate field-validation templates and reject fabricated results."""

from __future__ import annotations

import csv
import json
from pathlib import Path

try:
    from evidence import EvidenceError, validate_register
except ModuleNotFoundError:  # imported by the repository test loader
    from hardware.validation.tools.evidence import EvidenceError, validate_register

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "hardware" / "validation"


def rows(name: str) -> list[dict[str, str]]:
    with (PACKAGE / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def derive_results(scenarios: set[str], evidence: list[dict]) -> dict[str, str]:
    results_by_scenario = {scenario_id: "NOT_EXECUTED" for scenario_id in scenarios}
    for scenario_id in scenarios:
        results = {record["result"] for record in evidence if record["scenario_id"] == scenario_id}
        if "FAIL" in results:
            results_by_scenario[scenario_id] = "FAIL"
        elif "HOLD" in results:
            results_by_scenario[scenario_id] = "HOLD"
        elif results == {"PASS"}:
            results_by_scenario[scenario_id] = "PASS"
    return results_by_scenario


def validate() -> dict:
    matrix = rows("sim2real-matrix.csv")
    faults = rows("fault-scenarios.csv")
    units = rows("first-batch-acceptance.csv")
    statuses = {"NOT_EXECUTED", "PASS", "FAIL", "HOLD"}
    scenarios = {row["scenario_id"] for row in faults}
    units_by_id = {
        row["unit_id"]: (row["hardware_revision"], row["firmware_hash"])
        for row in units
        if row["hardware_revision"] != "UNASSIGNED" and row["firmware_hash"] != "UNASSIGNED"
    }
    evidence_error = None
    try:
        evidence = validate_register(
            PACKAGE / "evidence-register.jsonl", root=ROOT, scenarios=scenarios, units=units_by_id
        )
    except EvidenceError as exc:
        evidence = []
        evidence_error = str(exc)
    evidence_counts = {
        kind: sum(record["evidence_kind"] == kind for record in evidence)
        for kind in ("simulation", "bench", "physical")
    }
    scenario_results = derive_results(scenarios, evidence)
    physical_evidence = [record for record in evidence if record["evidence_kind"] == "physical"]
    physical_scenario_results = derive_results(scenarios, physical_evidence)
    if evidence_error:
        physical_results = "HOLD"
    elif not physical_evidence:
        physical_results = "NOT_EXECUTED"
    elif "FAIL" in physical_scenario_results.values():
        physical_results = "FAIL"
    elif set(physical_scenario_results.values()) == {"PASS"}:
        physical_results = "PASS"
    else:
        physical_results = "HOLD"
    checks = {
        "sim2real_matrix_has_required_dimensions": {row["dimension"] for row in matrix}
        >= {"response_time", "position_accuracy", "grasp_success", "thermal rise"},
        "fault_library_has_twenty_scenarios": len(faults) == 20 and len({row["scenario_id"] for row in faults}) == 20,
        "faults_have_recovery_and_evidence": all(row["recovery_or_safe_state"] and row["evidence"] for row in faults),
        "faults_have_owner_and_procedure": all(row["owner"] and row["procedure_ref"] for row in faults),
        "first_batch_has_ten_units": len(units) == 10 and all(row["unit_id"] for row in units),
        "results_are_explicitly_statused": all(row["status"] in statuses for row in units + faults + matrix),
        "template_does_not_claim_builds": all(row["final_result"] == "NOT_BUILT" for row in units),
        "summary_cannot_claim_execution": all(row["status"] == "NOT_EXECUTED" for row in faults),
        "evidence_register_is_valid": evidence_error is None,
    }
    report = {
        "package": "validation",
        "checks": checks,
        "pass": all(checks.values()),
        "sim2real_dimension_count": len(matrix),
        "fault_scenario_count": len(faults),
        "first_batch_unit_count": len(units),
        "status": "VALIDATED" if physical_results == "PASS" else "PHYSICAL_EXECUTION_REQUIRED",
        "physical_results": physical_results,
        "evidence_counts": evidence_counts,
        # The register this summary was derived from, so a release gate can tell a
        # derived summary from a hand-written one (issue #429).
        "evidence_register": str((PACKAGE / "evidence-register.jsonl").relative_to(ROOT)),
        "evidence_register_is_valid": evidence_error is None,
        "evidence_error": evidence_error,
        "scenario_results": dict(sorted(scenario_results.items())),
        "physical_scenario_results": dict(sorted(physical_scenario_results.items())),
    }
    output = PACKAGE / "generated" / "validation_report.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    result = validate()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["pass"] else 1)
