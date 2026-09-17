import io
import json
import re
import socket
import sys
import tempfile
import threading
import tomllib
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
        # Issue #179: an action result is execution evidence. It must never be
        # the branch that assigns an entity location on the map.
        self.assertIn("function renderExecutionClaims", script)
        self.assertNotIn('payload.status === "succeeded"', script)
        self.assertNotIn("entities.set(entityId, { ...previous, location: payload.resulting_location })", script)
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


class NonFiniteJsonBoundaryTests(unittest.TestCase):
    """Issue #183: a non-finite value must not survive the read-model boundary.

    Python's decoder accepts bare NaN/Infinity and its encoder emits them again,
    so the value survives a full round trip and only fails in a strict client -
    after `/readyz` already claimed the source was usable.
    """

    @staticmethod
    def _strict_loads(text: str) -> object:
        return json.loads(
            text,
            parse_constant=lambda name: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {name}")),
        )

    def _model(self, payload_value: object, *, nested: bool = False) -> tuple[DashboardReadModel, str]:
        directory = tempfile.mkdtemp()
        event = stored_event("run-nonfinite", 0, "observation")
        event["payload"] = {"entity_id": "alpha", "entity_type": "block", "confidence": 0.5}
        if nested:
            event["payload"]["nested"] = {"deep": payload_value}
        else:
            event["payload"]["metric"] = payload_value
        # json.dumps writes the bare token, which is what a broken producer emits.
        path = Path(directory) / "run.jsonl"
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        return DashboardReadModel(directory), directory

    def test_all_three_non_finite_constants_are_rejected(self) -> None:
        for name, value in (("NaN", float("nan")), ("Infinity", float("inf")), ("-Infinity", float("-inf"))):
            with self.subTest(constant=name):
                model, _ = self._model(value)
                with self.assertRaisesRegex(ReadModelError, rf"non-standard JSON constant {re.escape(name)}"):
                    model.list_runs()
                self.assertFalse(model.ready())

    def test_nested_non_finite_value_is_rejected(self) -> None:
        model, _ = self._model(float("nan"), nested=True)
        with self.assertRaisesRegex(ReadModelError, "non-standard JSON constant"):
            model.list_runs()
        self.assertFalse(model.ready())

    def test_non_finite_source_is_not_cached_as_valid(self) -> None:
        model, directory = self._model(float("nan"))
        with self.assertRaises(ReadModelError):
            model.list_runs()

        # A corrected file must be readable, and the failed parse must not have
        # left a poisoned cache entry behind.
        event = stored_event("run-nonfinite", 0, "observation")
        event["payload"] = {"entity_id": "alpha", "entity_type": "block", "confidence": 0.5}
        Path(directory, "run.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

        runs = model.list_runs()
        self.assertEqual([run["run_id"] for run in runs], ["run-nonfinite"])
        self.assertTrue(model.ready())

    def test_finite_control_still_loads_and_serves_strict_json(self) -> None:
        model, _ = self._model(1.5)

        events = model.list_events("run-nonfinite")
        body = json.dumps(events, ensure_ascii=False, allow_nan=False)

        self.assertEqual(self._strict_loads(body)[0]["payload"]["metric"], 1.5)
        self.assertTrue(model.ready())

    def test_non_finite_source_returns_503_while_health_stays_live(self) -> None:
        directory = tempfile.mkdtemp()
        event = stored_event("run-nonfinite", 0, "observation")
        event["payload"] = {"entity_id": "alpha", "entity_type": "block", "confidence": float("nan")}
        Path(directory, "run.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

        server = create_server("127.0.0.1", 0, data_dir=directory)
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

    def test_unencodable_payload_is_reported_without_echoing_it(self) -> None:
        """A read model that cannot be encoded must not produce a truncated 200."""
        secret = "do-not-leak-this-value"

        class NonFiniteReadModel:
            data_source = "injected"

            def ready(self) -> bool:
                return True

            def list_runs(self) -> list[dict]:
                return [{"run_id": "r1", "metric": float("nan"), "note": secret}]

        server = create_server("127.0.0.1", 0)
        server.RequestHandlerClass.read_model = NonFiniteReadModel()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"{base_url}/api/runs", timeout=2)
            with caught.exception as response:
                self.assertEqual(response.code, 503)
                body = response.read().decode("utf-8")

            self.assertNotIn(secret, body)
            payload = self._strict_loads(body)
            self.assertEqual(payload["error"], "response_not_encodable")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


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


if __name__ == "__main__":
    unittest.main()
