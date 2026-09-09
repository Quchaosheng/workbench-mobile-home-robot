"""The ROS-free controller port used by future simulator and hardware adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from workbench_motion.joint_limits import AcceptedTrajectory
from workbench_motion.motion_types import ExecutionReceipt, RobotCommand, RobotState


@runtime_checkable
class Controller(Protocol):
    """Admission and lifecycle boundary below planners and policies.

    An ``ExecutionReceipt`` documents only local admission. Adapters must emit
    separate execution observations and evidence before any world-state update
    or verification can occur.
    """

    def submit_command(self, command: RobotCommand, *, now_s: float) -> ExecutionReceipt:
        """Admit one bounded position or velocity command."""
        ...

    def submit_trajectory(
        self,
        request_id: str,
        trajectory: AcceptedTrajectory,
        *,
        now_s: float,
    ) -> ExecutionReceipt:
        """Admit only an immutable trajectory accepted by the preflight gate."""
        ...

    def get_state(self, *, now_s: float) -> RobotState | None:
        """Return fresh feedback, or ``None`` when feedback is unavailable/stale."""
        ...

    def hold(self, request_id: str, *, now_s: float) -> ExecutionReceipt:
        """Request a local hold at the control boundary."""
        ...

    def stop(self, request_id: str, *, now_s: float) -> ExecutionReceipt:
        """Request a safe stop at the control boundary."""
        ...

    def reset(self, request_id: str, *, now_s: float) -> ExecutionReceipt:
        """Reset a stopped, holding, or faulted controller after external recovery."""
        ...


__all__ = ["Controller"]
