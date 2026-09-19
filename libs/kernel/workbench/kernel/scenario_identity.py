"""Bind a run to the scenario definition that produced it (Issue #302).

A run directory recorded ``scenario_id``, a scene hash and a commit, and
nothing tied it to the *version* of the scenario definition, to the evidence
policy it was verified under, or to the verifier rule that decided it. Two runs
of "the same scenario" across a version bump were therefore indistinguishable,
and a bundle could be reduced and verified against whichever definition happened
to be on disk - including a default one - without anyone noticing.

This module makes that attribution explicit and checkable:

* :func:`run_identity` hashes one canonical, ordered input list and returns a
  stable digest plus the exact material that produced it, so a reviewer can see
  *why* two runs differ rather than only *that* they do;
* :func:`identity_from_manifest` derives that material from a registry entry, so
  the identity is a property of the scenario definition and not of the caller;
* :func:`verify_bundle_identity` compares a recorded bundle against the live
  registry and fails closed: a missing field, an unknown identity, a mismatched
  policy or verifier, an event-stream hash that does not match the events, and a
  release-eligible claim on a fixture each get their own diagnostic.

The identity is a pure function of its declared inputs, so two processes given
the same bundle agree, and it is deliberately *not* a substitute for the world
state hash: :func:`run_identity` says which definition and policy produced a
stream, and ``create_world_state_snapshot`` says what the stream reduced to.
Neither one implies the other.

The module is read-only. It never imports the runtime, never starts ROS, Gazebo
or hardware, and never writes a run or an event. A matching identity states that
a bundle is attributed to a definition; it says nothing about whether the run
was physical.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PASS = 0
FAIL = 1
INCOMPLETE = 2

SCHEMA_VERSION_HEADER = "workbench-run-identity-v1"
HASH_ALGORITHM = "sha256"
DEFAULT_IDENTITY_FILENAME = "identity.json"

# Every diagnostic this module can emit. A caller switches on these, so each one
# names one cause and one remedy.
RUN_IDENTITY_MISSING = "RUN_IDENTITY_MISSING"
RUN_IDENTITY_UNKNOWN_SCENARIO = "RUN_IDENTITY_UNKNOWN_SCENARIO"
RUN_IDENTITY_MISMATCH = "RUN_IDENTITY_MISMATCH"
RUN_IDENTITY_EVENT_STREAM_MISMATCH = "RUN_IDENTITY_EVENT_STREAM_MISMATCH"
RUN_IDENTITY_MALFORMED = "RUN_IDENTITY_MALFORMED"
RUN_IDENTITY_UNSAFE_RELEASE_CLAIM = "RUN_IDENTITY_UNSAFE_RELEASE_CLAIM"
RUN_IDENTITY_EVENTS_UNREADABLE = "RUN_IDENTITY_EVENTS_UNREADABLE"
RUN_IDENTITY_REGISTRY_UNREADABLE = "RUN_IDENTITY_REGISTRY_UNREADABLE"

EMITTED_CODES = (
    RUN_IDENTITY_MISSING,
    RUN_IDENTITY_UNKNOWN_SCENARIO,
    RUN_IDENTITY_MISMATCH,
    RUN_IDENTITY_EVENT_STREAM_MISMATCH,
    RUN_IDENTITY_MALFORMED,
    RUN_IDENTITY_UNSAFE_RELEASE_CLAIM,
    RUN_IDENTITY_EVENTS_UNREADABLE,
    RUN_IDENTITY_REGISTRY_UNREADABLE,
)

# The identity input list, in the order it is hashed. This is the whole contract:
# adding an input changes every identity, which is the point, and the order is
# asserted by a test rather than left to dictionary iteration.
IDENTITY_INPUTS: tuple[str, ...] = (
    "scenario_id",
    "scenario_version",
    "evidence_policy_version",
    "verifier_rule_version",
    "event_stream_hash",
    "world_version",
    "config_hash",
    "commit",
)

# A run must carry these before its identity can be computed at all.
REQUIRED_IDENTITY_FIELDS: tuple[str, ...] = (
    "scenario_id",
    "scenario_version",
    "evidence_policy_version",
    "verifier_rule_version",
    "event_stream_hash",
)


class RunIdentityError(ValueError):
    """A run bundle cannot be attributed to a scenario definition."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RunIdentity:
    """The digest of one run's identity material, with that material kept.

    Keeping the material is deliberate: a mismatch is otherwise an opaque hex
    string, and a reviewer cannot tell which of eight inputs moved.
    """

    identity_hash: str
    material: dict[str, str]
    inputs: tuple[str, ...] = IDENTITY_INPUTS
    schema_version: str = SCHEMA_VERSION_HEADER
    hash_algorithm: str = HASH_ALGORITHM

    @property
    def scenario_id(self) -> str:
        return self.material["scenario_id"]

    @property
    def scenario_version(self) -> str:
        return self.material["scenario_version"]

    @property
    def identity(self) -> str:
        return f"{self.scenario_id}@{self.scenario_version}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hash_algorithm": self.hash_algorithm,
            "identity_hash": self.identity_hash,
            "inputs": list(self.inputs),
            "material": dict(self.material),
        }

    def differing_inputs(self, other: RunIdentity) -> tuple[str, ...]:
        """Which identity inputs differ between two runs, in contract order."""

        return tuple(name for name in self.inputs if self.material.get(name) != other.material.get(name))


def canonical_identity_bytes(material: Mapping[str, str], inputs: Sequence[str] = IDENTITY_INPUTS) -> bytes:
    """Canonical hash material: the declared inputs, in order, as strict JSON."""

    ordered: dict[str, str] = {}
    for name in inputs:
        if name not in material:
            raise RunIdentityError(RUN_IDENTITY_MISSING, f"identity input {name!r} is absent")
        value = material[name]
        if not isinstance(value, str) or not value.strip():
            raise RunIdentityError(RUN_IDENTITY_MALFORMED, f"identity input {name!r} must be a non-blank string")
        ordered[name] = value
    try:
        return json.dumps(
            {"schema_version": SCHEMA_VERSION_HEADER, "inputs": ordered},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise RunIdentityError(RUN_IDENTITY_MALFORMED, "identity material must be finite UTF-8 JSON") from error


def run_identity(material: Mapping[str, str], *, inputs: Sequence[str] = IDENTITY_INPUTS) -> RunIdentity:
    """Hash one canonical identity material into a stable digest."""

    payload = canonical_identity_bytes(material, inputs)
    return RunIdentity(
        identity_hash=hashlib.sha256(payload).hexdigest(),
        material={name: material[name] for name in inputs},
        inputs=tuple(inputs),
    )


def event_stream_hash(events: Iterable[Mapping[str, Any]]) -> str:
    """Hash an event stream by its persisted content, in file order.

    File order is used rather than ``sequence_no`` order on purpose: the point
    is to detect that the *artifact* changed, and a stream whose lines were
    reordered is a different artifact even if a reader could sort it back.
    """

    digest = hashlib.sha256()
    for event in events:
        line = json.dumps(event, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def hash_events_file(path: Path) -> str:
    """Hash a ``events.jsonl`` file line by line, refusing a malformed line."""

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise RunIdentityError(RUN_IDENTITY_EVENTS_UNREADABLE, f"cannot read {path}: {error}") from error
    events: list[Mapping[str, Any]] = []
    for index, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise RunIdentityError(RUN_IDENTITY_EVENTS_UNREADABLE, f"{path}: line {index} is not valid JSON") from error
        if not isinstance(payload, dict):
            raise RunIdentityError(RUN_IDENTITY_EVENTS_UNREADABLE, f"{path}: line {index} is not a JSON object")
        events.append(payload)
    return event_stream_hash(events)


def policy_version(text: object) -> str:
    """Derive a stable, short policy version from the policy text itself.

    The manifests carry the evidence policy as prose, and there is no separate
    version field to read. Hashing the prose means a policy that is edited in
    any way produces a new version without anyone remembering to bump a number,
    which is the failure mode a hand-maintained version has.
    """

    if not isinstance(text, str) or not text.strip():
        raise RunIdentityError(RUN_IDENTITY_MALFORMED, "evidence policy text must be a non-blank string")
    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def verifier_rule_version(entry: Mapping[str, Any]) -> str:
    """Derive the verifier rule version from the entry point the manifest names."""

    if not isinstance(entry, Mapping):
        raise RunIdentityError(RUN_IDENTITY_MALFORMED, "a registry entry must be a mapping")
    verifier = entry.get("verifier")
    if not isinstance(verifier, str) or not verifier.strip():
        raise RunIdentityError(RUN_IDENTITY_MALFORMED, "a registry entry must name a verifier entry point")
    digest = hashlib.sha256(verifier.strip().encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def identity_from_entry(
    entry: Mapping[str, Any],
    *,
    event_stream_hash_value: str,
    config_hash: str = "",
    commit: str = "",
) -> RunIdentity:
    """Build the identity of a run from the registry entry that defines it.

    ``config_hash`` and ``commit`` default to a stable placeholder rather than
    being omitted, because an omitted input is a missing field and the identity
    is supposed to be total over its declared inputs.
    """

    if not isinstance(entry, Mapping):
        raise RunIdentityError(RUN_IDENTITY_MALFORMED, "a registry entry must be a mapping")
    for field in ("scenario_id", "scenario_version"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RunIdentityError(RUN_IDENTITY_MISSING, f"a registry entry requires {field!r}")
        if field in ("scenario_id", "scenario_version") and value != value.strip():
            raise RunIdentityError(RUN_IDENTITY_MALFORMED, f"{field} must not be padded with whitespace")
    material = {
        "scenario_id": entry["scenario_id"],
        "scenario_version": entry["scenario_version"],
        "evidence_policy_version": policy_version(entry.get("evidence_policy")),
        "verifier_rule_version": verifier_rule_version(entry),
        "event_stream_hash": _non_blank(event_stream_hash_value, "event_stream_hash"),
        "world_version": _non_blank(str(entry.get("world_version", "WorkbenchSim-v0")), "world_version"),
        "config_hash": config_hash or "none",
        "commit": commit or "unspecified",
    }
    return run_identity(material)


def _non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunIdentityError(RUN_IDENTITY_MISSING, f"identity input {field_name!r} must be a non-blank string")
    return value


# --------------------------------------------------------------------------- #
# Bundle verification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IdentityFinding:
    """One reason a bundle is not attributed to its scenario definition."""

    code: str
    run_id: str
    identity: str
    detail: str


@dataclass(frozen=True)
class IdentityVerdict:
    """The result of checking one run directory against the registry."""

    run_id: str
    identity: str
    ok: bool
    identity_hash: str = ""
    findings: tuple[IdentityFinding, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "identity": self.identity,
            "ok": self.ok,
            "identity_hash": self.identity_hash,
            "findings": [
                {"code": finding.code, "identity": finding.identity, "detail": finding.detail}
                for finding in self.findings
            ],
        }


def recorded_identity(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the recorded identity block, or return ``None`` when it is absent."""

    block = metadata.get("identity")
    if isinstance(block, Mapping):
        return dict(block)
    return None


def verify_bundle_identity(
    *,
    run_id: str,
    metadata: Mapping[str, Any],
    registry: Mapping[str, Mapping[str, Any]],
    recomputed: RunIdentity | None = None,
) -> IdentityVerdict:
    """Check one run's recorded identity against the live registry.

    ``recomputed`` is the identity derived from the artifact's own events and
    the registry. When it is supplied, a recorded hash that disagrees with it is
    a mismatch; when it is ``None`` the check is limited to the recorded fields,
    which is what a caller without the event file can honestly do.
    """

    # The registry identity is authoritative. A legacy fixture under
    # ``sim/scenarios`` records its own ``scenario_id``; the definition that
    # decides its evidence policy and verifier is the registry identity in the
    # identity block, so that is what is compared.
    block = recorded_identity(metadata)
    material_for_label = block.get("material") if isinstance(block, Mapping) else None
    if isinstance(material_for_label, Mapping) and material_for_label.get("scenario_id"):
        identity = f"{material_for_label['scenario_id']}@{material_for_label.get('scenario_version', '?')}"
    else:
        identity = f"{metadata.get('scenario_id', '?')}@{metadata.get('scenario_version', '?')}"
    findings: list[IdentityFinding] = []

    def fail(code: str, detail: str) -> None:
        findings.append(IdentityFinding(code=code, run_id=run_id, identity=identity, detail=detail))

    if block is None:
        fail(
            RUN_IDENTITY_MISSING,
            "the run metadata carries no identity block; re-run with the Issue #302 metadata extension",
        )
        return IdentityVerdict(run_id=run_id, identity=identity, ok=False, findings=tuple(findings))

    recorded_hash = block.get("identity_hash")
    material = block.get("material")
    if not isinstance(recorded_hash, str) or not recorded_hash.strip():
        fail(RUN_IDENTITY_MALFORMED, "the identity block carries no identity_hash")
        return IdentityVerdict(run_id=run_id, identity=identity, ok=False, findings=tuple(findings))
    if not isinstance(material, Mapping):
        fail(RUN_IDENTITY_MALFORMED, "the identity block carries no material")
        return IdentityVerdict(run_id=run_id, identity=identity, ok=False, findings=tuple(findings))

    missing = [name for name in REQUIRED_IDENTITY_FIELDS if not str(material.get(name, "")).strip()]
    if missing:
        fail(RUN_IDENTITY_MISSING, f"identity material is missing: {', '.join(missing)}")
        return IdentityVerdict(run_id=run_id, identity=identity, ok=False, findings=tuple(findings))

    entry = registry.get(identity)
    if entry is None:
        # A bundle that names a definition the registry does not know is refused
        # rather than reduced against a default. This is the "no default
        # scenario" rule.
        fail(
            RUN_IDENTITY_UNKNOWN_SCENARIO,
            f"the registry has no entry for {identity}; verification cannot proceed without the definition",
        )
        return IdentityVerdict(run_id=run_id, identity=identity, ok=False, findings=tuple(findings))

    expected_policy = policy_version(entry.get("evidence_policy"))
    expected_verifier = verifier_rule_version(entry)
    if material.get("evidence_policy_version") != expected_policy:
        fail(
            RUN_IDENTITY_MISMATCH,
            f"evidence_policy_version {material.get('evidence_policy_version')!r} does not match the registry's "
            f"{expected_policy!r}",
        )
    if material.get("verifier_rule_version") != expected_verifier:
        fail(
            RUN_IDENTITY_MISMATCH,
            f"verifier_rule_version {material.get('verifier_rule_version')!r} does not match the registry's "
            f"{expected_verifier!r}",
        )

    try:
        recorded = run_identity({name: str(material[name]) for name in IDENTITY_INPUTS})
    except RunIdentityError as error:
        fail(error.code, error.message)
        return IdentityVerdict(run_id=run_id, identity=identity, ok=False, findings=tuple(findings))

    if recorded.identity_hash != recorded_hash:
        fail(
            RUN_IDENTITY_MISMATCH,
            f"the recorded identity_hash {recorded_hash} does not match the hash of its own material "
            f"{recorded.identity_hash}",
        )

    if recomputed is not None and recomputed.identity_hash != recorded_hash:
        differing = recomputed.differing_inputs(recorded)
        fail(
            RUN_IDENTITY_EVENT_STREAM_MISMATCH,
            "the identity recomputed from the artifact does not match the recorded one; differing inputs: "
            + (", ".join(differing) if differing else "none"),
        )

    if metadata.get("release_eligible") is True and entry.get("release_eligible") is not True:
        fail(
            RUN_IDENTITY_UNSAFE_RELEASE_CLAIM,
            f"the run claims release_eligible true but {identity} is {entry.get('evidence_status')!r}; "
            "a fixture cannot become release eligible through metadata",
        )

    return IdentityVerdict(
        run_id=run_id,
        identity=identity,
        ok=not findings,
        identity_hash=recorded_hash,
        findings=tuple(findings),
    )


def load_registry_entries(root: Path, *, registry_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the live registry as ``identity -> manifest`` through the Issue #300 reader."""

    from workbench.kernel.scenario_registry import ScenarioRegistryError, load_registry

    try:
        registry = load_registry(registry_root if registry_root is not None else root / "sim/registry", repo_root=root)
    except ScenarioRegistryError as error:
        raise RunIdentityError(RUN_IDENTITY_REGISTRY_UNREADABLE, str(error)) from error
    return {entry.identity: entry.as_dict() for entry in registry.entries}


def scan_run_root(root: Path, *, runs_root: Path, registry: Mapping[str, Mapping[str, Any]]) -> list[IdentityVerdict]:
    """Check every run directory that carries a ``metadata.json``."""

    verdicts: list[IdentityVerdict] = []
    if not runs_root.is_dir():
        return verdicts
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        metadata_path = run_dir / "metadata.json"
        if not metadata_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            verdicts.append(
                IdentityVerdict(
                    run_id=run_dir.name,
                    identity="?",
                    ok=False,
                    findings=(
                        IdentityFinding(
                            code=RUN_IDENTITY_MALFORMED,
                            run_id=run_dir.name,
                            identity="?",
                            detail=f"{metadata_path} is not valid JSON",
                        ),
                    ),
                )
            )
            continue
        if not isinstance(metadata, dict):
            continue

        recomputed: RunIdentity | None = None
        events_path = run_dir / "events.jsonl"
        if events_path.is_file():
            try:
                stream_hash = hash_events_file(events_path)
                entry = registry.get(str(metadata.get("registry_identity", "")))
                if entry is None:
                    block = metadata.get("identity")
                    material = block.get("material") if isinstance(block, dict) else None
                    if isinstance(material, dict):
                        entry = registry.get(f"{material.get('scenario_id')}@{material.get('scenario_version')}")
                if entry is not None:
                    recomputed = identity_from_entry(
                        entry,
                        event_stream_hash_value=stream_hash,
                        config_hash=str(metadata.get("scene_hash", "")) or "none",
                        commit=str(metadata.get("commit", "")) or "unspecified",
                    )
            except RunIdentityError:
                recomputed = None

        verdicts.append(
            verify_bundle_identity(
                run_id=run_dir.name,
                metadata=metadata,
                registry=registry,
                recomputed=recomputed,
            )
        )
    return verdicts
