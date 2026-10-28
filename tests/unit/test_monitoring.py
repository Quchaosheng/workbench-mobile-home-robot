import sys
import threading
import unittest
from pathlib import Path


def _ensure_on_path(entry: str) -> None:
    """Add a path entry without taking precedence over the package the suite uses.

    `workbench` is a regular package under both libs/kernel and libs/application,
    so prepending one of them decides which copy every later import sees. Appending
    keeps this module's imports resolvable without changing that choice.
    """

    if entry not in sys.path:
        sys.path.append(entry)


ROOT = Path(__file__).resolve().parents[2]
_ensure_on_path(str(ROOT / "libs" / "application"))
sys.path.insert(0, str(ROOT / "tools" / "scripts"))

from benchmark_monitoring import benchmark
from workbench.application.monitoring import (
    DEFAULT_SPECS,
    ComponentHealthAdapter,
    HealthSnapshotCollector,
    HealthStatus,
    LinuxHealthAdapter,
    MetricError,
    MetricKind,
    MetricRegistry,
    MetricSpec,
    SystemMetrics,
)


def record(collector: HealthSnapshotCollector, name: str, value: object, *, observed_at: float | None = None) -> None:
    collector.record(
        name,
        value,
        source=collector.metrics.registry.get(name).source,
        observed_at=observed_at,
    )


def complete(collector: HealthSnapshotCollector, now: float = 100.0) -> None:
    values = {
        "app.readiness": True,
        "app.uptime_seconds": 60.0,
        "app.restart_count": 0,
        "app.queue_depth": 0,
        "app.last_successful_cycle_age_seconds": 0.1,
        "compute.cpu_percent": 12.0,
        "compute.memory_percent": 25.0,
        "compute.disk_free_bytes": 10_000_000,
        "compute.temperature_celsius": 40.0,
        "compute.disk_growth_bytes": 0,
        "compute.load_1m": 0.5,
        "safety.estop_channels_ok": True,
        "safety.mcu_watchdog_ok": True,
        "safety.contactor_permission": True,
        "power.bms_state": "RUN",
        "power.soc_percent": 80.0,
        "power.pack_voltage_volts": 48.0,
        "power.pack_current_amperes": 0.0,
        "power.pack_temperature_celsius": 30.0,
        "can.link_ok": True,
        "can.bus_state": "active",
        "can.error_count": 0,
        "can.last_frame_age_seconds": 0.1,
        "event_store.integrity_ok": True,
        "backend.available": True,
        "nav.localization_ok": True,
        "motion.controller_ok": True,
        "motion.stop_state": "idle",
        "perception.fresh": True,
        "task.active": False,
        "task.run_id": "none",
        "task.action_id": "none",
        "task.verification_state": "none",
        "task.timeout_count": 0,
        "task.fault_count": 0,
    }
    for name, value in values.items():
        record(collector, name, value, observed_at=now)


class MonitoringTests(unittest.TestCase):
    def test_complete_snapshot_is_healthy_and_serializable(self) -> None:
        collector = HealthSnapshotCollector(clock=lambda: 100.0)
        complete(collector)

        snapshot = collector.snapshot()

        self.assertEqual(snapshot.overall, HealthStatus.HEALTHY)
        self.assertEqual(snapshot.domain("safety").status, HealthStatus.HEALTHY)
        self.assertEqual(snapshot.as_dict()["domains"]["power"]["status"], "healthy")

    def test_missing_stale_and_faulted_critical_inputs_fail_closed(self) -> None:
        collector = HealthSnapshotCollector(clock=lambda: 100.0)
        record(collector, "safety.estop_channels_ok", True, observed_at=98.0)
        record(collector, "safety.mcu_watchdog_ok", False, observed_at=99.9)

        snapshot = collector.snapshot()

        self.assertEqual(snapshot.overall, HealthStatus.FAULT)
        self.assertEqual(snapshot.domain("safety").status, HealthStatus.FAULT)
        safety = {metric.name: metric for metric in snapshot.domain("safety").metrics}
        self.assertEqual(safety["safety.estop_channels_ok"].state, "stale")
        self.assertEqual(safety["safety.estop_channels_ok"].missing, False)
        self.assertEqual(safety["safety.mcu_watchdog_ok"].state, "fault")

        record(collector, "safety.mcu_watchdog_ok", True, observed_at=100.0)
        recovered = collector.snapshot()
        self.assertEqual(recovered.domain("safety").status, HealthStatus.UNKNOWN)
        self.assertEqual(recovered.overall, HealthStatus.UNKNOWN)

    def test_metric_contract_rejects_unknown_nonfinite_and_unbounded_labels(self) -> None:
        collector = HealthSnapshotCollector(clock=lambda: 1.0)
        with self.assertRaises(MetricError):
            collector.record("unknown.metric", 1, source="fixture")
        with self.assertRaises(MetricError):
            record(collector, "compute.cpu_percent", float("nan"))
        with self.assertRaises(MetricError):
            collector.record("compute.cpu_percent", 1.0, source="linux.proc", labels={"host": "not-allowed"})
        with self.assertRaises(MetricError):
            collector.record("compute.cpu_percent", 1.0, source="linux.proc", labels={"host": "line\nbreak"})
        with self.assertRaises(MetricError):
            collector.record("compute.cpu_percent", 1.0, source="linux.proc", clock_id="wall")
        with self.assertRaises(MetricError):
            collector.record("safety.estop_channels_ok", True, source="spoof")

    def test_histogram_uses_fixed_aggregates_not_sample_history(self) -> None:
        registry = MetricRegistry(
            (
                MetricSpec(
                    "motion.latency_seconds",
                    MetricKind.HISTOGRAM,
                    "seconds",
                    "robot",
                    "float",
                    5.0,
                ),
            )
        )
        metrics = SystemMetrics(registry, histogram_buckets=(0.1, 1.0))
        collector = HealthSnapshotCollector(metrics, clock=lambda: 10.0)
        for value in range(300):
            collector.record("motion.latency_seconds", value / 100, source="component", observed_at=10.0)

        exported = metrics.export_prometheus()

        self.assertIn("motion_latency_seconds_count 300", exported)
        self.assertIn('motion_latency_seconds_bucket{le="0.1"} 11', exported)
        self.assertIn('motion_latency_seconds_bucket{le="1.0"} 101', exported)
        self.assertIn('motion_latency_seconds_bucket{le="+Inf"} 300', exported)
        self.assertLessEqual(len(metrics._histograms[("motion.latency_seconds", ())][2]), 2)

    def test_updates_and_snapshots_are_thread_safe(self) -> None:
        metrics = SystemMetrics()
        collector = HealthSnapshotCollector(metrics, clock=lambda: 10.0)

        def writer() -> None:
            for _ in range(500):
                metrics.increment_counter("app.restart_count")

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for _ in range(100):
            collector.snapshot(collected_at=10.0)
        for thread in threads:
            thread.join()

        self.assertEqual(len(metrics.samples()), 1)
        self.assertEqual(metrics.samples()[0].value, 2_000)

    def test_older_and_conflicting_samples_fail_closed(self) -> None:
        collector = HealthSnapshotCollector(clock=lambda: 100.0)
        record(collector, "safety.estop_channels_ok", False, observed_at=100.0)
        with self.assertRaisesRegex(MetricError, "timestamp regressed"):
            record(collector, "safety.estop_channels_ok", True, observed_at=99.9)
        self.assertEqual(collector.snapshot().domain("safety").metrics[0].state, "fault")

        conflict = HealthSnapshotCollector(clock=lambda: 100.0)
        record(conflict, "safety.estop_channels_ok", True, observed_at=100.0)
        with self.assertRaisesRegex(MetricError, "conflict"):
            record(conflict, "safety.estop_channels_ok", False, observed_at=100.0)
        view = conflict.snapshot().domain("safety").metrics[0]
        self.assertEqual((view.state, view.source_status, view.value), ("conflict", "conflict", None))
        self.assertEqual(conflict.snapshot().domain("safety").status, HealthStatus.FAULT)

    def test_default_registry_is_fixed_and_documented(self) -> None:
        names = {spec.name for spec in MetricRegistry().specs()}
        self.assertEqual(len(names), len(DEFAULT_SPECS))
        self.assertIn("safety.estop_channels_ok", names)
        self.assertIn("perception.fresh", names)

    def test_normal_false_and_degraded_values_are_not_faults(self) -> None:
        collector = HealthSnapshotCollector(clock=lambda: 100.0)
        complete(collector, now=99.9)
        record(collector, "safety.contactor_permission", False, observed_at=100.0)
        record(collector, "app.readiness", False, observed_at=100.0)
        self.assertEqual(collector.snapshot().domain("safety").status, HealthStatus.HEALTHY)
        self.assertEqual(collector.snapshot().domain("application").status, HealthStatus.DEGRADED)

    def test_bms_and_stop_state_severity_is_explicit(self) -> None:
        collector = HealthSnapshotCollector(clock=lambda: 100.0)
        record(collector, "power.bms_state", "DERATE", observed_at=100.0)
        record(collector, "motion.stop_state", "fault", observed_at=100.0)
        self.assertEqual(collector.snapshot().domain("power").status, HealthStatus.DEGRADED)
        self.assertEqual(collector.snapshot().domain("robot").status, HealthStatus.FAULT)

    def test_contract_ranges_enum_and_counters_are_checked(self) -> None:
        collector = HealthSnapshotCollector(clock=lambda: 100.0)
        with self.assertRaises(MetricError):
            record(collector, "compute.cpu_percent", 101.0)
        with self.assertRaises(MetricError):
            record(collector, "power.bms_state", "bogus")
        with self.assertRaises(MetricError):
            record(collector, "app.restart_count", -1)
        with self.assertRaises(MetricError):
            record(collector, "app.restart_count", False)
        with self.assertRaises(MetricError):
            record(collector, "task.run_id", "recipient@example.com")
        with self.assertRaises(MetricError):
            record(collector, "compute.cpu_percent", 10**400)
        with self.assertRaises(MetricError):
            record(collector, "compute.cpu_percent", 1.0, observed_at=10**400)

    def test_health_values_require_the_declared_type(self) -> None:
        with self.assertRaises(MetricError):
            MetricRegistry(
                (
                    MetricSpec(
                        "metric.strict",
                        MetricKind.GAUGE,
                        "items",
                        "compute",
                        "int",
                        1.0,
                        fault_values=(False,),
                    ),
                )
            )
        with self.assertRaises(MetricError):
            MetricSpec("metric.secret", MetricKind.GAUGE, "value", "compute", "str", 1.0)
        with self.assertRaises(MetricError):
            MetricSpec("metric.type", MetricKind.GAUGE, "value", "compute", [], 1.0)  # type: ignore[arg-type]
        with self.assertRaises(MetricError):
            MetricSpec("metric.critical", MetricKind.GAUGE, "value", "compute", "int", 1.0, 1)  # type: ignore[arg-type]

    def test_sampling_interval_is_bounded_and_clock_regression_fails(self) -> None:
        collector = HealthSnapshotCollector(clock=lambda: 10.0, sampling_interval_s=2.0)
        self.assertFalse(collector.is_due(9.0))
        self.assertTrue(collector.is_due(8.0))
        with self.assertRaises(MetricError):
            collector.is_due(11.0)
        with self.assertRaises(MetricError):
            HealthSnapshotCollector(sampling_interval_s=0.01)
        with self.assertRaisesRegex(MetricError, "difference"):
            HealthSnapshotCollector(clock=lambda: 1e308).is_due(-1e308)

        overflow = HealthSnapshotCollector(clock=lambda: 1e308)
        record(overflow, "safety.estop_channels_ok", True, observed_at=-1e308)
        with self.assertRaisesRegex(MetricError, "difference"):
            overflow.snapshot()

    def test_series_limit_and_histogram_validation_are_atomic(self) -> None:
        registry = MetricRegistry(
            (
                MetricSpec(
                    "metric.histogram",
                    MetricKind.HISTOGRAM,
                    "seconds",
                    "compute",
                    "float",
                    5.0,
                    allowed_labels=("channel",),
                ),
            )
        )
        metrics = SystemMetrics(registry, max_series=1, histogram_buckets=(1.0,))
        metrics.record_histogram("metric.histogram", 0.5, source="component", labels={"channel": "a"})
        with self.assertRaises(MetricError):
            metrics.record_histogram("metric.histogram", float("nan"), source="component")
        with self.assertRaises(MetricError):
            metrics.record_histogram("metric.histogram", 0.5, source="component", labels={"channel": "b"})
        self.assertIn('metric_histogram_count{channel="a"} 1', metrics.export_prometheus())
        self.assertNotIn('channel="b"', metrics.export_prometheus())

    def test_linux_adapter_uses_injected_bounded_sources(self) -> None:
        files = {
            "/proc/meminfo": "MemTotal:       1000 kB\nMemAvailable:    250 kB\n",
            "/proc/loadavg": "0.25 0.10 0.05 1/10 1\n",
            "/sys/class/thermal/thermal_zone0/temp": "500\n",
        }
        cpu = iter(("cpu  100 0 0 80 0 0 0 0 0 0\n", "cpu  200 0 0 130 0 0 0 0 0 0\n"))

        def read_text(path: str) -> str:
            return next(cpu) if path == "/proc/stat" else files[path]

        adapter = LinuxHealthAdapter(read_text=read_text, disk_usage=lambda _: (100, 60, 40))
        collector = HealthSnapshotCollector(clock=lambda: 10.0)
        names = adapter.collect(collector, observed_at=10.0)
        self.assertIn("compute.memory_percent", names)
        self.assertNotIn("app.uptime_seconds", names)
        self.assertNotIn("compute.disk_growth_bytes", names)
        second = adapter.collect(collector, observed_at=11.0)
        self.assertIn("compute.cpu_percent", second)
        self.assertIn("compute.disk_growth_bytes", second)
        samples = {sample.name: sample.value for sample in collector.metrics.samples()}
        self.assertEqual(samples["compute.temperature_celsius"], 0.5)
        self.assertTrue(all(sample.source.startswith("linux.") for sample in collector.metrics.samples()))

    def test_component_and_ros_diagnostic_readers_are_injectable(self) -> None:
        diagnostic = {"localization_ok": True}
        adapter = ComponentHealthAdapter("nav2", {"nav.localization_ok": lambda: diagnostic["localization_ok"]})
        collector = HealthSnapshotCollector(clock=lambda: 10.0)
        self.assertEqual(adapter.collect(collector), ("nav.localization_ok",))
        self.assertEqual(collector.metrics.samples()[0].source, "nav2")

    def test_resource_report_is_bounded_and_marks_physical_work_unexecuted(self) -> None:
        report = benchmark(10)
        self.assertEqual(report["evidence_class"], "local_software")
        self.assertEqual(report["target_hardware_measurement"], "NOT_EXECUTED")
        self.assertEqual(report["physical_source_validation"], "NOT_EXECUTED")
        self.assertEqual(report["resources"]["threads_before"], report["resources"]["threads_after"])
        self.assertLess(report["output"]["snapshot_json_bytes"], 16_384)
        self.assertEqual(report["snapshots"]["complete_synthetic"]["overall"], "healthy")
        self.assertEqual(report["snapshots"]["fault_synthetic"]["overall"], "fault")


if __name__ == "__main__":
    unittest.main()
