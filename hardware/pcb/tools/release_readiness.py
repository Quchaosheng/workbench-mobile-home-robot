from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "generated" / "release_readiness.json"
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from initialize_approval_signatures import board_revision, sha256_file, validate_signature_register

EXPECTED_COPPER_LAYERS = ["F.Cu", *[f"In{index}.Cu" for index in range(1, 7)], "B.Cu"]
ISOLATED_POWER_TBD = "TBD_36_60V_TO_12V_240W_ISOLATED"
ISOLATED_POWER_TBD_LAND_PATTERN = "WB:Isolated_48V_12V_240W_TBD"
ISOLATED_POWER_TBD_FOOTPRINT = "Isolated_48V_12V_240W_TBD"
ISOLATED_POWER_TBD_SYMBOL = "ISOLATED_DC_DC_TBD"
INCOMPATIBLE_ISOLATED_POWER_MARKERS = ("DCM3623", "Vicor_DCM3623")
REQUIRED_BRINGUP_TEST_IDS = {
    "SAFE_RESISTANCE",
    "INRUSH_36V",
    "RAILS_36V",
    "RAILS_48V",
    "RAILS_60V",
    "UV_TRIP",
    "OV_TRIP",
    "REVERSE_POLARITY",
    "JETSON_BRANCH_SHORT",
    "LOAD_TRANSIENT",
    "POWER_SEQUENCE",
    "ESTOP_DUAL_CHANNEL",
    "ESTOP_DISCREPANCY",
    "CAN_FD",
    "J2_REGEN_TRANSIENT",
    "THERMAL_SOAK_LONG",
    "INTERFACE_FIXTURE",
}
CRITICAL_TEST_ACCESS_NETS = {
    "GND_PWR",
    "GND_CAN_ISO",
    "5V_CAN_ISO",
    "MOTOR_ENABLE_SAFE",
    "ESTOP_CH_A_RETURN",
    "ESTOP_CH_B_RETURN",
    "3V3_PGOOD",
    "JETSON_PGOOD",
    "JETSON_FAULT_N",
    "U3_IMON",
    "ESTOP_A_MON",
    "ESTOP_B_MON",
}
TEST_ACCESS_DESIGN_STATES = {"CONNECTOR_ACCESS_CONFIRMED", "ECO_REQUIRED"}
TEST_ACCESS_VERIFICATION_STATES = {"NOT_BUILT", "PHYSICAL_VALIDATION_REQUIRED", "VERIFIED"}
SOURCE_REQUIRED_FIELDS = {"id", "vendor", "title", "url", "claim", "confidence", "freeze_status", "owner"}


def read_csv(name: str) -> list[dict[str, str]]:
    with (ROOT / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _reference_block(text: str, reference: str, opener: str) -> str:
    """Return one KiCad object block without relying on global markers."""
    marker = f'(property "Reference" "{reference}"'
    marker_index = text.find(marker)
    if marker_index < 0:
        return ""
    token = opener.strip().split(maxsplit=1)[0].lstrip("(")
    starts = list(re.finditer(rf"(?m)^\s*\({re.escape(token)}\b", text[:marker_index]))
    if not starts:
        return ""
    start = starts[-1].start()
    next_match = re.search(rf"\n\s*\({re.escape(token)}\b", text[marker_index + len(marker) :])
    end = len(text) if next_match is None else marker_index + len(marker) + next_match.start()
    return text[start:end]


def check_connector_limit_semantics(
    connectors: list[dict[str, str]],
    harness_rows: list[dict[str, str]],
    motor_spec: dict[str, object],
    interface_text: str,
    wiring_text: str,
) -> dict[str, object]:
    """Keep connector contact ratings distinct from controlled system limits."""
    j2_rows = [row for row in connectors if row.get("reference") == "J2"]
    h02_rows = [row for row in harness_rows if row.get("harness_id") == "H02"]
    power = motor_spec.get("power", {})
    checks = {
        "single_j2_row": len(j2_rows) == 1,
        "contact_rating_is_explicit": len(j2_rows) == 1 and j2_rows[0].get("rating") == "12V 16A contact",
        "controlled_limit_is_10a": len(j2_rows) == 1 and j2_rows[0].get("controlled_system_limit_a") == "10",
        "limit_basis_is_120w_aggregate": len(j2_rows) == 1 and "120 W aggregate" in j2_rows[0].get("limit_basis", ""),
        "branch_protection_is_defined": len(j2_rows) == 1 and bool(j2_rows[0].get("branch_protection", "").strip()),
        "h02_matches_10a_limit": len(h02_rows) == 1 and h02_rows[0].get("max_current_a") == "10",
        "motor_spec_matches_10a_limit": power.get("aggregate_input_current_limit_a") == 10.0,
        "interface_document_matches": "120 W maximum aggregate" in interface_text,
        "wiring_document_matches": "120 W aggregate" in wiring_text,
    }
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "j2_contact_rating": j2_rows[0].get("rating") if j2_rows else None,
        "j2_controlled_system_limit_a": j2_rows[0].get("controlled_system_limit_a") if j2_rows else None,
        "motor_spec_limit_a": power.get("aggregate_input_current_limit_a"),
        "note": "J2 contact capability is not a permission to exceed the 10 A / 120 W system envelope.",
    }


def check_bringup_plan(rows: list[dict[str, str]]) -> dict[str, object]:
    test_ids = [row.get("test_id", "") for row in rows]
    missing_test_ids = sorted(REQUIRED_BRINGUP_TEST_IDS - set(test_ids))
    duplicate_test_ids = sorted({test_id for test_id in test_ids if test_ids.count(test_id) > 1})
    required_fields = {"step", "test_id", "input_condition", "stimulus", "measurement", "acceptance", "evidence"}
    incomplete_rows = [
        row.get("test_id") or f"step-{row.get('step', '?')}"
        for row in rows
        if any(not row.get(field, "").strip() for field in required_fields)
    ]
    try:
        steps = [int(row["step"]) for row in rows]
    except (KeyError, TypeError, ValueError):
        steps = []
    sequential_steps = steps == list(range(1, len(rows) + 1))
    endpoint_test_ids = {"36V": "RAILS_36V", "48V": "RAILS_48V", "60V": "RAILS_60V"}
    endpoint_tests = {
        voltage: test_id
        if any(row.get("test_id") == test_id and voltage in row.get("input_condition", "") for row in rows)
        else None
        for voltage, test_id in endpoint_test_ids.items()
    }
    passed = (
        not missing_test_ids
        and not duplicate_test_ids
        and not incomplete_rows
        and sequential_steps
        and all(endpoint_tests.values())
    )
    return {
        "pass": passed,
        "required_test_ids": sorted(REQUIRED_BRINGUP_TEST_IDS),
        "declared_test_ids": test_ids,
        "missing_test_ids": missing_test_ids,
        "duplicate_test_ids": duplicate_test_ids,
        "incomplete_rows": incomplete_rows,
        "sequential_steps": sequential_steps,
        "input_endpoint_evidence": endpoint_tests,
        "physical_execution_required": True,
    }


# A physical verification record has to say which net it measured, when, with what,
# and over which file. `evidence_ref` is therefore `path#sha256=<digest>`, and the
# row has to name the net it covers. A bare path used to be enough, so a README
# could stand in for the measurement of a safety-critical net (issue #445).
EVIDENCE_REF_PATTERN = re.compile(r"^(?P<path>[^#]+)#sha256=(?P<digest>[0-9a-f]{64})$")


def _evidence_problems(row: dict[str, str], net: str, root: Path) -> list[str]:
    """Why this row's evidence is not a verification of *this* net."""

    problems: list[str] = []
    evidence_ref = row.get("evidence_ref", "").strip()
    match = EVIDENCE_REF_PATTERN.fullmatch(evidence_ref)
    if match is None:
        return ["verified_without_bound_evidence"]

    path = (root / match.group("path")).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return ["verified_evidence_outside_repository"]
    if not path.is_file():
        return ["verified_evidence_missing"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != match.group("digest"):
        problems.append("verified_evidence_hash_mismatch")

    covered = row.get("evidence_net", "").strip()
    if not covered:
        problems.append("verified_without_evidence_net")
    elif covered != net:
        problems.append("verified_evidence_covers_another_net")

    for field in ("evidence_instrument", "evidence_captured_at"):
        if not row.get(field, "").strip():
            problems.append(f"verified_without_{field.removeprefix('evidence_')}")
    return problems


def check_fixture_access_plan(
    rows: list[dict[str, str]],
    pinout: list[dict[str, str]],
    fixtures: list[dict[str, str]],
) -> dict[str, object]:
    nets = [row.get("net", "") for row in rows]
    missing_nets = sorted(CRITICAL_TEST_ACCESS_NETS - set(nets))
    duplicate_nets = sorted({net for net in nets if nets.count(net) > 1})
    fixture_ids = {row.get("fixture_id", "") for row in fixtures if row.get("fixture_id") != "TOTAL"}
    connector_access = {f"{row['reference']}.{row['pin']}": row["net"] for row in pinout}
    invalid_rows: list[dict[str, str]] = []
    eco_accesses: set[str] = set()
    for row in rows:
        net = row.get("net", "")
        design_state = row.get("design_state", "")
        verification_state = row.get("verification_state", "")
        planned_access = row.get("planned_access", "")
        reason = ""
        if design_state not in TEST_ACCESS_DESIGN_STATES:
            reason = "invalid_design_state"
        elif verification_state not in TEST_ACCESS_VERIFICATION_STATES:
            reason = "invalid_verification_state"
        elif row.get("required_fixture") not in fixture_ids:
            reason = "unknown_fixture"
        elif not all(row.get(field, "").strip() for field in ("measurement", "acceptance", "owner")):
            reason = "required_field_blank"
        elif design_state == "CONNECTOR_ACCESS_CONFIRMED":
            access_points = planned_access.split("|")
            if not access_points or any(connector_access.get(point) != net for point in access_points):
                reason = "connector_access_does_not_match_net"
        elif not re.fullmatch(r"ECO-TP\d+", planned_access):
            reason = "invalid_eco_testpoint"
        elif int(planned_access.removeprefix("ECO-TP")) <= 8:
            reason = "eco_testpoint_collides_with_existing_pad"
        elif planned_access in eco_accesses:
            reason = "duplicate_eco_testpoint"
        else:
            eco_accesses.add(planned_access)
        if verification_state == "VERIFIED":
            problems = _evidence_problems(row, net, ROOT.parents[1])
            if problems:
                reason = reason or problems[0]
        if reason:
            invalid_rows.append({"net": net, "reason": reason})
    engineering_pass = not missing_nets and not duplicate_nets and not invalid_rows
    design_ready = engineering_pass and all(row.get("design_state") != "ECO_REQUIRED" for row in rows)
    # `release_ready` restates the per-row judgement rather than re-testing the
    # evidence_ref string: a row that reached this point with a bad reference is
    # already in `invalid_rows`, and engineering_pass is false.
    release_ready = (
        design_ready and engineering_pass and all(row.get("verification_state") == "VERIFIED" for row in rows)
    )
    return {
        "pass": engineering_pass,
        "design_ready": design_ready,
        "release_ready": release_ready,
        "required_nets": sorted(CRITICAL_TEST_ACCESS_NETS),
        "missing_nets": missing_nets,
        "duplicate_nets": duplicate_nets,
        "invalid_rows": invalid_rows,
        "eco_required_nets": sorted(row.get("net", "") for row in rows if row.get("design_state") == "ECO_REQUIRED"),
        "physical_validation_required_nets": sorted(
            row.get("net", "") for row in rows if row.get("verification_state") != "VERIFIED"
        ),
    }


def check_source_baseline(
    component_matrix: list[dict[str, str]], source_baseline: dict[str, object]
) -> dict[str, object]:
    sources = source_baseline.get("sources", [])
    if not isinstance(sources, list):
        sources = []
    source_ids = [source.get("id", "") for source in sources if isinstance(source, dict)]
    duplicate_source_ids = sorted({source_id for source_id in source_ids if source_ids.count(source_id) > 1})
    required_source_ids = {row.get("source_id", "") for row in component_matrix}
    missing_source_ids = sorted(required_source_ids - set(source_ids))
    invalid_sources: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            invalid_sources.append("<non-object-source>")
        elif (
            not SOURCE_REQUIRED_FIELDS <= source.keys()
            or not str(source.get("url", "")).startswith("https://")
            or any(not str(source.get(field, "")).strip() for field in SOURCE_REQUIRED_FIELDS)
        ):
            invalid_sources.append(str(source.get("id", "<missing-id>")))
    invalid_sources.sort()
    exclusion_users = sorted(
        row["reference"] for row in component_matrix if row.get("source_id") == "SRC-VICOR-DCM3623-EXCLUSION"
    )
    exclusion_use_pass = exclusion_users == ["U2"] and any(
        row.get("reference") == "U2" and row.get("primary_candidate") == ISOLATED_POWER_TBD for row in component_matrix
    )
    passed = not duplicate_source_ids and not missing_source_ids and not invalid_sources and exclusion_use_pass
    return {
        "pass": passed,
        "required_source_ids": sorted(required_source_ids),
        "missing_source_ids": missing_source_ids,
        "duplicate_source_ids": duplicate_source_ids,
        "invalid_sources": invalid_sources,
        "unreferenced_source_ids": sorted(set(source_ids) - required_source_ids),
        "excluded_source_users": exclusion_users,
        "excluded_source_use_pass": exclusion_use_pass,
    }


def check_isolated_power_tbd_guard(
    component_matrix: list[dict[str, str]],
    approval_register: list[dict[str, str]],
    bom: list[dict[str, str]],
    artifact_texts: dict[str, str],
) -> dict[str, object]:
    matrix_rows = [row for row in component_matrix if row["reference"] == "U2"]
    approval_rows = [row for row in approval_register if row["reference"] == "U2"]
    bom_rows = [row for row in bom if row["reference"] == "U2"]
    matrix_uses_tbd = len(matrix_rows) == 1 and matrix_rows[0]["primary_candidate"] == ISOLATED_POWER_TBD
    approval_uses_tbd = len(approval_rows) == 1 and approval_rows[0]["candidate"] == ISOLATED_POWER_TBD
    bom_uses_tbd = (
        len(bom_rows) == 1
        and bom_rows[0]["design_candidate"] == ISOLATED_POWER_TBD
        and bom_rows[0]["package_or_module"] == ISOLATED_POWER_TBD_LAND_PATTERN
    )
    incompatible_occurrences = {
        name: [marker for marker in INCOMPATIBLE_ISOLATED_POWER_MARKERS if marker in text]
        for name, text in artifact_texts.items()
    }
    incompatible_occurrences = {name: markers for name, markers in incompatible_occurrences.items() if markers}
    required_artifact_markers = {
        "design_data.py": [ISOLATED_POWER_TBD, ISOLATED_POWER_TBD_FOOTPRINT, ISOLATED_POWER_TBD_SYMBOL],
        "controller.kicad_pcb": [ISOLATED_POWER_TBD_FOOTPRINT, "U2 LAND PATTERN TBD", "DO NOT FIT"],
        "controller.kicad_sch": [ISOLATED_POWER_TBD, ISOLATED_POWER_TBD_FOOTPRINT, ISOLATED_POWER_TBD_SYMBOL],
        "controller.kicad_sym": [ISOLATED_POWER_TBD_SYMBOL],
        "controller.ses": [ISOLATED_POWER_TBD_FOOTPRINT],
        "controller.net": [ISOLATED_POWER_TBD, ISOLATED_POWER_TBD_FOOTPRINT],
        "fabrication/positions.csv": [ISOLATED_POWER_TBD_FOOTPRINT],
        "WB.pretty": [ISOLATED_POWER_TBD_FOOTPRINT],
    }
    missing_placeholder_markers = {
        name: [marker for marker in markers if marker not in artifact_texts.get(name, "")]
        for name, markers in required_artifact_markers.items()
    }
    missing_placeholder_markers = {name: markers for name, markers in missing_placeholder_markers.items() if markers}
    board_u2_block = _reference_block(artifact_texts.get("controller.kicad_pcb", ""), "U2", "(footprint ")
    schematic_u2_block = _reference_block(artifact_texts.get("controller.kicad_sch", ""), "U2", "  (symbol ")
    u2_board_is_dnp = bool(re.search(r"\(attr[^\n]*\bdnp\b", board_u2_block))
    u2_schematic_is_dnp = "(dnp yes)" in schematic_u2_block
    passed = (
        matrix_uses_tbd
        and approval_uses_tbd
        and bom_uses_tbd
        and not incompatible_occurrences
        and not missing_placeholder_markers
        and u2_board_is_dnp
        and u2_schematic_is_dnp
    )
    return {
        "pass": passed,
        "required_placeholder": ISOLATED_POWER_TBD,
        "required_land_pattern_placeholder": ISOLATED_POWER_TBD_LAND_PATTERN,
        "component_matrix_uses_tbd": matrix_uses_tbd,
        "approval_register_uses_tbd": approval_uses_tbd,
        "fabrication_bom_uses_tbd": bom_uses_tbd,
        "incompatible_markers": list(INCOMPATIBLE_ISOLATED_POWER_MARKERS),
        "incompatible_occurrences": incompatible_occurrences,
        "missing_placeholder_markers": missing_placeholder_markers,
        "u2_board_is_dnp": u2_board_is_dnp,
        "u2_schematic_is_dnp": u2_schematic_is_dnp,
        "note": (
            "The former DCM3623 selection is excluded. U2 remains a requirement envelope until an orderable "
            "36-60 V to regulated 12 V isolated 240 W-class MPN and its land pattern are frozen by ECO."
        ),
    }


def audit() -> dict[str, object]:
    pinout = read_csv("connector-pinout.csv")
    connectors = read_csv("connectors.csv")
    component_matrix = read_csv("component-selection-matrix.csv")
    approval_register = read_csv("component-approval-register.csv")
    approval_signatures = read_csv("component-approval-signatures.csv")
    testpoint_coverage = read_csv("testpoint-coverage.csv")
    fixture_access_rows = read_csv("fixture-access-plan.csv")
    bringup_rows = read_csv("fabrication/bringup-test-plan.csv")
    bom = read_csv("fabrication/bom.csv")
    fixture_rows = read_csv("../manufacturing/fixture-budget.csv")
    source_baseline = json.loads((ROOT / "source-baseline.json").read_text(encoding="utf-8"))
    schematic = (ROOT / "kicad/controller.kicad_sch").read_text(encoding="utf-8")
    board = (ROOT / "kicad/controller.kicad_pcb").read_text(encoding="utf-8")
    design_data = (ROOT / "tools/design_data.py").read_text(encoding="utf-8")
    bom_text = (ROOT / "fabrication/bom.csv").read_text(encoding="utf-8")
    symbol_library = (ROOT / "kicad/controller.kicad_sym").read_text(encoding="utf-8")
    routing_session = (ROOT / "kicad/controller.ses").read_text(encoding="utf-8")
    netlist = (ROOT / "generated/controller.net").read_text(encoding="utf-8")
    positions = (ROOT / "fabrication/positions.csv").read_text(encoding="utf-8")
    footprint_paths = sorted((ROOT / "kicad/WB.pretty").glob("*.kicad_mod"))
    footprint_library = "\n".join(f"{path.name}\n{path.read_text(encoding='utf-8')}" for path in footprint_paths)
    custom_rules = (ROOT / "kicad/controller.kicad_dru").read_text(encoding="utf-8")
    fabrication_notes = (ROOT / "fabrication/fabrication-notes.csv").read_text(encoding="utf-8")
    gerber_job = json.loads((ROOT / "fabrication/gerbers/controller-job.gbrjob").read_text(encoding="utf-8"))
    drc = (ROOT / "generated/drc.rpt").read_text(encoding="utf-8")
    erc = (ROOT / "generated/erc.rpt").read_text(encoding="utf-8")
    bringup = (ROOT / "fabrication/bringup-test-plan.csv").read_text(encoding="utf-8")
    stackup_rows = read_csv("fabrication/stackup.csv")
    safety_truth_table = read_csv("safety-gate-truth-table.csv")
    harness_report = json.loads(
        (ROOT.parent / "manufacturing/generated/harness_report.json").read_text(encoding="utf-8")
    )
    connectivity_report = json.loads((ROOT / "generated/connectivity_report.json").read_text(encoding="utf-8"))
    layout_report = json.loads((ROOT / "generated/layout_report.json").read_text(encoding="utf-8"))
    harness_rows = read_csv("../manufacturing/harness-spec.csv")
    motor_spec = json.loads((ROOT.parent / "motor_driver/electrical-spec.json").read_text(encoding="utf-8"))
    interface_text = (ROOT / "interface-control.md").read_text(encoding="utf-8")
    wiring_text = (ROOT.parent.parent / "docs/hardware/wiring.md").read_text(encoding="utf-8")

    board_layers = [
        match.group(1) for match in re.finditer(r'^\s*\(\d+ "((?:F|B|In\d+)\.Cu)" signal\)$', board, flags=re.MULTILINE)
    ]
    stackup_layers = [row["layer"] for row in stackup_rows]
    stackup_text = "\n".join(",".join(row.values()) for row in stackup_rows)

    pin_counts = {
        reference: len([row for row in pinout if row["reference"] == reference])
        for reference in {row["reference"] for row in pinout}
    }
    duplicate_pins = len({(row["reference"], row["pin"]) for row in pinout}) != len(pinout)
    required_populated = {"J1", "J2", "J3", "J4", "J5", "J6", "J10", "J11", "J12"}
    procurement_holds = [row["reference"] for row in bom if row["procurement_gate"] != "APPROVED"]
    bom_references = {reference for row in bom for reference in row["reference"].split()}
    board_references = set(re.findall(r'\(property "Reference" "([^"]+)"', board))
    current_revision = board_revision(ROOT / "kicad/controller.kicad_pcb")
    current_bom_sha256 = sha256_file(ROOT / "fabrication/bom.csv")
    approval_signature_report = validate_signature_register(
        approval_register,
        approval_signatures,
        current_revision,
        current_bom_sha256,
        ROOT.parents[1],
    )
    fully_approved_references = set(approval_signature_report["fully_approved_references"])
    isolated_power_guard = check_isolated_power_tbd_guard(
        component_matrix,
        approval_register,
        bom,
        {
            "design_data.py": design_data,
            "fabrication/bom.csv": bom_text,
            "controller.kicad_pcb": board,
            "controller.kicad_sch": schematic,
            "controller.kicad_sym": symbol_library,
            "controller.ses": routing_session,
            "controller.net": netlist,
            "fabrication/positions.csv": positions,
            "WB.pretty": footprint_library,
        },
    )
    connector_limit_semantics = check_connector_limit_semantics(
        connectors, harness_rows, motor_spec, interface_text, wiring_text
    )
    bringup_plan = check_bringup_plan(bringup_rows)
    fixture_access_plan = check_fixture_access_plan(fixture_access_rows, pinout, fixture_rows)
    source_baseline_report = check_source_baseline(component_matrix, source_baseline)

    engineering_checks = {
        "drc_clean": "Found 0 DRC violations" in drc and "Found 0 unconnected pads" in drc,
        "erc_clean": "0  Errors 0  Warnings" in erc,
        "controlled_pinout_complete": pin_counts.get("J4") == 20
        and required_populated <= set(pin_counts)
        and pin_counts.get("J2") == 4
        and pin_counts.get("J5") == 4
        and pin_counts.get("J6") == 4
        and pin_counts.get("J10") == 4
        and pin_counts.get("J11") == 4
        and pin_counts.get("J12") == 4
        and not duplicate_pins,
        "jetson_test_plan_uses_12v": any(
            "Jetson" in " ".join(row.values()) and "12V" in " ".join(row.values()) for row in bringup_rows
        )
        and "5V rail" not in bringup,
        "stackup_matches_current_rails": "protected Jetson 12V" in stackup_text
        and "5V Jetson power" not in stackup_text,
        "eight_copper_layers_declared": board_layers == EXPECTED_COPPER_LAYERS
        and stackup_layers == EXPECTED_COPPER_LAYERS,
        "primary_secondary_8mm_clearance_rule_declared": "constraint clearance (min 8mm)" in custom_rules
        and "VBAT_PROTECTED" in custom_rules
        and "GND_PWR" in custom_rules,
        "software_enable_is_not_safety_output": "MOTOR_ENABLE_REQ" in board and "MOTOR_ENABLE_SAFE" in board,
        "safety_truth_table_covers_channel_discrepancy": any(
            row["channel_a_closed"] != row["channel_b_closed"] and row["motor_enable_safe"] == "0"
            for row in safety_truth_table
        ),
        "harness_engineering_pass": harness_report["engineering_package_pass"],
        "connector_limit_semantics_consistent": connector_limit_semantics["pass"],
        "bringup_plan_covers_input_envelope_and_faults": bringup_plan["pass"],
        "critical_test_access_plan_complete": fixture_access_plan["pass"],
        "component_source_ids_resolve": source_baseline_report["pass"],
        "component_matrix_covers_all_active_modules": {row["reference"] for row in component_matrix}
        >= {"U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8"},
        "bom_covers_all_board_components": bom_references == board_references,
        "approval_register_covers_all_pending_bom_lines": {row["reference"] for row in approval_register}
        == set(procurement_holds)
        and all(row["candidate"] and row["required_approver"] for row in approval_register),
        "approval_signatures_initialized_per_required_role": approval_signature_report["pass"],
        "approval_signatures_bound_to_current_bom_and_revision": (
            approval_signature_report["checks"]["hardware_revision_matches"]
            and approval_signature_report["checks"]["bom_hash_matches"]
        ),
        "board_connectivity_audit_pass": connectivity_report["pass"],
        "board_layout_hard_gates_pass": layout_report["hard_gate_pass"],
        "eight_testpoints_have_measurement_coverage": len(testpoint_coverage) == 8
        and {row["reference"] for row in testpoint_coverage} == {f"TP{index}" for index in range(1, 9)},
        "fabrication_metadata_controlled": '(rev "EVT1")' in board
        and '(copper_finish "ENIG")' in board
        and gerber_job["GeneralSpecs"]["ProjectId"]["Revision"] == "EVT1"
        and gerber_job["GeneralSpecs"]["Finish"] == "ENIG"
        and "FAB-003" in fabrication_notes,
    }
    evt_prototype_order_checks = {
        "detailed_schematic_has_symbols": schematic.count("(symbol (lib_id") >= 100
        and schematic.count("(wire ") >= 300,
        "all_bom_roles_signed": approval_signature_report["all_references_fully_approved"],
        "safety_design_analysis_approved": False,
        "supplier_dfm_closed": False,
        "component_mpn_and_avl_closed": all(row["procurement_status"] == "APPROVED" for row in component_matrix),
        "isolated_power_excluded_part_absent_and_tbd_placeholders_consistent": isolated_power_guard["pass"],
        "isolated_power_mpn_and_land_pattern_frozen": False,
        "critical_test_access_design_closed": fixture_access_plan["design_ready"],
        "u7_system_isolation_target_met": layout_report["details"]["u7_full_copper_isolation_keepout"].get(
            "system_target_met", False
        ),
    }
    engineering_package_pass = all(engineering_checks.values())
    evt_prototype_order_ready = engineering_package_pass and all(evt_prototype_order_checks.values())
    production_release_checks = {
        "evt_prototype_order_ready": evt_prototype_order_ready,
        "physical_bringup_evidence_attached": False,
        "measured_safety_timing_approved": False,
        "harness_physical_release_checks_closed": all(harness_report["release_checks"].values()),
        "critical_test_access_physically_verified": fixture_access_plan["release_ready"],
    }
    production_release_ready = all(production_release_checks.values())
    evt_blockers = [name for name, passed in evt_prototype_order_checks.items() if not passed]
    if not engineering_package_pass:
        evt_blockers.insert(0, "engineering_package_pass")
    production_blockers = [name for name, passed in production_release_checks.items() if not passed]
    return {
        "schema_version": 2,
        "package": "controller-pcb-release-readiness",
        "status": "PRODUCTION_RELEASE_READY" if production_release_ready else "PRODUCTION_RELEASE_BLOCKED",
        "legacy_status": "ORDER_RELEASED" if production_release_ready else "ORDER_RELEASE_BLOCKED",
        "engineering_package_pass": engineering_package_pass,
        "engineering_checks": engineering_checks,
        "evt_prototype_order": {
            "status": "EVT_PROTOTYPE_ORDER_READY" if evt_prototype_order_ready else "EVT_PROTOTYPE_ORDER_BLOCKED",
            "ready": evt_prototype_order_ready,
            "checks": evt_prototype_order_checks,
            "blocker_count": len(evt_blockers),
            "blockers": evt_blockers,
            "note": "Physical bring-up is downstream evidence and is intentionally not an EVT prototype-order gate.",
        },
        "production_release": {
            "status": "PRODUCTION_RELEASE_READY" if production_release_ready else "PRODUCTION_RELEASE_BLOCKED",
            "ready": production_release_ready,
            "checks": production_release_checks,
            "blocker_count": len(production_blockers),
            "blockers": production_blockers,
        },
        "order_release_checks": {
            "detailed_schematic_has_symbols": evt_prototype_order_checks["detailed_schematic_has_symbols"],
            "all_bom_lines_approved": evt_prototype_order_checks["all_bom_roles_signed"],
            "physical_bringup_evidence_attached": production_release_checks["physical_bringup_evidence_attached"],
            "safety_analysis_approved": evt_prototype_order_checks["safety_design_analysis_approved"],
            "supplier_dfm_closed": evt_prototype_order_checks["supplier_dfm_closed"],
            "harness_release_checks_closed": production_release_checks["harness_physical_release_checks_closed"],
            "component_mpn_and_avl_closed": evt_prototype_order_checks["component_mpn_and_avl_closed"],
            "isolated_power_excluded_part_absent_and_tbd_placeholders_consistent": evt_prototype_order_checks[
                "isolated_power_excluded_part_absent_and_tbd_placeholders_consistent"
            ],
            "isolated_power_mpn_and_land_pattern_frozen": evt_prototype_order_checks[
                "isolated_power_mpn_and_land_pattern_frozen"
            ],
            "critical_test_access_design_closed": evt_prototype_order_checks["critical_test_access_design_closed"],
            "critical_test_access_physically_verified": production_release_checks[
                "critical_test_access_physically_verified"
            ],
            "u7_system_isolation_target_met": evt_prototype_order_checks["u7_system_isolation_target_met"],
        },
        "procurement_hold_references": [
            row["reference"] for row in approval_register if row["reference"] not in fully_approved_references
        ],
        "blocker_count": len(production_blockers),
        "approval_signatures": approval_signature_report,
        "approval_baseline": {
            "hardware_revision": current_revision,
            "bom_path": "hardware/pcb/fabrication/bom.csv",
            "bom_sha256": current_bom_sha256,
        },
        "component_counts": {
            "board_footprints": len(board_references),
            "bom_references": len(bom_references),
        },
        "layout_status": {
            "status": layout_report["status"],
            "open_risks": layout_report["warnings"],
        },
        "isolated_power_guard": isolated_power_guard,
        "connector_limit_semantics": connector_limit_semantics,
        "bringup_plan": bringup_plan,
        "fixture_access_plan": fixture_access_plan,
        "source_baseline": source_baseline_report,
        "copper_layers": {
            "expected": EXPECTED_COPPER_LAYERS,
            "board": board_layers,
            "stackup": stackup_layers,
        },
        "note": (
            "The checked-in schematic is component-level and its ERC is clean. U2 is not selected, and any stale "
            "excluded-part data fails the engineering guard until an ECO replaces the schematic, "
            "BOM, footprint, placement, routing and thermal model. Every required role signs independently against "
            "the current revision and BOM hash. Physical evidence remains a production-release gate, not a "
            "prerequisite for ordering the prototypes needed to collect that evidence."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=("evt", "production", "structure"),
        default="production",
        help="return success only when this PCB release stage is ready; structure checks engineering data only",
    )
    args = parser.parse_args()
    report = audit()
    with OUT.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if args.stage == "structure":
        return 0 if report["engineering_package_pass"] else 1
    stage = report["evt_prototype_order"] if args.stage == "evt" else report["production_release"]
    return 0 if report["engineering_package_pass"] and stage["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
