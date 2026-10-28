"""Tests for the Assurance Track register and its gate.

The register is the claim; this file is what stops the claim from rotting. Two
kinds of assertion live here:

* the committed artifacts pass, and the generated page is the page they generate;
* every rejection the gate can emit is asserted against an otherwise-valid copy,
  so a validator that rejects everything cannot pass this file.

The terminal-state vocabularies are read from the runtime's own source by AST
rather than by importing it: a drift test should fail when an enum value moves,
and it should not need a simulator to notice.
"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts"), str(ROOT / "libs/kernel"), str(ROOT / "libs/contracts")]

from workbench.kernel.assurance import (
    GENERATED_PATH,
    INVARIANTS_PATH,
    MATRIX_PATH,
    AssuranceDocuments,
    AssuranceError,
    generated_document,
    load_documents,
    validate,
)


def committed() -> AssuranceDocuments:
    return load_documents(INVARIANTS_PATH, MATRIX_PATH)


def mutated(**changes: object) -> AssuranceDocuments:
    """A deep copy of the committed register with one field replaced per call."""

    documents = committed()
    invariants = copy.deepcopy(documents.invariants)
    matrix = copy.deepcopy(documents.matrix)
    for dotted, value in changes.items():
        target = invariants if dotted.startswith("invariants") else matrix
        cursor: object = target
        parts = dotted.split(".")
        for part in parts[:-1]:
            cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
        if isinstance(cursor, list):
            cursor[int(parts[-1])] = value
        else:
            cursor[parts[-1]] = value
    return AssuranceDocuments(invariants=invariants, matrix=matrix)


def codes(verdict) -> set[str]:
    return {finding.code for finding in verdict.findings}


def test_committed_register_passes() -> None:
    verdict = validate(committed(), root=ROOT)
    assert verdict.exit_code == 0, [finding.detail for finding in verdict.findings]
    assert verdict.status == "PASS"


def test_generated_page_is_the_document_the_register_generates() -> None:
    assert GENERATED_PATH.read_text(encoding="utf-8") == generated_document(committed())


def test_gate_passes_on_the_committed_register() -> None:
    result = subprocess.run(
        [sys.executable, "tools/scripts/check_assurance.py", "--check-generated"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_environment(),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout


def test_gate_reports_incomplete_for_an_unreadable_register(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "tools/scripts/check_assurance.py", "--invariants", str(tmp_path / "missing.json")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_environment(),
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "INCOMPLETE" in result.stderr
    with pytest.raises(AssuranceError):
        load_documents(tmp_path / "missing.json", MATRIX_PATH)


def _environment() -> dict[str, str]:
    import os

    parts = ["libs/kernel", "libs/contracts", "services/agent_runtime", "services/world_model", "firmware/virtual_mcu"]
    existing = os.environ.get("PYTHONPATH", "")
    prefix = ":".join(str(ROOT / part) for part in parts)
    return {**os.environ, "PYTHONPATH": f"{prefix}:{existing}" if existing else prefix}


def test_an_enforced_invariant_without_resolving_evidence_fails() -> None:
    documents = mutated(**{"invariants.1.evidence": ["tests/unit/test_assurance_matrix.py::test_does_not_exist"]})
    verdict = validate(documents, root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_UNRESOLVED_EVIDENCE" in codes(verdict)


def test_a_not_implemented_invariant_that_cites_evidence_fails() -> None:
    documents = mutated(
        **{"invariants.8.evidence": ["tests/unit/test_assurance_matrix.py::test_committed_register_passes"]}
    )
    verdict = validate(documents, root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS" in codes(verdict)


def test_a_not_implemented_invariant_without_a_gap_fails() -> None:
    documents = mutated(**{"invariants.8.gap": ""})
    verdict = validate(documents, root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS" in codes(verdict)


def test_an_enforcement_symbol_that_does_not_resolve_fails() -> None:
    documents = mutated(**{"invariants.0.enforced_by": "tools/scripts/sim_cli.py::_renamed_helper"})
    verdict = validate(documents, root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_UNRESOLVED_SYMBOL" in codes(verdict)


def test_a_fault_case_that_expects_confirmation_fails() -> None:
    documents = mutated(**{"rows.0.expected_verdict": "confirmed"})
    verdict = validate(documents, root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_FAULT_CLAIMS_COMPLETION" in codes(verdict)


def test_an_undeclared_terminal_state_fails() -> None:
    documents = mutated(**{"rows.0.expected_terminal_state": {"domain": "execution", "state": "nearly_done"}})
    verdict = validate(documents, root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_INVALID_TERMINAL_STATE" in codes(verdict)


def test_a_row_referencing_an_undeclared_invariant_fails() -> None:
    documents = mutated(**{"rows.0.invariants": ["INV-99"]})
    verdict = validate(documents, root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_UNKNOWN_INVARIANT" in codes(verdict)


def test_an_invariant_no_case_exercises_fails() -> None:
    documents = mutated()
    rows = [row for row in documents.matrix["rows"] if row["case_id"] != "evidence-gap"]
    matrix = {
        **documents.matrix,
        "rows": rows,
        "required_faults": [f for f in documents.matrix["required_faults"] if f != "evidence_gap"],
    }
    verdict = validate(AssuranceDocuments(invariants=documents.invariants, matrix=matrix), root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_UNCOVERED_INVARIANT" in codes(verdict)


def test_a_fault_with_no_acceptance_case_fails() -> None:
    documents = mutated()
    rows = [row for row in documents.matrix["rows"] if row["fault"] != "ack_late"]
    matrix = {**documents.matrix, "rows": rows}
    verdict = validate(AssuranceDocuments(invariants=documents.invariants, matrix=matrix), root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_UNKNOWN_FAULT" in codes(verdict)


def test_an_unexecuted_case_with_evidence_fails() -> None:
    documents = mutated(**{"rows.7.evidence": ["tests/unit/test_assurance_matrix.py::test_committed_register_passes"]})
    verdict = validate(documents, root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS" in codes(verdict)


def test_an_executed_case_without_evidence_fails() -> None:
    documents = mutated(**{"rows.5.evidence": []})
    verdict = validate(documents, root=ROOT)
    assert verdict.exit_code == 1
    assert "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS" in codes(verdict)


def test_declared_terminal_states_exist_in_the_runtime() -> None:
    """A state name in the matrix must be a name the runtime actually has."""

    declared = committed().matrix["terminal_states"]
    assert set(declared["execution"]) == _enum_values(
        "services/agent_runtime/workbench_agent_runtime/execution_controller.py", "ExecutionState"
    )
    assert set(declared["can_link"]) == _enum_values(
        "libs/hardware/workbench/hardware/can_driver_safe.py", "CanLinkState"
    )
    assert set(declared["gate_reason"]) <= _enum_values(
        "services/agent_runtime/workbench_agent_runtime/post_action.py", "GateReasonCode"
    )
    assert set(declared["run_status"]) == _assigned_set("tools/scripts/sim_cli.py", "RUNNER_STATUSES")


def _enum_values(relative: str, class_name: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            values: set[str] = set()
            for statement in node.body:
                if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant):
                    if isinstance(statement.value.value, str):
                        values.add(statement.value.value)
            return values
    raise AssertionError(f"{class_name} not found in {relative}")


def _assigned_set(relative: str, name: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in {relative}")


def test_unregistered_scenario_cannot_start_a_run(tmp_path: Path) -> None:
    """INV-01: the refusal happens before a runner starts and leaves no artifact."""

    import sim_cli

    manifest = json.loads((ROOT / "sim/scenarios/frozen/normal-001.json").read_text(encoding="utf-8"))
    manifest["scenario_id"] = "not-a-registered-scenario"
    manifest["task_id"] = "task-not-in-this-repository"
    # Built directly: the loader refuses an unknown task_id even earlier, and this
    # test is about the admission guard inside run_scenario, not about the loader.
    scenario = sim_cli.Scenario(tmp_path / "unregistered.json", manifest, b"{}", {})

    output = tmp_path / "runs"
    with pytest.raises(sim_cli.SimulationInputError):
        sim_cli.run_scenario(scenario, runner="scripted", output_dir=output, version="assurance-test")

    assert not output.exists(), "a refused run must not create an output directory"
