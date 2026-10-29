"""Truthful simulation-only pilot runner for an OmniSim World Harness."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import OmniSimClient, OmniSimError, OmniSimProtocolError, OmniSimUnavailable

_TRUSTED_PHYSICS_SOURCES = frozenset({"sidecar", "engine_log"})


@dataclass(frozen=True)
class OmniSimPilotResult:
    """Published result for one isolated OmniSim probe."""

    run_id: str
    status: str
    executed: bool
    release_eligible: bool
    artifact_dir: Path
    reason: str | None


class OmniSimPilotRunner:
    """Load, reset, step, and record one bounded OmniSim world probe."""

    def __init__(self, client: OmniSimClient) -> None:
        self._client = client

    def run(self, world_path: str, output_dir: Path, *, steps: int = 1) -> OmniSimPilotResult:
        if not isinstance(world_path, str) or not world_path.strip():
            raise ValueError("world_path must be non-empty")
        if type(steps) is not int or not 1 <= steps <= 100:
            raise ValueError("steps must be an integer between 1 and 100")

        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        # The run identity has to be a function of the probe, not of the clock: a
        # uuid4 made every rerun of the same world a different artifact that could
        # not be compared to its predecessor (issue #455). The digest is taken from
        # the world file that was actually loaded, so an artifact can be tied back
        # to the revision it came from.
        world_digest = _world_digest(world_path)
        run_id = f"omnisim-{_run_suffix(world_digest, steps)}"
        staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.", suffix=".partial", dir=destination))
        final = destination / run_id
        responses: dict[str, dict[str, Any]] = {}
        executed = False
        status = "NOT_EXECUTED"
        reason: str | None = None
        started_at = datetime.now(UTC).isoformat()

        try:
            responses["health"] = self._client.health()
            _require_true(responses["health"].get("ok"), "health.ok")

            responses["world-load"] = self._client.load_world(world_path, light=True)
            _require_true(responses["world-load"].get("ok"), "world-load.ok")

            responses["capabilities"] = self._client.capabilities()
            _validate_capabilities(responses["capabilities"], steps)

            responses["reset"] = self._client.reset()
            _require_number(responses["reset"].get("sim_time_ms"), "reset.sim_time_ms")

            responses["step"] = self._client.step(steps)
            _validate_step(responses["step"])
            executed = True

            responses["events"] = self._client.events()
            _validate_events(responses["events"])
            status = "EXECUTED"
        except OmniSimUnavailable as error:
            status = "FAILED" if responses else "NOT_EXECUTED"
            reason = str(error)
        except OmniSimProtocolError as error:
            status = "INVALID_OUTPUT"
            reason = str(error)
        except OmniSimError as error:
            status = "FAILED" if responses else "NOT_EXECUTED"
            reason = str(error)

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
            "world_digest": world_digest,
            "requested_steps": steps,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "reason": reason,
        }
        _write_json(staging / "metadata.json", metadata)
        _write_checksums(staging)
        os.replace(staging, final)
        return OmniSimPilotResult(run_id, status, executed, False, final, reason)


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


def _world_digest(world_path: str) -> str:
    """The sha256 of the world file this probe loads.

    A missing or unreadable world is reported as ``unavailable`` rather than
    raising: the run still has to record why it produced no evidence, and the
    loader is the component that decides whether the world is usable.
    """

    path = Path(world_path)
    try:
        if not path.is_file():
            return "unavailable"
        return _sha256(path)
    except OSError:
        return "unavailable"


def _run_suffix(world_digest: str, steps: int) -> str:
    """A stable 12-hex suffix derived from the probe inputs."""

    material = json.dumps(
        {"world_digest": world_digest, "steps": steps},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:12]


def _write_checksums(directory: Path) -> None:
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.name != "checksums.sha256")
    content = "".join(f"{_sha256(path)}  {path.name}\n" for path in files)
    (directory / "checksums.sha256").write_text(content, encoding="utf-8")
