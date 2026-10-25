#include "device_bridge/diagnostic_state.hpp"

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

TEST(DeviceDiagnosticStateTest, RetryWarnsThenSuccessPreservesCounter)
{
  device_bridge::DeviceDiagnosticState state;
  state.begin_command(17);
  state.record_retry();
  auto status = state.snapshot(true);
  EXPECT_EQ(status.level, diagnostic_msgs::msg::DiagnosticStatus::WARN);
  EXPECT_EQ(value(status, "state"), "RETRYING");
  EXPECT_EQ(value(status, "active_command_id"), "17");
  EXPECT_EQ(value(status, "retry_count"), "1");

  state.record_success();
  status = state.snapshot(true);
  EXPECT_EQ(status.level, diagnostic_msgs::msg::DiagnosticStatus::OK);
  EXPECT_EQ(value(status, "active_command_id"), "0");
  EXPECT_EQ(value(status, "retry_count"), "1");
}

TEST(DeviceDiagnosticStateTest, FailuresLatchUntilNewSuccess)
{
  device_bridge::DeviceDiagnosticState state;
  state.begin_command(18);
  state.record_ack_timeout(201);
  EXPECT_EQ(state.snapshot(true).level, diagnostic_msgs::msg::DiagnosticStatus::ERROR);
  EXPECT_EQ(value(state.snapshot(true), "ack_timeout_count"), "1");

  state.begin_command(19);
  EXPECT_EQ(state.snapshot(true).level, diagnostic_msgs::msg::DiagnosticStatus::ERROR);
  state.record_retry();
  EXPECT_EQ(state.snapshot(true).level, diagnostic_msgs::msg::DiagnosticStatus::ERROR);
  state.record_success();
  EXPECT_EQ(state.snapshot(true).level, diagnostic_msgs::msg::DiagnosticStatus::OK);
  EXPECT_EQ(value(state.snapshot(true), "ack_timeout_count"), "1");
}

TEST(DeviceDiagnosticStateTest, StopFailureAndSocketDownAreErrors)
{
  device_bridge::DeviceDiagnosticState state;
  state.begin_command(20);
  state.mark_stopping();
  EXPECT_EQ(value(state.snapshot(true), "state"), "STOPPING");
  state.record_stop_failure(204);
  EXPECT_EQ(value(state.snapshot(true), "stop_failure_count"), "1");
  EXPECT_EQ(value(state.snapshot(true), "last_error_code"), "204");
  EXPECT_EQ(state.snapshot(false).level, diagnostic_msgs::msg::DiagnosticStatus::ERROR);
  EXPECT_EQ(value(state.snapshot(false), "ready"), "false");
}

TEST(DeviceDiagnosticStateTest, SnapshotExposesStableIdentityPhasesAndKeys)
{
  device_bridge::DeviceDiagnosticState state;
  state.begin_command(21);
  EXPECT_EQ(value(state.snapshot(true), "state"), "SENDING");
  state.mark_waiting_ack();
  EXPECT_EQ(value(state.snapshot(true), "state"), "WAITING_ACK");
  state.mark_sending();
  EXPECT_EQ(value(state.snapshot(true), "state"), "SENDING");
  state.mark_waiting_ack();
  const auto status = state.snapshot(true);

  EXPECT_EQ(status.name, "runtime/device_bridge");
  EXPECT_EQ(status.hardware_id, "socketcan_bridge");
  EXPECT_EQ(value(status, "ready"), "true");
  EXPECT_EQ(value(status, "state"), "WAITING_ACK");
  EXPECT_EQ(value(status, "active_command_id"), "21");
  EXPECT_EQ(value(status, "retry_count"), "0");
  EXPECT_EQ(value(status, "ack_timeout_count"), "0");
  EXPECT_EQ(value(status, "device_fault_count"), "0");
  EXPECT_EQ(value(status, "stop_failure_count"), "0");
  EXPECT_EQ(value(status, "last_error_code"), "0");
  EXPECT_EQ(status.values.size(), 8U);
}

TEST(DeviceDiagnosticStateTest, FaultsAreCumulativeAndTransportDoesNotChangeCounters)
{
  device_bridge::DeviceDiagnosticState state;
  state.begin_command(22);
  state.record_device_fault(301);
  state.begin_command(23);
  state.record_device_fault(302);
  state.begin_command(24);
  state.record_transport_error(200);
  const auto status = state.snapshot(true);

  EXPECT_EQ(status.level, diagnostic_msgs::msg::DiagnosticStatus::ERROR);
  EXPECT_EQ(value(status, "device_fault_count"), "2");
  EXPECT_EQ(value(status, "ack_timeout_count"), "0");
  EXPECT_EQ(value(status, "stop_failure_count"), "0");
  EXPECT_EQ(value(status, "last_error_code"), "200");
}

TEST(DeviceDiagnosticStateTest, ConfirmedCancellationClearsLatchedError)
{
  device_bridge::DeviceDiagnosticState state;
  state.begin_command(25);
  state.record_ack_timeout(201);
  state.begin_command(26);
  state.record_canceled();
  const auto status = state.snapshot(true);

  EXPECT_EQ(status.level, diagnostic_msgs::msg::DiagnosticStatus::OK);
  EXPECT_EQ(value(status, "state"), "IDLE");
  EXPECT_EQ(value(status, "active_command_id"), "0");
  EXPECT_EQ(value(status, "last_error_code"), "0");
}

}
