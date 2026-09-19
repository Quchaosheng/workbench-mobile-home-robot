"""The fail-closed Scenario Registry (Issue #300).

Operators list and select known scenarios by stable ``scenario_id@scenario_version``.
Every manifest is validated against the frozen contract in
``workbench.kernel.scenario_contract`` before a run exists, so an invalid or
unsafe definition is rejected before the first event is written.

Three properties are deliberate, and each has a test:

* **Loading is deterministic and order independent.** Discovery is a sorted
  directory walk and the catalog is sorted by ``(scenario_id, scenario_version)``,
  so two runs over the same tree produce the same order.
* **Failure is closed.** A malformed, duplicate, unsafe or incompatible manifest
  raises :class:`ScenarioRegistryError` with a stable diagnostic code rather than
  being skipped. There is no partial catalog.
* **Read-only and dependency-free.** The registry imports no ROS, Gazebo, MoveIt
  or hardware SDK. It reads JSON and returns immutable records; it never opens
  the Event Store, starts a run, or writes anything.

Version matching is exact. There is no closest-match path and no implicit
upgrade, because a replay that resolves to a different scenario body than its
run is the failure this boundary exists to prevent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scenario_contract import (
    ERROR,
    ContractError,
    Verdict,
    approved_semantic_actions,
    load_contract,
    validate_many,
)

DEFAULT_MANIFEST_ROOT = Path("sim/scenarios")
MAX_MANIFEST_BYTES = 64 * 1024


class ScenarioRegistryError(ValueError):
    """A registry could not be loaded, or a manifest is not registerable."""

    def __init__(self, message: str, *, code: str = "SCENARIO_REGISTRY_INVALID", path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class ScenarioEntry:
    """One registered scenario, frozen so a caller cannot mutate the catalog."""

    scenario_id: str
    scenario_version: str
    goal: str
    semantic_actions: tuple[str, ...]
    required_adapters: tuple[str, ...]
    evidence_status: str
    evidence_policy: str
    verifier: str
    path: str
    release_eligible: bool
    executable: bool
    notices: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        return f"{self.scenario_id}@{self.scenario_version}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "identity": self.identity,
            "goal": self.goal,
            "semantic_actions": list(self.semantic_actions),
            "required_adapters": list(self.required_adapters),
            "evidence_status": self.evidence_status,
            "evidence_policy": self.evidence_policy,
            "verifier": self.verifier,
            "path": self.path,
            "release_eligible": self.release_eligible,
            "executable": self.executable,
            "notices": list(self.notices),
        }


def _manifest_paths(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ScenarioRegistryError(f"manifest root is not a directory: {root}", code="SCENARIO_REGISTRY_ROOT_MISSING")
    # Sorted so discovery order does not depend on the filesystem.
    return sorted(path for path in root.rglob("*.json") if path.is_file())


def _read_manifest(path: Path, *, base: Path) -> tuple[str, Any]:
    label = _relative_label(path, base)
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ScenarioRegistryError(
            f"cannot stat {label}: {error}", code="SCENARIO_REGISTRY_READ_FAILED", path=label
        ) from error
    if size > MAX_MANIFEST_BYTES:
        raise ScenarioRegistryError(
            f"{label} is {size} bytes; the limit is {MAX_MANIFEST_BYTES}",
            code="SCENARIO_REGISTRY_MANIFEST_TOO_LARGE",
            path=label,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ScenarioRegistryError(
            f"cannot read {label}: {error}", code="SCENARIO_REGISTRY_READ_FAILED", path=label
        ) from error
    except json.JSONDecodeError as error:
        raise ScenarioRegistryError(
            f"{label} is not valid JSON: {error}", code="SCENARIO_REGISTRY_MALFORMED_JSON", path=label
        ) from error
    return label, payload


def _relative_label(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _entry(
    label: str, manifest: dict[str, Any], verdict: Verdict, contract: dict[str, Any], *, base: Path
) -> ScenarioEntry:
    evidence_status = manifest["evidence_status"]
    notices = tuple(finding.detail for finding in verdict.findings if finding.severity != ERROR)
    return ScenarioEntry(
        scenario_id=manifest["scenario_id"],
        scenario_version=manifest["scenario_version"],
        goal=manifest["goal"],
        semantic_actions=tuple(manifest["semantic_actions"]),
        required_adapters=tuple(manifest["required_adapters"]),
        evidence_status=evidence_status,
        evidence_policy=manifest["evidence_policy"],
        verifier=manifest["verifier"],
        path=label,
        release_eligible=evidence_status not in contract["non_release_eligible_status"],
        executable=verdict.status != "NOT_EXECUTABLE",
        notices=notices,
    )


class ScenarioRegistry:
    """An immutable, validated view of every registered scenario."""

    def __init__(self, entries: tuple[ScenarioEntry, ...], *, contract_version: str) -> None:
        self._entries = entries
        self.contract_version = contract_version
        self._by_id: dict[str, ScenarioEntry] = {entry.scenario_id: entry for entry in entries}

    @property
    def entries(self) -> tuple[ScenarioEntry, ...]:
        return self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)

    def resolve(self, scenario_id: str, scenario_version: str | None = None) -> ScenarioEntry:
        """Resolve an identity, or raise with a stable diagnostic code.

        ``scenario_version`` is exact when supplied. When it is omitted the ID
        must be unambiguous: two registered versions of one ID is an error, not
        an arbitrary pick, because silently choosing the newest is the implicit
        upgrade this registry refuses.
        """

        entry = self._by_id.get(scenario_id)
        if entry is None:
            raise ScenarioRegistryError(
                f"unknown scenario_id {scenario_id!r}", code="SCENARIO_REGISTRY_UNKNOWN_ID", path=scenario_id
            )
        if scenario_version is None:
            same_id = [candidate for candidate in self._entries if candidate.scenario_id == scenario_id]
            if len(same_id) > 1:
                raise ScenarioRegistryError(
                    f"scenario_id {scenario_id!r} has multiple versions; name one explicitly",
                    code="SCENARIO_REGISTRY_AMBIGUOUS_ID",
                    path=scenario_id,
                )
            return entry
        if entry.scenario_version != scenario_version:
            raise ScenarioRegistryError(
                f"{scenario_id!r} is registered at version {entry.scenario_version!r}, not {scenario_version!r}",
                code="SCENARIO_REGISTRY_VERSION_MISMATCH",
                path=scenario_id,
            )
        return entry

    def catalog(self) -> list[dict[str, Any]]:
        return [entry.as_dict() for entry in self._entries]

    def describe(self, scenario_id: str, scenario_version: str | None = None) -> dict[str, Any]:
        return self.resolve(scenario_id, scenario_version).as_dict()


def load_registry(
    root: Path | None = None,
    *,
    repo_root: Path | None = None,
    contract_path: Path | None = None,
    require_executable: bool = False,
) -> ScenarioRegistry:
    """Load and validate every manifest under ``root``.

    The whole set is validated before any entry is returned, so a caller can
    never observe a partially valid registry.
    """

    base = Path(repo_root) if repo_root is not None else Path.cwd()
    manifest_root = Path(root) if root is not None else base / DEFAULT_MANIFEST_ROOT
    contract = load_contract(contract_path) if contract_path is not None else load_contract()
    try:
        actions = approved_semantic_actions()
    except ContractError as error:
        raise ScenarioRegistryError(str(error), code="SCENARIO_REGISTRY_CONTRACT_UNREADABLE") from error

    paths = _manifest_paths(manifest_root)
    if not paths:
        raise ScenarioRegistryError(
            f"no scenario manifests found under {manifest_root}", code="SCENARIO_REGISTRY_EMPTY"
        )

    loaded = [_read_manifest(path, base=base) for path in paths]
    exit_code, verdicts = validate_many(
        loaded,
        contract,
        approved_actions=actions,
        root=base,
        require_executable=require_executable,
    )
    if exit_code != 0:
        failures = [verdict for verdict in verdicts if not verdict.ok]
        first = failures[0]
        detail = "; ".join(f"{finding.code} {finding.path}: {finding.detail}" for finding in first.findings)
        raise ScenarioRegistryError(
            f"{first.manifest} is not registerable: {detail}",
            code=first.findings[0].code if first.findings else "SCENARIO_REGISTRY_INVALID",
            path=first.manifest,
        )

    by_label = {verdict.manifest: verdict for verdict in verdicts}
    entries = []
    for label, manifest in loaded:
        verdict = by_label.get(label)
        if verdict is None:  # pragma: no cover - validate_many returns one verdict per manifest
            raise ScenarioRegistryError(
                f"no verdict produced for {label}", code="SCENARIO_REGISTRY_INVALID", path=label
            )
        entries.append(_entry(label, manifest, verdict, contract, base=base))

    # Deterministic, order-independent catalog order.
    entries.sort(key=lambda entry: (entry.scenario_id, entry.scenario_version))
    return ScenarioRegistry(tuple(entries), contract_version=contract["contract_version"])
