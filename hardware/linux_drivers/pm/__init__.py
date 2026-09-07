"""Software-only power-management lifecycle contract."""

from .contract import (
    FakePowerManager,
    PMError,
    PMResource,
    PMState,
    PMTimeout,
)

__all__ = ["FakePowerManager", "PMError", "PMResource", "PMState", "PMTimeout"]
