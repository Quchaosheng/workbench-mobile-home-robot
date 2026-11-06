#include "task_executor/diagnostic_state.hpp"

#include <gtest/gtest.h>

#include <string>

namespace
{

std::string value(
  const diagnostic_msgs::msg::DiagnosticStatus & status, const std::string & key)
{
  for (const auto & item : status.values) {
    if (item.key == key) {
      return item.value;
    }
  }
  return {};
}

TEST(TaskDiagnosticStateTest, BridgeReadinessControlsInitialHealth)
{
  task_executor::TaskDiagnosticState state;
  EXPECT_EQ(state.snapshot().level, diagnostic_msgs::msg::DiagnosticStatus::ERROR);
  state.set_bridge_ready(true);
  EXPECT_EQ(state.snapshot().level, diagnostic_msgs::msg::DiagnosticStatus::OK);
  EXPECT_EQ(value(state.snapshot(), "ready"), "true");
}

TEST(TaskDiagnosticStateTest, RecoveringIsWarnWhenNoFailureIsLatched)
{
  task_executor::TaskDiagnosticState state;
  state.set_bridge_ready(true);
  state.begin_task("task-1");
  state.mark_recovering();
  EXPECT_EQ(state.snapshot().level, diagnostic_msgs::msg::DiagnosticStatus::WARN);
  EXPECT_EQ(value(state.snapshot(), "active_task_id"), "task-1");
}

TEST(TaskDiagnosticStateTest, FailureClearsOnlyAfterSuccessfulTask)
{
  task_executor::TaskDiagnosticState state;
  state.set_bridge_ready(true);
  state.begin_task("task-1");
  state.record_failure("SAFE_STOP", 204);
  EXPECT_EQ(state.snapshot().level, diagnostic_msgs::msg::DiagnosticStatus::ERROR);

  state.begin_task("task-2");
  state.mark_running();
  EXPECT_EQ(state.snapshot().level, diagnostic_msgs::msg::DiagnosticStatus::ERROR);
  state.record_success("COMPLETED");
  EXPECT_EQ(state.snapshot().level, diagnostic_msgs::msg::DiagnosticStatus::OK);
  EXPECT_EQ(value(state.snapshot(), "last_error_code"), "0");
}

TEST(TaskDiagnosticStateTest, SnapshotExposesStableIdentityPhasesAndKeys)
{
  task_executor::TaskDiagnosticState state;
  state.set_bridge_ready(true);
  state.begin_task("task-3");
  EXPECT_EQ(value(state.snapshot(), "state"), "VALIDATING");
  state.mark_validating();
  EXPECT_EQ(value(state.snapshot(), "state"), "VALIDATING");
  state.mark_running();
  EXPECT_EQ(value(state.snapshot(), "state"), "RUNNING");
  state.mark_waiting_ack();
  const auto status = state.snapshot();

  EXPECT_EQ(status.name, "runtime/task_executor");
  EXPECT_EQ(status.hardware_id, "task_runtime");
  EXPECT_EQ(value(status, "ready"), "true");
  EXPECT_EQ(value(status, "state"), "WAITING_ACK");
  EXPECT_EQ(value(status, "active_task_id"), "task-3");
  EXPECT_EQ(value(status, "bridge_ready"), "true");
  EXPECT_EQ(value(status, "last_outcome"), "NONE");
  EXPECT_EQ(value(status, "last_error_code"), "0");
  EXPECT_EQ(status.values.size(), 6U);
}

TEST(TaskDiagnosticStateTest, CancellationDoesNotClearLatchedFailure)
{
  task_executor::TaskDiagnosticState state;
  state.set_bridge_ready(true);
  state.begin_task("task-4");
  state.record_failure("DEVICE_FAULT", 301);
  state.begin_task("task-5");
  state.record_canceled();
  const auto status = state.snapshot();

  EXPECT_EQ(status.level, diagnostic_msgs::msg::DiagnosticStatus::ERROR);
  EXPECT_EQ(value(status, "state"), "IDLE");
  EXPECT_EQ(value(status, "active_task_id"), "");
  EXPECT_EQ(value(status, "last_outcome"), "CANCELED");
  EXPECT_EQ(value(status, "last_error_code"), "301");
}

}
