"""Make a run's determinism inputs explicit, canonical and checkable (Issue #313).

Issue #302 bound a run to the scenario *definition* that produced it. It did not
say anything about the inputs that decide what the run *produced*: the seed, the
clock and time source the events were stamped on, the ordering rule a reader must
apply, the adapter versions, or which environment class the run belongs to.

That gap has two failure modes, and both are silent:

* two runs of one scenario with different seeds reduce to different states, yet
  carried the same identity, so a comparison between them looked meaningful;
* a scripted fixture and a Gazebo run can be reduced to the same state hash, and
  a hash alone cannot tell a reviewer which class of evidence they are holding.

This module makes those inputs one canonical, ordered field table with its own
digest, so a bundle either declares them or is refused:

* :func:`run_provenance` hashes the declared inputs and keeps the material, so a
  mismatch names the field that moved instead of only reporting a digest;
* :func:`environment_class_for_runner` derives the environment class from the
  runner that actually ran, so the class is a property of the run rather than a
  label a caller can type;
* :func:`replay_compatible` refuses two bundles as replay partners when their
  environment classes differ, even when every other field agrees and the state
  hashes happen to match.

The module is read-only and pure: it imports nothing from the runtime, starts no
simulator, and writes no run. It is deliberately not a second identity: #302 says
*which definition* produced a stream and this says *what inputs* it was produced
with, and both are needed before two runs can be compared.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PASS = 0
FAIL = 1
INCOMPLETE = 2

PROVENANCE_SCHEMA_VERSION = "workbench-run-provenance-v1"
HASH_ALGORITHM = "sha256"
DEFAULT_PROVENANCE_FILENAME = "provenance.json"
UNSPECIFIED = "unspecified"

# Every diagnostic this module can emit. Each names one cause and one remedy, and
# a caller switches on these rather than on a message string.
RUN_PROVENANCE_MISSING = "RUN_PROVENANCE_MISSING"
RUN_PROVENANCE_MALFORMED = "RUN_PROVENANCE_MALFORMED"
RUN_PROVENANCE_HASH_MISMATCH = "RUN_PROVENANCE_HASH_MISMATCH"
RUN_PROVENANCE_ENVIRONMENT_CONFLICT = "RUN_PROVENANCE_ENVIRONMENT_CONFLICT"
RUN_PROVENANCE_CLOCK_CONFLICT = "RUN_PROVENANCE_CLOCK_CONFLICT"
RUN_PROVENANCE_ADAPTER_CONFLICT = "RUN_PROVENANCE_ADAPTER_CONFLICT"
RUN_PROVENANCE_SOURCE_CONFLICT = "RUN_PROVENANCE_SOURCE_CONFLICT"
RUN_PROVENANCE_UNSAFE_ENVIRONMENT_CLAIM = "RUN_PROVENANCE_UNSAFE_ENVIRONMENT_CLAIM"

EMITTED_CODES = (
    RUN_PROVENANCE_MISSING,
    RUN_PROVENANCE_MALFORMED,
    RUN_PROVENANCE_HASH_MISMATCH,
    RUN_PROVENANCE_ENVIRONMENT_CONFLICT,
    RUN_PROVENANCE_CLOCK_CONFLICT,
    RUN_PROVENANCE_ADAPTER_CONFLICT,
    RUN_PROVENANCE_SOURCE_CONFLICT,
    RUN_PROVENANCE_UNSAFE_ENVIRONMENT_CLAIM,
)

# The provenance input list, in the order it is hashed. This is the whole
# contract: adding an input changes every provenance digest, which is the point,
# and the order is asserted by a test rather than left to dict iteration.
PROVENANCE_INPUTS: tuple[str, ...] = (
    "seed",
    "clock_mode",
    "time_source",
    "event_ordering",
    "adapter_versions",
    "environment_class",
)

# A run must carry these before its provenance can be computed at all. They are
# the first five inputs; ``environment_class`` is deliberately excluded because
# :func:`environment_class_for_runner` derives it, and a caller that could omit
# it could also disagree with the runner.
REQUIRED_PROVENANCE_FIELDS: tuple[str, ...] = (
    "seed",
    "clock_mode",
    "time_source",
    "event_ordering",
    "adapter_versions",
)

# The clock a run stamped its events on. These are the shared contract's clock
# identifiers; this module does not define a second vocabulary for them.
CLOCK_MODES: tuple[str, ...] = ("monotonic", "wall")

# How the wall time an event carries was produced. A fixed base is reproducible
# from the seed alone, which is what a fixture needs; the host clock is not, and
# a bundle that claims one while recording the other cannot be replayed.
TIME_SOURCES: tuple[str, ...] = ("fixed_base", "host_clock")

# The ordering rule a reader must apply to the stream. ``sequence_no`` is the
# canonical total order the runtime writes; ``file_order`` is the order the lines
# were persisted in, which is what the event-stream hash in #302 binds to.
EVENT_ORDERINGS: tuple[str, ...] = ("sequence_no", "file_order")

# The environment classes, taken from the committed capability matrix vocabulary
# (docs/architecture/scenario-capability-matrix-v1.json). They are ordered by
# strength so a class may never be silently upgraded.
ENVIRONMENT_CLASSES: tuple[str, ...] = (
    "NOT_EXECUTED",
    "BLOCKED",
    "SCRIPTED_FIXTURE",
    "GAZEBO",
    "PHYSICAL",
)

ENVIRONMENT_RANK: Mapping[str, int] = {name: rank for rank, name in enumerate(ENVIRONMENT_CLASSES)}

# What each runner may honestly claim. This repository has no Gazebo or hardware
# adapter wired into ``sim_cli``, so a runner can never reach PHYSICAL through it
# and the strongest class any runner here can claim is GAZEBO. PHYSICAL is absent
# on purpose: physical evidence enters through the hardware evidence path, not
# through a simulation runner, so no runner is allowed to claim it.
CLAIMABLE_ENVIRONMENT_CLASSES: Mapping[str, frozenset[str]] = {
    "scripted": frozenset({"NOT_EXECUTED", "SCRIPTED_FIXTURE"}),
    "gazebo": frozenset({"NOT_EXECUTED", "GAZEBO"}),
    "external": frozenset({"NOT_EXECUTED", "GAZEBO"}),
}

# The statuses a runner can finish in. A run that never started is
# ``NOT_EXECUTED``; a run that started and then failed, timed out or produced an
# unreadable log is still evidence *from its environment*, so a Gazebo run that
# timed out is GAZEBO rather than NOT_EXECUTED. Conflating the two would let a
# failed Gazebo run hide behind the class a fixture uses.
STOPPED_STATUSES: frozenset[str] = frozenset({"FAILED", "TIMED_OUT", "INVALID_OUTPUT", "INTERRUPTED"})
RUN_STATUSES: frozenset[str] = frozenset({"NOT_EXECUTED", "SCRIPTED_FIXTURE", "EXECUTED"}) | STOPPED_STATUSES


class RunProvenanceError(ValueError):
    """A run's determinism inputs are absent, malformed or contradictory."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def environment_class_for_runner(runner: object, *, status: object) -> str:
    """Derive the environment class from the runner that ran and how it finished.

    Deriving rather than accepting the class is the whole point: a class a caller
    types is a label, and a fixture that labels itself ``GAZEBO`` is the exact
    dishonesty the capability matrix refuses.

    ``status`` rather than an ``executed`` flag decides the class, because a run
    that started and then failed is still evidence from the environment it ran
    in. A scripted runner must report ``SCRIPTED_FIXTURE``: a scripted run that
    claims to have executed is the false-completion case, so it is refused rather
    than mapped to the nearest class.
    """

    name = str(runner)
    allowed = CLAIMABLE_ENVIRONMENT_CLASSES.get(name)
    if allowed is None:
        raise RunProvenanceError(
            RUN_PROVENANCE_MALFORMED,
            f"runner {runner!r} is not one of {', '.join(sorted(CLAIMABLE_ENVIRONMENT_CLASSES))}",
        )
    status_text = str(status)
    if status_text not in RUN_STATUSES:
        raise RunProvenanceError(
            RUN_PROVENANCE_MALFORMED,
            f"run status {status!r} is not one of {', '.join(sorted(RUN_STATUSES))}",
        )
    if status_text == "NOT_EXECUTED":
        return "NOT_EXECUTED"
    if name == "scripted":
        if status_text != "SCRIPTED_FIXTURE":
            raise RunProvenanceError(
                RUN_PROVENANCE_MALFORMED,
                f"a scripted runner cannot report status {status_text!r}; a scripted run is a fixture, never "
                "an execution",
            )
        return "SCRIPTED_FIXTURE"
    if status_text == "SCRIPTED_FIXTURE":
        raise RunProvenanceError(
            RUN_PROVENANCE_MALFORMED,
            f"runner {name!r} cannot report status 'SCRIPTED_FIXTURE'; that status belongs to the scripted runner",
        )
    return "GAZEBO"


def canonical_adapter_versions(versions: Mapping[str, Any] | None) -> str:
    """Canonicalize an adapter->version mapping into one stable string.

    The mapping is sorted by adapter name so two runs that name the same adapters
    in a different order agree, and an adapter with no known version is recorded
    as ``unspecified`` rather than dropped or guessed. An empty mapping is a
    legitimate value: a scenario that needs no adapter has no versions to name.
    """

    if versions is None:
        return ""
    if not isinstance(versions, Mapping):
        raise RunProvenanceError(RUN_PROVENANCE_MALFORMED, "adapter_versions must be a mapping")
    pairs: list[str] = []
    for name in sorted(str(key) for key in versions):
        if not name.strip():
            raise RunProvenanceError(RUN_PROVENANCE_MALFORMED, "an adapter name must be a non-blank string")
        value = versions[name]
        if value is None or not str(value).strip():
            pairs.append(f"{name}={UNSPECIFIED}")
            continue
        text = str(value).strip()
        if "," in text or "=" in text:
            raise RunProvenanceError(
                RUN_PROVENANCE_MALFORMED,
                f"adapter version for {name!r} must not contain ',' or '=': {text!r}",
            )
        pairs.append(f"{name}={text}")
    return ",".join(pairs)


def known_adapters(versions: str) -> dict[str, str]:
    """Parse a canonical adapter string back into ``name -> version``."""

    parsed: dict[str, str] = {}
    if not versions:
        return parsed
    for pair in versions.split(","):
        name, _, value = pair.partition("=")
        parsed[name] = value
    return parsed


def provenance_material(
    *,
    seed: object,
    clock_mode: object,
    time_source: object,
    event_ordering: object,
    adapter_versions: object,
    environment_class: object,
) -> dict[str, str]:
    """Build the canonical provenance material, or refuse the run that cannot be."""

    seed_text = _non_blank_scalar(seed, "seed")
    clock_text = _one_of(clock_mode, CLOCK_MODES, "clock_mode")
    source_text = _one_of(time_source, TIME_SOURCES, "time_source")
    ordering_text = _one_of(event_ordering, EVENT_ORDERINGS, "event_ordering")
    class_text = _one_of(environment_class, ENVIRONMENT_CLASSES, "environment_class")
    if isinstance(adapter_versions, str):
        adapters_text = adapter_versions
    else:
        adapters_text = canonical_adapter_versions(adapter_versions)
    return {
        "seed": seed_text,
        "clock_mode": clock_text,
        "time_source": source_text,
        "event_ordering": ordering_text,
        "adapter_versions": adapters_text,
        "environment_class": class_text,
    }


def _non_blank_scalar(value: object, field_name: str) -> str:
    """Render a scalar as a non-blank string, refusing a bool or a blank.

    ``bool`` is refused rather than stringified because ``True`` is a plausible
    seed-looking value whose string form would silently collide with the string
    ``"True"``.
    """

    if isinstance(value, bool) or value is None:
        raise RunProvenanceError(RUN_PROVENANCE_MALFORMED, f"{field_name} must be a non-blank string or number")
    text = str(value).strip()
    if not text:
        raise RunProvenanceError(RUN_PROVENANCE_MISSING, f"{field_name} must be a non-blank string or number")
    return text


def _one_of(value: object, allowed: Sequence[str], field_name: str) -> str:
    text = str(value).strip()
    if text not in allowed:
        raise RunProvenanceError(
            RUN_PROVENANCE_MALFORMED,
            f"{field_name} must be one of {', '.join(allowed)}; got {value!r}",
        )
    return text


@dataclass(frozen=True)
class RunProvenance:
    """The digest of one run's determinism inputs, with that material kept."""

    provenance_hash: str
    material: dict[str, str]
    inputs: tuple[str, ...] = PROVENANCE_INPUTS
    schema_version: str = PROVENANCE_SCHEMA_VERSION
    hash_algorithm: str = HASH_ALGORITHM

    @property
    def environment_class(self) -> str:
        return self.material["environment_class"]

    @property
    def seed(self) -> str:
        return self.material["seed"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hash_algorithm": self.hash_algorithm,
            "provenance_hash": self.provenance_hash,
            "inputs": list(self.inputs),
            "material": dict(self.material),
        }

    def differing_inputs(self, other: RunProvenance) -> tuple[str, ...]:
        """Which provenance inputs differ between two runs, in contract order."""

        return tuple(name for name in self.inputs if self.material.get(name) != other.material.get(name))


def canonical_provenance_bytes(
    material: Mapping[str, str],
    inputs: Sequence[str] = PROVENANCE_INPUTS,
) -> bytes:
    """Canonical hash material: the declared inputs, in order, as strict JSON."""

    ordered: dict[str, str] = {}
    for name in inputs:
        if name not in material:
            raise RunProvenanceError(RUN_PROVENANCE_MISSING, f"provenance input {name!r} is absent")
        value = material[name]
        if not isinstance(value, str):
            raise RunProvenanceError(RUN_PROVENANCE_MALFORMED, f"provenance input {name!r} must be a string")
        ordered[name] = value
    try:
        return json.dumps(
            {"schema_version": PROVENANCE_SCHEMA_VERSION, "inputs": ordered},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise RunProvenanceError(RUN_PROVENANCE_MALFORMED, "provenance material must be finite UTF-8 JSON") from error


def run_provenance(material: Mapping[str, str], *, inputs: Sequence[str] = PROVENANCE_INPUTS) -> RunProvenance:
    """Hash one canonical provenance material into a stable digest."""

    payload = canonical_provenance_bytes(material, inputs)
    return RunProvenance(
        provenance_hash=hashlib.sha256(payload).hexdigest(),
        material={name: material[name] for name in inputs},
        inputs=tuple(inputs),
    )


def recorded_provenance(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the recorded provenance block, or return ``None`` when it is absent."""

    block = metadata.get("provenance")
    if isinstance(block, Mapping):
        return dict(block)
    return None


@dataclass(frozen=True)
class ProvenanceFinding:
    """One reason a bundle's provenance was refused."""

    code: str
    run_id: str
    detail: str


@dataclass(frozen=True)
class ProvenanceVerdict:
    """The result of checking one run's provenance block."""

    run_id: str
    ok: bool
    environment_class: str = "?"
    provenance_hash: str = ""
    findings: tuple[ProvenanceFinding, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ok": self.ok,
            "environment_class": self.environment_class,
            "provenance_hash": self.provenance_hash,
            "findings": [{"code": f.code, "detail": f.detail} for f in self.findings],
        }


def verify_bundle_provenance(
    *,
    run_id: str,
    metadata: Mapping[str, Any],
    runner: object = None,
    status: object = None,
) -> ProvenanceVerdict:
    """Check one run's recorded provenance against itself and its runner.

    The recorded digest must match the recorded material, every required field
    must be present, and the declared environment class must be one the runner
    that ran was allowed to claim. A bundle that claims a stronger class than its
    runner could produce is refused by name, because that is the claim a reviewer
    would otherwise believe.
    """

    findings: list[ProvenanceFinding] = []

    def fail(code: str, detail: str) -> None:
        findings.append(ProvenanceFinding(code=code, run_id=run_id, detail=detail))

    block = recorded_provenance(metadata)
    if block is None:
        fail(
            RUN_PROVENANCE_MISSING,
            "the run metadata carries no provenance block; re-run with the Issue #313 metadata extension",
        )
        return ProvenanceVerdict(run_id=run_id, ok=False, findings=tuple(findings))

    if block.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        fail(
            RUN_PROVENANCE_MALFORMED,
            f"provenance schema_version {block.get('schema_version')!r} is not {PROVENANCE_SCHEMA_VERSION!r}",
        )
        return ProvenanceVerdict(run_id=run_id, ok=False, findings=tuple(findings))

    recorded_hash = block.get("provenance_hash")
    material = block.get("material")
    if not isinstance(recorded_hash, str) or not recorded_hash.strip():
        fail(RUN_PROVENANCE_MALFORMED, "the provenance block carries no provenance_hash")
        return ProvenanceVerdict(run_id=run_id, ok=False, findings=tuple(findings))
    if not isinstance(material, Mapping):
        fail(RUN_PROVENANCE_MALFORMED, "the provenance block carries no material")
        return ProvenanceVerdict(run_id=run_id, ok=False, findings=tuple(findings))

    missing = [name for name in REQUIRED_PROVENANCE_FIELDS if not str(material.get(name, "")).strip()]
    if missing:
        fail(RUN_PROVENANCE_MISSING, f"provenance material is missing: {', '.join(missing)}")
        return ProvenanceVerdict(run_id=run_id, ok=False, findings=tuple(findings))

    class_name = str(material.get("environment_class", ""))
    if class_name not in ENVIRONMENT_CLASSES:
        fail(
            RUN_PROVENANCE_MALFORMED,
            f"environment_class {class_name!r} is not one of {', '.join(ENVIRONMENT_CLASSES)}",
        )
        return ProvenanceVerdict(run_id=run_id, ok=False, findings=tuple(findings))

    try:
        recorded = run_provenance({name: str(material[name]) for name in PROVENANCE_INPUTS})
    except RunProvenanceError as error:
        fail(error.code, error.message)
        return ProvenanceVerdict(run_id=run_id, ok=False, findings=tuple(findings))

    if recorded.provenance_hash != recorded_hash:
        fail(
            RUN_PROVENANCE_HASH_MISMATCH,
            f"the recorded provenance_hash {recorded_hash} does not match the hash of its own material "
            f"{recorded.provenance_hash}",
        )

    if runner is not None:
        allowed = CLAIMABLE_ENVIRONMENT_CLASSES.get(str(runner))
        if allowed is None:
            fail(RUN_PROVENANCE_MALFORMED, f"runner {runner!r} is not a known runner")
        else:
            try:
                derived = environment_class_for_runner(runner, status=status)
            except RunProvenanceError as error:
                fail(error.code, error.message)
            else:
                if class_name != derived:
                    fail(
                        RUN_PROVENANCE_UNSAFE_ENVIRONMENT_CLAIM,
                        f"the run declares environment_class {class_name!r} but runner {runner!r} with "
                        f"status {status!r} can only claim {derived!r}",
                    )

    return ProvenanceVerdict(
        run_id=run_id,
        ok=not findings,
        environment_class=class_name,
        provenance_hash=recorded_hash,
        findings=tuple(findings),
    )


def replay_compatible(left: RunProvenance, right: RunProvenance) -> tuple[str, ...]:
    """Which inputs make two runs incompatible replay partners.

    An empty tuple means the two bundles were produced under the same seed, clock
    and ordering, by the same adapter versions, in the same environment class. It
    does not mean they are byte-identical: two runs with the same inputs may
    legitimately produce the same state, and that equality is the thing being
    tested, so this returns the *inputs* that differ rather than a verdict.

    The environment class is reported even when every other input agrees, because
    a scripted fixture and a Gazebo run that reduce to one state hash are exactly
    the pair that must not be compared as if they were the same evidence.
    """

    return left.differing_inputs(right)


def describe_incompatibility(left: RunProvenance, right: RunProvenance) -> tuple[tuple[str, str], ...]:
    """The differing inputs with both values, so a report can name the cause."""

    return tuple(
        (name, f"{left.material.get(name, '?')} != {right.material.get(name, '?')}")
        for name in left.differing_inputs(right)
    )


def classify_incompatibility(differing: Iterable[str]) -> str:
    """Pick the single diagnostic code that best names a set of differing inputs."""

    names = set(differing)
    if "environment_class" in names:
        return RUN_PROVENANCE_ENVIRONMENT_CONFLICT
    if names & {"clock_mode", "time_source", "event_ordering"}:
        return RUN_PROVENANCE_CLOCK_CONFLICT
    if "adapter_versions" in names:
        return RUN_PROVENANCE_ADAPTER_CONFLICT
    if names:
        return RUN_PROVENANCE_SOURCE_CONFLICT
    return ""
