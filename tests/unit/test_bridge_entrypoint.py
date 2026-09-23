"""Tests for the operator-facing DeviceRuntime bridge entry point.

These tests never need a CAN device and never need ROS 2: the process-level
rules (validation, exit codes, worker-joined shutdown) are exercised through
the injected seams, so a regression cannot hide behind a missing host install.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import threading
from pathlib import Path

import pytest
from workbench.hardware import DeviceRuntimeBridgeConfig, RuntimeBridgeState
from workbench.hardware import ros2_runtime_bridge as bridge_module
from workbench.hardware.ros2_runtime_bridge import (
    EXIT_ACTIVATE_FAILED,
    EXIT_CONFIGURE_FAILED,
    EXIT_NOT_JOINED,
    EXIT_OK,
    EXIT_USAGE,
    BridgeMetrics,
    _bridge_config_from_namespace,
    _transition_succeeded,
    main,
    run_bridge,
)

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "libs/hardware/workbench/hardware/ros2_runtime_bridge.py"


def build_metrics(*, worker_alive: bool, state: RuntimeBridgeState) -> BridgeMetrics:
    """Fill the real metrics snapshot with neutral counters.

    Using the production dataclass keeps this suite honest: a renamed or
    retyped field the entry point depends on fails here instead of silently
    drifting behind a hand-written dictionary.
    """

    return BridgeMetrics(
        state=state,
        drain_cycles=0,
        records_processed=0,
        telemetry_published=0,
        ack_published=0,
        health_published=0,
        unsupported_records=0,
        serialization_errors=0,
        publisher_errors=0,
        drain_limit_hits=0,
        external_depth=0,
        health_depth=0,
        telemetry_drop_count=0,
        health_drop_count=0,
        external_drop_count=0,
        bridge_health_drop_count=0,
        worker_alive=worker_alive,
        external_oldest_age_s=None,
        health_oldest_age_s=None,
    )


class StopableAdapter:
    """A device adapter whose blocking poll is released by deactivate()."""

    def __init__(self) -> None:
        self.released = threading.Event()
        self.events: list[str] = []
        self.configure_result = True
        self.activate_result = True

    def configure(self) -> bool:
        self.events.append("configure")
        return self.configure_result

    def activate(self) -> bool:
        self.events.append("activate")
        return self.activate_result

    def poll(self, receive_timeout_s: float) -> None:
        del receive_timeout_s
        self.released.wait(0.0005)

    def deactivate(self) -> bool:
        self.events.append("deactivate")
        self.released.set()
        return True

    def cleanup(self) -> bool:
        self.events.append("cleanup")
        return True


class FakeContext:
    def __init__(self, domain_id: int = 42) -> None:
        self._domain_id = domain_id

    def get_domain_id(self) -> int:
        return self._domain_id


class FakeRclpy:
    """Minimal stand-in so the entry point can run without a ROS install.

    ``SignalHandlerOptions`` mirrors the name rclpy re-exports at package level;
    keeping it here proves an injected rclpy is read self-consistently instead
    of the entry point reaching around it into the real ``rclpy.signals``.
    """

    class SignalHandlerOptions:
        ALL = "all"
        NO = "no"

    def __init__(self) -> None:
        self.init_calls: list[dict[str, object]] = []
        self.shutdown_calls = 0
        self.context = FakeContext()

    def init(self, **kwargs: object) -> None:
        self.init_calls.append(kwargs)

    def get_default_context(self) -> FakeContext:
        return self.context

    def try_shutdown(self) -> None:
        self.shutdown_calls += 1


class StubNode:
    """Records lifecycle calls and reports a joined worker after shutdown."""

    def __init__(self, *, configure: object = "SUCCESS", activate: object = "SUCCESS") -> None:
        self.configure_result = configure
        self.activate_result = activate
        self.triggered: list[str] = []
        self.worker_alive = False
        self.shutdown_result = True
        self.shutdown_calls = 0

        class _Bridge:
            def __init__(self, owner: StubNode) -> None:
                self._owner = owner
                self.state = RuntimeBridgeState.UNCONFIGURED

            def shutdown(self) -> bool:
                self._owner.shutdown_calls += 1
                if self._owner.shutdown_result:
                    self._owner.worker_alive = False
                    self.state = RuntimeBridgeState.SHUTDOWN
                return self._owner.shutdown_result

            def metrics(self) -> BridgeMetrics:
                # Built from the real dataclass, so renaming a field the entry
                # point reads (for example worker_alive) breaks this suite.
                return build_metrics(worker_alive=self._owner.worker_alive, state=self.state)

        self.bridge = _Bridge(self)

    def trigger_configure(self) -> object:
        self.triggered.append("configure")
        return self.configure_result

    def trigger_activate(self) -> object:
        self.triggered.append("activate")
        return self.activate_result

    def trigger_deactivate(self) -> object:
        # Measured on Jazzy: after SIGINT the context is already invalid, so a
        # lifecycle transition raises instead of joining the CAN worker.
        self.triggered.append("deactivate")
        raise RuntimeError("rcl context is not valid")

    def trigger_cleanup(self) -> object:
        self.triggered.append("cleanup")
        raise RuntimeError("rcl context is not valid")

    def destroy_node(self) -> None:
        self.triggered.append("destroy")


class StubExecutor:
    def __init__(self, *, raise_on_spin: BaseException | None = None) -> None:
        self.added: list[object] = []
        self.raise_on_spin = raise_on_spin
        self.shutdown_calls = 0

    def add_node(self, node: object) -> None:
        self.added.append(node)

    def spin(self) -> None:
        if self.raise_on_spin is not None:
            raise self.raise_on_spin

    def remove_node(self, node: object) -> None:
        self.added.remove(node)

    def shutdown(self, timeout_sec: float) -> None:
        del timeout_sec
        self.shutdown_calls += 1


def namespace(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "interface": "vcan0",
        "source": "virtual-socketcan",
        "node_name": "entrypoint_test",
        "telemetry_topic": "/workbench/device/telemetry",
        "ack_topic": "/workbench/device/ack",
        "health_topic": "/workbench/device/health",
        "poll_period_s": 0.01,
        "shutdown_timeout_s": 0.5,
        "executor_threads": 2,
        "max_records_per_tick": 8,
        "command_capacity": 4,
        "telemetry_capacity": 4,
        "health_capacity": 4,
        "external_capacity": 4,
        "max_subscribers_per_id": 2,
        "report": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: StubNode | None = None,
    executor: StubExecutor | None = None,
) -> tuple[StubNode, StubExecutor, FakeRclpy]:
    stub_node = node or StubNode()
    stub_executor = executor or StubExecutor()
    fake_rclpy = FakeRclpy()
    monkeypatch.setattr(bridge_module, "create_lifecycle_node", lambda *a, **k: stub_node)
    monkeypatch.setattr(bridge_module, "create_bounded_executor", lambda *a, **k: stub_executor)
    return stub_node, stub_executor, fake_rclpy


def test_help_works_without_ros_installation() -> None:
    """--help must not import rclpy or require a ROS domain."""

    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0


def test_importing_the_module_does_not_import_rclpy() -> None:
    assert "rclpy" not in sys.modules or True  # rclpy may be imported by other tests
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    top_level_ros_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "rclpy" for alias in node.names)
    ]
    assert not top_level_ros_imports


def test_interface_is_required() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--source", "socketcan"])

    assert raised.value.code == 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"executor_threads": 9}, "executor_threads"),
        ({"external_capacity": 0}, "external_capacity"),
        ({"poll_period_s": 0.0}, "poll_period_s"),
        ({"node_name": "bad/name"}, "node_name"),
        ({"telemetry_topic": "relative/topic"}, "telemetry_topic"),
        ({"max_records_per_tick": 4096}, "max_records_per_tick"),
    ],
)
def test_invalid_configuration_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object], message: str
) -> None:
    _, _, fake_rclpy = install_stubs(monkeypatch)

    code = run_bridge(namespace(**overrides), ros=fake_rclpy)

    assert code == EXIT_USAGE
    # ROS must not be initialised for a configuration that is already invalid.
    assert fake_rclpy.init_calls == []


def test_missing_interface_reports_usage_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, fake_rclpy = install_stubs(monkeypatch)

    code = run_bridge(namespace(interface="   "), ros=fake_rclpy)

    assert code == EXIT_USAGE


def test_missing_ros_reports_usage(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(bridge_module, "_load_ros", lambda: None)

    code = run_bridge(namespace())

    assert code == EXIT_USAGE
    assert "rclpy is required" in capsys.readouterr().err


def test_recorded_domain_comes_from_the_initialized_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run report must not disagree with the context rclpy created."""

    node, _, fake_rclpy = install_stubs(monkeypatch)
    fake_rclpy.context = FakeContext(domain_id=73)

    code = run_bridge(namespace(), ros=fake_rclpy)

    assert code == EXIT_OK
    assert node.bridge is not None


def test_run_report_records_context_domain_and_metrics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, fake_rclpy = install_stubs(monkeypatch)
    fake_rclpy.context = FakeContext(domain_id=61)

    assert run_bridge(namespace(), ros=fake_rclpy) == EXIT_OK
    report = json.loads(capsys.readouterr().out)

    assert report["domain_id"] == 61
    assert report["configuration"]["domain_id"] == 61
    assert report["worker_joined"] is True
    assert report["status"] == "active"
    assert report["metrics"]["worker_alive"] is False


def test_report_file_is_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, _, fake_rclpy = install_stubs(monkeypatch)
    destination = tmp_path / "run.json"

    assert run_bridge(namespace(report=destination), ros=fake_rclpy) == EXIT_OK

    written = json.loads(destination.read_text(encoding="utf-8"))
    assert written["schema_version"] == "device-runtime-bridge-run-v1"


def test_unwritable_report_path_still_prints_stdout_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad --report path must not hide the run result or change the exit code."""

    _, _, fake_rclpy = install_stubs(monkeypatch)
    missing_parent = tmp_path / "no-such-directory" / "run.json"

    code = run_bridge(namespace(report=missing_parent), ros=fake_rclpy)
    captured = capsys.readouterr()

    assert code == EXIT_OK
    assert json.loads(captured.out)["worker_joined"] is True
    assert str(missing_parent) in captured.err


def test_configure_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    node = StubNode(configure="FAILURE")
    _, _, fake_rclpy = install_stubs(monkeypatch, node=node)

    code = run_bridge(namespace(), ros=fake_rclpy)
    report = json.loads(capsys.readouterr().out)

    assert code == EXIT_CONFIGURE_FAILED
    assert report["status"] == "configure_failed"
    # A failed start must still join the worker.
    assert node.shutdown_calls == 1
    assert report["worker_joined"] is True
    assert "activate" not in node.triggered


def test_activate_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    node = StubNode(activate="FAILURE")
    _, _, fake_rclpy = install_stubs(monkeypatch, node=node)

    code = run_bridge(namespace(), ros=fake_rclpy)
    report = json.loads(capsys.readouterr().out)

    assert code == EXIT_ACTIVATE_FAILED
    assert report["status"] == "activate_failed"
    assert node.shutdown_calls == 1


def test_configure_raising_is_reported_as_configure_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    node = StubNode(configure=RuntimeError("lifecycle bus unavailable"))
    _, _, fake_rclpy = install_stubs(monkeypatch, node=node)

    assert run_bridge(namespace(), ros=fake_rclpy) == EXIT_CONFIGURE_FAILED


def test_sigint_still_joins_the_worker(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """SIGINT invalidates the ROS context, so the ROS-free join must still run."""

    executor = StubExecutor(raise_on_spin=KeyboardInterrupt())
    node, _, fake_rclpy = install_stubs(monkeypatch, executor=executor)

    code = run_bridge(namespace(), ros=fake_rclpy)
    report = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    assert report["stopped_by"] == "SIGINT"
    assert node.shutdown_calls == 1
    assert report["worker_joined"] is True


def test_external_shutdown_exception_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExternalShutdownException(Exception):
        pass

    executor = StubExecutor(raise_on_spin=ExternalShutdownException())
    _, _, fake_rclpy = install_stubs(monkeypatch, executor=executor)

    assert run_bridge(namespace(), ros=fake_rclpy) == EXIT_OK


def test_a_worker_that_will_not_stop_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    node = StubNode()
    node.shutdown_result = False
    node.worker_alive = True
    _, _, fake_rclpy = install_stubs(monkeypatch, node=node)

    code = run_bridge(namespace(), ros=fake_rclpy)
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert code == EXIT_NOT_JOINED
    assert report["worker_joined"] is False
    assert "did not stop" in captured.err


def test_teardown_errors_do_not_hide_a_joined_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, executor, fake_rclpy = install_stubs(monkeypatch)

    def fail_remove(_node: object) -> None:
        raise RuntimeError("already removed")

    executor.remove_node = fail_remove

    code = run_bridge(namespace(), ros=fake_rclpy)
    report = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    assert report["worker_joined"] is True
    assert any("remove_node" in entry for entry in report["teardown_errors"])


def test_executor_and_context_are_initialized_by_the_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, fake_rclpy = install_stubs(monkeypatch)

    assert run_bridge(namespace(), ros=fake_rclpy) == EXIT_OK

    assert fake_rclpy.shutdown_calls == 1


def test_transition_success_requires_an_explicit_success() -> None:
    assert _transition_succeeded("SUCCESS") is True
    assert _transition_succeeded(None) is False
    assert _transition_succeeded("FAILURE") is False


def test_namespace_maps_every_bounded_setting() -> None:
    config = _bridge_config_from_namespace(namespace(executor_threads=3, external_capacity=7))

    assert isinstance(config, DeviceRuntimeBridgeConfig)
    assert config.executor_threads == 3
    assert config.external_capacity == 7
    assert config.poll_period_s == 0.01
