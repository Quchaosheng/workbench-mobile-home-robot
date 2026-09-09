"""ROS-free selection for existing point-to-point trajectory primitives.

This module chooses an explicitly requested internal algorithm; it does not
create commands or interact with a controller, simulator, or robot adapter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum

from workbench_motion.quintic_trajectory import JointMotionLimit, QuinticTrajectory, quintic_point_to_point
from workbench_motion.s_curve_trajectory import SCurveTrajectory, s_curve_point_to_point


class TrajectoryAlgorithm(StrEnum):
    """Canonical identifiers for supported point-to-point generators."""

    QUINTIC = "quintic"
    S_CURVE = "s_curve"


def generate_point_to_point(
    joint_names: Sequence[str],
    start_positions: Sequence[float],
    target_positions: Sequence[float],
    limits: Mapping[str, JointMotionLimit],
    *,
    strategy: TrajectoryAlgorithm | str = TrajectoryAlgorithm.QUINTIC,
) -> QuinticTrajectory | SCurveTrajectory:
    """Generate a point-to-point trajectory using one exact canonical strategy.

    Quintic remains the compatibility default. Invalid strategy names fail
    closed; the selected primitive remains authoritative for motion validation.
    """
    algorithm = TrajectoryAlgorithm(strategy)
    if algorithm is TrajectoryAlgorithm.QUINTIC:
        return quintic_point_to_point(joint_names, start_positions, target_positions, limits)
    if algorithm is TrajectoryAlgorithm.S_CURVE:
        return s_curve_point_to_point(joint_names, start_positions, target_positions, limits)
    raise ValueError(f"unsupported trajectory strategy: {strategy!r}")


__all__ = ["TrajectoryAlgorithm", "generate_point_to_point"]
