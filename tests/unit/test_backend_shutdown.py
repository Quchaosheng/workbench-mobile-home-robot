"""Issue #93: graceful shutdown and bounded backpressure for the backend.

The server must refuse new work while it drains, keep answering the life-cycle
probes, and never let a wedged handler keep the process alive.
"""

import json
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from workbench_backend.server import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    MAX_CONCURRENT_REQUESTS,
    MAX_DRAIN_TIMEOUT_SECONDS,
    create_server,
)


def read_json(url: str, timeout: float = 3.0) -> tuple[int, dict, dict]:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read()), dict(response.headers)
    except urllib.error.HTTPError as error:
        with error as response:
            return response.code, json.loads(response.read()), dict(response.headers)


class DrainStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server("127.0.0.1", 0, drain_timeout=0.5)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.begin_drain()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_readiness_flips_to_not_ready_before_the_socket_closes(self) -> None:
        status, payload, _ = read_json(f"{self.base_url}/readyz")
        self.assertEqual((status, payload["status"]), (200, "ready"))

        self.server.begin_drain()

        status, payload, _ = read_json(f"{self.base_url}/readyz")
        self.assertEqual((status, payload["status"]), (503, "not_ready"))

    def test_liveness_stays_up_while_the_process_is_only_draining(self) -> None:
        self.server.begin_drain()
        status, payload, _ = read_json(f"{self.base_url}/healthz")
        self.assertEqual((status, payload["status"]), (200, "ok"))

    def test_new_work_is_refused_with_a_retryable_503(self) -> None:
        self.server.begin_drain()
        for path in ("/api/v1/runs", "/api/v1/runs/run-confirmed/events", "/", "/api/v1/expression-states"):
            with self.subTest(path=path):
                status, payload, headers = read_json(f"{self.base_url}{path}")
                self.assertEqual(status, 503)
                self.assertEqual(payload["error"], "server_draining")
                self.assertEqual(headers.get("Retry-After"), "1")
                # The refusal must not leak the read model, paths or internals.
                self.assertNotIn("dashboard-fixtures", json.dumps(payload))

    def test_write_requests_are_refused_while_draining(self) -> None:
        self.server.begin_drain()
        request = urllib.request.Request(f"{self.base_url}/api/v1/runs", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            with error as response:
                self.assertEqual(response.code, 503)
                self.assertEqual(json.loads(response.read())["error"], "server_draining")
        else:
            self.fail("a write request was accepted while the server was draining")

    def test_drain_reports_idle_and_returns_immediately_when_nothing_is_in_flight(self) -> None:
        self.server.begin_drain()
        started = time.monotonic()
        self.assertTrue(self.server.wait_for_idle(2.0))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_shutdown_with_drain_stops_accepting_and_reports_drained(self) -> None:
        self.assertTrue(self.server.shutdown_with_drain(2.0))
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            read_json(f"{self.base_url}/healthz", timeout=2.0)


class DrainDeadlineTests(unittest.TestCase):
    def test_in_flight_request_completes_within_the_deadline(self) -> None:
        release = threading.Event()
        entered = threading.Event()

        class SlowReadModel:
            data_source = "slow-test-source"

            def ready(self) -> bool:
                return True

            def list_runs(self) -> list[dict]:
                entered.set()
                release.wait(timeout=5)
                return []

            def list_events(self, run_id: str) -> list[dict]:
                raise KeyError(run_id)

            def summarize(self, events, replay_index=None) -> dict:
                return {}

            def expression_contract(self) -> dict:
                return {"states": [], "transitions": {}}

        server = create_server("127.0.0.1", 0, drain_timeout=2.0)
        server.RequestHandlerClass.read_model = SlowReadModel()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        result: dict = {}

        def read_runs() -> None:
            try:
                with urllib.request.urlopen(f"{base_url}/api/v1/runs", timeout=5) as response:
                    result["status"] = response.status
            except urllib.error.HTTPError as error:
                result["status"] = error.code

        worker = threading.Thread(target=read_runs, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=3), "the request never reached the read model")
            self.assertEqual(server.active_request_count, 1)

            # Draining waits for work that already started instead of cancelling
            # or truncating it.
            drain = threading.Thread(target=lambda: result.update(drained=server.shutdown_with_drain(5.0)))
            drain.start()
            time.sleep(0.2)
            self.assertTrue(drain.is_alive(), "shutdown_with_drain did not wait for the in-flight request")
            # The probe must still answer while the drain is waiting, otherwise an
            # orchestrator cannot observe NOT_READY before the port closes.
            probe_status, probe, _ = read_json(f"{base_url}/readyz", timeout=3)
            self.assertEqual((probe_status, probe["status"]), (503, "not_ready"))

            release.set()
            worker.join(timeout=5)
            drain.join(timeout=5)

            self.assertEqual(result["status"], 200)
            self.assertTrue(result["drained"])
            self.assertEqual(server.active_request_count, 0)
            self.assertFalse(thread.is_alive(), "the accept loop did not stop after the drain")
        finally:
            release.set()
            server.begin_drain()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_a_wedged_handler_cannot_hold_the_process_open(self) -> None:
        release = threading.Event()
        entered = threading.Event()

        class WedgedReadModel:
            data_source = "wedged-test-source"

            def ready(self) -> bool:
                return True

            def list_runs(self) -> list[dict]:
                entered.set()
                # Ignore the release signal on purpose: this models a handler
                # that never returns, which must still not block shutdown.
                release.wait(timeout=30)
                return []

            def list_events(self, run_id: str) -> list[dict]:
                raise KeyError(run_id)

            def summarize(self, events, replay_index=None) -> dict:
                return {}

            def expression_contract(self) -> dict:
                return {"states": [], "transitions": {}}

        server = create_server("127.0.0.1", 0, drain_timeout=0.3)
        server.RequestHandlerClass.read_model = WedgedReadModel()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        worker = threading.Thread(
            target=lambda: urllib.request.urlopen(f"{base_url}/api/v1/runs", timeout=5),
            daemon=True,
        )
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=3), "the request never reached the read model")
            started = time.monotonic()
            drained = server.shutdown_with_drain()
            elapsed = time.monotonic() - started
            self.assertFalse(drained, "a wedged handler was reported as drained")
            self.assertLess(elapsed, 5.0, "the drain deadline was not enforced")
        finally:
            release.set()
            server.begin_drain()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class ConcurrencyBoundTests(unittest.TestCase):
    def test_overload_returns_a_stable_bounded_503(self) -> None:
        release = threading.Event()
        entered = threading.Event()
        condition = threading.Condition()
        active = 0

        class BlockingReadModel:
            data_source = "blocking-test-source"

            def ready(self) -> bool:
                return True

            def list_runs(self) -> list[dict]:
                nonlocal active
                with condition:
                    active += 1
                    if active == MAX_CONCURRENT_REQUESTS:
                        entered.set()
                release.wait(timeout=5)
                return []

            def list_events(self, run_id: str) -> list[dict]:
                raise KeyError(run_id)

            def summarize(self, events, replay_index=None) -> dict:
                return {}

            def expression_contract(self) -> dict:
                return {"states": [], "transitions": {}}

        server = create_server("127.0.0.1", 0, max_concurrent_requests=MAX_CONCURRENT_REQUESTS)
        server.RequestHandlerClass.read_model = BlockingReadModel()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        workers = [
            threading.Thread(
                target=lambda: read_json(f"{base_url}/api/v1/runs", timeout=5),
                daemon=True,
            )
            for _ in range(MAX_CONCURRENT_REQUESTS)
        ]
        try:
            for worker in workers:
                worker.start()
            self.assertTrue(entered.wait(timeout=5), "requests never reached the concurrency bound")

            status, payload, headers = read_json(f"{base_url}/api/v1/runs", timeout=3)
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"], "server_busy")
            self.assertEqual(headers.get("Retry-After"), "1")
            self.assertLessEqual(server.active_request_count, MAX_CONCURRENT_REQUESTS)
        finally:
            release.set()
            for worker in workers:
                worker.join(timeout=5)
            server.begin_drain()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class DrainConfigurationTests(unittest.TestCase):
    def test_drain_timeout_defaults_and_bounds_are_enforced(self) -> None:
        self.assertEqual(DEFAULT_DRAIN_TIMEOUT_SECONDS, 5.0)
        self.assertLess(DEFAULT_DRAIN_TIMEOUT_SECONDS, MAX_DRAIN_TIMEOUT_SECONDS)
        for drain_timeout in (-1.0, MAX_DRAIN_TIMEOUT_SECONDS + 0.001):
            with self.subTest(drain_timeout=drain_timeout), self.assertRaises(ValueError):
                create_server("127.0.0.1", 0, drain_timeout=drain_timeout)

    def test_created_server_exposes_its_drain_contract(self) -> None:
        server = create_server("127.0.0.1", 0, drain_timeout=1.5)
        try:
            self.assertEqual(server.drain_timeout, 1.5)
            self.assertFalse(server.draining)
            self.assertEqual(server.active_request_count, 0)
            server.begin_drain()
            self.assertTrue(server.draining)
        finally:
            server.server_close()


ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class SigtermShutdownTests(unittest.TestCase):
    """Drive the real CLI so signal handling and the exit status are covered."""

    def test_sigterm_drains_and_exits_zero(self) -> None:
        port = _free_port()
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "workbench_backend.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--drain-timeout",
                "2",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    status, payload, _ = read_json(f"{base_url}/readyz", timeout=1.0)
                except OSError:
                    time.sleep(0.1)
                    continue
                if status == 200:
                    break
                time.sleep(0.1)
            else:
                self.fail("the dashboard never became ready")

            self.assertEqual(payload["status"], "ready")

            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=20), 0, "SIGTERM must exit cleanly")

            output = process.stdout.read() if process.stdout else ""
            self.assertIn("service_draining", output)
            self.assertIn('"drained":true', output.replace(" ", ""))

            # The port must be released rather than left half-closed.
            with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
                read_json(f"{base_url}/healthz", timeout=1.0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    def test_drain_timeout_is_validated_by_the_cli(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "workbench_backend.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(_free_port()),
                "--drain-timeout",
                "31",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("drain_timeout", completed.stderr + completed.stdout)


if __name__ == "__main__":
    unittest.main()
