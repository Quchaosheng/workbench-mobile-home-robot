import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "hardware" / "linux_drivers"))

from gpio import (
    Edge,
    FakeGPIOProvider,
    GPIOConfig,
    GPIODirection,
    GPIOError,
    GPIOEvent,
    GPIOPermissionError,
    GPIOProviderClosed,
    GPIOQueueFull,
    GPIOStateError,
)


def provider(*, capacity: int = 4) -> FakeGPIOProvider:
    return FakeGPIOProvider(
        [
            GPIOConfig("status", GPIODirection.INPUT, edge=Edge.BOTH, debounce_ns=10),
            GPIOConfig("enable", GPIODirection.OUTPUT, active_high=True),
        ],
        event_capacity=capacity,
    )


def test_output_starts_inactive_and_input_is_unknown_until_observed() -> None:
    gpio = provider()
    with pytest.raises(GPIOStateError, match="unknown"):
        gpio.read("status")
    with pytest.raises(GPIOPermissionError, match="not an input"):
        gpio.read("enable")
    with pytest.raises(GPIOPermissionError, match="not an output"):
        gpio.write("status", True)
    gpio.write("enable", True)
    assert gpio._values["enable"] is True


def test_active_low_outputs_and_inputs_use_logical_active_values() -> None:
    gpio = FakeGPIOProvider(
        [
            GPIOConfig("active_low_input", GPIODirection.INPUT, active_high=False, edge=Edge.BOTH),
            GPIOConfig("active_low_output", GPIODirection.OUTPUT, active_high=False),
        ]
    )

    gpio.write("active_low_output", True)
    assert gpio._values["active_low_output"] is False
    gpio.inject_input("active_low_input", True, 0)
    assert gpio.read("active_low_input") is False
    event = gpio.inject_input("active_low_input", False, 1)
    assert event == GPIOEvent("active_low_input", 0, True, 1)


def test_input_edges_are_debounced_and_queued_with_monotonic_sequence() -> None:
    gpio = provider()
    assert gpio.inject_input("status", False, 100) is None
    assert gpio.inject_input("status", True, 105) is None
    event = gpio.inject_input("status", False, 120)
    assert event is not None
    assert event == GPIOEvent("status", 0, False, 120)
    assert gpio.read_event() == event
    assert gpio.read_event() is None


def test_timestamp_rollback_unknown_line_and_invalid_lines_fail_closed() -> None:
    gpio = provider()
    gpio.inject_input("status", False, 10)
    with pytest.raises(GPIOStateError, match="increase strictly"):
        gpio.inject_input("status", True, 10)
    with pytest.raises(GPIOStateError, match="increase strictly"):
        gpio.inject_input("status", True, 9)
    with pytest.raises(GPIOError, match="unknown GPIO"):
        gpio.configure("missing")
    with pytest.raises(GPIOError, match="line name"):
        GPIOConfig("", GPIODirection.INPUT)


def test_event_queue_backpressure_is_explicit() -> None:
    gpio = provider(capacity=1)
    gpio.inject_input("status", False, 0)
    gpio.inject_input("status", True, 10)
    with pytest.raises(GPIOQueueFull, match="full"):
        gpio.inject_input("status", False, 20)

    assert gpio.read("status") is True
    assert gpio._last_timestamp["status"] == 10
    assert gpio.event_count == 1
    assert gpio.read_event() == GPIOEvent("status", 0, True, 10)
    assert gpio.inject_input("status", False, 20) == GPIOEvent("status", 1, False, 20)


def test_close_clears_pending_events_and_rejects_future_access() -> None:
    gpio = provider()
    gpio.inject_input("status", False, 0)
    gpio.close()
    assert gpio.event_count == 0
    with pytest.raises(GPIOProviderClosed):
        gpio.read_event()
    with pytest.raises(GPIOProviderClosed):
        gpio.write("enable", True)


def test_public_configure_waits_for_provider_lock() -> None:
    gpio = provider()
    lock_held = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def hold_provider_lock() -> None:
        with gpio._lock:
            lock_held.set()
            release.wait(1.0)

    def configure_line() -> None:
        gpio.configure("status")
        completed.set()

    holder = threading.Thread(target=hold_provider_lock)
    reader = threading.Thread(target=configure_line)
    holder.start()
    assert lock_held.wait(1.0)
    reader.start()
    assert not completed.wait(0.05)
    release.set()
    holder.join(1.0)
    reader.join(1.0)
    assert completed.is_set()


def test_config_rejects_edge_subscription_on_output() -> None:
    with pytest.raises(GPIOError, match="output lines"):
        GPIOConfig("enable", GPIODirection.OUTPUT, edge=Edge.RISING)
