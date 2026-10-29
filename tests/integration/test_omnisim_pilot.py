import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from integrations.omnisim import OmniSimClient, OmniSimPilotRunner


class Handler(BaseHTTPRequestHandler):
    degraded = False
    requests: ClassVar[list[tuple[str, str, dict | None]]] = []

    def do_GET(self):
        path = urlparse(self.path).path
        self.__class__.requests.append(("GET", path, None))
        if path == "/healthz":
            self._send({"ok": True, "uptime_s": 1.0})
        elif path == "/capabilities":
            self._send(
                {
                    "ok": True,
                    "omnisim_wire": "1.1",
                    "service": "world_harness",
                    "sim_version": "test",
                    "physics": {
                        "backend": "newton",
                        "degraded": self.degraded,
                        "finalised": True,
                        "source": "sidecar",
                    },
                    "supervisor": {"connected": True, "light": True},
                    "world": {"load_ok": True, "load_state": "complete"},
                    "limits": {"recommended_max_steps_per_request": 20},
                }
            )
        elif path == "/sim/events":
            self._send(
                {
                    "events": [{"seq": 1, "type": "controller.log", "source": "log"}],
                    "next_since": 0,
                    "next_log_since": 1,
                }
            )
        else:
            self._send({"ok": False, "error": "not_found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length)) if length else None
        path = urlparse(self.path).path
        self.__class__.requests.append(("POST", path, payload))
        if path == "/world/load":
            self._send({"ok": True, "world": payload["path"], "supervisor": "connected"})
        elif path == "/sim/reset":
            self._send({"sim_time_ms": 0.0, "advanced_to_ms": 0.0, "restored": "__init__"})
        elif path == "/sim/step":
            self._send({"sim_time_ms": 32.0, "advanced_to_ms": 32.0})
        else:
            self._send({"ok": False, "error": "not_found"}, status=404)

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def harness():
    Handler.degraded = False
    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_pilot_writes_truthful_checksummed_simulation_artifact(harness, tmp_path):
    result = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path, steps=2)

    assert result.status == "EXECUTED"
    assert result.executed is True
    assert result.release_eligible is False
    metadata = json.loads((result.artifact_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["evidence_class"] == "SIMULATION"
    assert metadata["physical_evidence"] is False
    assert metadata["release_eligible"] is False
    assert metadata["mapped_to_workbench_event_contract"] is False
    assert "metadata.json" in (result.artifact_dir / "checksums.sha256").read_text(encoding="utf-8")
    assert [request[:2] for request in Handler.requests] == [
        ("GET", "/healthz"),
        ("POST", "/world/load"),
        ("GET", "/capabilities"),
        ("POST", "/sim/reset"),
        ("POST", "/sim/step"),
        ("GET", "/sim/events"),
    ]


def test_pilot_rejects_degraded_physics_without_stepping(harness, tmp_path):
    Handler.degraded = True

    result = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path)

    assert result.status == "INVALID_OUTPUT"
    assert result.executed is False
    metadata = json.loads((result.artifact_dir / "metadata.json").read_text(encoding="utf-8"))
    assert "degraded" in metadata["reason"]
    assert not any(path == "/sim/step" for _, path, _ in Handler.requests)


def test_pilot_records_unavailable_harness_as_not_executed(tmp_path):
    client = OmniSimClient("http://127.0.0.1:1", timeout_seconds=0.2)

    result = OmniSimPilotRunner(client).run("worlds/pick.omniworld", tmp_path)

    assert result.status == "NOT_EXECUTED"
    assert result.executed is False
    assert result.release_eligible is False


def test_client_rejects_non_loopback_control_url():
    with pytest.raises(ValueError, match="loopback"):
        OmniSimClient("https://example.com")


# --- Issue #455: the run identity comes from the probe, not the clock --------


def test_the_run_identity_is_stable_for_the_same_world(tmp_path: Path, harness) -> None:
    """Two probes of one world are the same probe, so they share an identity."""

    from integrations.omnisim.pilot import _run_suffix, _world_digest

    world = tmp_path / "world.usd"
    world.write_text("world-a", encoding="utf-8")
    digest = _world_digest(str(world))

    assert _run_suffix(digest, 1) == _run_suffix(digest, 1)
    assert digest != "unavailable"


def test_a_different_world_changes_the_identity(tmp_path: Path) -> None:
    from integrations.omnisim.pilot import _run_suffix, _world_digest

    first = tmp_path / "a.usd"
    second = tmp_path / "b.usd"
    first.write_text("world-a", encoding="utf-8")
    second.write_text("world-b", encoding="utf-8")

    assert _run_suffix(_world_digest(str(first)), 1) != _run_suffix(_world_digest(str(second)), 1)


def test_a_different_step_count_changes_the_identity(tmp_path: Path) -> None:
    from integrations.omnisim.pilot import _run_suffix, _world_digest

    world = tmp_path / "world.usd"
    world.write_text("world-a", encoding="utf-8")
    digest = _world_digest(str(world))

    assert _run_suffix(digest, 1) != _run_suffix(digest, 2)


def test_a_missing_world_reports_unavailable() -> None:
    from integrations.omnisim.pilot import _world_digest

    assert _world_digest("/nonexistent/world.usd") == "unavailable"


def test_the_published_metadata_records_the_world_digest(harness, tmp_path: Path) -> None:
    """The artifact has to be tieable to the world revision it loaded."""

    from integrations.omnisim.pilot import _world_digest

    world = tmp_path / "world.usd"
    world.write_text("world-a", encoding="utf-8")
    digest = _world_digest(str(world))
    assert digest != "unavailable"

    result = OmniSimPilotRunner(OmniSimClient(harness)).run(str(world), tmp_path / "runs", steps=1)
    metadata = json.loads((result.artifact_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["world_digest"] == digest
    assert metadata["requested_steps"] == 1
    assert metadata["run_id"].startswith("omnisim-")
    assert len(metadata["run_id"]) == len("omnisim-") + 12


def test_a_repeated_probe_publishes_the_same_run_id(harness, tmp_path: Path) -> None:
    """The issue: every invocation used to get a distinct uuid4 suffix."""

    world = tmp_path / "world.usd"
    world.write_text("world-a", encoding="utf-8")
    client = OmniSimClient(harness)

    first = OmniSimPilotRunner(client).run(str(world), tmp_path / "runs", steps=1)
    second = OmniSimPilotRunner(client).run(str(world), tmp_path / "runs-2", steps=1)

    assert first.run_id == second.run_id
