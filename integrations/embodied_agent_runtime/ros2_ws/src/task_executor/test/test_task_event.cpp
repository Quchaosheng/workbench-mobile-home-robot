#include "task_executor/task_event.hpp"

#include "gtest/gtest.h"
#include "robot_task_interfaces/action/execute_task.hpp"
#include "robot_task_interfaces/msg/task_event.hpp"

namespace
{

using ExecuteTask = robot_task_interfaces::action::ExecuteTask;
using TaskEvent = robot_task_interfaces::msg::TaskEvent;

builtin_interfaces::msg::Time stamp()
{
  builtin_interfaces::msg::Time value;
  value.sec = 123;
  value.nanosec = 456;
  return value;
}

TEST(TaskEvent, MakesCompletedEvent)
{
  const auto event = task_executor::make_task_event(
    "task-1", "dock_a", "request-1", "single_task", "goal-1",
    TaskEvent::SUCCEEDED, ExecuteTask::Result::COMPLETED,
    0, 37, "completed", stamp());

  EXPECT_EQ(event.stamp, stamp());
  EXPECT_EQ(event.task_id, "task-1");
  EXPECT_EQ(event.target_id, "dock_a");
  EXPECT_EQ(event.request_id, "request-1");
  EXPECT_EQ(event.workflow_id, "single_task");
  EXPECT_EQ(event.goal_id, "goal-1");
  EXPECT_EQ(event.action_status, TaskEvent::SUCCEEDED);
  EXPECT_EQ(event.outcome, ExecuteTask::Result::COMPLETED);
  EXPECT_EQ(event.error_code, 0);
  EXPECT_EQ(event.duration_ms, 37U);
  EXPECT_EQ(event.message, "completed");
}

TEST(TaskEvent, MakesSafeStopEvent)
{
  const auto event = task_executor::make_task_event(
    "task-2", "home", "", "", "",
    TaskEvent::ABORTED, ExecuteTask::Result::SAFE_STOP,
    204, 81, "safe stop", stamp());

  EXPECT_EQ(event.stamp, stamp());
  EXPECT_EQ(event.task_id, "task-2");
  EXPECT_EQ(event.target_id, "home");
  EXPECT_EQ(event.request_id, "");
  EXPECT_EQ(event.workflow_id, "");
  EXPECT_EQ(event.goal_id, "");
  EXPECT_EQ(event.action_status, TaskEvent::ABORTED);
  EXPECT_EQ(event.outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(event.error_code, 204);
  EXPECT_EQ(event.duration_ms, 81U);
  EXPECT_EQ(event.message, "safe stop");
}

TEST(TaskEvent, MakesCanceledEvent)
{
  const auto event = task_executor::make_task_event(
    "task-3", "dock_a", "request-3", "single_task", "goal-3",
    TaskEvent::CANCELED, ExecuteTask::Result::CANCELED,
    0, 12, "canceled", stamp());

  EXPECT_EQ(event.stamp, stamp());
  EXPECT_EQ(event.task_id, "task-3");
  EXPECT_EQ(event.target_id, "dock_a");
  EXPECT_EQ(event.action_status, TaskEvent::CANCELED);
  EXPECT_EQ(event.outcome, ExecuteTask::Result::CANCELED);
  EXPECT_EQ(event.error_code, 0);
  EXPECT_EQ(event.duration_ms, 12U);
  EXPECT_EQ(event.message, "canceled");
}

}  // namespace
