"""Motion (robot/control) semantic-action adapter package.

The package exposes ROS-free contracts and safety boundaries for evidence,
controller state, and command dispatch. ROS, Gazebo, MoveIt, and hardware
adapters remain behind module-specific ports.
"""

from .controller import (
    CommandType,
    Controller,
    ControllerMode,
    DispatchReceipt,
    DispatchStatus,
    RobotCommand,
    RobotState,
)
from .evidence import EvidenceRef, EvidenceSink, ExecutionEvent, FakeEvidenceSink
from .gazebo_adapter import (
    GazeboActionStatus,
    GazeboExecutionResult,
    GazeboTrajectoryController,
    JointStateConversionError,
    JointStateReason,
    accepted_to_follow_joint_goal,
    joint_state_to_robot_state,
)
from .logging_setup import configure_logging, get_action_logger
from .trajectory_executor import (
    AcceptedTrajectoryExecutor,
    ExecutionGateReason,
    ExecutionGateStatus,
    TrajectoryExecutionResult,
    state_hash,
)

__all__ = [
    "AcceptedTrajectoryExecutor",
    "CommandType",
    "Controller",
    "ControllerMode",
    "DispatchReceipt",
    "DispatchStatus",
    "EvidenceRef",
    "EvidenceSink",
    "ExecutionEvent",
    "ExecutionGateReason",
    "ExecutionGateStatus",
    "FakeEvidenceSink",
    "GazeboActionStatus",
    "GazeboExecutionResult",
    "GazeboTrajectoryController",
    "JointStateConversionError",
    "JointStateReason",
    "RobotCommand",
    "RobotState",
    "TrajectoryExecutionResult",
    "accepted_to_follow_joint_goal",
    "configure_logging",
    "get_action_logger",
    "joint_state_to_robot_state",
    "state_hash",
]
