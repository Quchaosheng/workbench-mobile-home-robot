#include "task_executor/task_event.hpp"

namespace task_executor
{

robot_task_interfaces::msg::TaskEvent make_task_event(
  const std::string & task_id,
  const std::string & target_id,
  const std::string & request_id,
  const std::string & workflow_id,
  const std::string & goal_id,
  const std::uint8_t action_status,
  const std::uint8_t outcome,
  const std::uint16_t error_code,
  const std::uint64_t duration_ms,
  const std::string & message,
  const builtin_interfaces::msg::Time & stamp)
{
  robot_task_interfaces::msg::TaskEvent event;
  event.stamp = stamp;
  event.task_id = task_id;
  event.target_id = target_id;
  event.request_id = request_id;
  event.workflow_id = workflow_id;
  event.goal_id = goal_id;
  event.action_status = action_status;
  event.outcome = outcome;
  event.error_code = error_code;
  event.duration_ms = duration_ms;
  event.message = message;
  return event;
}

}  // namespace task_executor
