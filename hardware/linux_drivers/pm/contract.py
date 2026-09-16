"""Bounded suspend/resume lifecycle model for software tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PMError(RuntimeError):
    """Base class for invalid power-management operations."""


class PMTimeout(TimeoutError, PMError):
    """A suspend or resume operation exceeded its bounded deadline."""


class PMState(StrEnum):
    ACTIVE = "active"
    SUSPENDING = "suspending"
    SUSPENDED = "suspended"
    RESUMING = "resuming"
    FAULT = "fault"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class PMResource:
    """A deterministic software resource in the PM dependency order."""

    name: str
    suspend_cost_s: float = 0.0
    resume_cost_s: float = 0.0

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise PMError("resource name must be non-empty")
        for value, label in ((self.suspend_cost_s, "suspend_cost_s"), (self.resume_cost_s, "resume_cost_s")):
            if type(value) not in (int, float) or value < 0:
                raise PMError(f"{label} must be non-negative")


class FakePowerManager:
    """Fail-closed suspend/resume coordinator with explicit resource order."""

    def __init__(self, resources: tuple[PMResource, ...] = ()) -> None:
        names = [resource.name for resource in resources]
        if len(names) != len(set(names)):
            raise PMError("resource names must be unique")
        self.resources = resources
        self._state = PMState.ACTIVE
        self._suspended: list[str] = []

    @property
    def state(self) -> PMState:
        return self._state

    @property
    def suspended_resources(self) -> tuple[str, ...]:
        return tuple(self._suspended)

    def suspend(self, *, timeout_s: float = 1.0) -> None:
        self._ensure_timeout(timeout_s)
        if self._state is not PMState.ACTIVE:
            raise PMError(f"cannot suspend from {self._state.value}")
        self._state = PMState.SUSPENDING
        remaining = float(timeout_s)
        self._suspended.clear()
        try:
            for resource in reversed(self.resources):
                remaining -= resource.suspend_cost_s
                if remaining < 0:
                    raise PMTimeout("suspend exceeded deadline")
                self._suspended.append(resource.name)
        except PMError:
            self._state = PMState.FAULT
            raise
        self._state = PMState.SUSPENDED

    def resume(self, *, timeout_s: float = 1.0) -> None:
        self._ensure_timeout(timeout_s)
        if self._state is not PMState.SUSPENDED:
            raise PMError(f"cannot resume from {self._state.value}")
        self._state = PMState.RESUMING
        remaining = float(timeout_s)
        try:
            for resource in self.resources:
                remaining -= resource.resume_cost_s
                if remaining < 0:
                    raise PMTimeout("resume exceeded deadline")
        except PMError:
            self._state = PMState.FAULT
            raise
        self._suspended.clear()
        self._state = PMState.ACTIVE

    def close(self) -> None:
        if self._state is PMState.CLOSED:
            return
        if self._state in {PMState.SUSPENDING, PMState.RESUMING}:
            raise PMError(f"cannot close during {self._state.value}")
        self._suspended.clear()
        self._state = PMState.CLOSED

    def _ensure_timeout(self, timeout_s: float) -> None:
        if type(timeout_s) not in (int, float) or timeout_s <= 0:
            raise PMError("timeout_s must be positive")
