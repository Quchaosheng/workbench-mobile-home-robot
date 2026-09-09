import sys
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


def test_close_clears_pending_events_and_rejects_future_access() -> None:
    gpio = provider()
    gpio.inject_input("status", False, 0)
    gpio.close()
    assert gpio.event_count == 0
    with pytest.raises(GPIOProviderClosed):
        gpio.read_event()
    with pytest.raises(GPIOProviderClosed):
        gpio.write("enable", True)


def test_config_rejects_edge_subscription_on_output() -> None:
    with pytest.raises(GPIOError, match="output lines"):
        GPIOConfig("enable", GPIODirection.OUTPUT, edge=Edge.RISING)
