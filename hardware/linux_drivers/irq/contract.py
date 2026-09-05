"""Bounded IRQ/top-half/bottom-half lifecycle model for software tests."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic


class IRQError(RuntimeError):
    """Base class for invalid IRQ operations."""


class IRQHandlerTimeout(TimeoutError, IRQError):
    """An active handler or bottom-half did not stop before the deadline."""


class IRQNotShared(IRQError):
    """A shared line was triggered without a registered owner."""


class IRQWorkCancelled(IRQError):
    """Work was cancelled before execution."""


class IRQState(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    STOPPING = "stopping"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class IRQLine:
    number: int
    shared: bool = False
    priority: int = 0

    def __post_init__(self) -> None:
        if type(self.number) is not int or self.number < 0:
            raise IRQError("IRQ number must be a non-negative integer")
        if type(self.shared) is not bool:
            raise IRQError("shared must be boolean")
        if type(self.priority) is not int or not 0 <= self.priority <= 99:
            raise IRQError("priority must be between 0 and 99")


@dataclass(frozen=True, slots=True)
class IRQWork:
    sequence: int
    owner: str
    line: int


class FakeIRQProvider:
    """Thread-safe IRQ router with explicit top/bottom-half ownership."""

    def __init__(self, line: IRQLine, *, work_capacity: int = 16) -> None:
        if type(work_capacity) is not int or not 1 <= work_capacity <= 1024:
            raise IRQError("work_capacity must be between 1 and 1024")
        self.line = line
        self.work_capacity = work_capacity
        self._state = IRQState.DISABLED
        self._handlers: set[str] = set()
        self._works: list[IRQWork] = []
        self._active_work: set[int] = set()
        self._next_sequence = 0
        self._lock = threading.RLock()
        self._active_changed = threading.Condition(self._lock)

    @property
    def state(self) -> IRQState:
        with self._lock:
            return self._state

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active_work)

    @property
    def pending_work(self) -> int:
        with self._lock:
            return len(self._works)

    def register(self, owner: str) -> None:
        with self._lock:
            self._ensure_not_closed()
            if not owner or owner in self._handlers:
                raise IRQError("IRQ owner must be unique and non-empty")
            if not self.line.shared and self._handlers:
                raise IRQError("non-shared IRQ line accepts one owner")
            self._handlers.add(owner)

    def enable(self) -> None:
        with self._lock:
            self._ensure_not_closed()
            if not self._handlers:
                raise IRQNotShared("cannot enable an IRQ without an owner")
            if self._state is IRQState.STOPPING:
                raise IRQError("cannot enable an IRQ while stopping")
            self._state = IRQState.ENABLED

    def trigger(self, owner: str) -> IRQWork:
        """Confirm an interrupt in the top-half and enqueue one bottom-half work item."""
        with self._lock:
            self._ensure_not_closed()
            if self._state is not IRQState.ENABLED:
                raise IRQError(f"IRQ is {self._state.value}")
            if owner not in self._handlers:
                raise IRQNotShared(f"IRQ owner is not registered: {owner}")
            if len(self._works) >= self.work_capacity:
                raise IRQError("IRQ bottom-half queue is full")
            work = IRQWork(self._next_sequence, owner, self.line.number)
            self._next_sequence += 1
            self._works.append(work)
            self._active_work.add(work.sequence)
            return work

    def complete_top_half(self, work: IRQWork) -> None:
        with self._lock:
            if work not in self._works or work.sequence not in self._active_work:
                raise IRQError("IRQ top-half work is not active")
            self._active_work.remove(work.sequence)
            self._active_changed.notify_all()

    def run_bottom_half(self, work: IRQWork) -> None:
        with self._lock:
            if work not in self._works:
                raise IRQWorkCancelled("IRQ work is not pending")
            if work.sequence in self._active_work:
                raise IRQError("IRQ bottom-half cannot run before top-half completion")
            self._works.remove(work)

    def stop(self, *, timeout_s: float = 1.0) -> None:
        with self._lock:
            self._ensure_not_closed()
            self._state = IRQState.STOPPING
            deadline = monotonic() + timeout_s
            while self._active_work:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise IRQHandlerTimeout("active IRQ handler did not stop before deadline")
                self._active_changed.wait(remaining)
            self._works.clear()
            self._state = IRQState.DISABLED

    def cancel_work(self) -> int:
        with self._lock:
            cancellable = [work for work in self._works if work.sequence not in self._active_work]
            count = len(cancellable)
            self._works = [work for work in self._works if work.sequence in self._active_work]
            return count

    def close(self, *, timeout_s: float = 1.0) -> None:
        with self._lock:
            if self._state is IRQState.CLOSED:
                return
        self.stop(timeout_s=timeout_s)
        with self._lock:
            self._handlers.clear()
            self._state = IRQState.CLOSED

    def _ensure_not_closed(self) -> None:
        if self._state is IRQState.CLOSED:
            raise IRQError("IRQ provider is closed")
