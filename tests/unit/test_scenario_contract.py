"""Contract tests for the Issue #308 multi-scenario registry boundary.

Issue #308 freezes a contract, so the contract is what gets tested. Every
rejection the contract claims is exercised here on a manifest that is otherwise
valid, because a validator that rejects everything proves nothing. The accepted
case is the committed example manifest, so the example cannot drift away from
the contract without failing this file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts"), str(ROOT / "libs/kernel"), str(ROOT / "libs/contracts")]

# The rules live in the kernel so this gate and the Issue #300 registry enforce
# one contract. ``rules`` is that shared module; ``gate`` is the command-line
# reader, imported so the tests can prove it reports the same verdicts.
import check_scenario_contract as gate
from workbench.kernel import scenario_contract as rules

EXAMPLE_DIR = ROOT / "docs/architecture/examples"
HAPPY_PATH = EXAMPLE_DIR / "scenario-pick-place-red-block-v1.json"


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return rules.load_contract()


@pytest.fixture(scope="module")
def actions() -> frozenset[str]:
    return rules.approved_semantic_actions()


@pytest.fixture
def manifest() -> dict[str, Any]:
    return json.loads(HAPPY_PATH.read_text(encoding="utf-8"))


def codes(verdict: rules.Verdict) -> set[str]:
    return {finding.code for finding in verdict.findings}


def test_committed_example_is_accepted(contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]):
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert verdict.status == "PASS", verdict.findings
    assert verdict.exit_code == rules.PASS


def test_contract_file_declares_every_emitted_code(contract: dict[str, Any]):
    assert set(rules.EMITTED_CODES) <= set(contract["diagnostic_codes"])


def test_action_vocabulary_is_imported_from_the_shared_contract(actions: frozenset[str]):
    assert "grasp" in actions and "place" in actions
    assert "clean_workspace" not in actions


@pytest.mark.parametrize("field", ["scenario_id", "scenario_version", "goal", "semantic_actions"])
def test_missing_required_field_is_reported(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any], field: str
):
    del manifest[field]
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert verdict.status == "FAIL"
    assert "SCENARIO_MISSING_FIELD" in codes(verdict)


def test_unknown_field_fails_closed(contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]):
    manifest["future_field"] = "value"
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_UNKNOWN_FIELD" in codes(verdict)


@pytest.mark.parametrize(
    "field",
    ["joint_positions", "joint_trajectory", "velocity", "torque", "can_frame", "controller_goal", "emergency_stop"],
)
def test_forbidden_field_name_is_rejected(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any], field: str
):
    manifest[field] = [0.1, 0.2]
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert verdict.status == "FAIL"
    assert "SCENARIO_FORBIDDEN_FIELD" in codes(verdict)


def test_forbidden_field_is_rejected_when_nested(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]
):
    manifest["recovery_policy"] = {"joint_positions": [0.0]}
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_FORBIDDEN_FIELD" in codes(verdict)


@pytest.mark.parametrize("field", ["joint_torque", "can_payload", "drive_velocity"])
def test_forbidden_substring_is_rejected(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any], field: str
):
    manifest[field] = 1
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_FORBIDDEN_FIELD" in codes(verdict)


@pytest.mark.parametrize("field", ["policy", "verifier_impl", "bypass"])
def test_second_policy_or_verifier_implementation_is_rejected(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any], field: str
):
    manifest[field] = "inline"
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_FORBIDDEN_FIELD" in codes(verdict)


@pytest.mark.parametrize("value", ["../escape", "with space", "UPPER", "", "a/b"])
def test_unstable_scenario_id_is_rejected(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any], value: str
):
    manifest["scenario_id"] = value
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_INVALID_ID" in codes(verdict)


@pytest.mark.parametrize("value", ["1", "1.0.0", "v1.0", "^1.0", ""])
def test_inexact_scenario_version_is_rejected(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any], value: str
):
    manifest["scenario_version"] = value
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_INVALID_VERSION" in codes(verdict)


def test_unknown_semantic_action_is_rejected(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]
):
    manifest["semantic_actions"] = ["observe", "levitate"]
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_INVALID_ACTION" in codes(verdict)


def test_non_empty_actions_are_required(contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]):
    manifest["semantic_actions"] = []
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_INVALID_ACTION" in codes(verdict)


def test_unknown_adapter_is_rejected(contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]):
    manifest["required_adapters"] = ["motion", "telepathy"]
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_INVALID_ADAPTER" in codes(verdict)


def test_empty_adapter_list_is_rejected(contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]):
    manifest["required_adapters"] = []
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_INVALID_ADAPTER" in codes(verdict)


@pytest.mark.parametrize("value", ["physical", "SIMULATION", "", "gazebo "])
def test_evidence_status_outside_the_vocabulary_is_rejected(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any], value: str
):
    manifest["evidence_status"] = value
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_INVALID_EVIDENCE_STATUS" in codes(verdict)


def test_scripted_fixture_is_not_release_eligible(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]
):
    assert "SCRIPTED_FIXTURE" in contract["non_release_eligible_status"]
    assert manifest["evidence_status"] in contract["non_release_eligible_status"]


@pytest.mark.parametrize("value", ["/etc/passwd.py::verify", "../../outside.py::verify", "verifier.py", ""])
def test_unsafe_verifier_entry_point_is_rejected(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any], value: str
):
    manifest["verifier"] = value
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_INVALID_VERIFIER" in codes(verdict)


def test_oversized_string_is_rejected(contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]):
    manifest["goal"] = "x" * (contract["bounds"]["max_string_length"] + 1)
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_OVERSIZED_VALUE" in codes(verdict)


def test_oversized_list_is_rejected(contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]):
    limit = contract["bounds"]["max_list_items"]
    manifest["non_goals"] = [f"item {index}" for index in range(limit + 1)]
    verdict = rules.validate_manifest(manifest, contract, approved_actions=actions)
    assert "SCENARIO_OVERSIZED_VALUE" in codes(verdict)


def test_non_object_manifest_is_rejected(contract: dict[str, Any], actions: frozenset[str]):
    verdict = rules.validate_manifest(["not", "an", "object"], contract, approved_actions=actions)
    assert verdict.status == "FAIL"
    assert "SCENARIO_MISSING_FIELD" in codes(verdict)


def test_duplicate_identity_fails_closed(contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]):
    other = dict(manifest)
    other["goal"] = "A different goal on the same identity"
    findings = rules.duplicate_findings([("a.json", manifest), ("b.json", other)])
    assert [finding.code for finding in findings] == ["SCENARIO_DUPLICATE_ID"]


def test_distinct_versions_are_not_duplicates(
    contract: dict[str, Any], actions: frozenset[str], manifest: dict[str, Any]
):
    other = dict(manifest)
    other["scenario_version"] = "2.0"
    assert rules.duplicate_findings([("a.json", manifest), ("b.json", other)]) == []


def test_cleaning_example_is_not_executable_rather_than_invalid(contract: dict[str, Any], actions: frozenset[str]):
    """The hypothetical cleaning scenario documents a shape the runtime cannot run yet."""

    cleaning = json.loads((EXAMPLE_DIR / "scenario-clean-workspace-v1.json").read_text(encoding="utf-8"))
    verdict = rules.validate_manifest(cleaning, contract, approved_actions=actions)
    assert verdict.status == "NOT_EXECUTABLE"
    assert verdict.exit_code == rules.PASS
    assert codes(verdict) == {"SCENARIO_PENDING_ACTION"}
    assert cleaning["evidence_status"] == "NOT_EXECUTED"


def test_require_executable_turns_a_pending_action_into_a_failure():
    exit_code, verdicts = gate.run([EXAMPLE_DIR / "scenario-clean-workspace-v1.json"], require_executable=True)
    assert exit_code == rules.FAIL
    assert [verdict.status for verdict in verdicts] == ["FAIL"]


def test_examples_pass_without_require_executable():
    exit_code, verdicts = gate.run(sorted(EXAMPLE_DIR.glob("*.json")))
    assert exit_code == rules.PASS, [verdict.findings for verdict in verdicts]


def test_cli_reports_pass_for_committed_examples():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/scripts/check_scenario_contract.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == rules.PASS, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_cli_reports_failure_for_a_malicious_manifest(tmp_path: Path, manifest: dict[str, Any]):
    manifest["semantic_actions"] = ["observe", "launch_missile"]
    bad = tmp_path / "malicious.json"
    bad.write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/scripts/check_scenario_contract.py"), str(bad)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == rules.FAIL
    assert "SCENARIO_INVALID_ACTION" in result.stdout


def test_cli_reports_incomplete_when_the_contract_is_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gate, "CONTRACT_PATH", tmp_path / "absent.json")
    assert gate.main([]) == rules.INCOMPLETE


def test_detector_never_imports_the_runtime_or_opens_a_run():
    """The gate must not be able to start a run, a simulator or a device."""

    sources = {
        "gate": ROOT / "tools/scripts/check_scenario_contract.py",
        "rules": ROOT / "libs/kernel/workbench/kernel/scenario_contract.py",
    }
    for label, path in sources.items():
        source = path.read_text(encoding="utf-8")
        for forbidden in ("import subprocess", "rclpy", "gazebo", "socket", "http.client"):
            assert forbidden not in source, f"{label} must not reference {forbidden}"
