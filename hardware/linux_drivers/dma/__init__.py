"""Software-only DMA ownership contract and deterministic fake engine."""

from .contract import (
    BufferOwner,
    DMABackpressure,
    DMABuffer,
    DMACompletion,
    DMADescriptor,
    DMAError,
    DMAOwnershipError,
    DMAProviderClosed,
    DMAState,
    DMAStateError,
    DMAStatus,
    FakeDMAProvider,
)

__all__ = [
    "BufferOwner",
    "DMABackpressure",
    "DMABuffer",
    "DMACompletion",
    "DMADescriptor",
    "DMAError",
    "DMAOwnershipError",
    "DMAProviderClosed",
    "DMAState",
    "DMAStateError",
    "DMAStatus",
    "FakeDMAProvider",
]
