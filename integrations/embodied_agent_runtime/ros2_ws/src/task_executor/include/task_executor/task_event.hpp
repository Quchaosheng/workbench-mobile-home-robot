#pragma once

#include "builtin_interfaces/msg/time.hpp"
#include "robot_task_interfaces/msg/task_event.hpp"

#include <cstdint>
#include <string>

namespace task_executor
{

// Correlation identity copied from the ExecuteTask goal. `goal_id` is the ROS 2
// goal UUID, which the goal handle only exposes after acceptance, so it is set
// by the caller rather than by this builder.
robot_task_interfaces::msg::TaskEvent make_task_event(
  const std::string & task_id,
  const std::string & target_id,
  const std::string & request_id,
  const std::string & workflow_id,
  const std::string & goal_id,
  std::uint8_t action_status,
  std::uint8_t outcome,
  std::uint16_t error_code,
  std::uint64_t duration_ms,
  const std::string & message,
  const builtin_interfaces::msg::Time & stamp);

}  // namespace task_executor
