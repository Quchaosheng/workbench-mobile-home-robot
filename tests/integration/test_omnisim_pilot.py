import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from integrations.omnisim import (
    IDENTITY_INPUTS,
    PILOT_TOOL_VERSION,
    OmniSimClient,
    OmniSimPilotRunner,
    OmniSimProtocolError,
    pilot_identity,
)


class Handler(BaseHTTPRequestHandler):
    degraded = False
    # Issue #455: a load report names the digest of the world it loaded, and the
    # pilot refuses a harness that cannot. Both attributes are class state so a
    # test can serve a different world revision, omit the digest, or make the
    # two reports disagree, without a second handler.
    world_digest = "a1" * 32
    capability_digest = None
    report_digest = True
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
                    **({"digest": self.capability_digest or self.world_digest} if self.report_digest else {}),
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
            report = {"ok": True, "world": payload["path"], "supervisor": "connected"}
            if self.report_digest:
                report["digest"] = self.world_digest
            self._send(report)
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
    Handler.world_digest = "a1" * 32
    Handler.capability_digest = None
    Handler.report_digest = True
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


class TestPilotWorldDigestIdentity:
    """Issue #455: a pilot identity is a verified world digest, never a uuid4.

    This is one self-contained class at the end of the module on purpose: Issue
    #466 also edits this file, so every addition below is new and nothing above
    it moved, was reordered or was restructured.
    """

    # A child process that imports the installed module and prints one run ID, so
    # the cross-process vector is measured rather than asserted from one process.
    IDENTITY_SCRIPT = (
        "import sys; from integrations.omnisim import pilot_identity;"
        "print(pilot_identity(world_sha256=sys.argv[1], requested_steps=int(sys.argv[2])).run_id)"
    )

    @staticmethod
    def _metadata(result):
        return json.loads((result.artifact_dir / "metadata.json").read_text(encoding="utf-8"))

    @staticmethod
    def _identity_in_another_process(digest, steps):
        completed = subprocess.run(
            [sys.executable, "-c", TestPilotWorldDigestIdentity.IDENTITY_SCRIPT, digest, str(steps)],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            timeout=120,
        )
        return completed.stdout.strip()

    def test_same_world_digest_and_request_derive_the_same_run_id_across_processes(self, harness, tmp_path):
        first = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path)
        rerun = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path / "rerun")

        assert first.status == "EXECUTED"
        assert first.run_id == rerun.run_id
        assert self._metadata(first)["run_id"] == first.run_id
        # The identity is a property of the digest and the request, so a second
        # process given the same two inputs derives the same ID without sharing
        # any state with this one.
        assert self._identity_in_another_process(Handler.world_digest, 1) == first.run_id

    def test_changed_world_digest_derives_a_different_run_id(self, harness, tmp_path):
        first = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path)

        Handler.world_digest = "b2" * 32
        second = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path)

        assert first.run_id != second.run_id
        assert self._metadata(second)["world_sha256"] == "b2" * 32

    def test_changed_step_count_derives_a_different_run_id(self, harness, tmp_path):
        one = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path, steps=1)
        two = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path, steps=2)

        assert one.run_id != two.run_id
        assert self._metadata(two)["requested_steps"] == 2

    def test_missing_world_digest_is_refused_without_a_derived_identity(self, harness, tmp_path):
        Handler.report_digest = False

        result = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path)

        assert result.status == "INVALID_OUTPUT"
        assert result.executed is False
        metadata = self._metadata(result)
        assert metadata["identity"] is None
        assert metadata["world_sha256"] is None
        assert "no world digest" in metadata["reason"]
        assert not any(path == "/sim/step" for _, path, _ in Handler.requests)

    def test_conflicting_world_digests_are_refused(self, harness, tmp_path):
        Handler.capability_digest = "c3" * 32

        result = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path)

        assert result.status == "INVALID_OUTPUT"
        assert result.executed is False
        assert "conflicting world digests" in self._metadata(result)["reason"]

    def test_an_absolute_local_world_must_match_the_digest_the_harness_loaded(self, harness, tmp_path):
        world = tmp_path / "pick.omniworld"
        world.write_bytes(b"authored world revision")
        Handler.world_digest = hashlib.sha256(world.read_bytes()).hexdigest()

        matched = OmniSimPilotRunner(OmniSimClient(harness)).run(str(world), tmp_path / "runs")
        assert matched.status == "EXECUTED"
        assert self._metadata(matched)["world_sha256"] == Handler.world_digest

        Handler.world_digest = "d4" * 32
        mismatched = OmniSimPilotRunner(OmniSimClient(harness)).run(str(world), tmp_path / "runs")

        assert mismatched.status == "INVALID_OUTPUT"
        assert "local world file" in self._metadata(mismatched)["reason"]

    def test_a_relative_world_path_is_not_hashed_as_provenance(self, harness, tmp_path):
        # The pilot cannot read the harness's world, so the digest must come from
        # the load report rather than from hashing the path string it was given.
        Handler.world_digest = "e5" * 32

        result = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path)

        assert result.run_id == pilot_identity(world_sha256="e5" * 32, requested_steps=1).run_id
        assert result.run_id != pilot_identity(world_sha256="f6" * 32, requested_steps=1).run_id

    def test_a_caller_pinned_digest_must_match_the_digest_the_harness_loaded(self, harness, tmp_path):
        runner = OmniSimPilotRunner(OmniSimClient(harness))
        pinned = runner.run("worlds/pick.omniworld", tmp_path, world_digest="a1" * 32)

        assert pinned.status == "EXECUTED"

        mismatched = runner.run("worlds/pick.omniworld", tmp_path / "mismatched", world_digest="b2" * 32)

        assert mismatched.status == "INVALID_OUTPUT"
        assert "world_digest" in self._metadata(mismatched)["reason"]

    def test_existing_output_is_refused_rather_than_overwritten(self, harness, tmp_path):
        runner = OmniSimPilotRunner(OmniSimClient(harness))
        first = runner.run("worlds/pick.omniworld", tmp_path)
        checksums = (first.artifact_dir / "checksums.sha256").read_text(encoding="utf-8")

        with pytest.raises(FileExistsError, match="refusing to overwrite"):
            runner.run("worlds/pick.omniworld", tmp_path)

        assert (first.artifact_dir / "checksums.sha256").read_text(encoding="utf-8") == checksums
        assert sorted(path.name for path in tmp_path.iterdir()) == [first.run_id]

    def test_metadata_retains_simulation_only_claims_and_records_the_identity(self, harness, tmp_path):
        result = OmniSimPilotRunner(OmniSimClient(harness)).run("worlds/pick.omniworld", tmp_path, steps=2)

        metadata = self._metadata(result)
        assert metadata["release_eligible"] is False
        assert metadata["evidence_class"] == "SIMULATION"
        assert metadata["physical_evidence"] is False
        assert metadata["mapped_to_workbench_event_contract"] is False
        assert metadata["pilot_tool_version"] == PILOT_TOOL_VERSION
        assert metadata["world_sha256"] == Handler.world_digest
        assert metadata["identity"]["inputs"] == list(IDENTITY_INPUTS)
        assert metadata["identity"]["material"]["requested_steps"] == "2"
        assert result.run_id == f"omnisim-{metadata['identity']['identity_hash'][:16]}"
        assert "metadata.json" in (result.artifact_dir / "checksums.sha256").read_text(encoding="utf-8")

    def test_identity_inputs_are_a_committed_order_and_a_malformed_input_is_refused(self):
        assert IDENTITY_INPUTS == (
            "world_sha256",
            "requested_steps",
            "pilot_tool_version",
            "pilot_identity_schema_version",
        )
        with pytest.raises(OmniSimProtocolError, match="sha256"):
            pilot_identity(world_sha256="not-a-digest", requested_steps=1)
        with pytest.raises(OmniSimProtocolError, match="step count"):
            pilot_identity(world_sha256="a1" * 32, requested_steps=0)
        assert pilot_identity(world_sha256="SHA256:" + "A1" * 32, requested_steps=1) == pilot_identity(
            world_sha256="a1" * 32, requested_steps=1
        )
