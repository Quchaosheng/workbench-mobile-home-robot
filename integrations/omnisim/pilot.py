"""Truthful simulation-only pilot runner for an OmniSim World Harness.

Issue #455: the pilot identity used to be ``omnisim-<uuid4>``, so two runs of the
same world, the same step count and the same simulator configuration produced
different run IDs and could not be compared as the same probe. The identity is
now a function of the digest of the world that was actually loaded plus the
bounded request inputs, and it is derived only after the harness has reported a
digest it can vouch for.

The digest is the whole point, so it is never guessed:

* the harness that loaded the world must report its digest;
* a caller may pin the digest it expects for a remote world, and a disagreement
  with the harness is refused;
* when the caller names an absolute local world file, that file's own bytes must
  agree with the harness, so the path a caller asked for and the world that was
  loaded cannot silently diverge;
* a missing, ambiguous or unreadable digest fails closed as ``INVALID_OUTPUT``
  before a run identity exists.

There is no random fallback. An identity that cannot be derived is not evidence,
so the refusal is recorded under one constant directory name and never under a
freshly generated one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import OmniSimClient, OmniSimError, OmniSimProtocolError, OmniSimUnavailable

_TRUSTED_PHYSICS_SOURCES = frozenset({"sidecar", "engine_log"})

# The identity input list, in the order it is hashed. This is the whole contract:
# adding an input changes every pilot run ID, which is the point, and the order is
# asserted by a test rather than left to dictionary iteration. ``world_sha256`` is
# the verified digest of the loaded world, never its path: a path is a name, and a
# name cannot tell a reviewer which revision of the world they are holding.
IDENTITY_INPUTS: tuple[str, ...] = (
    "world_sha256",
    "requested_steps",
    "pilot_tool_version",
    "pilot_identity_schema_version",
)

IDENTITY_SCHEMA_VERSION = "omnisim-pilot-identity-v1"

# The pilot tool version. It is a constant rather than a package lookup so two
# checkouts of the same source agree, and it is an identity input so a pilot whose
# protocol handling changed cannot silently claim the old identity.
PILOT_TOOL_VERSION = "1.0.0"

# The directory a refused pilot is recorded under. A refusal has no identity by
# construction, so it cannot be given a derived one; the name is a constant so two
# refusals of one cause can be compared, and it never has the ``omnisim-<hex>``
# shape of an evidence identity.
UNIDENTIFIED_RUN_ID = "omnisim-unidentified"

# A digest is carried in one spelling only. A bare 64-character hex SHA-256 is
# accepted in either case and canonicalized to lower case, so one world cannot
# acquire two identities from two spellings of the same digest.
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class OmniSimPilotResult:
    """Published result for one isolated OmniSim probe."""

    run_id: str
    status: str
    executed: bool
    release_eligible: bool
    artifact_dir: Path
    reason: str | None


@dataclass(frozen=True)
class PilotIdentity:
    """The digest of one pilot's identity material, with that material kept.

    Keeping the material is deliberate: a mismatch is otherwise an opaque hex
    string, and a reviewer cannot tell which input moved.
    """

    identity_hash: str
    material: dict[str, str]
    inputs: tuple[str, ...] = IDENTITY_INPUTS
    schema_version: str = IDENTITY_SCHEMA_VERSION
    hash_algorithm: str = "sha256"

    @property
    def run_id(self) -> str:
        return f"omnisim-{self.identity_hash[:16]}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hash_algorithm": self.hash_algorithm,
            "identity_hash": self.identity_hash,
            "inputs": list(self.inputs),
            "material": dict(self.material),
        }


def parse_world_digest(value: object) -> str:
    """Canonicalize one reported or caller-supplied digest, refusing anything else.

    Only a bare 64-character hex SHA-256 is accepted, optionally prefixed by
    ``sha256:``. A path, a truncated digest, a non-string or a hex string of the
    wrong length is refused rather than coerced, because a digest that is guessed
    is not provenance.
    """

    if not isinstance(value, str):
        raise OmniSimProtocolError("OmniSim world digest must be a string")
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    if not _SHA256_HEX.fullmatch(text):
        raise OmniSimProtocolError("OmniSim world digest must be a bare sha256 hex digest")
    return text


def reported_world_digests(response: Any) -> set[str]:
    """Every distinct digest one harness response reports, in canonical spelling.

    A harness may report the digest at ``world.digest`` or at the response's
    top-level ``digest``. A response that reports two different values is
    ambiguous; it is returned as a set so the caller can refuse it by name.
    """

    found: set[str] = set()
    if not isinstance(response, dict):
        return found
    world = response.get("world")
    if isinstance(world, dict) and world.get("digest") is not None:
        found.add(parse_world_digest(world["digest"]))
    if response.get("digest") is not None:
        found.add(parse_world_digest(response["digest"]))
    return found


def hash_world_file(path: Path) -> str:
    """Hash a world file's own bytes, so a local world can be cross-checked."""

    try:
        with Path(path).open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise OmniSimProtocolError(f"OmniSim world file cannot be read for hashing: {error}") from error
    return digest.hexdigest()


def pilot_identity(*, world_sha256: object, requested_steps: object) -> PilotIdentity:
    """Derive the pilot identity from a verified world digest and the request.

    This is a pure function of its declared inputs, so two processes given the
    same world digest and the same bounded request agree without sharing state.
    It raises :class:`OmniSimProtocolError` when an input cannot be a digest or a
    bounded step count, because a caller that cannot supply those has no
    reproducible identity to publish.
    """

    digest = parse_world_digest(world_sha256)
    if type(requested_steps) is not int or not 1 <= requested_steps <= 100:
        raise OmniSimProtocolError("OmniSim pilot identity requires a bounded integer step count")
    material = {
        "world_sha256": digest,
        "requested_steps": str(requested_steps),
        "pilot_tool_version": PILOT_TOOL_VERSION,
        "pilot_identity_schema_version": IDENTITY_SCHEMA_VERSION,
    }
    payload = json.dumps(
        {"schema_version": IDENTITY_SCHEMA_VERSION, "inputs": {name: material[name] for name in IDENTITY_INPUTS}},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")
    return PilotIdentity(material=material, identity_hash=hashlib.sha256(payload).hexdigest())


class OmniSimPilotRunner:
    """Load, reset, step, and record one bounded OmniSim world probe."""

    def __init__(self, client: OmniSimClient) -> None:
        self._client = client

    def run(
        self,
        world_path: str,
        output_dir: Path,
        *,
        steps: int = 1,
        world_digest: str | None = None,
    ) -> OmniSimPilotResult:
        """Run one probe into ``output_dir``, or refuse before it publishes an identity.

        ``world_digest`` is the digest a caller expects the harness to have
        loaded. It is a cross-check, never a substitute: a world this process
        cannot read still needs the harness to report the digest it actually
        loaded, so a caller cannot pin a revision and call it verified.

        Raises :class:`FileExistsError` when the derived directory already holds
        an artifact. The identity is deterministic, so a rerun of one probe names
        the same directory; refusing is how the first artifact survives.
        """

        if not isinstance(world_path, str) or not world_path.strip():
            raise ValueError("world_path must be non-empty")
        if type(steps) is not int or not 1 <= steps <= 100:
            raise ValueError("steps must be an integer between 1 and 100")
        if world_digest is not None:
            world_digest = parse_world_digest(world_digest)

        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        status = "NOT_EXECUTED"
        executed = False
        reason: str | None = None
        responses: dict[str, dict[str, Any]] = {}
        identity: PilotIdentity | None = None
        started_at = datetime.now(UTC).isoformat()

        try:
            responses["health"] = self._client.health()
            _require_true(responses["health"].get("ok"), "health.ok")

            responses["world-load"] = self._client.load_world(world_path, light=True)
            _require_true(responses["world-load"].get("ok"), "world-load.ok")

            responses["capabilities"] = self._client.capabilities()
            _validate_capabilities(responses["capabilities"], steps)

            # The identity is derived from the digest the harness actually
            # reported, cross-checked against the caller's claim and the local
            # file. Any disagreement is refused here, before a run directory
            # exists that could be mistaken for evidence.
            identity = pilot_identity(
                world_sha256=_verified_world_digest(
                    responses["world-load"],
                    responses["capabilities"],
                    world_path=world_path,
                    claimed=world_digest,
                ),
                requested_steps=steps,
            )

            responses["reset"] = self._client.reset()
            _require_number(responses["reset"].get("sim_time_ms"), "reset.sim_time_ms")

            responses["step"] = self._client.step(steps)
            _validate_step(responses["step"])
            executed = True

            responses["events"] = self._client.events()
            _validate_events(responses["events"])
            status = "EXECUTED"
        except OmniSimProtocolError as error:
            # A protocol defect -- including a missing, ambiguous or unreadable
            # world digest -- is INVALID_OUTPUT, never a freshly generated
            # identity. The refusal is still published under a constant name so
            # the refusal itself is auditable rather than silently dropped.
            status = "INVALID_OUTPUT"
            reason = str(error)
            identity = None
        except OmniSimUnavailable as error:
            status = "FAILED" if responses else "NOT_EXECUTED"
            reason = str(error)
        except OmniSimError as error:
            status = "FAILED" if responses else "NOT_EXECUTED"
            reason = str(error)

        run_id = identity.run_id if identity is not None else UNIDENTIFIED_RUN_ID
        final = destination / run_id
        # The refusal comes before a staging directory is created, so a collision
        # leaves no partial artifact behind and no prior evidence is replaced.
        if final.exists():
            raise FileExistsError(f"refusing to overwrite existing OmniSim pilot artifact: {final}")
        staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.", suffix=".partial", dir=destination))

        for name, response in responses.items():
            _write_json(staging / f"{name}.json", response)
        metadata = {
            "schema_version": "omnisim-pilot-v1",
            "run_id": run_id,
            "status": status,
            "executed": executed,
            "release_eligible": False,
            "evidence_class": "SIMULATION",
            "physical_evidence": False,
            "mapped_to_workbench_event_contract": False,
            "world_path": world_path,
            "world_sha256": identity.material["world_sha256"] if identity is not None else None,
            "requested_steps": steps,
            "pilot_tool_version": PILOT_TOOL_VERSION,
            "identity": identity.as_dict() if identity is not None else None,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "reason": reason,
        }
        _write_json(staging / "metadata.json", metadata)
        _write_checksums(staging)
        os.replace(staging, final)
        return OmniSimPilotResult(run_id, status, executed, False, final, reason)


def _verified_world_digest(
    load_response: dict[str, Any],
    capabilities: dict[str, Any],
    *,
    world_path: str,
    claimed: str | None,
) -> str:
    """The one digest the harness reports, the caller expects and the file agrees on."""

    reported = reported_world_digests(load_response) | reported_world_digests(capabilities)
    if not reported:
        raise OmniSimProtocolError(
            "OmniSim harness reported no world digest; the pilot identity cannot be derived from a path name"
        )
    if len(reported) > 1:
        raise OmniSimProtocolError("OmniSim harness reported conflicting world digests")
    digest = reported.pop()

    if claimed is not None and claimed != digest:
        raise OmniSimProtocolError("the supplied world_digest does not match the digest the harness loaded")
    local = _absolute_local_world(world_path)
    if local is not None and hash_world_file(local) != digest:
        raise OmniSimProtocolError("the loaded world digest does not match the local world file")
    return digest


def _absolute_local_world(world_path: str) -> Path | None:
    """The absolute local path a caller named, when it is a file this process may hash.

    A relative path is deliberately not resolved: the pilot and the harness may
    run from different working directories, and refusing a legitimate remote
    world because an unrelated relative path happens to exist would make the
    fail-closed rule useless. An absolute path is unambiguous, so it is checked.
    """

    candidate = Path(world_path)
    if not candidate.is_absolute():
        return None
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _validate_capabilities(capabilities: dict[str, Any], requested_steps: int) -> None:
    _require_true(capabilities.get("ok"), "capabilities.ok")
    if capabilities.get("service") != "world_harness":
        raise OmniSimProtocolError("OmniSim capability response is not from a world_harness")
    version = capabilities.get("omnisim_wire")
    if not isinstance(version, str) or version.split(".", maxsplit=1)[0] != "1":
        raise OmniSimProtocolError("unsupported OmniSim wire protocol major")

    physics = capabilities.get("physics")
    if not isinstance(physics, dict):
        raise OmniSimProtocolError("OmniSim capabilities are missing physics status")
    if (
        physics.get("backend") != "newton"
        or physics.get("degraded") is not False
        or physics.get("finalised") is not True
        or physics.get("source") not in _TRUSTED_PHYSICS_SOURCES
    ):
        raise OmniSimProtocolError("OmniSim Newton physics is absent, degraded, or unverified")

    supervisor = capabilities.get("supervisor")
    if not isinstance(supervisor, dict) or supervisor.get("connected") is not True:
        raise OmniSimProtocolError("OmniSim supervisor is not connected")
    world = capabilities.get("world")
    if not isinstance(world, dict) or world.get("load_ok") is not True or world.get("load_state") != "complete":
        raise OmniSimProtocolError("OmniSim world load is not complete")

    limits = capabilities.get("limits")
    recommendation = limits.get("recommended_max_steps_per_request") if isinstance(limits, dict) else None
    if type(recommendation) is int and requested_steps > recommendation:
        raise OmniSimProtocolError("requested steps exceed OmniSim's measured recommendation")
    if recommendation is None and requested_steps > 10:
        raise OmniSimProtocolError("requested steps exceed the unmeasured pilot limit")


def _validate_step(response: dict[str, Any]) -> None:
    sim_time = _require_number(response.get("sim_time_ms"), "step.sim_time_ms")
    advanced_to = _require_number(response.get("advanced_to_ms"), "step.advanced_to_ms")
    if sim_time < 0 or advanced_to < 0 or sim_time != advanced_to:
        raise OmniSimProtocolError("OmniSim step returned inconsistent simulation time")


def _validate_events(response: dict[str, Any]) -> None:
    events = response.get("events")
    if not isinstance(events, list):
        raise OmniSimProtocolError("OmniSim events response is missing events")
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise OmniSimProtocolError("OmniSim events response contains a malformed event")


def _require_true(value: object, field: str) -> None:
    if value is not True:
        raise OmniSimProtocolError(f"OmniSim response requires {field}=true")


def _require_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OmniSimProtocolError(f"OmniSim response requires numeric {field}")
    return float(value)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_checksums(directory: Path) -> None:
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.name != "checksums.sha256")
    content = "".join(f"{_sha256(path)}  {path.name}\n" for path in files)
    (directory / "checksums.sha256").write_text(content, encoding="utf-8")
