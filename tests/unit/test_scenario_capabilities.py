"""Tests for the Issue #311 scenario capability matrix.

The matrix is a join over the registry, and the join is the specification. The
tests assert both directions of it -- a registered identity with no row fails and
a row with no registration fails -- because one direction alone would let the
matrix rot into a list of aspirations.

The other half of the file is the honesty rule the Issue names: a row may restate
or weaken a manifest's evidence status, never strengthen it. Every rejection is
asserted against an otherwise-valid matrix, so a validator that rejects
everything cannot pass this file.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts"), str(ROOT / "libs/kernel"), str(ROOT / "libs/contracts")]

from workbench.kernel.scenario_capability import (
    ENVIRONMENT_RANK,
    ENVIRONMENT_STATUSES,
    MATRIX_VERSION,
    CapabilityMatrixError,
    generated_document,
    load_matrix,
    load_registered,
    rows_to_markdown,
    validate_matrix,
)
from workbench.kernel.scenario_contract import FAIL, PASS, approved_semantic_actions, load_contract

MATRIX_PATH = ROOT / "docs/architecture/scenario-capability-matrix-v1.json"
GENERATED_PATH = ROOT / "docs/architecture/scenario-capability-matrix.md"
GATE = ROOT / "tools/scripts/check_scenario_capabilities.py"


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return load_contract()


@pytest.fixture(scope="module")
def registered() -> dict[str, dict[str, Any]]:
    return load_registered(ROOT)


@pytest.fixture(scope="module")
def actions() -> frozenset[str]:
    return approved_semantic_actions()


@pytest.fixture
def committed() -> dict[str, Any]:
    return copy.deepcopy(load_matrix(MATRIX_PATH))


def valid_row(**overrides: Any) -> dict[str, Any]:
    """A row that is otherwise valid, so a single rejection is isolated."""

    row: dict[str, Any] = {
        "scenario_id": "sample-scenario",
        "scenario_version": "1.0",
        "task_family": "sample",
        "status": "REGISTERED",
        "environment_status": "SCRIPTED_FIXTURE",
        "semantic_actions": ["observe", "grasp"],
        "required_adapters": ["motion", "perception"],
        "missing_capabilities": [],
        "verifier": "services/world_model/workbench_world_model/verifier.py::verify_object_in_tray",
        "recovery_policy": ["re_observe", "abort"],
        "evidence_status": "SCRIPTED_FIXTURE",
        "scenario_rules_owner": "Task / Simulation",
        "adapter_owner": "Motion / Perception",
        "evidence_owner": "World Model",
        "release_owner": "Release / QA",
        "manifest": "sim/registry/pick-place-red-block.json",
        "tests": ["tests/unit/test_scenario_registry.py"],
        "evidence_report": "sim/registry/pick-place-red-block.json",
        "notes": "",
    }
    row.update(overrides)
    return row


def matrix_with(rows: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "matrix_version": MATRIX_VERSION,
        "status": "proposed",
        "issue": 311,
        "decision_record": "docs/decisions/ADR-0006-scenario-registry-contract.md",
        "environment_statuses": list(ENVIRONMENT_STATUSES),
        "rows": rows,
    }
    document.update(overrides)
    return document


def run_gate(document: dict[str, Any], tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(GATE), "--matrix", str(path), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_committed_matrix_validates(committed: dict[str, Any], registered, contract, actions):
    exit_code, verdicts = validate_matrix(
        committed, registered=registered, contract=contract, approved_actions=actions, root=ROOT
    )
    assert exit_code == PASS
    assert all(verdict.ok for verdict in verdicts)


def test_committed_matrix_covers_every_registered_identity(committed: dict[str, Any], registered):
    declared = {f"{row['scenario_id']}@{row['scenario_version']}" for row in committed["rows"]}
    assert set(registered) <= declared


def test_every_family_has_a_row_even_when_planned_or_blocked(committed: dict[str, Any]):
    """The Issue requires all five families plus cleaning, whatever their state."""

    families = {row["task_family"] for row in committed["rows"]}
    assert {"pick_place", "kitting", "inspection", "assembly", "parcels", "cleaning"} <= families


def test_a_family_row_is_explicit_about_its_status(committed: dict[str, Any]):
    statuses = {row["task_family"]: row["status"] for row in committed["rows"]}
    assert statuses["pick_place"] == "REGISTERED"
    assert statuses["assembly"] == "BLOCKED"


# --- the join, in both directions -------------------------------------------


def test_registered_identity_without_a_row_fails(committed: dict[str, Any], registered, contract, actions):
    dropped = committed["rows"][0]["scenario_id"]
    rows = [row for row in committed["rows"] if row["scenario_id"] != dropped]
    exit_code, verdicts = validate_matrix(
        matrix_with(rows), registered=registered, contract=contract, approved_actions=actions, root=ROOT
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_MISSING_ROW" for verdict in verdicts for finding in verdict.findings)


def test_row_claiming_registered_without_a_registration_fails(committed: dict[str, Any], registered, contract, actions):
    rows = [
        valid_row(scenario_id="not-registered-anywhere", status="REGISTERED"),
        *[row for row in committed["rows"] if row["status"] == "REGISTERED"],
    ]
    exit_code, verdicts = validate_matrix(
        matrix_with(rows), registered=registered, contract=contract, approved_actions=actions, root=ROOT
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_UNREGISTERED_ROW" for verdict in verdicts for finding in verdict.findings)


def test_duplicate_row_fails(committed: dict[str, Any], registered, contract, actions):
    row = valid_row()
    exit_code, verdicts = validate_matrix(
        matrix_with([row, copy.deepcopy(row)]),
        registered={},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_DUPLICATE_ROW" for verdict in verdicts for finding in verdict.findings)


# --- evidence status never inflates -----------------------------------------


def test_row_may_not_claim_a_stronger_environment_than_its_manifest(committed, registered, contract, actions):
    row = valid_row(environment_status="PHYSICAL", evidence_status="PHYSICAL")
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    codes = {finding.code for verdict in verdicts for finding in verdict.findings}
    assert "MATRIX_STATUS_EXCEEDS_EVIDENCE" in codes


def test_row_evidence_status_must_match_its_manifest(committed, registered, contract, actions):
    row = valid_row(evidence_status="GAZEBO")
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_STATUS_EXCEEDS_EVIDENCE" for verdict in verdicts for finding in verdict.findings)


def test_a_row_may_restate_its_own_evidence_status(committed, registered, contract, actions):
    """Weakening is allowed; only inflation is a violation."""

    row = valid_row(environment_status="NOT_EXECUTED", evidence_status="SCRIPTED_FIXTURE")
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == PASS, [finding.detail for v in verdicts for finding in v.findings]


def test_a_physical_manifest_may_back_a_physical_row(contract, actions):
    row = valid_row(environment_status="PHYSICAL", evidence_status="PHYSICAL")
    exit_code, _ = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "PHYSICAL"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == PASS


def test_scripted_fixture_is_never_release_eligible(committed: dict[str, Any]):
    assert ENVIRONMENT_RANK["SCRIPTED_FIXTURE"] < ENVIRONMENT_RANK["GAZEBO"]


# --- the motion and safety boundary -----------------------------------------


@pytest.mark.parametrize("field", ["joint_trajectory", "can_frame", "torque", "emergency_stop"])
def test_row_may_not_carry_raw_control_fields(field, contract, actions):
    row = valid_row(**{field: "value"})
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_FORBIDDEN_FIELD" for verdict in verdicts for finding in verdict.findings)


@pytest.mark.parametrize("field", ["policy", "verifier_impl", "bypass"])
def test_row_may_not_declare_a_second_implementation(field, contract, actions):
    row = valid_row(**{field: "impl"})
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_FORBIDDEN_FIELD" for verdict in verdicts for finding in verdict.findings)


def test_unknown_semantic_action_fails(contract, actions):
    row = valid_row(semantic_actions=["observe", "levitate"])
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_INVALID_ACTION" for verdict in verdicts for finding in verdict.findings)


def test_unknown_adapter_fails(contract, actions):
    row = valid_row(required_adapters=["motion", "telepathy"])
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_INVALID_ADAPTER" for verdict in verdicts for finding in verdict.findings)


def test_missing_capability_must_use_the_declared_vocabulary(contract, actions):
    row = valid_row(missing_capabilities=["probably_fine"])
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_INVALID_STATUS" for verdict in verdicts for finding in verdict.findings)


# --- required fields, ownership and references ------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "scenario_id",
        "scenario_version",
        "task_family",
        "status",
        "environment_status",
        "scenario_rules_owner",
        "adapter_owner",
        "evidence_owner",
        "release_owner",
        "missing_capabilities",
    ],
)
def test_each_required_field_is_required(field, contract, actions):
    row = valid_row()
    row.pop(field)
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_MISSING_FIELD" for verdict in verdicts for finding in verdict.findings)


@pytest.mark.parametrize("field", ["scenario_rules_owner", "adapter_owner", "evidence_owner", "release_owner"])
def test_every_ownership_column_must_name_an_owner(field, contract, actions):
    row = valid_row(**{field: "   "})
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_INVALID_OWNER" for verdict in verdicts for finding in verdict.findings)


def test_unknown_row_field_fails(contract, actions):
    row = valid_row(surprise="value")
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_UNKNOWN_FIELD" for verdict in verdicts for finding in verdict.findings)


def test_a_row_that_links_a_missing_file_fails(contract, actions):
    row = valid_row(evidence_report="docs/evaluation/absent.md")
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_UNRESOLVED_EVIDENCE" for verdict in verdicts for finding in verdict.findings)


def test_a_row_that_traverses_out_of_the_repository_fails(contract, actions):
    row = valid_row(manifest="../../etc/passwd")
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_UNRESOLVED_EVIDENCE" for verdict in verdicts for finding in verdict.findings)


def test_a_registered_row_must_name_a_verifier(contract, actions):
    row = valid_row()
    row.pop("verifier")
    exit_code, verdicts = validate_matrix(
        matrix_with([row]),
        registered={"sample-scenario@1.0": {"evidence_status": "SCRIPTED_FIXTURE"}},
        contract=contract,
        approved_actions=actions,
        root=ROOT,
    )
    assert exit_code == FAIL
    assert any(finding.code == "MATRIX_MISSING_FIELD" for verdict in verdicts for finding in verdict.findings)


# --- document structure and generated output --------------------------------


def test_unknown_matrix_version_is_refused(tmp_path: Path):
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix_with([], matrix_version="matrix-v99")), encoding="utf-8")
    with pytest.raises(CapabilityMatrixError):
        load_matrix(path)


def test_missing_matrix_key_is_refused(tmp_path: Path):
    document = matrix_with([])
    document.pop("rows")
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CapabilityMatrixError):
        load_matrix(path)


def test_unknown_matrix_key_is_refused(tmp_path: Path):
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix_with([], surprise=True)), encoding="utf-8")
    with pytest.raises(CapabilityMatrixError):
        load_matrix(path)


def test_malformed_json_is_refused(tmp_path: Path):
    path = tmp_path / "matrix.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CapabilityMatrixError):
        load_matrix(path)


def test_missing_matrix_file_is_refused(tmp_path: Path):
    with pytest.raises(CapabilityMatrixError):
        load_matrix(tmp_path / "absent.json")


def test_declared_environment_vocabulary_must_match(tmp_path: Path):
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix_with([], environment_statuses=["SCRIPTED_FIXTURE"])), encoding="utf-8")
    with pytest.raises(CapabilityMatrixError):
        load_matrix(path)


def test_generated_document_is_byte_identical_across_runs(committed: dict[str, Any]):
    assert generated_document(committed) == generated_document(copy.deepcopy(committed))


def test_generated_table_orders_rows_deterministically(committed: dict[str, Any]):
    shuffled = copy.deepcopy(committed)
    shuffled["rows"] = list(reversed(shuffled["rows"]))
    assert rows_to_markdown(shuffled) == rows_to_markdown(committed)


def test_committed_page_matches_the_matrix(committed: dict[str, Any]):
    assert GENERATED_PATH.read_text(encoding="utf-8") == generated_document(committed)


def test_generated_page_names_the_source_of_truth(committed: dict[str, Any]):
    document = generated_document(committed)
    assert "scenario-capability-matrix-v1.json" in document
    assert "Do not edit by hand" in document


# --- the command-line gate ---------------------------------------------------


def test_gate_passes_on_the_committed_matrix():
    result = subprocess.run(
        [sys.executable, str(GATE), "--check-generated"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_gate_reports_incomplete_for_an_unreadable_matrix(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(GATE), "--matrix", str(tmp_path / "absent.json")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 2
    assert "INCOMPLETE" in result.stderr


def test_gate_fails_when_the_generated_page_is_stale(committed: dict[str, Any], tmp_path: Path):
    changed = copy.deepcopy(committed)
    changed["rows"][0]["adapter_owner"] = "a changed cell that the table renders"
    result = run_gate(changed, tmp_path, "--check-generated")
    assert result.returncode == FAIL
    assert "MATRIX_GENERATED_STALE" in result.stderr


def test_gate_fails_a_registry_entry_without_a_row(committed: dict[str, Any], tmp_path: Path):
    changed = copy.deepcopy(committed)
    changed["rows"] = [row for row in changed["rows"] if row["scenario_id"] != "pick-place-red-block"]
    result = run_gate(changed, tmp_path)
    assert result.returncode == FAIL
    assert "MATRIX_MISSING_ROW" in result.stderr


def test_require_registered_rejects_intent_rows(committed: dict[str, Any], tmp_path: Path):
    result = run_gate(committed, tmp_path, "--require-registered")
    assert result.returncode == FAIL
    assert "MATRIX_UNREGISTERED_ROW" in result.stderr


def test_gate_json_summary_is_machine_readable(tmp_path: Path):
    import json as json_module

    result = subprocess.run(
        [sys.executable, str(GATE), "--json"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json_module.loads(result.stdout)
    assert payload["exit_code"] == 0
    assert payload["rows"] == len(load_matrix(MATRIX_PATH)["rows"])
    assert payload["failures"] == []


# --- the module stays a read-only reader ------------------------------------


def test_module_imports_no_runtime_or_transport_module():
    source = (ROOT / "libs/kernel/workbench/kernel/scenario_capability.py").read_text(encoding="utf-8")
    for forbidden in ("import rclpy", "import gazebo", "import moveit", "import serial", "import can"):
        assert forbidden not in source


def test_module_does_not_write_files():
    source = (ROOT / "libs/kernel/workbench/kernel/scenario_capability.py").read_text(encoding="utf-8")
    assert "write_text" not in source
    assert "open(" not in source


def test_module_exposes_no_network_or_subprocess_use():
    source = (ROOT / "libs/kernel/workbench/kernel/scenario_capability.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "socket", "urllib", "requests"):
        assert forbidden not in source
