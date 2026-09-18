"""Issue #170: the backend read-only health history and active-alert API.

The backend consumes health documents produced by the monitoring layer, so these
tests build real snapshots with a fixed monotonic clock and then check that the
backend re-validates them instead of trusting the serialized status.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "application"))
sys.path.insert(0, str(ROOT / "services" / "backend"))

from workbench.application.alerts import AlertPolicy
from workbench.application.monitoring import HealthSnapshotCollector
from workbench_backend.health import (
    HEALTH_HISTORY_VERSION,
    MAX_SNAPSHOTS,
    HealthHistoryError,
    HealthStore,
    load_health_documents,
    snapshot_from_document,
)
from workbench_backend.server import create_server

BASE = 1000.0


def collector(at: float = BASE, *, complete: bool = True) -> HealthSnapshotCollector:
    snapshot_collector = HealthSnapshotCollector()
    if complete:
        for spec in snapshot_collector.metrics.registry.specs():
            value = next(
                (
                    allowed
                    for allowed in spec.allowed_values
                    if allowed not in spec.fault_values and allowed not in spec.degraded_values
                ),
                None,
            )
            if value is None:
                if spec.value_type == "bool":
                    value = True
                elif spec.value_type == "str":
                    value = "none"
                elif spec.value_type == "int":
                    value = 5
                else:
                    value = 5.0
            snapshot_collector.record(spec.name, value, source=spec.source, observed_at=at)
    return snapshot_collector


def snapshot(*, at: float = BASE, complete: bool = True, faults: dict[str, object] | None = None):
    observed = at
    built = collector(at - 0.01, complete=complete)
    for name, value in (faults or {}).items():
        built.record(name, value, source=built.metrics.registry.get(name).source, observed_at=observed)
    return built.snapshot(collected_at=at)


def write_health_document(path: Path, snapshots) -> None:
    path.write_text("".join(json.dumps(snapshot.as_dict()) + "\n" for snapshot in snapshots), encoding="utf-8")


class HealthDocumentTests(unittest.TestCase):
    def test_a_healthy_document_rebuilds_to_the_collector_status(self) -> None:
        original = snapshot()
        rebuilt = snapshot_from_document(original.as_dict())
        self.assertEqual(rebuilt.overall, original.overall)
        self.assertEqual([domain for domain, _ in rebuilt.domains], [domain for domain, _ in original.domains])

    def test_a_document_cannot_claim_healthy_while_carrying_a_fault(self) -> None:
        document = snapshot(faults={"safety.estop_channels_ok": False}).as_dict()
        document["overall"] = "healthy"
        document["domains"]["safety"]["status"] = "healthy"
        rebuilt = snapshot_from_document(document)
        self.assertEqual(rebuilt.overall.value, "fault")
        self.assertEqual(rebuilt.domain("safety").status.value, "fault")

    def test_a_truncated_document_that_drops_a_registered_metric_is_refused(self) -> None:
        # A collector encodes a missing critical input as missing=true, never as
        # an absent metric. A document that removes the faulted metric has been
        # truncated, and computing from it could turn a dropped fault healthy.
        document = snapshot(faults={"can.link_ok": False}).as_dict()
        document["domains"]["communication"]["metrics"] = [
            metric for metric in document["domains"]["communication"]["metrics"] if metric["name"] != "can.link_ok"
        ]
        with self.assertRaisesRegex(HealthHistoryError, "missing registered metrics"):
            snapshot_from_document(document)

    def test_a_missing_critical_metric_is_unknown_never_healthy(self) -> None:
        # Encode the faulted critical metric as missing instead of faulted: the
        # per-metric, domain and overall statuses must all defect to unknown
        # rather than follow the document's optimistic claim.
        document = snapshot(faults={"safety.estop_channels_ok": False}).as_dict()
        metric = next(
            metric
            for metric in document["domains"]["safety"]["metrics"]
            if metric["name"] == "safety.estop_channels_ok"
        )
        metric.update(
            {
                "value": None,
                "state": "missing",
                "source_status": "missing",
                "missing": True,
                "observed_at": None,
                "age_s": None,
            }
        )
        document["domains"]["safety"]["status"] = "healthy"
        document["overall"] = "healthy"
        rebuilt = snapshot_from_document(document)
        self.assertEqual(rebuilt.domain("safety").status.value, "unknown")
        self.assertEqual(rebuilt.overall.value, "unknown")

    def test_every_collector_snapshot_shape_round_trips_to_the_same_status(self) -> None:
        # The backend must classify a document exactly the way the collector
        # classified the same samples. Complete, stale, missing, faulted and
        # degraded snapshots all have to survive the trust boundary unchanged.
        built = collector(BASE - 0.01)
        built.record("can.link_ok", False, source="can", observed_at=BASE)
        stale_collector = collector(BASE - 0.01)
        shapes = {
            "healthy": snapshot(),
            "faulted": snapshot(faults={"can.link_ok": False}),
            "degraded": snapshot(faults={"power.bms_state": "DERATE"}),
            "missing": snapshot(complete=False),
            "stale": stale_collector.snapshot(collected_at=BASE + 600),
            "conflict": built.snapshot(collected_at=BASE),
        }
        for name, original in shapes.items():
            with self.subTest(shape=name):
                rebuilt = snapshot_from_document(original.as_dict())
                self.assertEqual(rebuilt.overall, original.overall)
                self.assertEqual(
                    [(domain, health.status) for domain, health in rebuilt.domains],
                    [(domain, health.status) for domain, health in original.domains],
                )

    def test_a_metric_that_contradicts_its_value_is_refused(self) -> None:
        # A fresh claim on a faulted value, then a faulted value smuggled under a
        # fresh state: both are contradictions of the recomputed classification.
        document = snapshot().as_dict()
        metric = next(
            metric for metric in document["domains"]["communication"]["metrics"] if metric["name"] == "can.link_ok"
        )
        metric["value"] = False
        with self.assertRaisesRegex(HealthHistoryError, "contradicts"):
            snapshot_from_document(document)

        document = snapshot(faults={"can.link_ok": False}).as_dict()
        metric = next(
            metric for metric in document["domains"]["communication"]["metrics"] if metric["name"] == "can.link_ok"
        )
        metric["state"] = "fresh"
        with self.assertRaisesRegex(HealthHistoryError, "contradicts"):
            snapshot_from_document(document)

    def test_an_unregistered_metric_duplicate_domain_and_wrong_source_are_refused(self) -> None:
        document = snapshot().as_dict()
        document["domains"]["safety"]["metrics"][0]["name"] = "made.up"
        with self.assertRaises(HealthHistoryError):
            snapshot_from_document(document)

        document = snapshot().as_dict()
        document["domains"]["safety"]["metrics"].append(document["domains"]["safety"]["metrics"][0])
        with self.assertRaises(HealthHistoryError):
            snapshot_from_document(document)

        document = snapshot().as_dict()
        document["domains"]["safety"]["metrics"][0]["source"] = "attacker"
        with self.assertRaises(HealthHistoryError):
            snapshot_from_document(document)

    def test_a_non_monotonic_clock_and_non_finite_values_are_refused(self) -> None:
        document = snapshot().as_dict()
        document["clock_id"] = "realtime"
        with self.assertRaises(HealthHistoryError):
            snapshot_from_document(document)

        document = snapshot().as_dict()
        document["collected_at"] = float("nan")
        with self.assertRaises(HealthHistoryError):
            snapshot_from_document(document)

        document = snapshot().as_dict()
        document["domains"]["safety"]["metrics"][0]["observed_at"] = float("inf")
        with self.assertRaises(HealthHistoryError):
            snapshot_from_document(document)

    def test_a_missing_metric_must_not_carry_a_value(self) -> None:
        document = snapshot(complete=False).as_dict()
        metric = document["domains"]["safety"]["metrics"][0]
        self.assertTrue(metric["missing"])
        metric["value"] = 1
        with self.assertRaises(HealthHistoryError):
            snapshot_from_document(document)


class HealthLoaderTests(unittest.TestCase):
    def test_document_limits_and_malformed_input_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory, "health.jsonl")
            write_health_document(path, [snapshot()])
            self.assertEqual(len(load_health_documents(path)), 1)

        with TemporaryDirectory() as directory:
            path = Path(directory, "health.jsonl")
            path.write_text("{not json}\n", encoding="utf-8")
            with self.assertRaises(HealthHistoryError):
                load_health_documents(path)

        with TemporaryDirectory() as directory:
            path = Path(directory, "health.jsonl")
            path.write_text('{"collected_at": 1.0, "collected_at": 2.0}\n', encoding="utf-8")
            with self.assertRaises(HealthHistoryError):
                load_health_documents(path)

        with TemporaryDirectory() as directory:
            path = Path(directory, "health.jsonl")
            path.write_text('{"collected_at": NaN}\n', encoding="utf-8")
            with self.assertRaises(HealthHistoryError):
                load_health_documents(path)

        with TemporaryDirectory() as directory:
            path = Path(directory, "health.jsonl")
            write_health_document(path, [snapshot()])
            with self.assertRaises(HealthHistoryError):
                load_health_documents(path, max_bytes=10)


class HealthStoreTests(unittest.TestCase):
    def test_missing_data_is_unknown_and_a_missing_document_is_not_an_error(self) -> None:
        store = HealthStore()
        self.assertFalse(store.has_data)
        self.assertEqual(store.status(), "unknown")
        self.assertEqual(store.current_payload()["reason"], "no_health_snapshot")

    def test_history_is_bounded_by_snapshot_count(self) -> None:
        store = HealthStore(max_snapshots=3)
        for index in range(6):
            store.ingest(snapshot(at=BASE + index))
        self.assertEqual(store.snapshot_count(), 3)
        history = store.history_payload()
        self.assertEqual([entry["collected_at"] for entry in history["snapshots"]], [BASE + 3, BASE + 4, BASE + 5])

    def test_a_backwards_clock_is_refused_as_a_source_restart(self) -> None:
        store = HealthStore()
        store.ingest(snapshot(at=BASE))
        with self.assertRaises(HealthHistoryError):
            store.ingest(snapshot(at=BASE - 1))

    def test_a_faulted_snapshot_opens_a_critical_alert_and_clearing_records_history(self) -> None:
        store = HealthStore(policy=AlertPolicy(open_after_s=0.0, clear_after_s=0.0))
        store.ingest(snapshot(at=BASE, faults={"safety.estop_channels_ok": False}))
        payload = store.current_payload()
        self.assertEqual(payload["status"], "fault")
        self.assertEqual([alert["condition"] for alert in payload["alerts"]["active"]], ["estop_unavailable"])

        store.ingest(snapshot(at=BASE + 1))
        self.assertEqual(store.current_payload()["status"], "healthy")
        cleared = store.history_payload()["cleared_alerts"]
        self.assertEqual([alert["condition"] for alert in cleared], ["estop_unavailable"])
        self.assertEqual(cleared[0]["state"], "cleared")

    def test_debounce_holds_an_alert_open_until_it_has_cleared_long_enough(self) -> None:
        store = HealthStore(policy=AlertPolicy(open_after_s=0.0, clear_after_s=5.0))
        store.ingest(snapshot(at=BASE, faults={"can.link_ok": False}))
        self.assertEqual(len(store.current_payload()["alerts"]["active"]), 1)
        store.ingest(snapshot(at=BASE + 1))
        # Still inside the clear window: the alert is still listed as active.
        self.assertEqual(len(store.current_payload()["alerts"]["active"]), 1)
        store.ingest(snapshot(at=BASE + 6))
        self.assertEqual(store.current_payload()["alerts"]["active"], [])

    def test_the_payload_declares_its_limits_and_versions(self) -> None:
        store = HealthStore(max_snapshots=4)
        store.ingest(snapshot())
        payload = store.current_payload()
        self.assertEqual(payload["history_version"], HEALTH_HISTORY_VERSION)
        self.assertEqual(payload["rules_version"], "robot-alerts-v1")
        self.assertEqual(payload["alerts"]["summary"]["total"], 0)
        self.assertEqual(payload["history"]["limits"]["max_snapshots"], 4)

    def test_invalid_limits_and_non_snapshots_are_refused(self) -> None:
        with self.assertRaises(HealthHistoryError):
            HealthStore(max_snapshots=0)
        with self.assertRaises(HealthHistoryError):
            HealthStore(max_snapshots=MAX_SNAPSHOTS + 1)
        with self.assertRaises(HealthHistoryError):
            HealthStore().ingest("not-a-snapshot")  # type: ignore[arg-type]

    def test_concurrent_readers_never_observe_a_partial_history(self) -> None:
        store = HealthStore(max_snapshots=8)
        errors: list[BaseException] = []

        def writer() -> None:
            try:
                for index in range(200):
                    store.ingest(snapshot(at=BASE + index))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(400):
                    payload = store.current_payload()
                    count = payload["history"]["snapshot_count"]
                    self.assertTrue(0 <= count <= 8)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer), *(threading.Thread(target=reader) for _ in range(3))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])


class HealthApiTests(unittest.TestCase):
    def start_server(self, health_document: str | None = None, **options):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data_dir = Path(directory.name)
        if health_document is not None:
            data_dir.joinpath("health.jsonl").write_text(health_document, encoding="utf-8")
        server = create_server("127.0.0.1", 0, data_dir=data_dir, **options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def read_json(url: str) -> tuple[int, dict, dict]:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status, json.loads(response.read()), dict(response.headers)

    def test_health_endpoint_is_read_only_and_reports_a_fault(self) -> None:
        base_url = self.start_server(json.dumps(snapshot(faults={"safety.estop_channels_ok": False}).as_dict()) + "\n")
        status, payload, headers = self.read_json(f"{base_url}/api/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["status"], "fault")
        self.assertEqual(payload["alerts"]["summary"]["total"], 1)
        self.assertEqual(headers["X-API-Version"], "1")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_health_history_endpoint_honours_the_since_filter(self) -> None:
        document = "".join(json.dumps(snapshot(at=BASE + index).as_dict()) + "\n" for index in range(4))
        base_url = self.start_server(document)
        status, payload, _ = self.read_json(f"{base_url}/api/v1/health/history?since={BASE + 2}")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["snapshots"]), 4)
        self.assertEqual(payload["limits"]["max_snapshots"], MAX_SNAPSHOTS)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{base_url}/api/v1/health/history?since=bad", timeout=2)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(json.loads(caught.exception.read())["error"], "invalid_since")

    def test_a_malformed_health_document_returns_503_not_a_partial_projection(self) -> None:
        base_url = self.start_server("{not json}\n")
        for path in ("/api/v1/health", "/api/v1/health/history"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"{base_url}{path}", timeout=3)
            with caught.exception as response:
                self.assertEqual(response.code, 503)
                self.assertEqual(json.loads(response.read())["error"], "invalid_health_source")

    def test_health_endpoints_are_get_only_and_absent_health_is_unknown(self) -> None:
        base_url = self.start_server()
        status, payload, _ = self.read_json(f"{base_url}/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "unknown")
        self.assertIsNone(payload["current"])
        self.assertEqual(payload["reason"], "no_health_snapshot")
        request = urllib.request.Request(f"{base_url}/api/v1/health", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 405)

    def test_health_reloads_when_the_document_changes(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data_dir = Path(directory.name)
        healthy = json.dumps(snapshot(at=BASE).as_dict()) + "\n"
        data_dir.joinpath("health.jsonl").write_text(healthy, encoding="utf-8")
        server = create_server("127.0.0.1", 0, data_dir=data_dir)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        self.assertEqual(self.read_json(f"{base_url}/api/v1/health")[1]["status"], "healthy")
        faulted = json.dumps(snapshot(at=BASE + 1, faults={"can.link_ok": False}).as_dict()) + "\n"
        # Advance the mtime so the size/mtime cache key changes even on a fast disk.
        data_dir.joinpath("health.jsonl").write_text(faulted, encoding="utf-8")
        import os

        os.utime(data_dir / "health.jsonl", (BASE + 10, BASE + 10))
        self.assertEqual(self.read_json(f"{base_url}/api/v1/health")[1]["status"], "fault")


if __name__ == "__main__":
    unittest.main()
