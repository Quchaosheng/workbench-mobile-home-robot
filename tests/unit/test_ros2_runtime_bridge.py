"""Tests for the ROS-free and optional ROS 2 DeviceRuntime bridge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from workbench.hardware import (
    BRIDGE_SCHEMA_VERSION,
    EXTERNAL_PROJECTION_ALLOWLIST,
    HEALTH_PROJECTION_ALLOWLIST,
    BridgeHealthRecord,
    BridgePublishers,
    CanDiagnostic,
    CanDiagnosticCode,
    CanExternalRecord,
    CanFrameKind,
    CanLinkState,
    CanReceiveStatus,
    DeviceRuntimeBridgeConfig,
    RuntimeBridgeCore,
    RuntimeBridgeState,
    SafeCANBus,
    create_bounded_executor,
    create_lifecycle_node,
    create_socketcan_adapter_factory,
    serialize_external_projection,
    serialize_health_projection,
)

ROOT = Path(__file__).resolve().parents[2]


class RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.payloads: list[str] = []
        self.fail = fail

    def publish(self, payload: str) -> None:
        if self.fail:
            raise RuntimeError("publisher unavailable")
        self.payloads.append(payload)


class FakeAdapter:
    def __init__(self, *, configure_result: bool = True, activate_result: bool = True) -> None:
        self.configure_result = configure_result
        self.activate_result = activate_result
        self.events: list[str] = []
        self.poll_count = 0
        self._stop_polling = threading.Event()

    def configure(self) -> bool:
        self.events.append("configure")
        return self.configure_result

    def activate(self) -> bool:
        self.events.append("activate")
        return self.activate_result

    def poll(self, receive_timeout_s: float) -> None:
        del receive_timeout_s
        self.poll_count += 1
        self._stop_polling.wait(0.0001)

    def deactivate(self) -> bool:
        self.events.append("deactivate")
        self._stop_polling.set()
        return True

    def cleanup(self) -> bool:
        self.events.append("cleanup")
        return True


def bridge_config(**overrides: object) -> DeviceRuntimeBridgeConfig:
    values: dict[str, object] = {
        "poll_period_s": 0.001,
        "shutdown_timeout_s": 0.5,
        "max_records_per_tick": 8,
        "command_capacity": 4,
        "telemetry_capacity": 4,
        "health_capacity": 4,
        "external_capacity": 4,
        "max_subscribers_per_id": 2,
    }
    values.update(overrides)
    return DeviceRuntimeBridgeConfig(**values)


def external_record(
    frame_kind: CanFrameKind,
    sequence: int,
    *,
    confirmed: bool = False,
) -> CanExternalRecord:
    is_ack = frame_kind in {CanFrameKind.ACK, CanFrameKind.STOP_ACK}
    return CanExternalRecord(
        status=CanReceiveStatus.ACCEPTED,
        source="virtual-socketcan",
        interface="vcan0",
        ingress_sequence=sequence,
        health=CanLinkState.ACTIVE,
        frame_valid=True,
        exposure_allowed=True,
        event_type="action_result" if is_ack else "telemetry",
        frame_kind=frame_kind,
        arbitration_id=0x101 if frame_kind is CanFrameKind.ACK else 0x180,
        raw_can_id=0x101 if frame_kind is CanFrameKind.ACK else 0x180,
        dlc=8,
        data=b"\x10\x00\x01\x01\x00\x00\x00\x00" if is_ack else b"\x10\x00\x00\x00\x01\x00\x00\x00",
        is_extended_id=False,
        is_remote_frame=False,
        is_error_frame=False,
        monotonic_ts=float(sequence + 1),
        wall_ts=float(sequence + 1),
        timestamp_source="host",
        confirmed=confirmed,
        command_id=1 if is_ack else None,
        sequence_no=sequence if not is_ack else None,
        opcode=1 if is_ack else None,
        retry_count=0 if is_ack else None,
        result_code=0 if is_ack else None,
        fault_code=0,
        device_mode=0,
        evidence_refs=(f"can-ingress://virtual-socketcan/vcan0/{sequence}",),
    )


def publishers(
    *, health_fail: bool = False
) -> tuple[
    BridgePublishers,
    RecordingPublisher,
    RecordingPublisher,
    RecordingPublisher,
]:
    telemetry = RecordingPublisher()
    ack = RecordingPublisher()
    health = RecordingPublisher(fail=health_fail)
    return BridgePublishers(telemetry=telemetry, ack=ack, health=health), telemetry, ack, health


def test_config_rejects_unbounded_or_ambiguous_settings() -> None:
    with pytest.raises(ValueError, match="executor_threads"):
        bridge_config(executor_threads=9)
    with pytest.raises(ValueError, match="external_capacity"):
        bridge_config(external_capacity=0)
    with pytest.raises(ValueError, match="domain_id"):
        bridge_config(domain_id=233)
    with pytest.raises(ValueError, match="telemetry_topic"):
        bridge_config(telemetry_topic="device/telemetry")
    with pytest.raises(ValueError, match="telemetry_topic"):
        bridge_config(telemetry_topic="/device/telemetry with-space")
    with pytest.raises(ValueError, match="telemetry_topic"):
        bridge_config(telemetry_topic="/device/telemetry/")
    with pytest.raises(ValueError, match="node_name"):
        bridge_config(node_name="device/runtime")
    with pytest.raises(TypeError, match="telemetry_qos"):
        bridge_config(telemetry_qos=object())
    with pytest.raises(ValueError, match="ack_qos"):
        bridge_config(ack_qos=bridge_config().telemetry_qos)


def test_configuration_snapshot_records_qos_domain_executor_and_queue_bounds() -> None:
    config = bridge_config(domain_id=42, executor_threads=3)
    snapshot = RuntimeBridgeCore(lambda: FakeAdapter(), config=config).configuration_snapshot()

    assert snapshot["schema_version"] == BRIDGE_SCHEMA_VERSION
    assert snapshot["domain_id"] == 42
    assert snapshot["rmw_implementation"] == "rmw_fastrtps_cpp"
    assert snapshot["executor_threads"] == 3
    assert snapshot["executor_threads_max"] == 8
    assert snapshot["queues"]["external"] == 4
    assert snapshot["qos"]["ack"]["reliability"] == "reliable"
    assert snapshot["qos"]["telemetry"]["reliability"] == "best_effort"


def test_external_projection_is_deterministic_and_allowlisted() -> None:
    record = external_record(CanFrameKind.TELEMETRY, 7)
    first = serialize_external_projection(record)
    second = serialize_external_projection(record)
    payload = json.loads(first)

    assert first == second
    assert payload["schema_version"] == BRIDGE_SCHEMA_VERSION
    assert payload["record_type"] == "telemetry"
    assert set(payload) - {"schema_version", "record_type"} == set(EXTERNAL_PROJECTION_ALLOWLIST)
    assert "data" not in payload
    assert payload["data_hex"] == record.data_hex
    assert payload["evidence_refs"] == list(record.evidence_refs)

    with pytest.raises(ValueError, match="exposure-allowed"):
        serialize_external_projection(
            CanExternalRecord(
                status=CanReceiveStatus.DUPLICATE,
                source=record.source,
                interface=record.interface,
                ingress_sequence=8,
                health=record.health,
                frame_valid=True,
                exposure_allowed=False,
                frame_kind=CanFrameKind.TELEMETRY,
            )
        )

    with pytest.raises(ValueError, match="event_type"):
        serialize_external_projection(replace(external_record(CanFrameKind.TELEMETRY, 8), event_type="action_result"))


def test_core_rejects_invalid_constructor_boundaries() -> None:
    with pytest.raises(TypeError, match="config"):
        RuntimeBridgeCore(lambda: FakeAdapter(), config=object())
    with pytest.raises(TypeError, match="publishers"):
        RuntimeBridgeCore(lambda: FakeAdapter(), publishers=object())


def test_health_projection_is_allowlisted_for_runtime_and_bridge_records() -> None:
    diagnostic = CanDiagnostic(CanDiagnosticCode.LINK_LOST, 3.5, "link unavailable", 9)
    runtime_payload = json.loads(serialize_health_projection(diagnostic))
    bridge_payload = json.loads(
        serialize_health_projection(
            BridgeHealthRecord(
                sequence=2,
                code="publisher_failed",
                observed_at=4.5,
                detail="health publisher failed",
            )
        )
    )

    assert set(runtime_payload) - {"schema_version", "record_type"} == set(HEALTH_PROJECTION_ALLOWLIST)
    assert runtime_payload["source"] == "device-runtime"
    assert bridge_payload["source"] == "ros2-runtime-bridge"
    assert bridge_payload["sequence"] == 2


def test_core_reuses_safe_can_bus_runtime_instead_of_wrapping_a_second_runtime() -> None:
    class NoopTransport:
        def open(self) -> None:
            pass

        def send(self, frame: object) -> None:
            del frame

        def receive(self, timeout_s: float) -> None:
            del timeout_s
            return None

        def recover(self) -> bool:
            return True

        def close(self) -> None:
            pass

    adapter = SafeCANBus(NoopTransport())
    bridge_publishers, *_ = publishers()
    core = RuntimeBridgeCore(lambda: adapter, config=bridge_config(), publishers=bridge_publishers)

    assert core.configure()
    assert core.runtime is adapter.runtime
    assert core.activate()
    assert core.runtime is adapter.runtime
    assert core.deactivate()
    assert adapter.runtime.state.value == "cleaned"


def test_core_routes_records_with_a_fixed_drain_budget_and_preserves_plane_counters() -> None:
    created: list[FakeAdapter] = []

    def factory() -> FakeAdapter:
        adapter = FakeAdapter()
        created.append(adapter)
        return adapter

    bridge_publishers, telemetry, ack, health = publishers()
    config = bridge_config(max_records_per_tick=3, external_capacity=2)
    core = RuntimeBridgeCore(factory, config=config, publishers=bridge_publishers)

    assert core.configure()
    assert core.activate()
    runtime = core.runtime
    assert runtime is not None
    runtime.publish_external(external_record(CanFrameKind.TELEMETRY, 1))
    runtime.publish_external(external_record(CanFrameKind.ACK, 2, confirmed=True))
    runtime.record_health(CanDiagnostic(CanDiagnosticCode.LINK_LOST, 2.0, "link lost"))

    assert core.drain_once() == 3
    assert len(telemetry.payloads) == 1
    assert len(ack.payloads) == 1
    assert len(health.payloads) == 1
    assert core.metrics().records_processed == 3
    assert core.metrics().external_drop_count == 0
    assert core.deactivate()
    assert core.state is RuntimeBridgeState.INACTIVE
    assert not core.metrics().worker_alive
    assert created[0].events[-2:] == ["deactivate", "cleanup"]


def test_external_queue_drop_and_unsupported_objects_never_cross_the_projection() -> None:
    bridge_publishers, telemetry, ack, health = publishers()
    core = RuntimeBridgeCore(
        lambda: FakeAdapter(),
        config=bridge_config(external_capacity=1),
        publishers=bridge_publishers,
    )
    assert core.configure()
    assert core.activate()
    runtime = core.runtime
    assert runtime is not None
    runtime.publish_external(external_record(CanFrameKind.TELEMETRY, 1))
    runtime.publish_external(object())

    assert core.metrics().external_drop_count == 1
    assert core.drain_once() == 1
    assert not telemetry.payloads
    assert not ack.payloads
    assert core.metrics().unsupported_records == 1
    assert core.drain_once() == 1
    assert health.payloads
    assert json.loads(health.payloads[0])["code"] == "external_projection_rejected"
    assert core.deactivate()


def test_health_snapshot_is_not_republished_on_each_timer_tick() -> None:
    bridge_publishers, _, _, health = publishers()
    core = RuntimeBridgeCore(lambda: FakeAdapter(), config=bridge_config(), publishers=bridge_publishers)
    assert core.configure()
    assert core.activate()
    runtime = core.runtime
    assert runtime is not None
    runtime.record_health(CanDiagnostic(CanDiagnosticCode.BUS_OFF, 1.0, "bus off"))

    assert core.drain_once(limit=1) == 1
    assert core.metrics().health_depth == 0
    assert core.drain_once(limit=1) == 0
    assert len(health.payloads) == 1
    assert core.deactivate()


def test_publisher_failure_is_bounded_and_does_not_escape_timer_drain() -> None:
    bridge_publishers, _, _, health = publishers(health_fail=False)
    failing_telemetry = RecordingPublisher(fail=True)
    bridge_publishers = BridgePublishers(
        telemetry=failing_telemetry,
        ack=bridge_publishers.ack,
        health=bridge_publishers.health,
    )
    core = RuntimeBridgeCore(lambda: FakeAdapter(), config=bridge_config(), publishers=bridge_publishers)
    assert core.configure()
    assert core.activate()
    runtime = core.runtime
    assert runtime is not None
    runtime.publish_external(external_record(CanFrameKind.TELEMETRY, 1))

    assert core.drain_once() == 1
    assert core.metrics().publisher_errors == 1
    assert core.drain_once() == 1
    assert health.payloads
    assert json.loads(health.payloads[0])["code"] == "publisher_failed"
    assert core.deactivate()


def test_deactivate_recreates_a_clean_runtime_for_later_activation() -> None:
    created: list[FakeAdapter] = []

    def factory() -> FakeAdapter:
        adapter = FakeAdapter()
        created.append(adapter)
        return adapter

    bridge_publishers, *_ = publishers()
    core = RuntimeBridgeCore(factory, config=bridge_config(), publishers=bridge_publishers)
    assert core.configure()
    assert core.activate()
    first_runtime = core.runtime
    assert first_runtime is not None
    assert core.deactivate()
    assert first_runtime.state.value == "cleaned"
    assert not first_runtime.worker_alive

    assert core.activate()
    assert len(created) == 2
    assert core.runtime is not first_runtime
    assert core.deactivate()
    assert core.cleanup()
    assert core.state is RuntimeBridgeState.UNCONFIGURED


def test_failed_activation_is_fail_closed_and_cleanup_remains_available() -> None:
    bridge_publishers, *_ = publishers()
    core = RuntimeBridgeCore(
        lambda: FakeAdapter(configure_result=False),
        config=bridge_config(),
        publishers=bridge_publishers,
    )

    assert core.configure()
    assert not core.activate()
    assert core.state is RuntimeBridgeState.FAILED
    assert core.bridge_health_records()
    assert core.cleanup()
    assert core.state is RuntimeBridgeState.UNCONFIGURED


def test_shutdown_is_terminal_and_idempotent() -> None:
    bridge_publishers, *_ = publishers()
    core = RuntimeBridgeCore(lambda: FakeAdapter(), config=bridge_config(), publishers=bridge_publishers)
    assert core.shutdown()
    assert core.state is RuntimeBridgeState.SHUTDOWN
    assert core.shutdown()
    assert not core.configure()


def test_importing_hardware_bridge_does_not_import_rclpy() -> None:
    hardware_root = ROOT / "libs" / "hardware"
    environment = os.environ.copy()
    existing_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(path for path in (str(hardware_root), existing_path) if path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import workbench.hardware; assert 'rclpy' not in sys.modules",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_socketcan_factory_is_side_effect_free_until_runtime_activation() -> None:
    config = bridge_config()
    factory = create_socketcan_adapter_factory("vcan0", config=config, source="virtual-socketcan")
    adapter = factory()

    assert isinstance(adapter, SafeCANBus)
    assert adapter.runtime.state.value == "new"
    assert not adapter._transport.is_open


def test_ros_factory_and_bounded_executor_work_when_jazzy_is_available() -> None:
    rclpy = pytest.importorskip("rclpy")
    from rclpy.lifecycle import TransitionCallbackReturn
    from rclpy.qos import qos_profile_sensor_data
    from std_msgs.msg import String

    initialized_here = False
    if not rclpy.ok():
        ros_log_dir = Path("/tmp/workbench-issue230-ros-log")
        ros_log_dir.mkdir(parents=True, exist_ok=True)
        os.environ["ROS_LOG_DIR"] = str(ros_log_dir)
        rclpy.init(args=[])
        initialized_here = True
    config = bridge_config(node_name="issue230_bridge_smoke")
    created: list[FakeAdapter] = []
    received: list[str] = []

    def factory() -> FakeAdapter:
        adapter = FakeAdapter()
        created.append(adapter)
        return adapter

    node = create_lifecycle_node(factory, config=config)
    executor = create_bounded_executor(config, context=rclpy.get_default_context())
    observer = rclpy.create_node("issue230_bridge_observer")
    observer.create_subscription(
        String,
        config.telemetry_topic,
        lambda message: received.append(message.data),
        qos_profile_sensor_data,
    )
    try:
        executor.add_node(node)
        executor.add_node(observer)
        assert node.trigger_configure() is TransitionCallbackReturn.SUCCESS
        assert node.trigger_activate() is TransitionCallbackReturn.SUCCESS
        assert node.bridge.metrics().worker_alive
        runtime = node.bridge.runtime
        assert runtime is not None
        record = external_record(CanFrameKind.TELEMETRY, 4)
        expected_payload = serialize_external_projection(record)
        for _ in range(3):
            executor.spin_once(timeout_sec=0.01)
        runtime.publish_external(record)
        for _ in range(10):
            executor.spin_once(timeout_sec=0.01)
            if received:
                break
        assert node.bridge.metrics().telemetry_published == 1
        assert received == [expected_payload]
        assert node.trigger_deactivate() is TransitionCallbackReturn.SUCCESS
        assert not node.bridge.metrics().worker_alive
        assert node.trigger_activate() is TransitionCallbackReturn.SUCCESS
        assert len(created) == 2
        assert node.trigger_deactivate() is TransitionCallbackReturn.SUCCESS
        assert node.trigger_cleanup() is TransitionCallbackReturn.SUCCESS
        assert node.trigger_shutdown() is TransitionCallbackReturn.SUCCESS
    finally:
        executor.remove_node(node)
        executor.remove_node(observer)
        executor.shutdown(timeout_sec=1.0)
        node.destroy_node()
        observer.destroy_node()
        if initialized_here and rclpy.ok():
            rclpy.shutdown()
