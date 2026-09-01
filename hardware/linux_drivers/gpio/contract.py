"""Bounded GPIO contract for software adapter and lifecycle tests."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import IntEnum, StrEnum


class GPIOError(ValueError):
    """Base class for invalid GPIO configuration or events."""


class GPIOPermissionError(GPIOError):
    """The requested operation is not allowed for the configured direction."""


class GPIOStateError(GPIOError):
    """The operation violates the line's state or timestamp contract."""


class GPIOProviderClosed(ConnectionError):
    """The provider has been closed and cannot be accessed."""


class GPIOQueueFull(TimeoutError):
    """The bounded input event queue is full."""


class GPIODirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class Edge(IntEnum):
    NONE = 0
    RISING = 1
    FALLING = 2
    BOTH = 3


@dataclass(frozen=True, slots=True)
class GPIOConfig:
    """Logical line configuration, independent of physical line numbering."""

    name: str
    direction: GPIODirection
    active_high: bool = True
    edge: Edge = Edge.NONE
    debounce_ns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or len(self.name) > 64:
            raise GPIOError("line name must be 1-64 characters")
        if not isinstance(self.direction, GPIODirection):
            raise GPIOError("direction must be input or output")
        if type(self.active_high) is not bool:
            raise GPIOError("active_high must be a boolean")
        if not isinstance(self.edge, Edge):
            raise GPIOError("edge must be a valid Edge value")
        if type(self.debounce_ns) is not int or not 0 <= self.debounce_ns <= 60_000_000_000:
            raise GPIOError("debounce_ns must be between 0 and 60 seconds")
        if self.direction is GPIODirection.OUTPUT and self.edge is not Edge.NONE:
            raise GPIOError("output lines cannot subscribe to input edges")


@dataclass(frozen=True, slots=True)
class GPIOEvent:
    """A validated input transition delivered to the bounded event queue."""

    line: str
    sequence: int
    value: bool
    timestamp_ns: int


class FakeGPIOProvider:
    """Thread-safe fake GPIO provider with fail-closed lifecycle semantics."""

    def __init__(self, configs: list[GPIOConfig], *, event_capacity: int = 16) -> None:
        if type(event_capacity) is not int or not 1 <= event_capacity <= 1024:
            raise GPIOError("event_capacity must be between 1 and 1024")
        if not configs:
            raise GPIOError("at least one GPIO line is required")
        if len({config.name for config in configs}) != len(configs):
            raise GPIOError("GPIO line names must be unique")
        self._configs = {config.name: config for config in configs}
        self._values = {config.name: False for config in configs}
        self._observed: set[str] = set()
        self._last_timestamp: dict[str, int] = {}
        self._events: list[GPIOEvent] = []
        self._next_sequence = 0
        self._event_capacity = event_capacity
        self._closed = False
        self._lock = threading.RLock()

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._events.clear()

    def configure(self, name: str) -> GPIOConfig:
        with self._lock:
            self._ensure_open()
            try:
                return self._configs[name]
            except KeyError as exc:
                raise GPIOError(f"unknown GPIO line: {name}") from exc

    def read(self, name: str) -> bool:
        with self._lock:
            config = self.configure(name)
            if config.direction is not GPIODirection.INPUT:
                raise GPIOPermissionError(f"GPIO line is not an input: {name}")
            if name not in self._observed:
                raise GPIOStateError(f"input value is unknown until observed: {name}")
            return self._values[name]

    def write(self, name: str, value: bool) -> None:
        with self._lock:
            config = self.configure(name)
            if config.direction is not GPIODirection.OUTPUT:
                raise GPIOPermissionError(f"GPIO line is not an output: {name}")
            if type(value) is not bool:
                raise GPIOError("GPIO value must be a boolean")
            self._values[name] = value

    def inject_input(self, name: str, value: bool, timestamp_ns: int) -> GPIOEvent | None:
        with self._lock:
            config = self.configure(name)
            if config.direction is not GPIODirection.INPUT:
                raise GPIOPermissionError(f"GPIO line is not an input: {name}")
            if type(value) is not bool:
                raise GPIOError("GPIO value must be a boolean")
            if type(timestamp_ns) is not int or timestamp_ns < 0:
                raise GPIOError("timestamp_ns must be a non-negative integer")
            previous_timestamp = self._last_timestamp.get(name)
            if previous_timestamp is not None and timestamp_ns <= previous_timestamp:
                raise GPIOStateError("input timestamps must increase strictly")
            previous_value = self._values[name]
            self._last_timestamp[name] = timestamp_ns
            self._values[name] = value
            if name not in self._observed:
                self._observed.add(name)
                return None
            if previous_value == value:
                return None
            if config.debounce_ns and timestamp_ns - previous_timestamp < config.debounce_ns:
                return None
            edge = Edge.RISING if not previous_value and value else Edge.FALLING
            if config.edge is Edge.NONE or (config.edge is not edge and config.edge is not Edge.BOTH):
                return None
            if len(self._events) >= self._event_capacity:
                raise GPIOQueueFull(f"GPIO event queue is full for {name}")
            event = GPIOEvent(name, self._next_sequence, value, timestamp_ns)
            self._next_sequence += 1
            self._events.append(event)
            return event

    def read_event(self) -> GPIOEvent | None:
        with self._lock:
            self._ensure_open()
            return self._events.pop(0) if self._events else None

    def _ensure_open(self) -> None:
        if self._closed:
            raise GPIOProviderClosed("GPIO provider is closed")
