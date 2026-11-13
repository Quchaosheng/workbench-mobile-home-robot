"""Legacy compatibility stub for the MCU state machine. NOT safety evidence.

This module is a Python model of the P0 safety boundary that the repository once
used as its reference. It is **retired as a safety reference and parity oracle**;
it remains only as a legacy compatibility stub for earlier Python consumers. See
`firmware/mcu/README.md`, "Authority boundary", for the sentence this repeats.

The authoritative implementation is `firmware/mcu/core/state_machine.c`, and the
two are **not** in parity. One difference is recorded by
`tests/property/test_virtual_mcu_parity.py`: the C dispatch rejects `STOP` in
`MCU_STATE_FAULT` (`MCU_FAULT_STOP_REJECTED`), while this stub accepts it from
every state. That is a finding, not a bug to fix here -- the stub is not the
reference, so the C behaviour wins and this file must not be quoted as evidence
that the firmware does what it does.

A green property or mutation gate that names this module is therefore **software
evidence about a test double**, not firmware evidence. The gate archives carry the
label `legacy` for that reason.

What this module is good for: a deterministic, dependency-free double for the
offline demo and for property tests of the surrounding plumbing. What it is not:
proof that any MCU behaves this way, that a CAN frame is well-formed, or that a
stop was physically achieved.
"""

from dataclasses import dataclass
from enum import StrEnum


class McuState(StrEnum):
    IDLE = "idle"
    EXECUTING = "executing"
    SAFE_STOP = "safe_stop"
    FAULT = "fault"


class McuCommandStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class McuCommandRejection(StrEnum):
    NON_STRING = "non_string_command"
    EMPTY = "empty_command"
    MALFORMED = "malformed_command"
    UNKNOWN = "unknown_command"
    INVALID_STATE = "invalid_state_transition"


@dataclass(frozen=True)
class McuCommandResult:
    status: McuCommandStatus
    state: McuState
    reason: McuCommandRejection | None = None

    def __post_init__(self) -> None:
        if self.status is McuCommandStatus.ACCEPTED and self.reason is not None:
            raise ValueError("accepted command results cannot have a rejection reason")
        if self.status is McuCommandStatus.REJECTED and self.reason is None:
            raise ValueError("rejected command results require a rejection reason")

    @property
    def accepted(self) -> bool:
        return self.status is McuCommandStatus.ACCEPTED

    @property
    def rejected(self) -> bool:
        return self.status is McuCommandStatus.REJECTED


_COMMAND_ALLOWED_STATES: dict[str, frozenset[McuState]] = {
    "stop": frozenset(McuState),
    "reset": frozenset({McuState.SAFE_STOP, McuState.FAULT}),
    "execute": frozenset({McuState.IDLE}),
    "complete": frozenset({McuState.EXECUTING}),
}


class VirtualMcu:
    """Tiny deterministic model of the P0 safety boundary."""

    def __init__(self) -> None:
        self.state = McuState.IDLE
        self.fault_code: str | None = None

    def command(self, command: object) -> McuCommandResult:
        if not isinstance(command, str):
            return self._reject(McuCommandRejection.NON_STRING)
        if not command:
            return self._reject(McuCommandRejection.EMPTY)
        if command != command.strip():
            return self._reject(McuCommandRejection.MALFORMED)
        allowed_states = _COMMAND_ALLOWED_STATES.get(command)
        if allowed_states is None:
            return self._reject(McuCommandRejection.UNKNOWN)
        if self.state not in allowed_states:
            return self._reject(McuCommandRejection.INVALID_STATE)

        if command == "stop":
            self.state = McuState.SAFE_STOP
        elif command == "reset":
            self.state = McuState.IDLE
            self.fault_code = None
        elif command == "execute":
            self.state = McuState.EXECUTING
        elif command == "complete":
            self.state = McuState.IDLE
        return McuCommandResult(McuCommandStatus.ACCEPTED, self.state)

    def _reject(self, reason: McuCommandRejection) -> McuCommandResult:
        return McuCommandResult(McuCommandStatus.REJECTED, self.state, reason)

    def watchdog_timeout(self) -> McuState:
        self.state = McuState.FAULT
        self.fault_code = "WATCHDOG_TIMEOUT"
        return self.state
