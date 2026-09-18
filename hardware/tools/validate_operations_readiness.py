from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "hardware/operations-readiness.json"
OUTPUT = ROOT / "hardware/generated/operations_readiness_report.json"
BMS_VALIDATOR = ROOT / "hardware/power/tools/validate_bms_state_machine.py"
MECHANICAL_SPEC = ROOT / "hardware/mechanical/design-spec.json"
MECHANICAL_REPORT = ROOT / "hardware/mechanical/generated/analysis.json"
MECHANICAL_GENERATOR = ROOT / "hardware/mechanical/tools/generate_artifacts.py"


def read_csv(relative: str) -> list[dict[str, str]]:
    with (ROOT / relative).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def validate_bms_state_machine() -> dict[str, Any]:
    """Run the power-package validator through the existing readiness gate."""
    spec = importlib.util.spec_from_file_location("workbench_bms_state_machine_validator", BMS_VALIDATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load BMS validator: {BMS_VALIDATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate(write_report=True)


def validate_mechanical_mass_model(
    mechanical_spec: dict[str, object], mechanical_report: dict[str, object], mass_rows: list[dict[str, str]]
) -> dict[str, object]:
    spec = importlib.util.spec_from_file_location("workbench_mechanical_generator", MECHANICAL_GENERATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load mechanical generator: {MECHANICAL_GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    validation = module.validate_mass_model(mechanical_spec, mass_rows)
    mass_model = mechanical_report.get("mass_model", {})
    source_hashes = mass_model.get("source_hashes", {}) if isinstance(mass_model, dict) else {}
    legacy_rows = read_csv("hardware/mechanical/mass-ledger-legacy.csv")
    validation["checks"].update(
        {
            "generated_mass_model_is_present": isinstance(mass_model, dict) and bool(mass_model),
            "generated_mass_model_hash_is_present": isinstance(mass_model, dict)
            and isinstance(mass_model.get("mass_model_sha256"), str)
            and len(mass_model["mass_model_sha256"]) == 64,
            "generated_mass_model_hash_matches_source": isinstance(mass_model, dict)
            and mass_model.get("mass_model_sha256") == validation.get("mass_model_sha256"),
            "generated_design_spec_hash_matches_source": isinstance(source_hashes, dict)
            and source_hashes.get("design_spec_sha256") == module.sha256_file(MECHANICAL_SPEC),
            "generated_mass_matches_source": isinstance(mass_model, dict)
            and mass_model.get("mass_kg") == mechanical_report.get("mass_kg")
            and mass_model.get("mass_kg") == validation.get("total_mass_kg")
            and mass_model.get("center_of_gravity_mm") == validation.get("center_of_gravity_mm"),
            "legacy_mass_rows_are_superseded": bool(legacy_rows)
            and all(
                row.get("status") == "SUPERSEDED" and row.get("inclusion_rule") == "EXCLUDED" for row in legacy_rows
            ),
        }
    )
    validation["pass"] = all(validation["checks"].values())
    return validation


def validate() -> dict[str, object]:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    mechanical_spec = json.loads(MECHANICAL_SPEC.read_text(encoding="utf-8"))
    mechanical_report = json.loads(MECHANICAL_REPORT.read_text(encoding="utf-8"))
    bms_report = validate_bms_state_machine()
    mass_rows = read_csv("hardware/mechanical/mass-ledger.csv")
    mass_validation = validate_mechanical_mass_model(mechanical_spec, mechanical_report, mass_rows)
    stability_report = mechanical_report.get("stability", {})
    current_mass_rows = [row for row in mass_rows if row.get("inclusion_rule") == "AUTHORITATIVE_CURRENT"]
    try:
        total_mass_kg = sum(float(row["mass_kg"]) for row in current_mass_rows)
        center_of_gravity_mm = (
            [
                round(sum(float(row["mass_kg"]) * float(row[axis]) for row in current_mass_rows) / total_mass_kg, 1)
                for axis in ("x_mm", "y_mm", "z_mm")
            ]
            if total_mass_kg > 0
            else []
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        total_mass_kg = 0.0
        center_of_gravity_mm = []
    bms_states = {row["state"] for row in read_csv("hardware/power/bms-state-machine.csv")}
    bms_transitions = read_csv("hardware/power/bms-transitions.csv")
    planning_bom_total_usd = sum(
        float(row["extended_cost_usd"]) for row in read_csv("hardware/procurement/planning-bom.csv")
    )
    fixture_budget_total_usd = sum(
        float(row["planned_cost_usd"])
        for row in read_csv("hardware/manufacturing/fixture-budget.csv")
        if row["fixture_id"] != "TOTAL"
    )
    approval_rows = read_csv("hardware/mechanical/mass-model-approval-register.csv")
    required_files = [
        "hardware/power/README.md",
        "hardware/power/bms-state-machine.csv",
        "hardware/power/bms-transitions.csv",
        "hardware/power/protection-thresholds.csv",
        "hardware/mechanical/system-integration.md",
        "hardware/mechanical/mass-ledger.csv",
        "hardware/mechanical/mass-ledger-legacy.csv",
        "hardware/mechanical/mass-model-approval-register.csv",
        "hardware/mechanical/interference-checklist.csv",
        "hardware/procurement/planning-baseline.md",
        "hardware/procurement/planning-bom.csv",
        "hardware/procurement/supplier-scorecard-planning.csv",
        "hardware/manufacturing/production-line.md",
        "hardware/manufacturing/station-map.csv",
        "hardware/manufacturing/fixture-budget.csv",
        "hardware/qa/reliability-gates.md",
        "hardware/qa/fmea-action-register.csv",
        "hardware/qa/early-failure-log.csv",
        "hardware/compliance/README.md",
        "hardware/compliance/evidence-matrix.csv",
        "hardware/support/README.md",
        "hardware/support/case-template.csv",
        "hardware/support/escalation-matrix.csv",
        "docs/user-guide/index.md",
        "docs/user-guide/installation.md",
        "docs/user-guide/operation.md",
        "docs/user-guide/maintenance.md",
        "docs/user-guide/troubleshooting.md",
        "docs/user-guide/demo-runbook.md",
        "docs/api.md",
        "mkdocs.yml",
    ]
    checks = {
        "all_package_files_exist": all((ROOT / path).is_file() for path in required_files),
        "power_is_48v_with_three_levels": baseline["power"]
        == {"nominal_pack_voltage_v": 48, "protection_levels": 3, "bms_fail_closed": True},
        "mechanical_baseline_is_revision_d": baseline["mechanical"]["revision"] == mechanical_spec["revision"] == "D",
        "mechanical_design_case_matches_generated_analysis": baseline["mechanical"]["design_case_mass_kg"]
        == mechanical_report["mass_kg"],
        "mechanical_drive_module_count_matches_spec": baseline["mechanical"]["drive_module_count"]
        == mechanical_spec["chassis"]["drive_module_count"]
        == 4,
        "planning_bom_is_5100_usd": baseline["procurement"]["planning_bom_usd"] == 5100,
        "certificates_block_po": baseline["procurement"]["critical_certificates_required_before_po"],
        "line_has_six_stations": baseline["manufacturing"]["station_count"] == 6,
        "fixture_budget_is_4000_usd": baseline["manufacturing"]["fixture_budget_usd"] == 4000,
        "minimum_fpy_is_85_percent": baseline["manufacturing"]["minimum_fpy_percent"] >= 85,
        "rpn_actions_start_above_100": baseline["quality"]["rpn_action_threshold"] == 100,
        "early_failure_limit_is_5_percent": baseline["quality"]["maximum_early_failure_percent"] <= 5,
        "demo_route_is_90_minutes": baseline["documentation"]["demo_route_minutes"] == 90,
        "required_compliance_programs_are_present": set(baseline["compliance"]["required_programs"])
        == {"CE", "FCC", "UN38.3"},
        "certification_is_not_claimed": baseline["compliance"]["certification_claim"] == "NOT_CERTIFIED",
        "external_evidence_remains_required": baseline["status"] == "EXTERNAL_EVIDENCE_REQUIRED",
        "bms_has_fail_closed_states": bms_states
        >= {"OFF", "SELF_TEST", "PRECHARGE", "RUN", "FAULT_LATCHED", "SERVICE"},
        "bms_transition_graph_uses_known_states": all(
            row["source_state"] in bms_states and row["target_state"] in bms_states for row in bms_transitions
        ),
        "bms_run_entry_requires_precharge_or_derate_recovery": all(
            row["source_state"] in {"PRECHARGE", "DERATE"} for row in bms_transitions if row["target_state"] == "RUN"
        ),
        "bms_fault_transitions_open_and_latch": all(
            row["contactor_action"] == "all_open" and row["latch"] == "yes"
            for row in bms_transitions
            if row["target_state"] == "FAULT_LATCHED"
        ),
        "bms_state_machine_has_executable_validation": bool(bms_report.get("pass")),
        "bms_transition_table_hash_is_present": isinstance(bms_report.get("transition_table_sha256"), str)
        and len(bms_report["transition_table_sha256"]) == 64,
        "bms_design_and_physical_status_are_preserved": bms_report.get("status") == "DESIGN_BASELINE_ONLY"
        and bms_report.get("physical_results") == "NOT_EXECUTED"
        and bms_report.get("release_ready") is False,
        "all_protection_levels_are_defined": {
            row["level"] for row in read_csv("hardware/power/protection-thresholds.csv")
        }
        == {"L1", "L2", "L3"},
        "mass_ledger_matches_generated_mass": abs(total_mass_kg - mechanical_report["mass_kg"]) < 1e-9,
        "mass_ledger_cg_matches_generated_analysis": center_of_gravity_mm == mechanical_report["center_of_gravity_mm"],
        "mechanical_mass_model_is_valid": bool(mass_validation["pass"]),
        "mechanical_mass_model_revision_is_current": mechanical_report.get("mass_model", {}).get("revision")
        == mechanical_spec.get("revision"),
        "mechanical_stability_analysis_is_directional": isinstance(stability_report, dict)
        and isinstance(stability_report.get("checks"), dict)
        and all(stability_report["checks"].values())
        and stability_report.get("directions") == ["+X", "-X", "+Y", "-Y"],
        "mechanical_mass_model_id_matches_baseline": mechanical_report.get("mass_model", {}).get("model_id")
        == baseline["mechanical"].get("mass_model_id")
        and mechanical_report.get("mass_model", {}).get("revision")
        == baseline["mechanical"].get("mass_model_revision"),
        "mass_model_approval_roles_are_explicit": {row.get("role") for row in approval_rows}
        >= {"Product", "Mechanical", "Hardware", "Safety"}
        and all(row.get("model_id") == mechanical_spec.get("mass_model", {}).get("model_id") for row in approval_rows),
        "mass_model_approval_is_required_before_release": bool(approval_rows)
        and all(row.get("approval_status") == "REQUIRED" for row in approval_rows),
        "mechanical_generated_geometry_is_measured": mechanical_report["generated_geometry"]["status"] == "MEASURED",
        "mechanical_generated_heights_match_spec": mechanical_report["generated_geometry"]["assembly_bounds_mm"][
            "zmax_mm"
        ]
        == mechanical_spec["enclosure"]["height"]
        and mechanical_report["generated_geometry"]["navigation_bounds_mm"]["zmax_mm"]
        == mechanical_spec["enclosure"]["stowed_height"],
        "mass_ledger_has_physical_validation_status": all(row["status"] == "ESTIMATE" for row in current_mass_rows),
        "planning_bom_rows_sum_to_5100": planning_bom_total_usd == 5100,
        "planning_bom_has_certificate_gate": all(
            row["certificate_gate"] == "REQUIRED_BEFORE_PO" for row in read_csv("hardware/procurement/planning-bom.csv")
        ),
        "station_map_has_six_unique_stations": len(
            {row["station"] for row in read_csv("hardware/manufacturing/station-map.csv")}
        )
        == 6,
        "fixture_budget_rows_sum_to_4000": fixture_budget_total_usd == 4000,
        "all_fmea_actions_are_above_rpn_threshold": all(
            int(row["current_rpn"]) > baseline["quality"]["rpn_action_threshold"]
            for row in read_csv("hardware/qa/fmea-action-register.csv")
        ),
        "early_failure_template_is_not_executed": all(
            row["status"] == "NOT_EXECUTED" for row in read_csv("hardware/qa/early-failure-log.csv")
        ),
        "compliance_matrix_has_three_required_programs": {
            row["program"] for row in read_csv("hardware/compliance/evidence-matrix.csv")
        }
        >= {"CE", "FCC", "UN38.3"},
        "compliance_rows_stop_po": all(
            row["po_gate"] == "STOP" for row in read_csv("hardware/compliance/evidence-matrix.csv")
        ),
        "support_has_four_severity_levels": {
            row["severity"] for row in read_csv("hardware/support/escalation-matrix.csv")
        }
        == {"S0", "S1", "S2", "S3"},
        # CI installs the dev and docs extras from the committed hash-checked lock
        # and then builds the strict documentation site. The lock covers both
        # extras, so this still proves the documentation toolchain is present in
        # CI; it asserts the invariant rather than the literal command string that
        # used to spell it out, which is what a lock-backed install replaces.
        "documentation_build_is_in_ci": all(
            needle in (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
            for needle in (
                "--require-hashes -r docker/requirements-dev.lock",
                "make docs",
            )
        ),
    }
    report: dict[str, object] = {
        "package": baseline["package"],
        "pass": all(checks.values()),
        "status": baseline["status"],
        "bms": {
            "pass": bms_report.get("pass"),
            "status": bms_report.get("status"),
            "physical_results": bms_report.get("physical_results"),
            "transition_table_sha256": bms_report.get("transition_table_sha256"),
            "state_machine_sha256": bms_report.get("state_machine_sha256"),
        },
        "checks": checks,
        "metrics": {
            "mass_kg": total_mass_kg,
            "center_of_gravity_mm": center_of_gravity_mm,
            "planning_bom_total_usd": planning_bom_total_usd,
            "fixture_budget_total_usd": fixture_budget_total_usd,
            "station_count": len({row["station"] for row in read_csv("hardware/manufacturing/station-map.csv")}),
        },
        "mechanical_mass_model": {
            "validation": mass_validation,
            "model_id": mechanical_report.get("mass_model", {}).get("model_id"),
            "revision": mechanical_report.get("mass_model", {}).get("revision"),
            "mass_model_sha256": mechanical_report.get("mass_model", {}).get("mass_model_sha256"),
            "source_hashes": mechanical_report.get("mass_model", {}).get("source_hashes"),
            "status": mechanical_report.get("mass_model", {}).get("status"),
        },
        "required_files": required_files,
        "note": "A passing document check never substitutes for quotes, certificates, pilot data, or physical tests.",
    }
    return report


def main() -> None:
    report = validate()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
