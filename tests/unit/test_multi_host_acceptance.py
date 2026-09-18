"""Issue #74: split-host acceptance for an absent, slow, partitioned or restarted peer.

`docs/deployment/multi-host.md` promises four behaviours about a split-host
controller: readiness becomes NOT_READY within a bounded time when the
simulation host is unavailable, a transient outage recovers without duplicating
runs, a partial or old event log is never rendered as fresh state, and a restart
preserves explicit run and source identity.

Those promises were documented but never measured. These tests drive the real
`workbench_backend.server` controller against a deterministic HTTP fake, so the
failure, recovery, partition, slow-response, DNS and malformed-response paths are
exercised without Docker or a second physical machine. Timing and failure
outcomes are recorded per request rather than inferred from a green run.

The fake is a stand-in for the simulation host's read-only event API, and every
request it receives is recorded as `(path, mode, outcome, elapsed_s)`, so a test
asserts what the peer was actually asked and how it behaved rather than only the
controller's final status code. Covered paths: absent peer, connection reset,
slow response, malformed response, non-JSON content type, an error status, a
sequence gap, a legitimate in-progress prefix, an old run, an unresolvable
hostname, and a source restart.

This is not physical-network evidence. Real inter-host latency, packet loss,
MTU behaviour and clock skew between two machines remain `NOT_EXECUTED`.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from workbench_backend.read_model import DashboardReadModel, ReadModelError, RemoteDashboardReadModel
from workbench_backend.server import create_server

CONTROLLER_SOURCE = "remote-simulation-event-source"
READINESS_DEADLINE_SECONDS = 5.0


def run_events(run_id: str, *, status: str = "confirmed", event_count: int = 3) -> list[dict]:
    """Build one canonical, contract-valid run event stream."""
    accepted = {
        "event_id": f"{run_id}-evt-0",
        "run_id": run_id,
        "sequence_no": 0,
        "event_type": "task_accepted",
        "occurred_at": "2026-08-22T00:00:00Z",
        "payload": {"task_id": "task-1", "goal": "Place the red block in the tray"},
        "evidence_refs": [],
    }
    verification = {
        "event_id": f"{run_id}-evt-1",
        "run_id": run_id,
        "sequence_no": 1,
        "event_type": "verification",
        "occurred_at": "2026-08-22T00:00:01Z",
        "payload": {"status": status, "missing_evidence": []},
        "evidence_refs": ["frame-1"],
    }
    if event_count == 2:
        return [accepted, verification]
    terminal = {
        "event_id": f"{run_id}-evt-2",
        "run_id": run_id,
        "sequence_no": 2,
        "event_type": "task_terminal",
        "occurred_at": "2026-08-22T00:00:02Z",
        "payload": {"status": status},
        "evidence_refs": [],
    }
    return [accepted, verification, terminal]


class ScriptableEventSource:
    """A deterministic stand-in for the simulation host's read-only event API.

    Every request is appended to `requests` as `(path, mode, outcome, elapsed_s)`
    so a test can assert what the peer was actually asked and how it behaved,
    instead of only observing the controller's final status code.
    """

    def __init__(self, runs: dict[str, list[dict]] | None = None) -> None:
        self.runs = runs if runs is not None else {"run-a": run_events("run-a")}
        self.mode = "ok"
        self.slow_seconds = 2.0
        self.requests: list[tuple[str, str, str, float]] = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._base_url: str | None = None

    @property
    def base_url(self) -> str:
        if self._base_url is None:
            raise RuntimeError("the event source is not running")
        return self._base_url

    def _begin(self, path: str, mode: str) -> int:
        """Record the request as it arrives, so a hung peer is still inspectable."""
        with self._lock:
            self.requests.append((path, mode, "in_progress", 0.0))
            return len(self.requests) - 1

    def _finish(self, index: int, outcome: str, elapsed: float) -> None:
        with self._lock:
            path, mode, _previous, _elapsed = self.requests[index]
            self.requests[index] = (path, mode, outcome, elapsed)

    def start(self) -> None:
        source = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                started = time.perf_counter()
                mode = source.mode
                index = source._begin(self.path, mode)
                if mode == "partition":
                    source._finish(index, "reset", time.perf_counter() - started)
                    self.close_connection = True
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    return
                if mode == "slow":
                    time.sleep(source.slow_seconds)
                status, body, content_type = source._response_for(self.path, mode)
                payload = body.encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    outcome = f"{status}"
                except (BrokenPipeError, ConnectionResetError):
                    outcome = "client_gone"
                source._finish(index, outcome, time.perf_counter() - started)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        self._base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
        self._base_url = None

    def _response_for(self, path: str, mode: str) -> tuple[int, str, str]:
        if mode == "malformed":
            return 200, '{"events": [', "application/json"
        if mode == "wrong_content_type":
            return 200, json.dumps({"runs": []}), "text/plain"
        if mode == "error":
            return 503, json.dumps({"error": "unavailable"}), "application/json"
        if mode == "sequence_gap":
            # A well-formed envelope whose run stream skips a sequence number.
            events = run_events("run-a", event_count=2)
            events[1]["sequence_no"] = 5
            return 200, json.dumps({"events": events}), "application/json"
        if mode == "in_progress":
            # A legitimate prefix of a run: accepted, nothing verified yet.
            return 200, json.dumps({"events": run_events("run-a", event_count=2)[:1]}), "application/json"
        if mode == "stale":
            # An old run whose timestamps must survive verbatim.
            events = run_events("run-a", event_count=2)
            for event in events:
                event["occurred_at"] = "2020-01-01T00:00:00Z"
            return 200, json.dumps({"events": events}), "application/json"
        if path == "/readyz":
            return 200, json.dumps({"status": "ready", "data_source": "dashboard-fixtures"}), "application/json"
        if path in {"/api/runs", "/api/v1/runs"}:
            runs = [{"run_id": run_id, "event_count": len(events)} for run_id, events in sorted(self.runs.items())]
            return 200, json.dumps({"runs": runs, "read_only": True}), "application/json"
        prefix = "/api/v1/runs/"
        if path.startswith(prefix) and path.endswith("/events"):
            run_id = path[len(prefix) : -len("/events")]
            events = self.runs.get(run_id)
            if events is None:
                return 404, json.dumps({"error": "not_found"}), "application/json"
            return 200, json.dumps({"events": events}), "application/json"
        return 404, json.dumps({"error": "not_found"}), "application/json"


class ControllerHarness:
    """Run the shipped controller in-process against a scripted event source."""

    def __init__(self, event_source_url: str, **options: object) -> None:
        self.server = create_server("127.0.0.1", 0, event_source_url=event_source_url, **options)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, path: str) -> tuple[int, dict, float]:
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(f"{self.base_url}{path}", timeout=READINESS_DEADLINE_SECONDS + 5) as response:
                payload = json.loads(response.read())
                return response.status, payload, time.perf_counter() - started
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read())
            return exc.code, payload, time.perf_counter() - started

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class SplitHostAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = ScriptableEventSource()
        self.source.start()
        self.addCleanup(self.source.stop)

    def harness(self, **options: object) -> ControllerHarness:
        harness = ControllerHarness(self.source.base_url, **options)
        self.addCleanup(harness.close)
        return harness

    def test_readiness_is_bounded_and_unavailable_when_the_peer_never_answers(self) -> None:
        """A peer that resets every connection must not hold readiness open."""
        controller = self.harness()
        status, payload, elapsed = controller.request("/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data_source"], CONTROLLER_SOURCE)

        self.source.mode = "partition"
        status, payload, elapsed = controller.request("/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "not_ready")
        self.assertEqual(payload["data_source"], CONTROLLER_SOURCE)
        self.assertLess(elapsed, READINESS_DEADLINE_SECONDS)
        self.assertEqual(self.source.requests[-1][1], "partition")
        self.assertEqual(self.source.requests[-1][2], "reset")

    def test_a_partition_recovers_without_duplicating_or_losing_runs(self) -> None:
        """An outage must not change the run set the operator sees afterwards."""
        self.source.runs = {"run-a": run_events("run-a"), "run-b": run_events("run-b", status="refuted")}
        controller = self.harness()
        before = controller.request("/api/v1/runs")[1]["runs"]

        self.source.mode = "partition"
        self.assertEqual(controller.request("/api/v1/runs")[0], 503)
        self.assertEqual(controller.request("/readyz")[0], 503)

        self.source.mode = "ok"
        status, _payload, elapsed = controller.request("/readyz")
        self.assertEqual(status, 200)
        self.assertLess(elapsed, READINESS_DEADLINE_SECONDS)
        after = controller.request("/api/v1/runs")[1]["runs"]
        self.assertEqual(after, before)
        self.assertEqual([run["run_id"] for run in after], ["run-a", "run-b"])

        events = controller.request("/api/v1/runs/run-b/events")[1]["events"]
        self.assertEqual([event["run_id"] for event in events], ["run-b"] * 3)
        self.assertEqual(events[1]["payload"]["status"], "refuted")

    def test_a_slow_peer_is_reported_unavailable_within_the_client_timeout(self) -> None:
        """A hanging peer must be bounded, not inherited as an indefinite wait."""
        self.source.mode = "slow"
        self.source.slow_seconds = 2.0
        controller = self.harness()
        status, payload, elapsed = controller.request("/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "not_ready")
        self.assertLess(elapsed, READINESS_DEADLINE_SECONDS, "readiness must be bounded by the client timeout")
        self.assertLess(elapsed, self.source.slow_seconds)
        path, mode, outcome, peer_elapsed = self.source.requests[-1]
        self.assertEqual(path, "/readyz")
        self.assertEqual(mode, "slow")
        # The controller answered before the peer finished responding, which is
        # the observable form of "readiness is bounded by the client timeout".
        self.assertEqual(outcome, "in_progress")
        self.assertEqual(peer_elapsed, 0.0)

    def test_a_malformed_response_is_never_rendered_as_state(self) -> None:
        """Truncated JSON and a non-JSON content type must both fail closed."""
        controller = self.harness()
        for mode in ("malformed", "wrong_content_type", "error"):
            with self.subTest(mode=mode):
                self.source.mode = mode
                status, payload, _elapsed = controller.request("/api/v1/runs")
                self.assertEqual(status, 503)
                self.assertEqual(payload["error"], "invalid_event_source")
                ready_status, ready_payload, _elapsed = controller.request("/readyz")
                self.assertEqual(ready_status, 503)
                self.assertEqual(ready_payload["status"], "not_ready")
            self.source.mode = "ok"

    def test_a_partial_stream_never_reports_a_run_as_complete(self) -> None:
        """A prefix of a run is honest `running`; a hole in the stream fails closed."""
        controller = self.harness()

        # A legitimate prefix means the run is still in flight. It must read as
        # running, never as the confirmed outcome the full stream would carry.
        self.source.mode = "in_progress"
        status, payload, _elapsed = controller.request("/api/v1/runs/run-a/events")
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"]["status"], "running")
        self.assertNotEqual(payload["run"]["status"], "confirmed")
        self.assertEqual([event["sequence_no"] for event in payload["events"]], [0])

        # A stream that skips a sequence number is not a run at all.
        self.source.mode = "sequence_gap"
        status, payload, _elapsed = controller.request("/api/v1/runs/run-a/events")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "invalid_event_source")
        self.assertEqual(controller.request("/readyz")[0], 503)

    def test_an_old_run_keeps_its_own_time_instead_of_being_stamped_fresh(self) -> None:
        """`updated_at` must be the run's observed time, not the read time."""
        controller = self.harness()
        self.source.mode = "stale"
        status, payload, _elapsed = controller.request("/api/v1/runs/run-a/events")
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"]["updated_at"], "2020-01-01T00:00:00Z")
        self.assertEqual([event["occurred_at"] for event in payload["events"]], ["2020-01-01T00:00:00Z"] * 2)

    def test_dns_failure_is_reported_unavailable_rather_than_crashing(self) -> None:
        """An unresolvable simulation host must yield NOT_READY, not an exception."""
        with TemporaryDirectory() as directory:
            server = create_server(
                "127.0.0.1",
                0,
                data_dir=Path(directory),
                event_source_url="http://sim.invalid:8090",
                event_source_allowlist="10.0.0.0/8",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                # Only the peer's name resolution fails; the test's own call to
                # the controller over 127.0.0.1 must keep working.
                blocker = mock.Mock(side_effect=socket.gaierror(socket.EAI_NONAME, "Name or service not known"))
                original = socket.getaddrinfo

                def selective_getaddrinfo(host, *args, **kwargs):
                    if host == "sim.invalid":
                        return blocker(host, *args, **kwargs)
                    return original(host, *args, **kwargs)

                with mock.patch("workbench_backend.remote_http.socket.getaddrinfo", side_effect=selective_getaddrinfo):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(f"{base_url}/readyz", timeout=READINESS_DEADLINE_SECONDS)
                    self.assertEqual(caught.exception.code, 503)
                    self.assertEqual(json.loads(caught.exception.read())["status"], "not_ready")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_run_and_source_identity_survive_a_source_restart(self) -> None:
        """A restarted source keeps its run IDs, and both roles stay self-describing."""
        controller = self.harness()
        first = controller.request("/api/v1/runs")[1]["runs"]
        first_source = controller.request("/readyz")[1]["data_source"]

        self.source.stop()
        self.source.start()
        self.source.runs = {"run-a": run_events("run-a")}
        restarted = ControllerHarness(self.source.base_url)
        self.addCleanup(restarted.close)

        second = restarted.request("/api/v1/runs")[1]["runs"]
        self.assertEqual(second, first, "a restart must preserve rather than renumber run identity")
        self.assertEqual([run["run_id"] for run in second], ["run-a"])
        self.assertEqual(restarted.request("/readyz")[1]["data_source"], first_source)
        self.assertEqual(first_source, CONTROLLER_SOURCE)

        events = restarted.request("/api/v1/runs/run-a/events")[1]["events"]
        self.assertEqual({event["run_id"] for event in events}, {"run-a"})

    def test_a_partially_written_local_log_is_not_served_as_ready(self) -> None:
        """The source side must not present a half-written run as fresh state."""
        with TemporaryDirectory() as directory:
            data_dir = Path(directory)
            log = data_dir / "run-a.jsonl"
            log.write_text("\n".join(json.dumps(event) for event in run_events("run-a")) + "\n", encoding="utf-8")
            model = DashboardReadModel(data_dir)
            self.assertTrue(model.ready())
            self.assertEqual(len(model.list_events("run-a")), 3)

            # A writer that is killed mid-line leaves an unparseable tail.
            truncated_tail = '{"event_id": "run-a-evt-3", "run_id": "run-'
            log.write_text(log.read_text(encoding="utf-8") + truncated_tail, encoding="utf-8")
            self.assertFalse(DashboardReadModel(data_dir).ready())
            with self.assertRaises(ReadModelError):
                DashboardReadModel(data_dir).list_events("run-a")

    def test_the_transport_identity_is_recorded_for_every_failure(self) -> None:
        """Failures leave an inspectable record of the path and the peer behaviour."""
        model = RemoteDashboardReadModel(self.source.base_url, timeout_s=0.25)
        self.assertEqual(model.list_runs()[0]["run_id"], "run-a")

        self.source.mode = "partition"
        with self.assertRaises(ReadModelError):
            model.list_runs()
        self.assertEqual(self.source.requests[-1][:3], ("/api/v1/runs", "partition", "reset"))

        self.source.mode = "slow"
        self.source.slow_seconds = 1.0
        started = time.perf_counter()
        with self.assertRaises(ReadModelError):
            model.list_runs()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0)
        path, mode, _outcome, peer_elapsed = self.source.requests[-1]
        self.assertEqual(path, "/api/v1/runs")
        self.assertEqual(mode, "slow")
        self.assertLess(elapsed, self.source.slow_seconds)
        self.assertLess(peer_elapsed, self.source.slow_seconds)


if __name__ == "__main__":
    unittest.main()
