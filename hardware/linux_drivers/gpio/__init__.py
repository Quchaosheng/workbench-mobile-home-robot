"""Software-only GPIO contract and deterministic fake provider."""

from .contract import (
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

__all__ = [
    "Edge",
    "FakeGPIOProvider",
    "GPIOConfig",
    "GPIODirection",
    "GPIOError",
    "GPIOEvent",
    "GPIOPermissionError",
    "GPIOProviderClosed",
    "GPIOQueueFull",
    "GPIOStateError",
]
