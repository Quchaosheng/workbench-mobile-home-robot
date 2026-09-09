import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "hardware" / "linux_drivers"))

from dma import (
    BufferOwner,
    DMABackpressure,
    DMAError,
    DMAOwnershipError,
    DMAProviderClosed,
    DMAState,
    DMAStateError,
    DMAStatus,
    FakeDMAProvider,
)


def test_preallocated_buffer_is_cpu_owned_and_submit_transfers_ownership() -> None:
    dma = FakeDMAProvider()
    buffer = dma.allocate(64)
    dma.write(buffer, b"hello")
    descriptor = dma.submit(buffer, 5)

    assert buffer.owner is BufferOwner.DMA
    with pytest.raises(DMAOwnershipError, match="CPU may read"):
        dma.read(buffer, 5)

    completion = dma.complete_next()
    assert completion.descriptor_id == descriptor.descriptor_id
    assert completion.bytes_transferred == 5
    assert buffer.owner is BufferOwner.CPU
    assert dma.read(buffer, 5) == b"hello"
    assert dma.recycle(descriptor.descriptor_id) is buffer
    assert buffer.owner is BufferOwner.FREE


def test_descriptor_ring_is_bounded_and_recycled() -> None:
    dma = FakeDMAProvider(buffer_capacity=2, descriptor_capacity=1)
    first = dma.allocate(8)
    second = dma.allocate(8)
    dma.write(first, b"one")
    dma.write(second, b"two")
    descriptor = dma.submit(first, 3)

    with pytest.raises(DMABackpressure, match="descriptor ring"):
        dma.submit(second, 3)
    dma.complete_next()
    dma.recycle(descriptor.descriptor_id)
    dma.submit(second, 3)


def test_cancel_returns_all_inflight_buffers_to_cpu_and_records_completions() -> None:
    dma = FakeDMAProvider()
    first = dma.allocate(8)
    second = dma.allocate(8)
    dma.write(first, b"one")
    dma.write(second, b"two")
    first_descriptor = dma.submit(first, 3)
    second_descriptor = dma.submit(second, 3)

    cancelled = dma.cancel_pending()

    assert [item.descriptor_id for item in cancelled] == [
        first_descriptor.descriptor_id,
        second_descriptor.descriptor_id,
    ]
    assert all(item.status is DMAStatus.CANCELLED for item in cancelled)
    assert first.owner is BufferOwner.CPU
    assert second.owner is BufferOwner.CPU
    assert dma.active_count == 0


def test_error_halts_engine_and_recovery_requires_cancelled_active_work() -> None:
    dma = FakeDMAProvider()
    buffer = dma.allocate(8)
    dma.write(buffer, b"fault")
    descriptor = dma.submit(buffer, 5)

    completion = dma.complete_next(status=DMAStatus.ERROR, error="bus fault")
    assert completion.status is DMAStatus.ERROR
    assert dma.state is DMAState.HALTED
    with pytest.raises(DMAStateError, match="halted"):
        dma.allocate(8)
    dma.recover()
    assert dma.state is DMAState.READY
    dma.recycle(descriptor.descriptor_id)


def test_recovery_rejects_remaining_active_descriptors() -> None:
    dma = FakeDMAProvider()
    first = dma.allocate(8)
    second = dma.allocate(8)
    dma.write(first, b"fault")
    dma.write(second, b"active")
    first_descriptor = dma.submit(first, 5)
    dma.submit(second, 6)
    dma.complete_next(status=DMAStatus.ERROR, error="bus fault")

    with pytest.raises(DMAStateError, match="active descriptors"):
        dma.recover()
    dma.cancel_pending()
    dma.recover()
    dma.recycle(first_descriptor.descriptor_id)


def test_recovery_rejects_active_descriptors_and_close_cancels_then_closes() -> None:
    dma = FakeDMAProvider()
    buffer = dma.allocate(8)
    dma.write(buffer, b"active")
    dma.submit(buffer, 6)
    dma.complete_next(status=DMAStatus.ERROR, error="fault")
    assert dma.state is DMAState.HALTED
    dma.close()
    assert dma.state is DMAState.CLOSED
    with pytest.raises(DMAProviderClosed):
        dma.read(buffer, 6)
    with pytest.raises(DMAProviderClosed):
        dma.recover()


def test_invalid_buffers_lengths_and_external_ownership_fail_closed() -> None:
    dma = FakeDMAProvider()
    buffer = dma.allocate(8)
    with pytest.raises(DMAError):
        dma.write(buffer, b"too large" * 2)
    with pytest.raises(DMAStateError, match="no active"):
        dma.complete_next()
    foreign = FakeDMAProvider().allocate(8)
    with pytest.raises(DMAOwnershipError, match="does not belong"):
        dma.submit(foreign, 1)
