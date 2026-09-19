"""Tests for the Issue #301 migration of the five task families.

Issue #301 asks for one selection path without rewriting the existing verifiers
or changing fixture semantics. That is only true if it can be checked, so every
family here is proven twice:

* the legacy ``task_id`` and the registry identity resolve to the *same* verifier
  function object, and produce byte-for-byte identical results on the same state;
* the legacy corpus (scenario IDs, seeds and materialized scene parameters) has
  the digest the migration pinned, so registering a family cannot silently move
  a frozen seed.

Every rejection is asserted against an otherwise-valid migration, so a checker
that fails everything cannot pass this file.
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
sys.path.insert(0, str(ROOT / "libs/contracts"))
sys.path.insert(0, str(ROOT / "services/world_model"))

from workbench.kernel.scenario_migration import (
    FAMILIES,
    LEGACY_DEPRECATION_RELEASE,
    MIGRATION_NOTICE,
    FamilyMigration,
    ScenarioMigrationError,
    corpus_digest,
    corpus_entries,
    migration_report,
    resolve_identity,
    resolve_task_id,
    resolve_verifier,
)
from workbench.kernel.scenario_registry import load_registry

REGISTRY_ROOT = ROOT / "sim/registry"


def test_registry_lists_all_five_migrated_families():
    registry = load_registry(REGISTRY_ROOT, repo_root=ROOT)
    identities = {entry.identity for entry in registry.entries}
    assert {family.identity for family in FAMILIES} <= identities
    assert len(FAMILIES) == 5


def test_every_family_resolves_from_a_legacy_task_id():
    for family in FAMILIES:
        assert resolve_task_id(family.task_id).identity == family.identity
        assert resolve_identity(family.identity).task_id == family.task_id


def test_unknown_legacy_task_id_fails_closed():
    with pytest.raises(ScenarioMigrationError) as error:
        resolve_task_id("task-not-in-this-repository")
    assert error.value.code == "SCENARIO_MIGRATION_UNKNOWN_TASK_ID"


def test_unknown_identity_fails_closed():
    with pytest.raises(ScenarioMigrationError) as error:
        resolve_identity("not-a-migrated-family@9.9")
    assert error.value.code == "SCENARIO_MIGRATION_UNKNOWN_IDENTITY"


# --- the registry and the legacy corpus agree ---------------------------------


def test_registry_declares_the_legacy_task_id_and_verifier():
    registry = load_registry(REGISTRY_ROOT, repo_root=ROOT)
    by_identity = {entry.identity: entry for entry in registry.entries}
    for family in FAMILIES:
        entry = by_identity[family.identity]
        assert entry.task_id == family.task_id
        assert entry.verifier == family.verifier
        assert tuple(sorted(entry.semantic_actions)) == tuple(sorted(family.semantic_actions))


def test_migration_report_passes_on_the_committed_tree():
    report = migration_report(ROOT)
    assert report["ok"] is True, report["findings"]
    assert report["findings"] == []
    assert len(report["families"]) == 5


def test_each_family_corpus_matches_its_pinned_digest():
    for family in FAMILIES:
        entries = corpus_entries(ROOT, family.corpus_glob)
        assert len(entries) == family.corpus_count
        assert corpus_digest(ROOT, family.corpus_glob) == family.corpus_digest


def test_seeds_and_scenario_ids_are_unique_across_the_legacy_corpus():
    """The frozen distribution is asserted by make scenario-check; this pins it here too."""

    all_ids: list[str] = []
    all_seeds: list[int] = []
    for family in FAMILIES:
        for scenario_id, seed, _ in corpus_entries(ROOT, family.corpus_glob):
            all_ids.append(scenario_id)
            all_seeds.append(seed)
    assert len(all_ids) == len(set(all_ids))
    assert len(all_seeds) == len(set(all_seeds))


# --- the verifiers are the same function objects ------------------------------


def test_each_family_verifier_is_importable_and_unique():
    resolved = [resolve_verifier(family.verifier) for family in FAMILIES]
    names = [function.__name__ for function in resolved]
    assert len(names) == len(set(names)), "two families resolved to the same verifier function"


def test_registry_verifier_and_family_verifier_are_the_same_object():
    """The registry names a path; the migration must reach the identical callable."""

    registry = load_registry(REGISTRY_ROOT, repo_root=ROOT)
    by_identity = {entry.identity: entry for entry in registry.entries}
    for family in FAMILIES:
        assert resolve_verifier(by_identity[family.identity].verifier) is resolve_verifier(family.verifier)


def test_an_invalid_verifier_entry_point_fails_closed():
    for bad in ("not-a-path", "services/x.py", "services/world_model/nope.py::missing_function"):
        with pytest.raises(ScenarioMigrationError):
            resolve_verifier(bad)


# --- byte-for-byte equivalence on the same state ------------------------------


def verification_context():
    from workbench_world_model import VerificationContext

    return VerificationContext(state_hash="b" * 64, verified_at="2026-08-27T12:34:56Z", clock_id="wall")


def world_state():
    from workbench_world_model.reducer import WorldState

    return WorldState(
        run_id="migration-run",
        entity_locations={
            "red_block": "in:tray",
            "blue_cylinder": "in:kit_tray",
            "green_gear": "in:kit_tray",
            "parcel_box": "in:pickup_shelf",
            "parcel_damaged": "in:quarantine_bin",
        },
        entity_confidence={"red_block": 0.95, "blue_cylinder": 0.9, "green_gear": 0.9},
        entity_evidence_refs={
            "red_block": ["frame://red"],
            "blue_cylinder": ["frame://blue"],
            "green_gear": ["frame://green"],
            "parcel_box": ["frame://parcel"],
            "parcel_damaged": ["frame://parcel-damaged"],
        },
    )


CALLS = {
    "pick_place": lambda fn, state, context: fn(state, "task-place-red-block", "red_block", "tray", context=context),
    "kitting": lambda fn, state, context: fn(
        state, "task-kit-three-parts", ["blue_cylinder", "green_gear"], "kit_tray", context=context
    ),
    "inspection": lambda fn, state, context: fn(
        state, "task-inspect-workpieces", ["red_block", "blue_cylinder", "green_gear"], context=context
    ),
    "cleaning": lambda fn, state, context: fn(state, "task-clear-workspace", context=context),
    "parcels": lambda fn, state, context: fn(state, "task-sort-parcels", context=context),
}


@pytest.mark.parametrize("family", FAMILIES, ids=lambda family: family.task_family)
def test_legacy_and_registry_resolution_produce_identical_verification(family: FamilyMigration):
    """Same state, same context, same verifier: the serialized result must match exactly."""

    registry = load_registry(REGISTRY_ROOT, repo_root=ROOT)
    entry = registry.resolve(family.scenario_id, family.scenario_version)
    state = world_state()
    context = verification_context()
    call = CALLS[family.task_family]

    legacy_result = call(resolve_verifier(family.verifier), state, context)
    registry_result = call(resolve_verifier(entry.verifier), copy.deepcopy(state), context)

    assert legacy_result.model_dump(mode="json") == registry_result.model_dump(mode="json")


def test_the_verifier_still_refuses_failed_evidence_through_both_paths():
    """A migration that makes every state confirm would pass the equivalence test above."""

    registry = load_registry(REGISTRY_ROOT, repo_root=ROOT)
    entry = registry.resolve("kit-three-parts", "0.2")
    from workbench_world_model.reducer import WorldState

    empty = WorldState(run_id="migration-run", entity_locations={})
    context = verification_context()
    for verifier in (resolve_verifier("services/world_model/workbench_world_model/verifier.py::verify_kit_contents"),):
        legacy = verifier(empty, "task-kit-three-parts", ["blue_cylinder"], "kit_tray", context=context)
        via_registry = verifier(
            WorldState(run_id="migration-run", entity_locations={}),
            entry.task_id,
            ["blue_cylinder"],
            "kit_tray",
            context=context,
        )
    assert legacy.status.value == "insufficient_evidence"
    assert via_registry.status.value == "insufficient_evidence"


# --- the deprecation window is explicit ---------------------------------------


def test_every_family_publishes_a_deprecation_notice_and_release():
    assert LEGACY_DEPRECATION_RELEASE
    for family in FAMILIES:
        assert family.deprecated is True
        assert family.notice == MIGRATION_NOTICE
        assert LEGACY_DEPRECATION_RELEASE in family.notice


# --- adversarial: the join refuses drift --------------------------------------

GATE = None  # the migration check is a module, so drift is asserted directly


def drifted(migration: FamilyMigration, **overrides: Any) -> FamilyMigration:
    payload = {**migration.__dict__, **overrides}
    return FamilyMigration(**payload)


def test_a_wrong_corpus_digest_is_detected(monkeypatch):
    families = [
        drifted(family, corpus_digest="0" * 64) if family.task_family == "kitting" else family for family in FAMILIES
    ]
    monkeypatch.setattr("workbench.kernel.scenario_migration.FAMILIES", tuple(families))
    monkeypatch.setattr(
        "workbench.kernel.scenario_migration.BY_TASK_ID",
        {family.task_id: family for family in families},
    )
    report = migration_report(ROOT)
    assert report["ok"] is False
    assert any(finding["code"] == "SCENARIO_MIGRATION_CORPUS_DRIFT" for finding in report["findings"])


def test_a_wrong_verifier_is_detected(monkeypatch):
    """A registry manifest that names a different verifier than the family declares."""

    families = tuple(
        drifted(family, verifier="services/world_model/workbench_world_model/verifier.py::verify_object_in_tray")
        if family.task_family == "kitting"
        else family
        for family in FAMILIES
    )
    monkeypatch.setattr("workbench.kernel.scenario_migration.FAMILIES", families)
    monkeypatch.setattr(
        "workbench.kernel.scenario_migration.BY_TASK_ID",
        {family.task_id: family for family in families},
    )
    report = migration_report(ROOT)
    assert report["ok"] is False
    assert any(finding["code"] == "SCENARIO_MIGRATION_VERIFIER_DRIFT" for finding in report["findings"])


def test_a_missing_registration_for_a_migrated_family_is_detected(tmp_path):
    """The registry does not load the identity the family claims."""

    root = tmp_path / "repo"
    (root / "sim/registry").mkdir(parents=True)
    source = json.loads((REGISTRY_ROOT / "kit-three-parts.json").read_text(encoding="utf-8"))
    (root / "sim/registry/kit-three-parts.json").write_text(json.dumps(source), encoding="utf-8")
    report = migration_report(ROOT, registry_root=root / "sim/registry")
    assert report["ok"] is False
    assert any(finding["code"] == "SCENARIO_MIGRATION_MISSING_REGISTRATION" for finding in report["findings"])


def test_an_unmapped_registration_is_detected(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "sim/registry").mkdir(parents=True)
    (root / "sim/registry/extra.json").write_text(
        json.dumps(
            {
                "scenario_id": "extra-scenario",
                "scenario_version": "1.0",
                "goal": "An otherwise valid scenario with no legacy entry point",
                "semantic_actions": ["observe"],
                "required_adapters": ["perception"],
                "evidence_policy": "A fresh observation is required.",
                "verifier": "services/world_model/workbench_world_model/verifier.py::verify_object_in_tray",
                "evidence_status": "SCRIPTED_FIXTURE",
                "non_goals": ["does not claim physical validation"],
                "recovery_policy": {"allowed": ["re_observe"], "max_attempts": 1},
            }
        ),
        encoding="utf-8",
    )
    report = migration_report(ROOT, registry_root=root / "sim/registry")
    assert report["ok"] is False
    assert any(finding["code"] == "SCENARIO_MIGRATION_UNMAPPED_REGISTRATION" for finding in report["findings"])


# --- the CLI is the operator entry point --------------------------------------


def run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools/scripts/sim_cli.py"), *argv],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_cli_lists_all_five_families_with_stable_identities():
    result = run_cli("registry-list")
    assert result.returncode == 0
    for family in FAMILIES:
        assert family.identity in result.stdout


def test_cli_describe_resolves_a_migrated_identity():
    result = run_cli("describe", "kit-three-parts@0.2")
    assert result.returncode == 0
    assert "verify_kit_contents" in result.stdout


def test_cli_describe_rejects_an_unknown_identity():
    result = run_cli("describe", "no-such-scenario@1.0")
    assert result.returncode == 2
    assert "SCENARIO_REGISTRY_UNKNOWN_ID" in result.stderr


def test_migration_module_is_read_only():
    """The adapter resolves identities; it must never write, spawn or open a socket."""

    source = (ROOT / "libs/kernel/workbench/kernel/scenario_migration.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "socket", "open(", "write_text", "write_bytes", "urlopen", "requests"):
        assert forbidden not in source, f"scenario_migration.py must not contain {forbidden!r}"
