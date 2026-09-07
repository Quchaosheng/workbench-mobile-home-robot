import io
import json
import math
import os
import socket
import sys
import tempfile
import threading
import tomllib
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.client import RemoteDisconnected
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "backend"))

from workbench_backend.expression import ExpressionMachine, ExpressionState, derive_expression
from workbench_backend.logging import StructuredLogger
from workbench_backend.read_model import DashboardReadModel, ReadModelError
from workbench_backend.server import (
    MAX_CONCURRENT_REQUESTS,
    MAX_REJECTED_REQUEST_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    OPENAPI_RESOURCE,
    DashboardHandler,
    create_server,
)


def stored_event(run_id: object, sequence_no: object, event_type: object) -> dict:
    return {
        "event_id": f"event-{sequence_no}",
        "run_id": run_id,
        "sequence_no": sequence_no,
        "event_type": event_type,
        "occurred_at": "2026-08-06T00:00:00Z",
        "payload": {},
    }


class ExpressionTests(unittest.TestCase):
    def test_all_four_states_are_reachable_through_valid_transitions(self) -> None:
        machine = ExpressionMachine()
        self.assertEqual(machine.state, ExpressionState.IDLE)
        self.assertEqual(machine.transition(ExpressionState.THINKING), ExpressionState.THINKING)
        self.assertEqual(machine.transition(ExpressionState.UNCERTAIN), ExpressionState.UNCERTAIN)
        self.assertEqual(machine.transition(ExpressionState.THINKING), ExpressionState.THINKING)
        self.assertEqual(machine.transition(ExpressionState.PLEASED), ExpressionState.PLEASED)

    def test_expression_is_derived_from_verifier_status(self) -> None:
        events = [
            {"event_type": "task_accepted", "payload": {}},
            {"event_type": "verification", "payload": {"status": "insufficient_evidence"}},
        ]
        self.assertEqual(derive_expression([]), ExpressionState.IDLE)
        self.assertEqual(derive_expression(events[:1]), ExpressionState.THINKING)
        self.assertEqual(derive_expression(events), ExpressionState.UNCERTAIN)
        events[-1]["payload"]["status"] = "confirmed"
        self.assertEqual(derive_expression(events), ExpressionState.PLEASED)


class ReadModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = DashboardReadModel(ROOT / "apps" / "dashboard" / "data")

    def test_replay_is_ordered_and_uncertain_lists_missing_evidence(self) -> None:
        events = self.model.list_events("run-uncertain")
        self.assertEqual([event["sequence_no"] for event in events], list(range(len(events))))
        summary = self.model.summarize(events)
        self.assertEqual(summary["status"], "insufficient_evidence")
        self.assertEqual(summary["expression"], "uncertain")
        self.assertEqual(
            summary["missing_evidence"],
            [
                "fresh_well_lit_frames",
                "blue_cylinder_confidence_above_0.80",
                "green_gear_confidence_above_0.80",
            ],
        )

    def test_recovery_path_retains_refuted_attempt_and_finishes_confirmed(self) -> None:
        events = self.model.list_events("run-recovery")
        final = self.model.summarize(events)
        first_refuted_index = next(
            index
            for index, event in enumerate(events)
            if event["event_type"] == "verification" and event["payload"]["status"] == "refuted"
        )
        first_attempt = self.model.summarize(events, replay_index=first_refuted_index)
        self.assertEqual(first_attempt["status"], "refuted")
        self.assertEqual(first_attempt["expression"], "uncertain")
        self.assertEqual(final["status"], "confirmed")
        self.assertEqual(final["recovery_count"], 1)

    def test_dashboard_fixtures_cover_kitting_inspection_clearance_and_parcels(self) -> None:
        summaries = {summary["run_id"]: summary for summary in self.model.list_runs()}
        self.assertEqual(
            {summary["task_id"] for summary in summaries.values()},
            {
                "task-kit-three-parts",
                "task-inspect-workpieces",
                "task-clear-workspace",
                "task-sort-parcels",
            },
        )

        kit_events = self.model.list_events("run-confirmed")
        observed_entities = {
            event["payload"]["entity_id"] for event in kit_events if event["event_type"] == "observation"
        }
        self.assertEqual(observed_entities, {"red_block", "blue_cylinder", "green_gear"})
        final_verification = next(event for event in reversed(kit_events) if event["event_type"] == "verification")
        self.assertEqual(
            final_verification["payload"]["required_conditions"],
            final_verification["payload"]["satisfied_conditions"],
        )

        recovery_events = self.model.list_events("run-recovery")
        resulting_locations = {
            event["payload"].get("resulting_location")
            for event in recovery_events
            if event["event_type"] == "action_result"
        }
        self.assertTrue({"in:staging_bin", "in:tray"}.issubset(resulting_locations))

        parcel_events = self.model.list_events("dashboard-parcel--parcel-intake-003")
        parcel_observations = [event for event in parcel_events if event["event_type"] == "observation"]
        self.assertEqual(
            {event["payload"]["entity_id"] for event in parcel_observations},
            {"parcel_box", "parcel_unreadable", "parcel_damaged"},
        )
        self.assertTrue(all(event["payload"].get("attributes") for event in parcel_observations))
        parcel_locations = {
            event["payload"].get("resulting_location")
            for event in parcel_events
            if event["event_type"] == "action_result"
        }
        self.assertTrue({"in:pickup_shelf", "in:quarantine_bin"}.issubset(parcel_locations))

    def test_dashboard_map_uses_event_driven_multi_entity_layer(self) -> None:
        dashboard = ROOT / "apps" / "dashboard"
        markup = (dashboard / "index.html").read_text(encoding="utf-8")
        script = (dashboard / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="map-entities"', markup)
        self.assertNotIn('id="map-block"', markup)
        self.assertIn("function buildWorkbenchState", script)
        self.assertIn("function applyEntityPositions", script)
        self.assertIn('data-left="${position.left}"', script)
        self.assertNotIn('style="left:${position.left}', script)
        self.assertIn('payload.status === "succeeded" && payload.resulting_location', script)
        self.assertIn('"task-sort-parcels"', script)
        self.assertIn("pickup_shelf", script)
        self.assertIn("quarantine_bin", script)
        self.assertIn("renderParcelDecisions", script)
        self.assertIn("destination_capacities", script)
        self.assertIn("route-priority", script)
        self.assertIn("reverse().find", script)
        self.assertIn("configuredPriorities", script)
        self.assertIn("manifest_statuses", script)
        self.assertIn("清单已匹配", script)
        self.assertIn("function parcelIdentityLabel", script)
        self.assertIn('id="parcel-decisions"', markup)
        self.assertIn("map-entity-envelope", script + (dashboard / "styles.css").read_text(encoding="utf-8"))

    def test_event_cache_reuses_parse_and_invalidates_on_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run-cache.jsonl"
            path.write_text(
                json.dumps(stored_event("run-cache", 0, "task_accepted")) + "\n",
                encoding="utf-8",
            )
            model = DashboardReadModel(temp_dir)
            first = model.list_events("run-cache")
            second = model.list_events("run-cache")
            self.assertIs(first, second)
            path.write_text(
                "\n".join(
                    [
                        json.dumps(stored_event("run-cache", 1, "task_terminal")),
                        json.dumps(stored_event("run-cache", 0, "task_accepted")),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ReadModelError, "contiguous"):
                model.list_events("run-cache")

            path.write_text(
                "\n".join(
                    [
                        json.dumps(stored_event("run-cache", 0, "task_accepted")),
                        json.dumps(stored_event("run-cache", 1, "task_terminal")),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            refreshed = model.list_events("run-cache")
            self.assertIsNot(first, refreshed)
            self.assertEqual([event["sequence_no"] for event in refreshed], [0, 1])

    def test_malformed_logs_and_duplicate_run_ids_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one.jsonl").write_text(
                json.dumps(stored_event("duplicate", 0, "task_accepted")) + "\n",
                encoding="utf-8",
            )
            (root / "two.jsonl").write_text(
                json.dumps(stored_event("duplicate", 0, "task_terminal")) + "\n",
                encoding="utf-8",
            )
            model = DashboardReadModel(root)
            with self.assertRaisesRegex(ReadModelError, "duplicate run_id"):
                model.list_runs()

            (root / "two.jsonl").write_text("{not-json}\n", encoding="utf-8")
            with self.assertRaisesRegex(ReadModelError, "valid JSONL"):
                model.list_runs()
            self.assertFalse(model.ready())

    def test_duplicate_json_keys_fail_closed_at_every_object_depth(self) -> None:
        event = stored_event("run-duplicate", 0, "task_accepted")
        event["payload"] = {"status": "confirmed"}
        encoded = json.dumps(event)
        cases = {
            "run_id": encoded.replace(
                '"run_id": "run-duplicate"',
                '"run_id": "forged", "run_id": "run-duplicate"',
            ),
            "status": encoded.replace(
                '"status": "confirmed"',
                '"status": "forged", "status": "confirmed"',
            ),
        }
        for duplicate_key, contents in cases.items():
            with self.subTest(duplicate_key=duplicate_key), tempfile.TemporaryDirectory() as temp_dir:
                Path(temp_dir, "duplicate.jsonl").write_text(contents + "\n", encoding="utf-8")
                model = DashboardReadModel(temp_dir)
                with self.assertRaisesRegex(ReadModelError, rf"duplicate JSON key: '{duplicate_key}'"):
                    model.list_runs()
                self.assertFalse(model.ready())

    def test_non_scalar_identifiers_are_rejected_as_data_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad-types.jsonl"
            path.write_text(
                json.dumps(stored_event(["not", "hashable"], 0, ["bad"])) + "\n",
                encoding="utf-8",
            )
            model = DashboardReadModel(temp_dir)
            with self.assertRaises(ReadModelError):
                model.list_runs()
            self.assertFalse(model.ready())

    def test_unknown_verification_status_is_displayed_without_crashing(self) -> None:
        events = [
            {"run_id": "future", "sequence_no": 0, "event_type": "task_accepted", "payload": {}},
            {
                "run_id": "future",
                "sequence_no": 1,
                "event_type": "verification",
                "payload": {"status": "future_status", "missing_evidence": "not-a-list"},
            },
        ]
        summary = self.model.summarize(events)
        self.assertEqual(summary["status"], "future_status")
        self.assertEqual(summary["status_label"], "未知状态")
        self.assertEqual(summary["missing_evidence"], [])


class LoggingTests(unittest.TestCase):
    def test_json_log_contains_run_id_and_monotonic_sequence(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger("test-service", stream)
        first = logger.emit("started", "one", run_id="run-1")
        second = logger.emit("finished", "two", run_id="run-1", source="hardware")
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([first["sequence_no"], second["sequence_no"]], [0, 1])
        self.assertEqual(records[1]["source"], "hardware")
        self.assertEqual(records[1]["run_id"], "run-1")


class DashboardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def read_json(self, path: str) -> tuple[int, dict]:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_health_ready_runs_and_replay_endpoints(self) -> None:
        health_status, health = self.read_json("/healthz")
        ready_status, ready = self.read_json("/readyz")
        runs_status, runs = self.read_json("/api/runs")
        events_status, replay = self.read_json("/api/runs/run-recovery/events")
        self.assertEqual((health_status, health["status"]), (200, "ok"))
        self.assertEqual((ready_status, ready["status"]), (200, "ready"))
        self.assertEqual(runs_status, 200)
        self.assertTrue(runs["read_only"])
        self.assertEqual(events_status, 200)
        self.assertEqual(replay["events"][0]["sequence_no"], 0)

    def test_versioned_contract_and_identifier_limits(self) -> None:
        status, contract = self.read_json("/api/v1/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(contract["info"]["version"], "1.0.0")
        self.assertTrue(all(set(operations) == {"get"} for operations in contract["paths"].values()))
        self.assertIn("RunEvents", contract["components"]["schemas"])
        request = urllib.request.Request(f"{self.base_url}/api/v1/runs/bad%2Fid")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{self.base_url}/api/v1/runs/missing", timeout=2)
        self.assertEqual(missing.exception.code, 404)
        with urllib.request.urlopen(f"{self.base_url}/api/v1/runs", timeout=2) as response:
            self.assertEqual(response.headers["X-API-Version"], "1")

    def test_openapi_contract_is_packaged_and_matches_checked_in_docs(self) -> None:
        packaged = json.loads(OPENAPI_RESOURCE.read_text(encoding="utf-8"))
        checked_in = json.loads((ROOT / "docs" / "api-openapi-v1.json").read_text(encoding="utf-8"))
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(packaged, checked_in)
        self.assertIn("api-openapi-v1.json", project["tool"]["setuptools"]["package-data"]["workbench_backend"])

    def test_oversized_api_response_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "large.jsonl").write_text(
                json.dumps(
                    {
                        **stored_event("large", 0, "task_accepted"),
                        "payload": {"goal": "x" * (4 * 1024 * 1024)},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            server = create_server("127.0.0.1", 0, data_dir=temp_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/v1/runs/large/events"
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url, timeout=2)
                self.assertEqual(caught.exception.code, 413)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_write_requests_fail_closed(self) -> None:
        request = urllib.request.Request(f"{self.base_url}/api/runs/run-confirmed", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        with caught.exception as response:
            payload = json.loads(response.read())
            self.assertEqual(response.code, 405)
        self.assertEqual(payload["error"], "read_only")

    def test_static_assets_support_conditional_and_immutable_caching(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/", timeout=2) as response:
            etag = response.headers["ETag"]
            self.assertEqual(response.headers["Cache-Control"], "no-cache")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("camera=()", response.headers["Permissions-Policy"])
        conditional = urllib.request.Request(f"{self.base_url}/", headers={"If-None-Match": etag})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(conditional, timeout=2)
        with caught.exception as response:
            self.assertEqual(response.code, 304)
        with urllib.request.urlopen(f"{self.base_url}/vendor/lucide.min.js", timeout=2) as response:
            self.assertEqual(response.headers["Cache-Control"], "public, max-age=31536000, immutable")

    def test_malformed_event_source_returns_503_while_health_stays_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "broken.jsonl").write_text("{broken}\n", encoding="utf-8")
            server = create_server("127.0.0.1", 0, data_dir=temp_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{base_url}/healthz", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                for endpoint in ("/readyz", "/api/runs"):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(f"{base_url}{endpoint}", timeout=2)
                    with caught.exception as response:
                        self.assertEqual(response.code, 503)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class DashboardInboundBoundaryTests(unittest.TestCase):
    def start_server(self, **options):
        server = create_server("127.0.0.1", 0, **options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(stop)
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def raw_request(port: int, request: bytes) -> tuple[int, bytes]:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            connection.settimeout(3)
            try:
                connection.sendall(request)
            except BrokenPipeError:
                pass
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            chunks: list[bytes] = []
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        response = b"".join(chunks)
        status = int(response.split(b" ", 2)[1])
        return status, response

    def test_reverse_proxy_mode_enforces_socket_peer_allowlist(self) -> None:
        _, allowed_url = self.start_server(
            published_host="127.0.0.1",
            trust_mode="reverse_proxy",
            trusted_proxy_allowlist="127.0.0.1/32",
        )
        with urllib.request.urlopen(f"{allowed_url}/api/v1/runs", timeout=2) as response:
            self.assertEqual(response.status, 200)

        _, denied_url = self.start_server(
            published_host="127.0.0.1",
            trust_mode="reverse_proxy",
            trusted_proxy_allowlist="10.20.30.0/24",
        )
        for path in ("/", "/healthz", "/readyz", "/api/v1/openapi.json", "/api/v1/runs"):
            spoofed = urllib.request.Request(
                f"{denied_url}{path}",
                headers={
                    "Forwarded": "for=10.20.30.40",
                    "X-Forwarded-For": "10.20.30.40",
                },
            )
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(spoofed, timeout=2)
            with caught.exception as response:
                self.assertEqual(response.code, 403)
                self.assertEqual(json.loads(response.read())["error"], "untrusted_client")

    def test_operational_routes_are_separate_from_data_routes(self) -> None:
        _, base_url = self.start_server()
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=2) as response:
            health = json.loads(response.read())
        with urllib.request.urlopen(f"{base_url}/readyz", timeout=2) as response:
            readiness = json.loads(response.read())
        with urllib.request.urlopen(f"{base_url}/api/v1/runs", timeout=2) as response:
            runs = json.loads(response.read())
        self.assertEqual(set(health), {"status", "service", "version"})
        self.assertEqual(set(readiness), {"status", "data_source"})
        self.assertEqual(set(runs), {"runs", "read_only"})

    def test_request_body_boundaries_fail_closed(self) -> None:
        server, _ = self.start_server()
        port = server.server_address[1]

        exact_body = b"x" * MAX_REJECTED_REQUEST_BODY_BYTES
        exact_request = (
            f"POST /api/v1/runs HTTP/1.0\r\nHost: localhost\r\nContent-Length: {len(exact_body)}\r\n\r\n"
        ).encode() + exact_body
        self.assertEqual(self.raw_request(port, exact_request)[0], 405)

        oversized_request = (
            f"POST /api/v1/runs HTTP/1.0\r\nHost: localhost\r\n"
            f"Content-Length: {MAX_REJECTED_REQUEST_BODY_BYTES + 1}\r\n\r\n"
        ).encode()
        oversized_status, oversized_response = self.raw_request(port, oversized_request)
        self.assertEqual(oversized_status, 413)
        self.assertEqual(json.loads(oversized_response.split(b"\r\n\r\n", 1)[1])["error"], "request_too_large")

        extremely_large_length = b"9" * 5000
        extremely_large_request = (
            b"POST /api/v1/runs HTTP/1.0\r\nHost: localhost\r\nContent-Length: " + extremely_large_length + b"\r\n\r\n"
        )
        self.assertEqual(self.raw_request(port, extremely_large_request)[0], 413)

        malformed_requests = (
            b"GET /api/v1/runs HTTP/1.0\r\nHost: localhost\r\nContent-Length: 1\r\n\r\nx",
            b"GET /api/v1/runs HTTP/1.0\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            b"GET /api/v1/runs HTTP/1.0\r\nHost: localhost\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
            b"GET /api/v1/runs HTTP/1.0\r\nHost: localhost\r\nContent-Length: invalid\r\n\r\n",
        )
        for request in malformed_requests:
            with self.subTest(request=request.split(b"\r\n", 1)[1].split(b"\r\n\r\n", 1)[0]):
                self.assertEqual(self.raw_request(port, request)[0], 400)

    def test_concurrent_requests_are_bounded(self) -> None:
        condition = threading.Condition()
        release = threading.Event()
        entered = 0

        class BlockingReadModel:
            def list_runs(self) -> list[dict]:
                nonlocal entered
                with condition:
                    entered += 1
                    condition.notify_all()
                if not release.wait(timeout=5):
                    raise AssertionError("test did not release blocked requests")
                return []

        server, base_url = self.start_server(max_concurrent_requests=MAX_CONCURRENT_REQUESTS)
        server.RequestHandlerClass.read_model = BlockingReadModel()

        def read_runs() -> int:
            with urllib.request.urlopen(f"{base_url}/api/v1/runs", timeout=5) as response:
                response.read()
                return response.status

        executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS)
        futures = [executor.submit(read_runs) for _ in range(MAX_CONCURRENT_REQUESTS)]
        try:
            with condition:
                self.assertTrue(
                    condition.wait_for(lambda: entered == MAX_CONCURRENT_REQUESTS, timeout=3),
                    f"only {entered} requests reached the handler",
                )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"{base_url}/api/v1/runs", timeout=2)
            with caught.exception as response:
                self.assertEqual(response.code, 503)
                self.assertEqual(json.loads(response.read())["error"], "server_busy")
        finally:
            release.set()
            self.assertEqual([future.result(timeout=5) for future in futures], [200] * MAX_CONCURRENT_REQUESTS)
            executor.shutdown(wait=True)

    def test_response_size_exact_boundary(self) -> None:
        def response_for_size(size: int) -> tuple[int, bytes]:
            server, base_url = self.start_server()
            template = {"runs": [{"padding": ""}], "read_only": True}
            overhead = len(json.dumps(template, ensure_ascii=False).encode())
            padding = size - overhead

            class SizedReadModel:
                def list_runs(self) -> list[dict]:
                    return [{"padding": "x" * padding}]

            server.RequestHandlerClass.read_model = SizedReadModel()
            try:
                with urllib.request.urlopen(f"{base_url}/api/v1/runs", timeout=3) as response:
                    body = response.read()
                    return response.status, body
            except urllib.error.HTTPError as response:
                return response.code, response.read()

        exact_status, exact_body = response_for_size(MAX_RESPONSE_BYTES)
        self.assertEqual(exact_status, 200)
        self.assertEqual(len(exact_body), MAX_RESPONSE_BYTES)

        oversized_status, oversized_body = response_for_size(MAX_RESPONSE_BYTES + 1)
        self.assertEqual(oversized_status, 413)
        self.assertLessEqual(len(oversized_body), MAX_RESPONSE_BYTES)
        self.assertEqual(json.loads(oversized_body)["error"], "response_too_large")


NONFINITE_JSON_TOKENS = ("NaN", "Infinity", "-Infinity", "1e400", "-1e400")
FINITE_JSON_CONTROLS = [0, -0.0, 1e308, 1e-300, 7, -7, True, False, None, "摄像头", "NaN", "Infinity"]


def strict_response_json(body: bytes):
    """Independent wire oracle: constants AND exponent overflow must fail."""

    def reject_constant(value):
        raise ValueError("non-JSON constant")

    decoded = json.loads(body, parse_constant=reject_constant)
    pending = [decoded]
    while pending:
        value = pending.pop()
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite decoded number")
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return decoded


def numeric_event_line(token: str, placement: str = "nested", sequence: int = 0) -> str:
    event = stored_event("run-numeric", sequence, "observation")
    if placement == "top":
        event["unused_number"] = "__NUMBER__"
    elif placement == "nested":
        event["payload"] = {"pose": {"position": {"x": "__NUMBER__"}}}
    elif placement == "array":
        event["payload"] = {"samples": [0, {"confidence": "__NUMBER__"}]}
    else:
        event["unused_metadata"] = {"samples": ["__NUMBER__"]}
    return json.dumps(event).replace('"__NUMBER__"', token)


def replace_numeric_log(path: Path, contents: str) -> None:
    # Deliberately advance the existing cache identity; never rely on a sleep.
    previous_mtime = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(contents + "\n", encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous_mtime + 1_000_000)))


class BackendStrictJsonReadModelTests(unittest.TestCase):
    def test_finite_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event = stored_event("run-finite", 0, "observation")
            event["payload"] = {"nested": {"values": FINITE_JSON_CONTROLS}}
            path = Path(directory, "finite.jsonl")
            path.write_text(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
            model = DashboardReadModel(directory)
            self.assertTrue(model.ready())
            values = model.list_events("run-finite")[0]["payload"]["nested"]["values"]
            self.assertEqual(values, FINITE_JSON_CONTROLS)
            self.assertEqual([type(value) for value in values], [type(value) for value in FINITE_JSON_CONTROLS])
            self.assertEqual(math.copysign(1, values[1]), -1)
            self.assertEqual(model.list_runs()[0]["run_id"], "run-finite")

    def test_local_nonfinite_matrix(self) -> None:
        for token in NONFINITE_JSON_TOKENS:
            for placement in ("top", "nested", "array", "unused"):
                for sequence in (0, 1):
                    with (
                        self.subTest(token=token, placement=placement, sequence=sequence),
                        tempfile.TemporaryDirectory() as directory,
                    ):
                        path = Path(directory, "numeric.jsonl")
                        prefix = numeric_event_line("0") + "\n" if sequence else ""
                        path.write_text(prefix + numeric_event_line(token, placement, sequence) + "\n")
                        model = DashboardReadModel(directory)
                        with self.assertRaises(ReadModelError):
                            model.list_events("run-numeric")
                        with self.assertRaises(ReadModelError):
                            model.list_runs()
                        self.assertFalse(model.ready())
                        self.assertNotIn(path, model._event_cache)

    def test_cache_recovers_after_nonfinite_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "numeric.jsonl")
            model = DashboardReadModel(directory)
            replace_numeric_log(path, numeric_event_line("0"))
            for token in NONFINITE_JSON_TOKENS:
                with self.subTest(token=token):
                    first = model.list_events("run-numeric")
                    cache_entry = model._event_cache[path]
                    self.assertIs(first, model.list_events("run-numeric"))
                    replace_numeric_log(path, numeric_event_line(token))
                    for read in (model.list_runs, lambda: model.list_events("run-numeric")):
                        with self.assertRaises(ReadModelError):
                            read()
                    self.assertFalse(model.ready())
                    self.assertIs(model._event_cache[path], cache_entry)
                    replace_numeric_log(path, numeric_event_line("1e308"))
                    refreshed = model.list_events("run-numeric")
                    self.assertIsNot(first, refreshed)
                    self.assertEqual(refreshed[0]["payload"]["pose"]["position"]["x"], 1e308)
                    self.assertTrue(model.ready())
                    self.assertIs(refreshed, model.list_events("run-numeric"))


class BackendStrictJsonResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory, "numeric.jsonl")
        replace_numeric_log(self.path, numeric_event_line("0"))
        self.server = create_server("127.0.0.1", 0, data_dir=self.directory)
        self.logs = io.StringIO()
        self.server.RequestHandlerClass.logger = StructuredLogger("strict-json-test", self.logs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request_json(self, route: str):
        try:
            response = urllib.request.urlopen(self.base_url + route, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        except RemoteDisconnected:
            self.fail("serialization failure disconnected instead of returning bounded JSON")
        with response:
            body = response.read()
            self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
            self.assertEqual(int(response.headers["Content-Length"]), len(body))
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            try:
                payload = strict_response_json(body)
            except ValueError:
                self.fail("HTTP body is not strict finite JSON")
            return response.status, response.headers, payload, body

    def test_local_http_fails_closed(self) -> None:
        # A good run sorting first must not turn a bad second run into partial success.
        Path(self.directory, "a-good.jsonl").write_text(json.dumps(stored_event("good", 0, "task_accepted")))
        for token in NONFINITE_JSON_TOKENS:
            replace_numeric_log(self.path, numeric_event_line(token, "unused"))
            for route, expected_status, key, value in (
                ("/healthz", 200, "status", "ok"),
                ("/readyz", 503, "status", "not_ready"),
                *(
                    (f"{prefix}/runs{suffix}", 503, "error", "invalid_event_source")
                    for prefix in ("/api", "/api/v1")
                    for suffix in ("", "/run-numeric", "/run-numeric/events")
                ),
            ):
                with self.subTest(token=token, route=route):
                    status, _, payload, _ = self.request_json(route)
                    self.assertEqual(status, expected_status)
                    self.assertEqual(payload[key], value)
                    self.assertNotIn("runs", payload)

    def test_serialization_failure_before_headers(self) -> None:
        circular = []
        circular.append(circular)
        cases = {
            "nan": float("nan"),
            "positive_inf": float("inf"),
            "negative_inf": -float("inf"),
            "type_error": {"secret-value"},
            "value_error": circular,
            "unicode_error": "secret-value\ud800",
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                handler = object.__new__(DashboardHandler)
                handler.wfile = io.BytesIO()
                emitted = []
                handler.send_response = lambda status, emitted=emitted: emitted.append(("status", status))
                handler.send_header = lambda key, value, emitted=emitted: emitted.append((key, value))
                handler.end_headers = lambda emitted=emitted: emitted.append(("end", None))
                try:
                    handler._send_json({"secret-field": [{"value": value}]}, api_version="1")
                except (TypeError, ValueError, UnicodeError):
                    self.fail("encoder exception escaped instead of generating a bounded 503")
                self.assertEqual(emitted[0], ("status", 503))
                self.assertEqual([item for item in emitted if item[0] == "status"], [("status", 503)])
                self.assertIn(("X-API-Version", "1"), emitted)
                body = handler.wfile.getvalue()
                self.assertEqual(strict_response_json(body)["error"], "response_serialization_failed")
                self.assertLess(len(body), 512)
                self.assertIn(("Content-Length", str(len(body))), emitted)
                self.assertNotIn(b"secret-", body)

        # Even if the encoder always fails, the fallback must not call it again.
        with mock.patch("workbench_backend.server.json.dumps", side_effect=ValueError("secret-field")) as encode:
            emitted.clear()
            handler.wfile = io.BytesIO()
            try:
                handler._send_json({"valid": "payload"}, api_version="1")
            except ValueError:
                self.fail("encoder failure escaped or fallback retried the broken encoder")
        self.assertEqual(encode.call_count, 1)
        self.assertEqual(emitted[0], ("status", 503))
        self.assertEqual(strict_response_json(handler.wfile.getvalue())["error"], "response_serialization_failed")

    def test_error_redaction_and_headers(self) -> None:
        sentinel = "private-source-value"
        field = "attacker-controlled-field"
        for token in NONFINITE_JSON_TOKENS:
            with self.subTest(source_token=token):
                event = stored_event("run-numeric", 0, "observation")
                event["payload"] = {field: [sentinel, "__NUMBER__"]}
                replace_numeric_log(self.path, json.dumps(event).replace('"__NUMBER__"', token))
                model = self.server.RequestHandlerClass.read_model
                with self.assertRaises(ReadModelError) as caught:
                    model.list_runs()
                status, headers, payload, body = self.request_json("/api/v1/runs")
                self.assertEqual(status, 503)
                self.assertEqual(payload["error"], "invalid_event_source")
                self.assert_security_headers(headers)
                for secret in (sentinel, field):
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertNotIn(secret.encode(), body)
                    self.assertNotIn(secret, self.logs.getvalue())

        for value in (float("nan"), float("inf"), -float("inf"), {sentinel}, sentinel + "\ud800"):
            for prefix in ("/api", "/api/v1"):
                with self.subTest(value_type=type(value).__name__, prefix=prefix):
                    self.server.RequestHandlerClass.read_model = mock.Mock(
                        list_runs=mock.Mock(return_value=[{field: [sentinel, value]}])
                    )
                    status, headers, payload, body = self.request_json(prefix + "/runs")
                    self.assertEqual(status, 503)
                    self.assertEqual(payload["error"], "response_serialization_failed")
                    self.assert_security_headers(headers)
                    self.assertEqual(headers.get("X-API-Version"), "1" if prefix == "/api/v1" else None)
                    self.assertLess(len(body), 512)
                    for secret in (sentinel, field):
                        self.assertNotIn(secret.encode(), body)
                        self.assertNotIn(secret, self.logs.getvalue())

    def assert_security_headers(self, headers) -> None:
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["Permissions-Policy"], "camera=(), microphone=(), geolocation=()")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_strict_json_oracle(self) -> None:
        for token in NONFINITE_JSON_TOKENS:
            with self.subTest(token=token), self.assertRaises(ValueError):
                strict_response_json(('{"nested": [0, {"value": ' + token + "}]}").encode())
        event = stored_event("run-numeric", 0, "observation")
        event["payload"] = {"nested": FINITE_JSON_CONTROLS}
        replace_numeric_log(self.path, json.dumps(event, ensure_ascii=False, allow_nan=False))
        status, headers, payload, body = self.request_json("/api/v1/runs/run-numeric/events")
        self.assertEqual(status, 200)
        values = payload["events"][0]["payload"]["nested"]
        self.assertEqual(values, FINITE_JSON_CONTROLS)
        self.assertEqual(math.copysign(1, values[1]), -1)
        self.assertIn("摄像头".encode(), body)
        self.assertEqual(headers["X-API-Version"], "1")
        self.assert_security_headers(headers)

    def test_strict_encoding_covers_normal_error_and_overload(self) -> None:
        original_dumps = json.dumps
        with mock.patch("workbench_backend.server.json.dumps", wraps=original_dumps) as encode:
            self.assertEqual(self.request_json("/healthz")[0], 200)
            self.assertEqual(self.request_json("/api/v1/runs/missing")[0], 404)
            # Reserve every slot only for the overload probe. Acquiring them also
            # waits for the preceding handlers to finish releasing their slots.
            with ExitStack() as slots:
                for _ in range(MAX_CONCURRENT_REQUESTS):
                    self.assertTrue(self.server._request_slots.acquire(timeout=3))
                    slots.callback(self.server._request_slots.release)
                status, _, payload, _ = self.request_json("/api/v1/runs")
                self.assertEqual((status, payload["error"]), (503, "server_busy"))
        wire_calls = [call for call in encode.call_args_list if "timestamp" not in call.args[0]]
        self.assertEqual(len(wire_calls), 3)
        for call in wire_calls:
            self.assertIs(call.kwargs.get("allow_nan"), False)


if __name__ == "__main__":
    unittest.main()
