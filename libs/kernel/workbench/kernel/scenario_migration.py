"""Legacy task-family entry points and their registry equivalents (Issue #301).

Before Issue #300 every task family was reached through its own scripted entry
point: a ``task_id`` string, a goal phrase and a verifier function imported by
name. Issue #301 asks for one selection path without rewriting those verifiers
or changing fixture semantics, so this module is the join that proves the two
paths agree.

Two properties are deliberate, and each has a test:

* **The equivalence is pinned, not recomputed loosely.** Every family records the
  digest of its legacy corpus (scenario IDs, seeds and materialized scene
  parameters) and the verifier entry point it has always used. The migration
  check recomputes both and refuses a mismatch, so registering a family cannot
  quietly change what its frozen fixtures mean.
* **The adapter is read-only.** Nothing here writes an event, opens a store or
  imports a simulator. It resolves a legacy ``task_id`` to the registry identity
  that now owns it, and reports the deprecation window for the old name.

The registry manifest is the single editable definition. ``sim/scenarios/**``
stays the regression corpus, and this module is what keeps the two in step.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scenario_registry import load_registry

DEFAULT_ROOT = Path.cwd()
REGISTRY_ROOT = Path("sim/registry")
SCENARIO_ROOT = Path("sim/scenarios")

# The deprecation window for the legacy ``task_id`` entry points. The registry
# identity is authoritative from the release named here; the old name still
# resolves, with a notice, until it is removed.
# The release the registry identity became authoritative, and the release the old
# task_id entry point is scheduled to be removed. They are deliberately separate:
# a removal date that is not announced is how an entry point disappears by accident.
AUTHORITATIVE_RELEASE = "v0.3"
LEGACY_DEPRECATION_RELEASE = "v0.4"
MIGRATION_NOTICE = (
    f"the registry identity is authoritative from {AUTHORITATIVE_RELEASE}; the task_id entry point is "
    f"deprecated and scheduled for removal in {LEGACY_DEPRECATION_RELEASE}"
)


class ScenarioMigrationError(RuntimeError):
    """A legacy entry point and its registry identity do not agree."""

    def __init__(self, message: str, *, code: str = "SCENARIO_MIGRATION_MISMATCH", path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class FamilyMigration:
    """One migrated task family, frozen so a caller cannot mutate the mapping."""

    task_id: str
    task_family: str
    identity: str
    scenario_id: str
    scenario_version: str
    verifier: str
    corpus_glob: str
    corpus_count: int
    corpus_digest: str
    legacy_goal: str
    semantic_actions: tuple[str, ...]
    deprecated: bool = True

    @property
    def notice(self) -> str:
        return MIGRATION_NOTICE if self.deprecated else "the scenario identity is the only entry point"

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_family": self.task_family,
            "identity": self.identity,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "verifier": self.verifier,
            "corpus_glob": self.corpus_glob,
            "corpus_count": self.corpus_count,
            "corpus_digest": self.corpus_digest,
            "legacy_goal": self.legacy_goal,
            "semantic_actions": list(self.semantic_actions),
            "deprecated": self.deprecated,
            "notice": self.notice,
        }


# The migration table. Every digest and count below is recomputed and compared by
# ``check_migration``; a different fixture set is a refusal, not a new baseline.
FAMILIES: tuple[FamilyMigration, ...] = (
    FamilyMigration(
        task_id="task-place-red-block",
        task_family="pick_place",
        identity="pick-place-red-block@1.0",
        scenario_id="pick-place-red-block",
        scenario_version="1.0",
        verifier="services/world_model/workbench_world_model/verifier.py::verify_object_in_tray",
        corpus_glob="sim/scenarios/frozen/*.json",
        corpus_count=12,
        corpus_digest="fddf5be4ae412f2f46c254b8ddfa95034cb6e741f1a4deeb8ab17291ea3819da",
        legacy_goal="Place the red block in the tray",
        semantic_actions=("observe", "grasp", "place"),
    ),
    FamilyMigration(
        task_id="task-kit-three-parts",
        task_family="kitting",
        identity="kit-three-parts@0.2",
        scenario_id="kit-three-parts",
        scenario_version="0.2",
        verifier="services/world_model/workbench_world_model/verifier.py::verify_kit_contents",
        corpus_glob="sim/scenarios/expanded/multi-object-*.json",
        corpus_count=6,
        corpus_digest="299470f8fcbd20302b32f0ca7c5a6e9977ea8ae7f976ab96f9517c6e9b82bb84",
        legacy_goal="Assemble a three-part kit in the kit tray",
        semantic_actions=("observe", "grasp", "place"),
    ),
    FamilyMigration(
        task_id="task-clear-workspace",
        task_family="cleaning",
        identity="clear-workspace@0.2",
        scenario_id="clear-workspace",
        scenario_version="0.2",
        verifier="services/world_model/workbench_world_model/verifier.py::verify_workspace_clearance",
        corpus_glob="sim/scenarios/expanded/path-blocked-*.json",
        corpus_count=6,
        corpus_digest="c9a5b8910796a97de3c8c5ba06ef29dd36e10fdacd51f8a34a265694e5e8d105",
        legacy_goal="Clear the blocking cylinder, then place the red block in the tray",
        semantic_actions=("observe", "grasp", "place"),
    ),
    FamilyMigration(
        task_id="task-inspect-workpieces",
        task_family="inspection",
        identity="inspect-workpieces@0.2",
        scenario_id="inspect-workpieces",
        scenario_version="0.2",
        verifier="services/world_model/workbench_world_model/verifier.py::verify_inspection_evidence",
        corpus_glob="sim/scenarios/expanded/low-light-*.json",
        corpus_count=6,
        corpus_digest="674f6cb094187e11752e62fb47177eb7bc2e6c76a321074a8c47fac3fe547548",
        legacy_goal="Inspect presence, identity, and orientation of three workpieces",
        semantic_actions=("observe",),
    ),
    FamilyMigration(
        task_id="task-sort-parcels",
        task_family="parcels",
        identity="sort-parcels@0.2",
        scenario_id="sort-parcels",
        scenario_version="0.2",
        verifier="services/world_model/workbench_world_model/verifier.py::verify_parcel_sorting",
        corpus_glob="sim/scenarios/expanded/parcel-intake-*.json",
        corpus_count=6,
        corpus_digest="9a8acfc98536bb36d26f05d51c7b8599e5a1e5a7ebe29aaeec0756d4a2b3158c",
        legacy_goal="Scan the parcel batch, route verified intact parcels to pickup, and isolate exceptions",
        semantic_actions=("observe", "grasp", "place"),
    ),
)

BY_TASK_ID = {family.task_id: family for family in FAMILIES}
BY_IDENTITY = {family.identity: family for family in FAMILIES}


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _materialize(manifest: dict[str, Any]) -> dict[str, Any]:
    """Reuse the frozen scripted materialization so the corpus digest is the same one."""

    from scenario_tools import materialize_scenario

    return materialize_scenario(manifest)


def corpus_entries(root: Path, pattern: str) -> list[tuple[str, int, str]]:
    """The legacy corpus as ``(scenario_id, seed, scene_hash)`` triples, sorted."""

    entries: list[tuple[str, int, str]] = []
    for path in sorted(root.glob(pattern)):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        entries.append((manifest["scenario_id"], manifest["seed"], canonical_hash(_materialize(manifest))))
    return entries


def corpus_digest(root: Path, pattern: str) -> str:
    return hashlib.sha256(json.dumps(corpus_entries(root, pattern), separators=(",", ":")).encode()).hexdigest()


def resolve_verifier(entry_point: str) -> Callable[..., Any]:
    """Import the verifier a manifest names, by its repository-relative entry point.

    This is the mechanism the migration proof uses to show that the legacy
    ``task_id`` and the registry identity reach the *same* function object, not
    two functions that happen to agree today.
    """

    module_path, separator, function_name = entry_point.partition("::")
    if separator != "::" or not function_name or not module_path.endswith(".py"):
        raise ScenarioMigrationError(
            f"{entry_point!r} is not a path.py::function entry point",
            code="SCENARIO_MIGRATION_INVALID_VERIFIER",
            path=entry_point,
        )
    parts = Path(module_path).with_suffix("").parts
    try:
        services_index = parts.index("services")
    except ValueError:
        raise ScenarioMigrationError(
            f"{entry_point!r} is not under services/",
            code="SCENARIO_MIGRATION_INVALID_VERIFIER",
            path=entry_point,
        ) from None
    # services/<name>/<package>/<module> -> <package>.<module>; the package root
    # is what enable_local_packages() puts on sys.path.
    qualified = ".".join(parts[services_index + 2 :])
    try:
        module = importlib.import_module(qualified)
    except ImportError as error:
        raise ScenarioMigrationError(
            f"cannot import {entry_point!r}: {error}",
            code="SCENARIO_MIGRATION_UNRESOLVED_VERIFIER",
            path=entry_point,
        ) from error
    function = getattr(module, function_name, None)
    if function is None:
        raise ScenarioMigrationError(
            f"{entry_point!r} names a function that does not exist",
            code="SCENARIO_MIGRATION_UNRESOLVED_VERIFIER",
            path=entry_point,
        )
    return function


def resolve_task_id(task_id: str) -> FamilyMigration:
    """Resolve a legacy ``task_id`` to its registry identity, or refuse."""

    family = BY_TASK_ID.get(task_id)
    if family is None:
        raise ScenarioMigrationError(
            f"unknown legacy task_id {task_id!r}",
            code="SCENARIO_MIGRATION_UNKNOWN_TASK_ID",
            path=task_id,
        )
    return family


def resolve_identity(identity: str) -> FamilyMigration:
    family = BY_IDENTITY.get(identity)
    if family is None:
        raise ScenarioMigrationError(
            f"identity {identity!r} is not a migrated task family",
            code="SCENARIO_MIGRATION_UNKNOWN_IDENTITY",
            path=identity,
        )
    return family


def migration_report(
    repo_root: Path = DEFAULT_ROOT,
    *,
    registry_root: Path | None = None,
) -> dict[str, Any]:
    """Join the legacy entry points to the live registry, failing closed."""

    registry = load_registry(
        registry_root if registry_root is not None else repo_root / REGISTRY_ROOT,
        repo_root=repo_root,
    )
    registered = {entry.identity: entry for entry in registry.entries}

    findings: list[dict[str, str]] = []
    families: list[dict[str, Any]] = []
    for family in FAMILIES:
        entry = registered.get(family.identity)
        if entry is None:
            findings.append(
                {
                    "code": "SCENARIO_MIGRATION_MISSING_REGISTRATION",
                    "path": family.identity,
                    "detail": f"legacy task_id {family.task_id!r} has no registry manifest",
                }
            )
        else:
            if entry.verifier != family.verifier:
                findings.append(
                    {
                        "code": "SCENARIO_MIGRATION_VERIFIER_DRIFT",
                        "path": family.identity,
                        "detail": f"registry verifier {entry.verifier!r} is not the legacy {family.verifier!r}",
                    }
                )
            declared = tuple(sorted(entry.semantic_actions))
            legacy_actions = tuple(sorted(family.semantic_actions))
            if declared != legacy_actions:
                findings.append(
                    {
                        "code": "SCENARIO_MIGRATION_ACTION_DRIFT",
                        "path": family.identity,
                        "detail": f"registry actions {declared} are not the legacy {legacy_actions}",
                    }
                )
            if entry.task_id != family.task_id:
                findings.append(
                    {
                        "code": "SCENARIO_MIGRATION_TASK_ID_DRIFT",
                        "path": family.identity,
                        "detail": f"registry declares task_id {entry.task_id!r}, not {family.task_id!r}",
                    }
                )

        entries = corpus_entries(repo_root, family.corpus_glob)
        digest = hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()
        if len(entries) != family.corpus_count or digest != family.corpus_digest:
            findings.append(
                {
                    "code": "SCENARIO_MIGRATION_CORPUS_DRIFT",
                    "path": family.corpus_glob,
                    "detail": (
                        f"the legacy corpus is {len(entries)} fixture(s) with digest {digest}, "
                        f"not {family.corpus_count} with {family.corpus_digest}"
                    ),
                }
            )
        families.append(
            {
                **family.as_dict(),
                "registered": entry is not None,
                "actual_corpus_count": len(entries),
                "actual_corpus_digest": digest,
            }
        )

    # The join runs the other way too: a registered identity that migrated here
    # must have a legacy entry point, so the mapping cannot silently shrink.
    migrated = {family.identity for family in FAMILIES}
    for identity in sorted(set(registered) - migrated):
        findings.append(
            {
                "code": "SCENARIO_MIGRATION_UNMAPPED_REGISTRATION",
                "path": identity,
                "detail": f"registered identity {identity} has no migrated legacy entry point",
            }
        )

    return {
        "migration_version": "scenario-migration-v1",
        "issue": 301,
        "authoritative_release": AUTHORITATIVE_RELEASE,
        "deprecation_release": LEGACY_DEPRECATION_RELEASE,
        "registry_root": str(registry_root or REGISTRY_ROOT),
        "families": families,
        "findings": findings,
        "ok": not findings,
    }
