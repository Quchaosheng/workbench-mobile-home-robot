"""Execution adapter for immutable, preflighted trajectories.

The adapter is the narrow seam between trajectory preflight and a concrete
controller.  It never accepts a mutable trajectory and never dispatches until
the preflight context and the state snapshot used by planning are still fresh.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .controller import Controller, DispatchReceipt, DispatchStatus, RobotCommand, RobotState

if TYPE_CHECKING:
    from .joint_limits import AcceptedTrajectory, PreflightContext


class ExecutionGateStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ExecutionGateReason(StrEnum):
    INVALID_TRAJECTORY = "invalid_trajectory"
    TRAJECTORY_HASH = "trajectory_hash"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    CONTEXT_MISMATCH = "context_mismatch"
    STATE_STALE = "state_stale"
    JOINT_SCHEMA = "joint_schema"
    CONTROLLER_REJECTED = "controller_rejected"


@dataclass(frozen=True, slots=True)
class TrajectoryExecutionResult:
    """Gate and dispatch evidence, deliberately separate from ActionResult."""

    status: ExecutionGateStatus
    reason: ExecutionGateReason | None
    dispatch_attempted: bool
    receipt: DispatchReceipt | None
    expected_state_hash: str
    observed_state_hash: str
    expected_context_hash: str
    observed_context_hash: str | None


def state_hash(state: RobotState) -> str:
    """Hash all state fields that can affect a trajectory dispatch decision."""
    payload = {
        "joint_names": list(state.joint_names),
        "positions": [value.hex() for value in state.positions],
        "velocities": [value.hex() for value in state.velocities],
        "accelerations": [value.hex() for value in state.accelerations],
        "timestamp_ns": state.timestamp_ns,
        "sequence": state.sequence,
        "mode": state.mode.value,
        "active_command_id": state.active_command_id,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _accepted_type() -> type:
    from .joint_limits import AcceptedTrajectory

    return AcceptedTrajectory


def _valid_trajectory_hash(accepted: AcceptedTrajectory) -> bool:
    try:
        canonical = accepted.canonical_bytes
        expected = accepted.trajectory_sha256
    except Exception:  # noqa: BLE001 - malformed/tampered contract data fails closed.
        return False
    if not isinstance(canonical, bytes) or not isinstance(expected, str):
        return False
    return expected == "sha256:" + hashlib.sha256(canonical).hexdigest()


class AcceptedTrajectoryExecutor:
    """Freshness-gated adapter that dispatches only ``AcceptedTrajectory``."""

    def __init__(self, controller: Controller, context_provider: Callable[[], PreflightContext]) -> None:
        if not isinstance(controller, Controller):
            raise TypeError("controller must be a Controller")
        if not callable(context_provider):
            raise TypeError("context_provider must be callable")
        self._controller = controller
        self._context_provider = context_provider

    def execute(self, accepted: AcceptedTrajectory, *, expected_state: RobotState) -> TrajectoryExecutionResult:
        """Validate freshness and dispatch once; rejected gates never call controller."""
        if not isinstance(expected_state, RobotState):
            raise TypeError("expected_state must be RobotState")
        observed_state = self._controller.state
        expected_state_hash = state_hash(expected_state)
        observed_state_hash = state_hash(observed_state)
        if not isinstance(accepted, _accepted_type()):
            return self._rejected(
                ExecutionGateReason.INVALID_TRAJECTORY,
                expected_state_hash,
                observed_state_hash,
                "",
                None,
            )
        try:
            accepted_context_hash = accepted.context_sha256
            snapshot = accepted.snapshot
        except Exception:  # noqa: BLE001 - malformed/tampered contract data fails closed.
            return self._rejected(
                ExecutionGateReason.INVALID_TRAJECTORY,
                expected_state_hash,
                observed_state_hash,
                "",
                None,
            )
        if not _valid_trajectory_hash(accepted):
            return self._rejected(
                ExecutionGateReason.TRAJECTORY_HASH,
                expected_state_hash,
                observed_state_hash,
                accepted_context_hash,
                None,
            )
        if not isinstance(accepted_context_hash, str) or not accepted_context_hash.startswith("sha256:"):
            return self._rejected(
                ExecutionGateReason.CONTEXT_MISMATCH,
                expected_state_hash,
                observed_state_hash,
                str(accepted_context_hash),
                None,
            )
        if observed_state_hash != expected_state_hash:
            return self._rejected(
                ExecutionGateReason.STATE_STALE,
                expected_state_hash,
                observed_state_hash,
                accepted_context_hash,
                None,
            )
        if snapshot.joint_names != observed_state.joint_names:
            return self._rejected(
                ExecutionGateReason.JOINT_SCHEMA,
                expected_state_hash,
                observed_state_hash,
                accepted_context_hash,
                None,
            )
        try:
            context = self._context_provider()
            observed_context_hash = context.context_sha256
        except Exception:  # noqa: BLE001 - unavailable context must prevent dispatch.
            return self._rejected(
                ExecutionGateReason.CONTEXT_UNAVAILABLE,
                expected_state_hash,
                observed_state_hash,
                accepted_context_hash,
                None,
            )
        if observed_context_hash != accepted_context_hash:
            return self._rejected(
                ExecutionGateReason.CONTEXT_MISMATCH,
                expected_state_hash,
                observed_state_hash,
                accepted_context_hash,
                observed_context_hash,
            )

        # Feedback may arrive while the context provider is reading its
        # snapshot. Re-check immediately before dispatch so a trajectory can
        # never be sent using a state that became stale during gate evaluation.
        latest_state = self._controller.state
        latest_state_hash = state_hash(latest_state)
        if latest_state_hash != expected_state_hash:
            return self._rejected(
                ExecutionGateReason.STATE_STALE,
                expected_state_hash,
                latest_state_hash,
                accepted_context_hash,
                observed_context_hash,
            )

        receipt = self._controller.dispatch(RobotCommand.trajectory(accepted.trajectory_sha256, accepted))
        if receipt.status is not DispatchStatus.ACCEPTED:
            return TrajectoryExecutionResult(
                ExecutionGateStatus.REJECTED,
                ExecutionGateReason.CONTROLLER_REJECTED,
                True,
                receipt,
                expected_state_hash,
                observed_state_hash,
                accepted_context_hash,
                observed_context_hash,
            )
        return TrajectoryExecutionResult(
            ExecutionGateStatus.ACCEPTED,
            None,
            True,
            receipt,
            expected_state_hash,
            observed_state_hash,
            accepted_context_hash,
            observed_context_hash,
        )

    @staticmethod
    def _rejected(
        reason: ExecutionGateReason,
        expected_state_hash: str,
        observed_state_hash: str,
        expected_context_hash: str,
        observed_context_hash: str | None,
    ) -> TrajectoryExecutionResult:
        return TrajectoryExecutionResult(
            ExecutionGateStatus.REJECTED,
            reason,
            False,
            None,
            expected_state_hash,
            observed_state_hash,
            expected_context_hash,
            observed_context_hash,
        )


__all__ = [
    "AcceptedTrajectoryExecutor",
    "ExecutionGateReason",
    "ExecutionGateStatus",
    "TrajectoryExecutionResult",
    "state_hash",
]
