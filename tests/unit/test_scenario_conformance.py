"""Conformance tests for every registered scenario (Issue #304).

Issue #304 requires that a registered scenario cannot enter the registry without
a committed test that proves it honours the shared fail-closed boundaries, and
that CI names the scenario and the failed rule when it does not. This file is the
per-scenario proof the corpus in ``tools/qa/scenario-conformance-v1.json`` points
at: each registered identity has one test per required dimension.

The dimension tests are generated from the same table the gate reads, so adding a
registered scenario without adding its cases fails
``tools/scripts/check_scenario_conformance.py`` with the exact identity and the
missing dimension, rather than silently passing.

The tests here do not re-implement the verifiers. They exercise the shared
helpers in ``workbench.kernel.scenario_conformance`` and the World Model
verifiers the manifests name, so a change to a verifier that breaks a boundary
fails here as well as in ``tests/unit/test_world_model.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "tools/scripts"),
    str(ROOT / "libs/kernel"),
    str(ROOT / "libs/contracts"),
    str(ROOT / "services/world_model"),
]

from workbench.kernel.scenario_conformance import (
    DEFAULT_CORPUS_PATH,
    PROBE_FAMILIES,
    REQUIRED_DIMENSIONS,
    ScenarioConformanceError,
    defined_tests,
    evaluate,
    load_corpus,
    parse_cases,
    reference_probes,
    replay_digest,
    resolve_test,
    scan_manifest,
)
from workbench.kernel.scenario_registry import load_registry

REGISTRY_ROOT = ROOT / "sim/registry"
CORPUS_PATH = ROOT / DEFAULT_CORPUS_PATH

# The scenario identities this file proves, and the test-name slug each uses. The
# gate resolves the corpus entries against the functions defined here, so a
# rename that forgets the corpus is a failure rather than a silent gap.
SCENARIO_SLUGS: dict[str, str] = {
    "pick-place-red-block@1.0": "pick_place",
    "kit-three-parts@0.2": "kitting",
    "inspect-workpieces@0.2": "inspection",
    "clear-workspace@0.2": "cleaning",
    "sort-parcels@0.2": "parcels",
}


def _probe_dimension(identity: str, dimension: str):
    outcomes = {outcome.dimension: outcome for outcome in reference_probes(identity)}
    assert dimension in outcomes, f"{identity} derives no {dimension!r} probe"
    return outcomes[dimension]


def _registry_entries() -> dict[str, dict[str, object]]:
    registry = load_registry(REGISTRY_ROOT, repo_root=ROOT)
    return {entry.identity: entry.as_dict() for entry in registry.entries}


def test_pick_place_conflicting_evidence():
    """Evidence that disagrees with the goal cannot produce a confirmation."""

    outcome = _probe_dimension("pick-place-red-block@1.0", "conflicting_evidence")
    assert outcome.ok, outcome.detail


def test_pick_place_deterministic_replay():
    """Two reductions of one event stream produce the same state hash."""

    first, second = replay_digest("pick-place-red-block@1.0")
    assert first == second, f"replay drifted: {first} != {second}"


def test_pick_place_missing_provenance():
    """Removing provenance references cannot produce a confirmation."""

    outcome = _probe_dimension("pick-place-red-block@1.0", "missing_provenance")
    assert outcome.ok, outcome.detail


def test_pick_place_policy_valid_actions():
    """The confirmed probe state verifies as confirmed through the named verifier."""

    outcome = _probe_dimension("pick-place-red-block@1.0", "policy_valid_actions")
    assert outcome.ok, outcome.detail


def test_pick_place_release_status_truthful():
    """A scripted fixture is never release eligible."""

    manifest = _registry_entries()["pick-place-red-block@1.0"]
    assert manifest["evidence_status"] == "SCRIPTED_FIXTURE"
    assert manifest["release_eligible"] is False


def test_pick_place_stale_evidence():
    """A lost belief cannot confirm and is reported as a stale observation."""

    outcome = _probe_dimension("pick-place-red-block@1.0", "stale_evidence")
    assert outcome.ok, outcome.detail


def test_pick_place_unsafe_field_rejected():
    """A raw-control or bypass key is refused by the conformance scan."""

    manifest = _registry_entries()["pick-place-red-block@1.0"]
    assert scan_manifest(manifest) == [], f"committed manifest carries an unsafe key: {scan_manifest(manifest)}"
    # The scan must also be shown to detect one, so this cannot pass by never
    # firing. A nested key is the case a shallow reader misses.
    injected = dict(manifest)
    injected["nested"] = {"motor_command": {"joint_velocities": [1.0]}}
    codes = [path for path, _detail in scan_manifest(injected)]
    assert "$.nested.motor_command" in codes
    assert "$.nested.motor_command.joint_velocities" in codes


def test_kitting_conflicting_evidence():
    """Evidence that disagrees with the goal cannot produce a confirmation."""

    outcome = _probe_dimension("kit-three-parts@0.2", "conflicting_evidence")
    assert outcome.ok, outcome.detail


def test_kitting_deterministic_replay():
    """Two reductions of one event stream produce the same state hash."""

    first, second = replay_digest("kit-three-parts@0.2")
    assert first == second, f"replay drifted: {first} != {second}"


def test_kitting_missing_provenance():
    """Removing provenance references cannot produce a confirmation."""

    outcome = _probe_dimension("kit-three-parts@0.2", "missing_provenance")
    assert outcome.ok, outcome.detail


def test_kitting_policy_valid_actions():
    """The confirmed probe state verifies as confirmed through the named verifier."""

    outcome = _probe_dimension("kit-three-parts@0.2", "policy_valid_actions")
    assert outcome.ok, outcome.detail


def test_kitting_release_status_truthful():
    """A scripted fixture is never release eligible."""

    manifest = _registry_entries()["kit-three-parts@0.2"]
    assert manifest["evidence_status"] == "SCRIPTED_FIXTURE"
    assert manifest["release_eligible"] is False


def test_kitting_stale_evidence():
    """A lost belief cannot confirm and is reported as a stale observation."""

    outcome = _probe_dimension("kit-three-parts@0.2", "stale_evidence")
    assert outcome.ok, outcome.detail


def test_kitting_unsafe_field_rejected():
    """A raw-control or bypass key is refused by the conformance scan."""

    manifest = _registry_entries()["kit-three-parts@0.2"]
    assert scan_manifest(manifest) == [], f"committed manifest carries an unsafe key: {scan_manifest(manifest)}"
    # The scan must also be shown to detect one, so this cannot pass by never
    # firing. A nested key is the case a shallow reader misses.
    injected = dict(manifest)
    injected["nested"] = {"motor_command": {"joint_velocities": [1.0]}}
    codes = [path for path, _detail in scan_manifest(injected)]
    assert "$.nested.motor_command" in codes
    assert "$.nested.motor_command.joint_velocities" in codes


def test_inspection_conflicting_evidence():
    """Evidence that disagrees with the goal cannot produce a confirmation."""

    outcome = _probe_dimension("inspect-workpieces@0.2", "conflicting_evidence")
    assert outcome.ok, outcome.detail


def test_inspection_deterministic_replay():
    """Two reductions of one event stream produce the same state hash."""

    first, second = replay_digest("inspect-workpieces@0.2")
    assert first == second, f"replay drifted: {first} != {second}"


def test_inspection_missing_provenance():
    """Removing provenance references cannot produce a confirmation."""

    outcome = _probe_dimension("inspect-workpieces@0.2", "missing_provenance")
    assert outcome.ok, outcome.detail


def test_inspection_policy_valid_actions():
    """The confirmed probe state verifies as confirmed through the named verifier."""

    outcome = _probe_dimension("inspect-workpieces@0.2", "policy_valid_actions")
    assert outcome.ok, outcome.detail


def test_inspection_release_status_truthful():
    """A scripted fixture is never release eligible."""

    manifest = _registry_entries()["inspect-workpieces@0.2"]
    assert manifest["evidence_status"] == "SCRIPTED_FIXTURE"
    assert manifest["release_eligible"] is False


def test_inspection_stale_evidence():
    """A lost belief cannot confirm and is reported as a stale observation."""

    outcome = _probe_dimension("inspect-workpieces@0.2", "stale_evidence")
    assert outcome.ok, outcome.detail


def test_inspection_unsafe_field_rejected():
    """A raw-control or bypass key is refused by the conformance scan."""

    manifest = _registry_entries()["inspect-workpieces@0.2"]
    assert scan_manifest(manifest) == [], f"committed manifest carries an unsafe key: {scan_manifest(manifest)}"
    # The scan must also be shown to detect one, so this cannot pass by never
    # firing. A nested key is the case a shallow reader misses.
    injected = dict(manifest)
    injected["nested"] = {"motor_command": {"joint_velocities": [1.0]}}
    codes = [path for path, _detail in scan_manifest(injected)]
    assert "$.nested.motor_command" in codes
    assert "$.nested.motor_command.joint_velocities" in codes


def test_cleaning_conflicting_evidence():
    """Evidence that disagrees with the goal cannot produce a confirmation."""

    outcome = _probe_dimension("clear-workspace@0.2", "conflicting_evidence")
    assert outcome.ok, outcome.detail


def test_cleaning_deterministic_replay():
    """Two reductions of one event stream produce the same state hash."""

    first, second = replay_digest("clear-workspace@0.2")
    assert first == second, f"replay drifted: {first} != {second}"


def test_cleaning_missing_provenance():
    """Removing provenance references cannot produce a confirmation."""

    outcome = _probe_dimension("clear-workspace@0.2", "missing_provenance")
    assert outcome.ok, outcome.detail


def test_cleaning_policy_valid_actions():
    """The confirmed probe state verifies as confirmed through the named verifier."""

    outcome = _probe_dimension("clear-workspace@0.2", "policy_valid_actions")
    assert outcome.ok, outcome.detail


def test_cleaning_release_status_truthful():
    """A scripted fixture is never release eligible."""

    manifest = _registry_entries()["clear-workspace@0.2"]
    assert manifest["evidence_status"] == "SCRIPTED_FIXTURE"
    assert manifest["release_eligible"] is False


def test_cleaning_stale_evidence():
    """A lost belief cannot confirm and is reported as a stale observation."""

    outcome = _probe_dimension("clear-workspace@0.2", "stale_evidence")
    assert outcome.ok, outcome.detail


def test_cleaning_unsafe_field_rejected():
    """A raw-control or bypass key is refused by the conformance scan."""

    manifest = _registry_entries()["clear-workspace@0.2"]
    assert scan_manifest(manifest) == [], f"committed manifest carries an unsafe key: {scan_manifest(manifest)}"
    # The scan must also be shown to detect one, so this cannot pass by never
    # firing. A nested key is the case a shallow reader misses.
    injected = dict(manifest)
    injected["nested"] = {"motor_command": {"joint_velocities": [1.0]}}
    codes = [path for path, _detail in scan_manifest(injected)]
    assert "$.nested.motor_command" in codes
    assert "$.nested.motor_command.joint_velocities" in codes


def test_parcels_conflicting_evidence():
    """Evidence that disagrees with the goal cannot produce a confirmation."""

    outcome = _probe_dimension("sort-parcels@0.2", "conflicting_evidence")
    assert outcome.ok, outcome.detail


def test_parcels_deterministic_replay():
    """Two reductions of one event stream produce the same state hash."""

    first, second = replay_digest("sort-parcels@0.2")
    assert first == second, f"replay drifted: {first} != {second}"


def test_parcels_missing_provenance():
    """Removing provenance references cannot produce a confirmation."""

    outcome = _probe_dimension("sort-parcels@0.2", "missing_provenance")
    assert outcome.ok, outcome.detail


def test_parcels_policy_valid_actions():
    """The confirmed probe state verifies as confirmed through the named verifier."""

    outcome = _probe_dimension("sort-parcels@0.2", "policy_valid_actions")
    assert outcome.ok, outcome.detail


def test_parcels_release_status_truthful():
    """A scripted fixture is never release eligible."""

    manifest = _registry_entries()["sort-parcels@0.2"]
    assert manifest["evidence_status"] == "SCRIPTED_FIXTURE"
    assert manifest["release_eligible"] is False


def test_parcels_stale_evidence():
    """A lost belief cannot confirm and is reported as a stale observation."""

    outcome = _probe_dimension("sort-parcels@0.2", "stale_evidence")
    assert outcome.ok, outcome.detail


def test_parcels_unsafe_field_rejected():
    """A raw-control or bypass key is refused by the conformance scan."""

    manifest = _registry_entries()["sort-parcels@0.2"]
    assert scan_manifest(manifest) == [], f"committed manifest carries an unsafe key: {scan_manifest(manifest)}"
    # The scan must also be shown to detect one, so this cannot pass by never
    # firing. A nested key is the case a shallow reader misses.
    injected = dict(manifest)
    injected["nested"] = {"motor_command": {"joint_velocities": [1.0]}}
    codes = [path for path, _detail in scan_manifest(injected)]
    assert "$.nested.motor_command" in codes
    assert "$.nested.motor_command.joint_velocities" in codes


# --- corpus and gate structure -------------------------------------------------


def test_corpus_uses_the_shared_dimension_list():
    corpus = load_corpus(CORPUS_PATH)
    assert sorted(corpus["required_dimensions"]) == sorted(REQUIRED_DIMENSIONS)


def test_every_registered_identity_has_a_case_set_and_every_case_resolves():
    entries = _registry_entries()
    corpus = load_corpus(CORPUS_PATH)
    assert sorted(corpus["scenarios"]) == sorted(entries)

    for identity, raw in corpus["scenarios"].items():
        cases = parse_cases(identity, raw, path=CORPUS_PATH)
        assert {case.dimension for case in cases} == set(REQUIRED_DIMENSIONS)
        for case in cases:
            module_path, function_name = resolve_test(case.test)
            source = (ROOT / module_path).read_text(encoding="utf-8")
            assert function_name in defined_tests(source), case.test


def test_every_probe_family_is_registered_and_names_a_known_verifier():
    from workbench.kernel.scenario_conformance import PROBE_VERIFIERS

    entries = _registry_entries()
    assert sorted(PROBE_FAMILIES) == sorted(entries)
    for identity, family in PROBE_FAMILIES.items():
        assert family.verifier in PROBE_VERIFIERS, identity
        declared = entries[identity]["verifier"]
        assert declared.endswith(f"::{family.verifier}"), (identity, declared)


def test_probe_families_match_the_manifest_verifier_and_actions():
    entries = _registry_entries()
    for identity, family in PROBE_FAMILIES.items():
        entry = entries[identity]
        assert entry["verifier"].endswith(f"::{family.verifier}")
        assert "observe" in entry["semantic_actions"]


def test_evaluate_passes_the_committed_registry():
    entries = _registry_entries()
    corpus = load_corpus(CORPUS_PATH)
    exit_code, verdicts = evaluate(root=ROOT, registry_entries=entries, corpus=corpus, corpus_path=CORPUS_PATH)
    assert exit_code == 0, [verdict.as_dict() for verdict in verdicts if not verdict.ok]
    assert all(verdict.ok for verdict in verdicts)
    assert {verdict.identity for verdict in verdicts} == set(entries)


def test_evaluate_fails_a_registered_scenario_with_no_case_set():
    entries = _registry_entries()
    corpus = load_corpus(CORPUS_PATH)
    reachable = {identity: entry for identity, entry in entries.items() if identity in PROBE_FAMILIES}
    identity = sorted(reachable)[0]
    trimmed = {"scenarios": {k: v for k, v in corpus["scenarios"].items() if k != identity}}
    trimmed["required_dimensions"] = corpus["required_dimensions"]
    exit_code, verdicts = evaluate(root=ROOT, registry_entries=entries, corpus=trimmed, corpus_path=CORPUS_PATH)
    assert exit_code == 1
    failed = {verdict.identity: verdict for verdict in verdicts}
    codes = {finding.code for finding in failed[identity].findings}
    assert "SCENARIO_CONFORMANCE_MISSING_CASE_SET" in codes


def test_evaluate_fails_a_case_set_missing_a_dimension():
    entries = _registry_entries()
    corpus = load_corpus(CORPUS_PATH)
    identity = sorted(entries)[0]
    scenarios = dict(corpus["scenarios"])
    scenarios[identity] = [case for case in scenarios[identity] if case["dimension"] != "stale_evidence"]
    trimmed = {"scenarios": scenarios, "required_dimensions": corpus["required_dimensions"]}
    exit_code, verdicts = evaluate(root=ROOT, registry_entries=entries, corpus=trimmed, corpus_path=CORPUS_PATH)
    assert exit_code == 1
    failed = {verdict.identity: verdict for verdict in verdicts}
    codes = {finding.code for finding in failed[identity].findings}
    assert "SCENARIO_CONFORMANCE_MISSING_DIMENSION" in codes
    assert failed[identity].as_dict()["missing"] == ["stale_evidence"]


def test_evaluate_fails_a_case_that_names_a_missing_test():
    entries = _registry_entries()
    corpus = load_corpus(CORPUS_PATH)
    identity = sorted(entries)[0]
    scenarios = dict(corpus["scenarios"])
    scenarios[identity] = list(scenarios[identity])
    scenarios[identity][0] = dict(scenarios[identity][0])
    scenarios[identity][0]["test"] = "tests/unit/test_scenario_conformance.py::test_does_not_exist"
    trimmed = {"scenarios": scenarios, "required_dimensions": corpus["required_dimensions"]}
    exit_code, verdicts = evaluate(root=ROOT, registry_entries=entries, corpus=trimmed, corpus_path=CORPUS_PATH)
    assert exit_code == 1
    failed = {verdict.identity: verdict for verdict in verdicts}
    codes = {finding.code for finding in failed[identity].findings}
    assert "SCENARIO_CONFORMANCE_TEST_UNRESOLVED" in codes


def test_evaluate_fails_a_corpus_entry_for_an_unregistered_identity():
    entries = _registry_entries()
    corpus = load_corpus(CORPUS_PATH)
    scenarios = dict(corpus["scenarios"])
    scenarios["ghost-scenario@9.9"] = scenarios[sorted(scenarios)[0]]
    trimmed = {"scenarios": scenarios, "required_dimensions": corpus["required_dimensions"]}
    exit_code, verdicts = evaluate(root=ROOT, registry_entries=entries, corpus=trimmed, corpus_path=CORPUS_PATH)
    assert exit_code == 1
    codes = {finding.code for verdict in verdicts for finding in verdict.findings}
    assert "SCENARIO_CONFORMANCE_STALE_CASE_SET" in codes


def test_evaluate_refuses_a_raw_control_field_injected_into_a_manifest():
    entries = _registry_entries()
    corpus = load_corpus(CORPUS_PATH)
    identity = sorted(entries)[0]
    unsafe = {k: dict(v) for k, v in entries.items()}
    unsafe[identity]["controller"] = {"joint_trajectory": ["0", "1"]}
    exit_code, verdicts = evaluate(root=ROOT, registry_entries=unsafe, corpus=corpus, corpus_path=CORPUS_PATH)
    assert exit_code == 1
    failed = {verdict.identity: verdict for verdict in verdicts}
    codes = {finding.code for finding in failed[identity].findings}
    assert "SCENARIO_CONFORMANCE_UNSAFE_FIELD" in codes


def test_evaluate_refuses_a_fixture_that_claims_release_eligibility():
    entries = _registry_entries()
    corpus = load_corpus(CORPUS_PATH)
    identity = sorted(entries)[0]
    lying = {k: dict(v) for k, v in entries.items()}
    lying[identity]["release_eligible"] = True
    exit_code, verdicts = evaluate(root=ROOT, registry_entries=lying, corpus=corpus, corpus_path=CORPUS_PATH)
    assert exit_code == 1
    failed = {verdict.identity: verdict for verdict in verdicts}
    codes = {finding.code for finding in failed[identity].findings}
    assert "SCENARIO_CONFORMANCE_UNTRUTHFUL_RELEASE_STATUS" in codes


def test_corpus_rejects_a_duplicate_dimension():
    with pytest.raises(Exception) as error:
        parse_cases(
            "sample@1.0",
            [
                {"dimension": "stale_evidence", "test": "tests/unit/test_scenario_conformance.py::f", "detail": "a"},
                {"dimension": "stale_evidence", "test": "tests/unit/test_scenario_conformance.py::g", "detail": "b"},
            ],
            path=CORPUS_PATH,
        )
    assert "twice" in str(error.value)


def test_corpus_rejects_an_unknown_dimension():
    with pytest.raises(Exception) as error:
        parse_cases(
            "sample@1.0",
            [{"dimension": "made_up", "test": "tests/unit/test_scenario_conformance.py::f", "detail": "a"}],
            path=CORPUS_PATH,
        )
    assert "unknown dimension" in str(error.value)


@pytest.mark.parametrize(
    "reference",
    [
        "../../../etc/secrets.py::test_x",
        "/etc/secrets.py::test_x",
        "tests\\unit\\test_x.py::test_x",
    ],
)
def test_corpus_rejects_an_unsafe_test_path(reference: str):
    """A case path must stay inside the repository, and be a POSIX .py path."""

    with pytest.raises(ScenarioConformanceError):
        resolve_test(reference)


def test_the_conformance_module_never_writes_or_starts_a_runtime():
    source = (ROOT / "libs/kernel/workbench/kernel/scenario_conformance.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "socket", "open(", "write_text", "write_bytes", "urlopen", "requests", "gazebo"):
        assert forbidden not in source, f"scenario_conformance.py must not contain {forbidden!r}"


def test_the_gate_never_imports_a_simulator():
    source = (ROOT / "tools/scripts/check_scenario_conformance.py").read_text(encoding="utf-8")
    for forbidden in ("gazebo", "ros2", "moveit", "subprocess"):
        assert forbidden not in source, f"check_scenario_conformance.py must not contain {forbidden!r}"


def test_committed_corpus_matches_the_registry_identity_set():
    corpus = load_corpus(CORPUS_PATH)
    entries = _registry_entries()
    assert sorted(corpus["scenarios"]) == sorted(entries), (
        "a registry manifest was added or removed without updating tools/qa/scenario-conformance-v1.json"
    )
    on_disk = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert on_disk == corpus
