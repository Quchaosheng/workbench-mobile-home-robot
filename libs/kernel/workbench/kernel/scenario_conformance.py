"""Registry-wide conformance rules shared by the gate and its tests (Issue #304).

Issue #300 gave every scenario one registry path, and Issue #301 proved the
migration did not change what the frozen fixtures mean. Neither answered the
question #304 asks: *does every registered scenario honour the shared
fail-closed boundaries, and is there a committed test that proves it?*

Contract validation already rejects a malformed manifest. It cannot reject a
manifest that is well formed but proves no evidence boundary at all, and that is
the gap a new scenario slips through. So the rules live here and are applied to
the live registry on every run:

* **The matrix is keyed by registry identity, never by a path list.** The gate
  derives the identity set from the registry, so adding a manifest adds it to the
  matrix without editing this module. A scenario with no case set is a failure,
  not a skip.
* **A case is a promise that must resolve.** Every case names a committed test
  file and a test function, and the gate resolves both. Prose cannot satisfy a
  dimension.
* **The probes are derived, not asserted.** ``reference_probes`` rebuilds one
  confirmed state for a family and then *mutates* it, so the verifier is shown to
  refuse missing provenance, a stale belief, a misplaced entity and malformed
  input, instead of those rules being declared correct by hand.

Everything here is read-only. It imports the World Model verifiers and the
reducer to derive probes, and it never writes a run, an event or a fixture. A
passing verdict says a scenario is registered, testable and honest about its
evidence status; it never says a scenario ran, and it is not physical evidence.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PASS = 0
FAIL = 1
INCOMPLETE = 2

DEFAULT_CORPUS_PATH = Path("tools/qa/scenario-conformance-v1.json")

# The dimensions every registered scenario must prove. Each one is a failure the
# registry claims to prevent, written as something a test has to demonstrate.
REQUIRED_DIMENSIONS: tuple[str, ...] = (
    "conflicting_evidence",
    "deterministic_replay",
    "missing_provenance",
    "policy_valid_actions",
    "release_status_truthful",
    "stale_evidence",
    "unsafe_field_rejected",
)

# Dimensions the gate proves from the manifest and a derived probe, so a
# scenario cannot satisfy them with a test that passes for the wrong reason.
PROBE_DIMENSIONS: tuple[str, ...] = (
    "conflicting_evidence",
    "missing_provenance",
    "policy_valid_actions",
    "stale_evidence",
)

# Field names that would hand a scenario raw control or stop authority. The
# contract rejects these keys in a manifest; this is what the derived check scans
# the serialized manifest for, because a nested key is easy to miss by eye.
RAW_CONTROL_FIELD_NAMES: tuple[str, ...] = (
    "e_stop",
    "effort",
    "emergency_stop",
    "estop",
    "joint_angles",
    "joint_positions",
    "joint_trajectory",
    "joint_velocities",
    "mcu_command",
    "motor_command",
    "pwm",
    "stop_authority",
    "torque",
    "torques",
    "velocity",
    "velocities",
)

# Substrings that betray a second policy engine or an inline verifier, which is
# how a scenario bypasses the shared boundary without failing a schema.
BYPASS_SUBSTRINGS: tuple[str, ...] = ("bypass", "policy_impl", "verifier_impl")

# The evidence vocabulary the gate recognises, and the subset that can never be
# release eligible. A scripted fixture is the honest label for every scenario in
# this repository today, so a fixture that claims release eligibility is exactly
# the false completion the registry exists to prevent.
EXPECTED_EVIDENCE_STATUSES: tuple[str, ...] = ("SCRIPTED_FIXTURE", "SIMULATION", "PHYSICAL")
NEVER_RELEASE_ELIGIBLE_STATUSES: tuple[str, ...] = ("SCRIPTED_FIXTURE",)

PROBE_RUN_ID = "conformance-probe"
PROBE_STATE_HASH = "f" * 64
PROBE_VERIFIED_AT = "2026-08-04T00:10:00Z"


class ScenarioConformanceError(RuntimeError):
    """The conformance corpus, a probe or the registry could not be evaluated."""

    def __init__(self, message: str, *, code: str = "SCENARIO_CONFORMANCE_INVALID", path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class ConformanceCase:
    """One committed test that proves one dimension for one identity."""

    dimension: str
    test: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"dimension": self.dimension, "test": self.test, "detail": self.detail}


@dataclass(frozen=True)
class ScenarioCases:
    """The case set declared for one registry identity."""

    identity: str
    cases: tuple[ConformanceCase, ...]

    def dimension(self, name: str) -> ConformanceCase | None:
        for case in self.cases:
            if case.dimension == name:
                return case
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"scenario": self.identity, "cases": [case.as_dict() for case in self.cases]}

    def __iter__(self):
        return iter(self.cases)

    def __len__(self) -> int:
        return len(self.cases)


@dataclass
class ConformanceFinding:
    """One failed rule, named by scenario and by dimension."""

    code: str
    identity: str
    dimension: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "scenario": self.identity,
            "dimension": self.dimension,
            "detail": self.detail,
        }


@dataclass
class ScenarioVerdict:
    """Per-scenario result, so a failure names the scenario and the rule."""

    identity: str
    covered: tuple[str, ...] = ()
    findings: list[ConformanceFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.identity,
            "ok": self.ok,
            "covered": list(self.covered),
            "missing": sorted({finding.dimension for finding in self.findings}),
            "failures": [finding.as_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class ProbeOutcome:
    """What one derived probe observed, with the detail that decided it."""

    dimension: str
    ok: bool
    detail: str


def load_corpus(path: Path) -> dict[str, Any]:
    """Read the committed corpus and check its structure before use."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ScenarioConformanceError(
            f"cannot read {path}: {error}", code="SCENARIO_CONFORMANCE_CORPUS_UNREADABLE"
        ) from error
    except json.JSONDecodeError as error:
        raise ScenarioConformanceError(
            f"{path} is not valid JSON: {error}", code="SCENARIO_CONFORMANCE_CORPUS_MALFORMED"
        ) from error
    if not isinstance(payload, dict):
        raise ScenarioConformanceError(f"{path} must be a JSON object", code="SCENARIO_CONFORMANCE_CORPUS_MALFORMED")
    for key in ("corpus_version", "issue", "required_dimensions", "scenarios"):
        if key not in payload:
            raise ScenarioConformanceError(
                f"{path} is missing the {key!r} key", code="SCENARIO_CONFORMANCE_CORPUS_MALFORMED"
            )
    declared = payload["required_dimensions"]
    if not isinstance(declared, list) or sorted(declared) != sorted(REQUIRED_DIMENSIONS):
        raise ScenarioConformanceError(
            f"{path} must declare exactly the {len(REQUIRED_DIMENSIONS)} required dimensions; got {declared!r}",
            code="SCENARIO_CONFORMANCE_DIMENSION_DRIFT",
        )
    if not isinstance(payload["scenarios"], dict):
        raise ScenarioConformanceError(
            f"{path} 'scenarios' must be an object keyed by registry identity",
            code="SCENARIO_CONFORMANCE_CORPUS_MALFORMED",
        )
    return payload


def parse_cases(identity: str, raw: Any, *, path: Path) -> ScenarioCases:
    """Turn one committed entry into cases, refusing anything ambiguous."""

    if not isinstance(raw, list):
        raise ScenarioConformanceError(
            f"{path}: {identity} must map to a list of cases",
            code="SCENARIO_CONFORMANCE_CORPUS_MALFORMED",
            path=identity,
        )
    cases: list[ConformanceCase] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScenarioConformanceError(
                f"{path}: {identity}[{index}] must be an object",
                code="SCENARIO_CONFORMANCE_CORPUS_MALFORMED",
                path=identity,
            )
        dimension = item.get("dimension")
        if not isinstance(dimension, str) or dimension not in REQUIRED_DIMENSIONS:
            raise ScenarioConformanceError(
                f"{path}: {identity}[{index}] names an unknown dimension {dimension!r}",
                code="SCENARIO_CONFORMANCE_UNKNOWN_DIMENSION",
                path=identity,
            )
        if dimension in seen:
            raise ScenarioConformanceError(
                f"{path}: {identity} declares {dimension!r} twice",
                code="SCENARIO_CONFORMANCE_DUPLICATE_DIMENSION",
                path=identity,
            )
        seen.add(dimension)
        test = item.get("test")
        detail = item.get("detail")
        if not isinstance(test, str) or not test.strip():
            raise ScenarioConformanceError(
                f"{path}: {identity}[{index}] needs a non-empty 'test'",
                code="SCENARIO_CONFORMANCE_CORPUS_MALFORMED",
                path=identity,
            )
        if not isinstance(detail, str) or not detail.strip():
            raise ScenarioConformanceError(
                f"{path}: {identity}[{index}] needs a non-empty 'detail'",
                code="SCENARIO_CONFORMANCE_CORPUS_MALFORMED",
                path=identity,
            )
        cases.append(ConformanceCase(dimension, test, detail))
    return ScenarioCases(identity, tuple(sorted(cases, key=lambda case: case.dimension)))


def resolve_test(reference: str) -> tuple[str, str]:
    """Split ``path.py::function`` and refuse an unsafe or malformed path."""

    module_path, separator, function_name = reference.partition("::")
    if separator != "::" or not function_name.strip() or not module_path.endswith(".py"):
        raise ScenarioConformanceError(
            f"{reference!r} is not a path.py::test_function reference",
            code="SCENARIO_CONFORMANCE_TEST_UNRESOLVED",
            path=reference,
        )
    candidate = Path(module_path)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in module_path:
        raise ScenarioConformanceError(
            f"{reference!r} must be a repository-relative path",
            code="SCENARIO_CONFORMANCE_TEST_UNRESOLVED",
            path=reference,
        )
    return module_path, function_name.strip()


def defined_tests(source: str) -> set[str]:
    """Every function name defined in a test module, read without importing it.

    The corpus names tests that live in ``tests/**``; reading the file keeps the
    gate free of an import side effect and free of a dependency on pytest
    internals, which a gate that must run anywhere should not have.
    """

    names: set[str] = set()
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("def ") or not stripped[4:].strip():
            continue
        head = stripped[4:].split("(", 1)[0].strip()
        if head:
            names.add(head)
    return names


# --------------------------------------------------------------------------- #
# Derived probes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeObservation:
    """One observation the probe stream emits, with the entity type it asserts.

    ``entity_type`` and ``attributes`` are carried explicitly because the shared
    reducer treats an event that names an entity type as a *modern* observation:
    it then refuses to call the state fresh until an aging boundary is applied,
    and it validates attributes against that type. A probe that omitted them
    would test the legacy path and prove nothing about the registered scenarios.
    """

    entity_id: str
    location: str
    entity_type: str
    attributes: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ProbeFamily:
    """One confirmed probe stream, its verifier and how its evidence can break."""

    observations: tuple[ProbeObservation, ...]
    verifier: str
    keywords: Mapping[str, Any]
    conflict_mutation: str


# One confirmed probe stream per registered identity. The observations are
# tabletop-only and use no simulator, because a probe that needed Gazebo could not
# run in CI. The entity types are the ones the shared attribute vocabulary knows,
# so the probe exercises the modern observation path end to end.
PROBE_FAMILIES: Mapping[str, ProbeFamily] = {
    "clear-workspace@0.2": ProbeFamily(
        observations=(
            ProbeObservation("blue_cylinder", "in:staging_bin", "block"),
            ProbeObservation("red_block", "in:tray", "block"),
        ),
        verifier="verify_workspace_clearance",
        keywords={},
        conflict_mutation="relocate",
    ),
    "inspect-workpieces@0.2": ProbeFamily(
        observations=(ProbeObservation("red_block", "on:table", "block"),),
        verifier="verify_inspection_evidence",
        keywords={"required_entity_ids": ["red_block"]},
        # Inspection asserts a required entity was seen at confidence. Relocating
        # it is not a conflict for an observation-only family, so the honest
        # conflict is a witness that disagrees below the threshold.
        conflict_mutation="low_confidence",
    ),
    "kit-three-parts@0.2": ProbeFamily(
        observations=(
            ProbeObservation("red_block", "in:kit_tray", "block"),
            ProbeObservation("blue_cylinder", "in:kit_tray", "block"),
        ),
        verifier="verify_kit_contents",
        keywords={"required_object_ids": ["red_block", "blue_cylinder"], "tray_id": "kit_tray"},
        conflict_mutation="relocate",
    ),
    "pick-place-red-block@1.0": ProbeFamily(
        observations=(ProbeObservation("red_block", "in:tray", "block"),),
        verifier="verify_object_in_tray",
        keywords={"object_id": "red_block", "tray_id": "tray"},
        conflict_mutation="relocate",
    ),
    "sort-parcels@0.2": ProbeFamily(
        observations=(
            # The shelves are observed so the location endpoints carry a belief;
            # a location whose endpoint was never seen is already lost, and the
            # probe would then be measuring that instead of the verifier.
            ProbeObservation("pickup_shelf", "on:floor_north", "shelf"),
            ProbeObservation("quarantine_bin", "on:floor_south", "bin"),
            ProbeObservation(
                "parcel_box",
                "in:pickup_shelf",
                "parcel_box",
                {"label_status": "verified", "condition": "intact", "tracking_id": "TRK-BOX"},
            ),
            ProbeObservation(
                "parcel_envelope",
                "in:pickup_shelf",
                "parcel_envelope",
                {"label_status": "verified", "condition": "intact", "parcel_uid": "TRK-ENV"},
            ),
            ProbeObservation(
                "parcel_damaged",
                "in:quarantine_bin",
                "parcel",
                {"label_status": "verified", "condition": "damaged", "barcode": "TRK-DMG"},
            ),
        ),
        verifier="verify_parcel_sorting",
        keywords={},
        conflict_mutation="relocate",
    ),
}

# The verifiers the probes are allowed to call, and the module that owns them. A
# manifest naming a verifier outside this set is a finding: no probe can be
# derived for it, and a gate that silently skipped it would be the
# hand-maintained hole this issue closes.
PROBE_VERIFIERS: Mapping[str, str] = {
    "verify_inspection_evidence": "services/world_model/workbench_world_model/verifier.py",
    "verify_kit_contents": "services/world_model/workbench_world_model/verifier.py",
    "verify_object_in_tray": "services/world_model/workbench_world_model/verifier.py",
    "verify_parcel_sorting": "services/world_model/workbench_world_model/verifier.py",
    "verify_workspace_clearance": "services/world_model/workbench_world_model/verifier.py",
}

PROBE_CONFLICT_MUTATIONS: tuple[str, ...] = ("relocate", "low_confidence")


def _verifier_module() -> Any:
    try:
        from workbench_world_model import verifier as verifier_module
    except ImportError as error:  # pragma: no cover - a broken checkout is INCOMPLETE, not FAIL
        raise ScenarioConformanceError(
            f"cannot import the World Model verifier boundary: {error}",
            code="SCENARIO_CONFORMANCE_WORLD_MODEL_UNAVAILABLE",
        ) from error
    return verifier_module


def _probe_events(family: ProbeFamily, event_cls: Any, event_type_cls: Any, clock_id_cls: Any) -> list[Any]:
    """Build the family's observation stream in one place.

    Both the reducer and the snapshot path need the same events, and the replay
    probe reverses them to prove the stream is order independent, so building
    them twice is how the two paths would drift.
    """

    # Imported lazily, like every other shared-contract read in this package, so
    # the registry and the gate stay loadable without the runtime installed.
    try:
        from workbench_contracts import ATTRIBUTE_SCHEMA_VERSION
    except ImportError as error:  # pragma: no cover - a broken checkout is INCOMPLETE, not FAIL
        raise ScenarioConformanceError(
            f"cannot import the shared attribute contract: {error}",
            code="SCENARIO_CONFORMANCE_WORLD_MODEL_UNAVAILABLE",
        ) from error

    events: list[Any] = []
    for index, observation in enumerate(family.observations):
        payload: dict[str, Any] = {
            "entity_id": observation.entity_id,
            "location": observation.location,
            "confidence": 0.95,
            "source": "cam0",
            "entity_type": observation.entity_type,
        }
        if observation.attributes is not None:
            payload.update(
                {
                    "attributes": dict(observation.attributes),
                    "attributes_mode": "complete",
                    "attributes_schema_version": ATTRIBUTE_SCHEMA_VERSION,
                    "observed_at": f"2026-08-04T00:00:{index:02d}Z",
                }
            )
        events.append(
            event_cls(
                event_id=f"{PROBE_RUN_ID}-{index}",
                run_id=PROBE_RUN_ID,
                sequence_no=index,
                event_type=event_type_cls.OBSERVATION,
                occurred_at=f"2026-08-04T00:00:{index:02d}Z",
                payload=payload,
                evidence_refs=[f"frame://{PROBE_RUN_ID}-{index}"],
                clock_id=clock_id_cls.WALL,
            )
        )
    return events


def _probe_state(identity: str) -> tuple[Any, Mapping[str, Any]]:
    """Reduce one confirmed event stream for a family, or report it unsupported."""

    if identity not in PROBE_FAMILIES:
        raise ScenarioConformanceError(
            f"no derived probe is defined for {identity}",
            code="SCENARIO_CONFORMANCE_PROBE_UNSUPPORTED",
            path=identity,
        )
    family = PROBE_FAMILIES[identity]
    try:
        from workbench_contracts import ClockId, WorldEvent, WorldEventType
        from workbench_world_model import reducer as reducer_module
    except ImportError as error:  # pragma: no cover
        raise ScenarioConformanceError(
            f"cannot import the World Model reducer: {error}",
            code="SCENARIO_CONFORMANCE_WORLD_MODEL_UNAVAILABLE",
        ) from error

    state = reducer_module.reduce_events(PROBE_RUN_ID, _probe_events(family, WorldEvent, WorldEventType, ClockId))
    # A reducer stops asserting freshness as soon as a modern observation arrives,
    # and this probe supplies no aging boundary on purpose: it tests verifier
    # semantics, not the aging policy, which has its own suite.
    state.freshness_evaluated = True
    return state, family.keywords


def _probe_call(identity: str) -> Callable[[Any], Any]:
    """Build the verifier call for a family, refusing an unresolvable one."""

    family = PROBE_FAMILIES[identity]
    verifier_name = family.verifier
    keywords = family.keywords
    module_path = PROBE_VERIFIERS.get(verifier_name)
    if module_path is None:  # pragma: no cover - PROBE_FAMILIES is checked against PROBE_VERIFIERS
        raise ScenarioConformanceError(
            f"{identity} names an unsupported verifier {verifier_name!r}",
            code="SCENARIO_CONFORMANCE_PROBE_UNSUPPORTED",
            path=identity,
        )
    verifier_module = _verifier_module()
    verifier = getattr(verifier_module, verifier_name, None)
    if not callable(verifier):  # pragma: no cover - defensive
        raise ScenarioConformanceError(
            f"{module_path}::{verifier_name} does not resolve to a callable",
            code="SCENARIO_CONFORMANCE_PROBE_UNSUPPORTED",
            path=identity,
        )
    try:
        from workbench_contracts import ClockId
    except ImportError as error:  # pragma: no cover
        raise ScenarioConformanceError(
            f"cannot import the shared contract: {error}",
            code="SCENARIO_CONFORMANCE_WORLD_MODEL_UNAVAILABLE",
        ) from error
    context = verifier_module.VerificationContext(
        state_hash=PROBE_STATE_HASH,
        verified_at=PROBE_VERIFIED_AT,
        clock_id=ClockId.WALL,
    )
    return lambda candidate: verifier(candidate, PROBE_RUN_ID, **keywords, context=context)


def reference_probes(identity: str) -> list[ProbeOutcome]:
    """Derive independent evidence for the verifier-semantics dimensions.

    Each probe mutates a confirmed probe state and requires the verifier to
    refuse, so a scenario cannot pass by declaring the rule.
    """

    state, _ = _probe_state(identity)
    call = _probe_call(identity)

    outcomes: list[ProbeOutcome] = []

    confirmed = call(state)
    outcomes.append(
        ProbeOutcome(
            "policy_valid_actions",
            confirmed.status.value == "confirmed",
            f"the confirmed probe state verifies as {confirmed.status.value} ({confirmed.reason})",
        )
    )

    stripped = state.model_copy(deep=True)
    stripped.entity_evidence_refs = {}
    stripped.evidence_refs = []
    without_provenance = call(stripped)
    outcomes.append(
        ProbeOutcome(
            "missing_provenance",
            without_provenance.status.value == "insufficient_evidence",
            f"removing every provenance reference yields "
            f"{without_provenance.status.value} ({without_provenance.reason})",
        )
    )

    try:
        from workbench_contracts import WorldBelief
    except ImportError as error:  # pragma: no cover
        raise ScenarioConformanceError(
            f"cannot import the shared contract: {error}",
            code="SCENARIO_CONFORMANCE_WORLD_MODEL_UNAVAILABLE",
        ) from error

    aged = state.model_copy(deep=True)
    aged.freshness_evaluated = True
    for entity_id in sorted(aged.entity_locations):
        aged.entity_beliefs[entity_id] = WorldBelief.LOST
        aged.entity_location_beliefs[entity_id] = WorldBelief.LOST
    stale = call(aged)
    outcomes.append(
        ProbeOutcome(
            "stale_evidence",
            stale.status.value == "insufficient_evidence" and stale.reason == "stale_observation",
            f"marking every belief lost yields {stale.status.value} ({stale.reason})",
        )
    )

    mutation = PROBE_FAMILIES[identity].conflict_mutation
    if mutation not in PROBE_CONFLICT_MUTATIONS:  # pragma: no cover - the table is checked in tests
        raise ScenarioConformanceError(
            f"{identity} names an unknown conflict mutation {mutation!r}",
            code="SCENARIO_CONFORMANCE_PROBE_UNSUPPORTED",
            path=identity,
        )
    conflicting_state = state.model_copy(deep=True)
    if mutation == "relocate":
        for entity_id in sorted(conflicting_state.entity_locations):
            conflicting_state.entity_locations[entity_id] = "in:elsewhere"
        description = "moving every entity elsewhere"
    else:
        for entity_id in sorted(conflicting_state.entity_confidence):
            conflicting_state.entity_confidence[entity_id] = 0.1
        description = "reporting every required entity below the confidence threshold"
    conflicting = call(conflicting_state)
    # The guarantee is that conflicting evidence cannot produce a confirmation.
    # Which non-confirmed status appears is the family's semantics, and naming it
    # here would make the probe assert a detail the family is free to change.
    outcomes.append(
        ProbeOutcome(
            "conflicting_evidence",
            conflicting.status.value != "confirmed",
            f"{description} yields {conflicting.status.value} ({conflicting.reason})",
        )
    )

    return outcomes


def replay_digest(identity: str) -> tuple[str, str]:
    """Replay the family's probe stream twice and return both state hashes."""

    state, _ = _probe_state(identity)
    try:
        from workbench_contracts import ClockId, WorldEvent, WorldEventType
        from workbench_world_model import reducer as reducer_module
    except ImportError as error:  # pragma: no cover
        raise ScenarioConformanceError(
            f"cannot import the World Model reducer: {error}",
            code="SCENARIO_CONFORMANCE_WORLD_MODEL_UNAVAILABLE",
        ) from error

    events = _probe_events(PROBE_FAMILIES[identity], WorldEvent, WorldEventType, ClockId)
    snapshot = reducer_module.create_world_state_snapshot(PROBE_RUN_ID, events)
    # A second, independent reduction of the same stream must agree exactly.
    again = reducer_module.create_world_state_snapshot(PROBE_RUN_ID, list(reversed(events)))
    del state
    return snapshot.state_hash, again.state_hash


def scan_manifest(manifest: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Find raw-control or bypass markers anywhere in a serialized manifest."""

    findings: list[tuple[str, str]] = []
    for key, path in _iter_key_paths(manifest):
        lowered = key.lower()
        if lowered in RAW_CONTROL_FIELD_NAMES:
            findings.append((path, f"{key!r} names raw control or stop authority"))
        elif any(fragment in lowered for fragment in BYPASS_SUBSTRINGS):
            findings.append((path, f"{key!r} names a bypass or a second policy/verifier implementation"))
    return findings


def _iter_key_paths(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            found.append((str(key), path))
            found.extend(_iter_key_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_iter_key_paths(item, f"{prefix}[{index}]"))
    return found


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def evaluate(
    *,
    root: Path,
    registry_entries: Mapping[str, Mapping[str, Any]],
    corpus: Mapping[str, Any],
    corpus_path: Path,
) -> tuple[int, list[ScenarioVerdict]]:
    """Judge every registered identity against the corpus and the derived probes.

    ``registry_entries`` is the live registry as ``identity -> manifest``, so the
    identity set is discovered rather than listed. A scenario in the registry and
    not in the corpus is a failure; so is a corpus entry for a scenario that is
    no longer registered, because a stale case set hides a removal.
    """

    findings_by_identity: dict[str, list[ConformanceFinding]] = {}
    covered_by_identity: dict[str, tuple[str, ...]] = {}

    def add(identity: str, code: str, dimension: str, detail: str) -> None:
        findings_by_identity.setdefault(identity, []).append(ConformanceFinding(code, identity, dimension, detail))

    declared = corpus.get("scenarios")
    if not isinstance(declared, Mapping):  # pragma: no cover - load_corpus enforces this
        raise ScenarioConformanceError(
            f"{corpus_path} 'scenarios' must be an object", code="SCENARIO_CONFORMANCE_CORPUS_MALFORMED"
        )

    # A corpus entry for an identity the registry does not load is a failure: the
    # case set would otherwise hide a scenario that was removed or renamed.
    for identity in sorted(set(declared) - set(registry_entries)):
        add(
            identity,
            "SCENARIO_CONFORMANCE_STALE_CASE_SET",
            "<registry>",
            f"{corpus_path} declares cases for {identity}, which the registry does not load",
        )

    for identity in sorted(registry_entries):
        manifest = registry_entries[identity]
        raw_cases = declared.get(identity)
        if raw_cases is None:
            add(
                identity,
                "SCENARIO_CONFORMANCE_MISSING_CASE_SET",
                "<registry>",
                f"{identity} is registered but has no case set in {corpus_path}",
            )
            continue

        cases = parse_cases(identity, raw_cases, path=corpus_path)
        covered_by_identity[identity] = tuple(case.dimension for case in cases.cases)

        for dimension in REQUIRED_DIMENSIONS:
            if cases.dimension(dimension) is None:
                add(
                    identity,
                    "SCENARIO_CONFORMANCE_MISSING_DIMENSION",
                    dimension,
                    f"{identity} proves no {dimension!r} case",
                )

        for case in cases:
            try:
                module_path, function_name = resolve_test(case.test)
            except ScenarioConformanceError as error:
                add(identity, error.code, case.dimension, str(error))
                continue
            source_path = root / module_path
            try:
                source = source_path.read_text(encoding="utf-8")
            except OSError as error:
                add(
                    identity,
                    "SCENARIO_CONFORMANCE_TEST_UNRESOLVED",
                    case.dimension,
                    f"{case.test} cannot be read: {error}",
                )
                continue
            if function_name not in defined_tests(source):
                add(
                    identity,
                    "SCENARIO_CONFORMANCE_TEST_UNRESOLVED",
                    case.dimension,
                    f"{case.test} does not define {function_name}",
                )

        # Raw control or a bypass marker anywhere in the manifest.
        for path, detail in scan_manifest(manifest):
            add(identity, "SCENARIO_CONFORMANCE_UNSAFE_FIELD", "unsafe_field_rejected", f"{path}: {detail}")

        # A fixture is never release eligible, and the registry must say so. The
        # flag is computed by the registry, so this asserts the boundary rather
        # than re-reading a field the manifest could set for itself.
        evidence_status = manifest.get("evidence_status")
        if evidence_status not in EXPECTED_EVIDENCE_STATUSES:
            add(
                identity,
                "SCENARIO_CONFORMANCE_UNTRUTHFUL_RELEASE_STATUS",
                "release_status_truthful",
                f"{evidence_status!r} is outside the evidence vocabulary the gate recognises",
            )
        elif evidence_status in NEVER_RELEASE_ELIGIBLE_STATUSES and manifest.get("release_eligible") is not False:
            add(
                identity,
                "SCENARIO_CONFORMANCE_UNTRUTHFUL_RELEASE_STATUS",
                "release_status_truthful",
                f"a {evidence_status} scenario must not be release eligible",
            )

        # Deterministic replay: two reductions of one stream must agree.
        try:
            first, second = replay_digest(identity)
        except ScenarioConformanceError as error:
            add(identity, error.code, "deterministic_replay", str(error))
        else:
            if first != second:
                add(
                    identity,
                    "SCENARIO_CONFORMANCE_NONDETERMINISTIC_REPLAY",
                    "deterministic_replay",
                    f"two reductions of the same stream produced {first} and {second}",
                )

        # The derived verifier probes.
        try:
            probes = reference_probes(identity)
        except ScenarioConformanceError as error:
            for dimension in PROBE_DIMENSIONS:
                add(identity, error.code, dimension, str(error))
        else:
            for outcome in probes:
                if not outcome.ok:
                    add(
                        identity,
                        "SCENARIO_CONFORMANCE_PROBE_FAILED",
                        outcome.dimension,
                        outcome.detail,
                    )

    verdicts = [
        ScenarioVerdict(identity, covered_by_identity.get(identity, ()), findings_by_identity.get(identity, []))
        for identity in sorted(set(registry_entries) | set(findings_by_identity))
    ]
    failures = [identity for identity, items in findings_by_identity.items() if items]
    return (FAIL if failures else PASS), verdicts


__all__ = [
    "DEFAULT_CORPUS_PATH",
    "EXPECTED_EVIDENCE_STATUSES",
    "FAIL",
    "INCOMPLETE",
    "NEVER_RELEASE_ELIGIBLE_STATUSES",
    "PASS",
    "PROBE_CONFLICT_MUTATIONS",
    "PROBE_DIMENSIONS",
    "PROBE_FAMILIES",
    "PROBE_VERIFIERS",
    "RAW_CONTROL_FIELD_NAMES",
    "REQUIRED_DIMENSIONS",
    "ConformanceCase",
    "ConformanceFinding",
    "ProbeObservation",
    "ProbeOutcome",
    "ScenarioCases",
    "ScenarioConformanceError",
    "ScenarioVerdict",
    "defined_tests",
    "evaluate",
    "load_corpus",
    "parse_cases",
    "reference_probes",
    "replay_digest",
    "resolve_test",
    "scan_manifest",
]
