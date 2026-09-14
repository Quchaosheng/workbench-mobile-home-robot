"""ROS-free controller port for planners, policies, and future adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from workbench_motion.joint_limits import AcceptedTrajectory
from workbench_motion.motion_types import ExecutionReceipt, RobotCommand, RobotState


@runtime_checkable
class Controller(Protocol):
    def update_state(self, state: RobotState) -> None:
        """Publish adapter feedback to the local control boundary."""
        ...

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
        """Admit only a preflight-accepted immutable trajectory."""
        ...

    def get_state(self, *, now_s: float) -> RobotState | None:
        """Return fresh feedback, or None when absent, future, or stale."""
        ...

    def hold(self, request_id: str, *, now_s: float) -> ExecutionReceipt: ...

    def stop(self, request_id: str, *, now_s: float) -> ExecutionReceipt: ...

    def reset(self, request_id: str, *, now_s: float) -> ExecutionReceipt: ...


__all__ = ["Controller"]
