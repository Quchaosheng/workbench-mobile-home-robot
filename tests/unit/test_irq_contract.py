import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "hardware" / "linux_drivers"))

from irq import FakeIRQProvider, IRQError, IRQHandlerTimeout, IRQLine, IRQNotShared, IRQState


def test_irq_lifecycle_routes_top_half_and_bottom_half_work() -> None:
    irq = FakeIRQProvider(IRQLine(42, shared=False, priority=20))
    irq.register("uart0")
    irq.enable()

    work = irq.trigger("uart0")
    assert work.owner == "uart0"
    assert irq.active_count == 1
    assert irq.pending_work == 1
    irq.complete_top_half(work)
    with pytest.raises(IRQError, match="not active"):
        irq.complete_top_half(work)
    irq.run_bottom_half(work)
    assert irq.active_count == 0
    assert irq.pending_work == 0
    irq.stop()
    assert irq.state is IRQState.DISABLED


def test_shared_irq_requires_registered_owner_and_preserves_owner_identity() -> None:
    irq = FakeIRQProvider(IRQLine(43, shared=True))
    irq.register("spi0")
    irq.register("gpio-exp")
    irq.enable()

    with pytest.raises(IRQNotShared):
        irq.trigger("missing")
    first = irq.trigger("spi0")
    second = irq.trigger("gpio-exp")
    assert (first.owner, second.owner) == ("spi0", "gpio-exp")
    with pytest.raises(IRQError, match="before top-half"):
        irq.run_bottom_half(first)
    irq.complete_top_half(first)
    irq.complete_top_half(second)
    irq.run_bottom_half(first)
    irq.run_bottom_half(second)


def test_stop_cancels_pending_bottom_half_and_waits_for_active_handler() -> None:
    irq = FakeIRQProvider(IRQLine(44))
    irq.register("can0")
    irq.enable()
    work = irq.trigger("can0")
    stop_result: list[Exception | None] = []

    def stop() -> None:
        try:
            irq.stop(timeout_s=1.0)
        except IRQError as exc:  # pragma: no cover - assertion captures this path
            stop_result.append(exc)

    thread = threading.Thread(target=stop)
    thread.start()
    deadline = time.monotonic() + 1.0
    while irq.state is not IRQState.STOPPING and time.monotonic() < deadline:
        time.sleep(0.001)
    assert irq.state is IRQState.STOPPING
    assert thread.is_alive()
    irq.complete_top_half(work)
    thread.join(1.0)
    assert not thread.is_alive()
    assert not stop_result
    assert irq.pending_work == 0
    with pytest.raises(IRQError):
        irq.run_bottom_half(work)


def test_stop_timeout_is_an_error_and_does_not_claim_cleanup() -> None:
    irq = FakeIRQProvider(IRQLine(45))
    irq.register("gpio")
    irq.enable()
    work = irq.trigger("gpio")

    with pytest.raises(IRQHandlerTimeout):
        irq.stop(timeout_s=0.0)
    assert irq.state is IRQState.STOPPING
    irq.complete_top_half(work)
    irq.stop(timeout_s=1.0)


def test_work_queue_backpressure_and_closed_lifecycle_fail_closed() -> None:
    irq = FakeIRQProvider(IRQLine(46), work_capacity=1)
    irq.register("spi")
    irq.enable()
    work = irq.trigger("spi")
    with pytest.raises(IRQError, match="queue is full"):
        irq.trigger("spi")
    irq.complete_top_half(work)
    irq.cancel_work()
    irq.close()
    assert irq.state is IRQState.CLOSED
    with pytest.raises(IRQError, match="closed"):
        irq.enable()


def test_cancel_work_preserves_active_top_half_until_completion() -> None:
    irq = FakeIRQProvider(IRQLine(47))
    irq.register("uart")
    irq.enable()
    work = irq.trigger("uart")

    assert irq.cancel_work() == 0
    assert irq.active_count == 1
    assert irq.pending_work == 1
    irq.complete_top_half(work)
    assert irq.cancel_work() == 1
    assert irq.active_count == 0
    assert irq.pending_work == 0
