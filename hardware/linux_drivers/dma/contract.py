"""Bounded DMA model for software ownership and recovery tests.

The fake engine models the invariants a physical Linux DMA driver must keep:
preallocated buffers, zero-copy descriptor submission, explicit CPU/DMA
ownership, bounded descriptor capacity, and fail-closed recovery. It does not
model a controller register layout or claim physical throughput.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum

MAX_BUFFER_BYTES = 1024 * 1024
MAX_DESCRIPTORS = 1024


class DMAError(ValueError):
    """Base class for invalid DMA configuration or operations."""


class DMAOwnershipError(DMAError):
    """A buffer or descriptor is used by the wrong owner or lifecycle state."""


class DMAStateError(DMAError):
    """The engine state does not permit the requested operation."""


class DMABackpressure(TimeoutError):
    """The fixed descriptor pool is full."""


class DMAProviderClosed(ConnectionError):
    """The DMA engine has been closed."""


class BufferOwner(StrEnum):
    CPU = "cpu"
    DMA = "dma"
    FREE = "free"


class DMAState(StrEnum):
    READY = "ready"
    HALTED = "halted"
    CLOSED = "closed"


class DMAStatus(StrEnum):
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class DMABuffer:
    """Preallocated storage whose access is constrained by ``owner``."""

    buffer_id: int
    capacity: int
    _data: bytearray
    owner: BufferOwner = BufferOwner.FREE


@dataclass(frozen=True, slots=True)
class DMADescriptor:
    descriptor_id: int
    buffer_id: int
    length: int
    status: DMAStatus | None = None


@dataclass(frozen=True, slots=True)
class DMACompletion:
    descriptor_id: int
    buffer_id: int
    status: DMAStatus
    bytes_transferred: int
    error: str | None = None


class FakeDMAProvider:
    """Thread-safe bounded DMA engine with explicit ownership transitions."""

    def __init__(self, *, buffer_capacity: int = 8, descriptor_capacity: int = 8) -> None:
        if type(buffer_capacity) is not int or not 1 <= buffer_capacity <= MAX_DESCRIPTORS:
            raise DMAError("buffer_capacity must be between 1 and 1024")
        if type(descriptor_capacity) is not int or not 1 <= descriptor_capacity <= MAX_DESCRIPTORS:
            raise DMAError("descriptor_capacity must be between 1 and 1024")
        self._buffer_capacity = buffer_capacity
        self._descriptor_capacity = descriptor_capacity
        self._buffers: dict[int, DMABuffer] = {}
        self._descriptors: dict[int, DMADescriptor] = {}
        self._active: list[int] = []
        self._completions: list[DMACompletion] = []
        self._next_buffer_id = 1
        self._next_descriptor_id = 1
        self._state = DMAState.READY
        self._lock = threading.RLock()

    @property
    def state(self) -> DMAState:
        with self._lock:
            return self._state

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    @property
    def completion_count(self) -> int:
        with self._lock:
            return len(self._completions)

    def allocate(self, capacity: int) -> DMABuffer:
        with self._lock:
            self._ensure_ready()
            if type(capacity) is not int or not 1 <= capacity <= MAX_BUFFER_BYTES:
                raise DMAError(f"buffer capacity must be between 1 and {MAX_BUFFER_BYTES}")
            if len(self._buffers) >= self._buffer_capacity:
                raise DMABackpressure("preallocated DMA buffer pool is full")
            buffer = DMABuffer(self._next_buffer_id, capacity, bytearray(capacity), BufferOwner.CPU)
            self._next_buffer_id += 1
            self._buffers[buffer.buffer_id] = buffer
            return buffer

    def write(self, buffer: DMABuffer, data: bytes) -> None:
        with self._lock:
            self._ensure_ready()
            owned = self._owned_buffer(buffer)
            if owned.owner is not BufferOwner.CPU:
                raise DMAOwnershipError("CPU may write only buffers owned by CPU")
            if not isinstance(data, bytes) or len(data) > owned.capacity:
                raise DMAError("data must be bytes no larger than the buffer capacity")
            owned._data[: len(data)] = data

    def read(self, buffer: DMABuffer, length: int | None = None) -> bytes:
        with self._lock:
            self._ensure_open()
            owned = self._owned_buffer(buffer)
            if owned.owner is not BufferOwner.CPU:
                raise DMAOwnershipError("CPU may read only buffers owned by CPU")
            size = owned.capacity if length is None else length
            if type(size) is not int or not 0 <= size <= owned.capacity:
                raise DMAError("read length is outside the buffer capacity")
            return bytes(owned._data[:size])

    def submit(self, buffer: DMABuffer, length: int) -> DMADescriptor:
        with self._lock:
            self._ensure_ready()
            owned = self._owned_buffer(buffer)
            if owned.owner is not BufferOwner.CPU:
                raise DMAOwnershipError("buffer must be CPU-owned before submission")
            if type(length) is not int or not 1 <= length <= owned.capacity:
                raise DMAError("descriptor length must be within the buffer capacity")
            if len(self._descriptors) >= self._descriptor_capacity:
                raise DMABackpressure("DMA descriptor ring is full")
            descriptor = DMADescriptor(self._next_descriptor_id, owned.buffer_id, length)
            self._next_descriptor_id += 1
            owned.owner = BufferOwner.DMA
            self._descriptors[descriptor.descriptor_id] = descriptor
            self._active.append(descriptor.descriptor_id)
            return descriptor

    def complete_next(self, *, status: DMAStatus = DMAStatus.COMPLETE, error: str | None = None) -> DMACompletion:
        with self._lock:
            self._ensure_open()
            if not self._active:
                raise DMAStateError("no active DMA descriptor")
            if not isinstance(status, DMAStatus) or status is DMAStatus.CANCELLED:
                raise DMAError("complete_next accepts COMPLETE or ERROR")
            if status is DMAStatus.ERROR and (not isinstance(error, str) or not error):
                raise DMAError("DMA error completion requires a non-empty error")
            descriptor_id = self._active.pop(0)
            descriptor = self._descriptors[descriptor_id]
            buffer = self._buffers[descriptor.buffer_id]
            buffer.owner = BufferOwner.CPU
            completed = DMACompletion(
                descriptor_id,
                descriptor.buffer_id,
                status,
                descriptor.length if status is DMAStatus.COMPLETE else 0,
                error,
            )
            self._descriptors[descriptor_id] = DMADescriptor(
                descriptor.descriptor_id, descriptor.buffer_id, descriptor.length, status
            )
            self._completions.append(completed)
            if status is DMAStatus.ERROR:
                self._state = DMAState.HALTED
            return completed

    def cancel_pending(self) -> list[DMACompletion]:
        with self._lock:
            self._ensure_open()
            cancelled: list[DMACompletion] = []
            while self._active:
                descriptor_id = self._active.pop(0)
                descriptor = self._descriptors[descriptor_id]
                self._buffers[descriptor.buffer_id].owner = BufferOwner.CPU
                completion = DMACompletion(descriptor_id, descriptor.buffer_id, DMAStatus.CANCELLED, 0, "cancelled")
                self._descriptors[descriptor_id] = DMADescriptor(
                    descriptor.descriptor_id, descriptor.buffer_id, descriptor.length, DMAStatus.CANCELLED
                )
                self._completions.append(completion)
                cancelled.append(completion)
            return cancelled

    def poll_completion(self) -> DMACompletion | None:
        with self._lock:
            self._ensure_open()
            return self._completions.pop(0) if self._completions else None

    def recycle(self, descriptor_id: int) -> DMABuffer:
        with self._lock:
            self._ensure_open()
            try:
                descriptor = self._descriptors[descriptor_id]
            except KeyError as exc:
                raise DMAError(f"unknown descriptor: {descriptor_id}") from exc
            if descriptor.status is None:
                raise DMAOwnershipError("descriptor is still owned by DMA")
            buffer = self._buffers[descriptor.buffer_id]
            if buffer.owner is not BufferOwner.CPU:
                raise DMAOwnershipError("completed descriptor buffer is not CPU-owned")
            buffer.owner = BufferOwner.FREE
            del self._descriptors[descriptor_id]
            return buffer

    def recover(self) -> None:
        with self._lock:
            if self._state is DMAState.CLOSED:
                raise DMAProviderClosed("DMA provider is closed")
            if self._state is not DMAState.HALTED:
                raise DMAStateError("DMA recovery is only valid after an error")
            if self._active:
                raise DMAStateError("cancel active descriptors before recovery")
            self._state = DMAState.READY

    def close(self) -> None:
        with self._lock:
            if self._state is DMAState.CLOSED:
                return
            self.cancel_pending()
            self._state = DMAState.CLOSED

    def _owned_buffer(self, buffer: DMABuffer) -> DMABuffer:
        if not isinstance(buffer, DMABuffer) or self._buffers.get(buffer.buffer_id) is not buffer:
            raise DMAOwnershipError("buffer does not belong to this DMA provider")
        return buffer

    def _ensure_open(self) -> None:
        if self._state is DMAState.CLOSED:
            raise DMAProviderClosed("DMA provider is closed")

    def _ensure_ready(self) -> None:
        self._ensure_open()
        if self._state is not DMAState.READY:
            raise DMAStateError(f"DMA provider is {self._state.value}")
