#include "runtime_monitor/health_aggregator.hpp"

#include <gtest/gtest.h>

namespace
{
diagnostic_msgs::msg::DiagnosticStatus status(
  const std::string & name, const std::uint8_t level)
{
  diagnostic_msgs::msg::DiagnosticStatus value;
  value.name = name;
  value.level = level;
  value.message = "test";
  return value;
}
}  // namespace

TEST(HealthAggregatorTest, WaitsForBothCoreSources)
{
  runtime_monitor::HealthAggregator aggregator(std::chrono::milliseconds(2000));
  const runtime_monitor::SteadyTime now{};
  EXPECT_TRUE(aggregator.update(
    status(runtime_monitor::kDeviceBridgeName, diagnostic_msgs::msg::DiagnosticStatus::OK), now));
  const auto health = aggregator.evaluate(now);
  EXPECT_FALSE(health.ready);
  EXPECT_EQ(health.level, diagnostic_msgs::msg::DiagnosticStatus::STALE);
  EXPECT_FALSE(health.task_executor.seen);
}

TEST(HealthAggregatorTest, WarnRemainsReady)
{
  runtime_monitor::HealthAggregator aggregator(std::chrono::milliseconds(2000));
  const runtime_monitor::SteadyTime now{};
  aggregator.update(
    status(runtime_monitor::kDeviceBridgeName, diagnostic_msgs::msg::DiagnosticStatus::WARN), now);
  aggregator.update(
    status(runtime_monitor::kTaskExecutorName, diagnostic_msgs::msg::DiagnosticStatus::OK), now);
  const auto health = aggregator.evaluate(now);
  EXPECT_TRUE(health.ready);
  EXPECT_EQ(health.level, diagnostic_msgs::msg::DiagnosticStatus::WARN);
}

TEST(HealthAggregatorTest, ErrorMakesRuntimeNotReady)
{
  runtime_monitor::HealthAggregator aggregator(std::chrono::milliseconds(2000));
  const runtime_monitor::SteadyTime now{};
  aggregator.update(
    status(runtime_monitor::kDeviceBridgeName, diagnostic_msgs::msg::DiagnosticStatus::ERROR), now);
  aggregator.update(
    status(runtime_monitor::kTaskExecutorName, diagnostic_msgs::msg::DiagnosticStatus::OK), now);
  const auto health = aggregator.evaluate(now);
  EXPECT_FALSE(health.ready);
  EXPECT_EQ(health.level, diagnostic_msgs::msg::DiagnosticStatus::ERROR);
}

TEST(HealthAggregatorTest, StaleTakesPrecedenceAndRecoveryIsAllowed)
{
  runtime_monitor::HealthAggregator aggregator(std::chrono::milliseconds(2000));
  const runtime_monitor::SteadyTime start{};
  aggregator.update(
    status(runtime_monitor::kDeviceBridgeName, diagnostic_msgs::msg::DiagnosticStatus::OK), start);
  aggregator.update(
    status(runtime_monitor::kTaskExecutorName, diagnostic_msgs::msg::DiagnosticStatus::OK), start);
  EXPECT_FALSE(aggregator.evaluate(start + std::chrono::milliseconds(2001)).ready);

  const auto recovered_at = start + std::chrono::milliseconds(2100);
  aggregator.update(
    status(runtime_monitor::kDeviceBridgeName, diagnostic_msgs::msg::DiagnosticStatus::OK),
    recovered_at);
  aggregator.update(
    status(runtime_monitor::kTaskExecutorName, diagnostic_msgs::msg::DiagnosticStatus::OK),
    recovered_at);
  EXPECT_TRUE(aggregator.evaluate(recovered_at).ready);
}

TEST(HealthAggregatorTest, IgnoresUnknownAndOwnSummary)
{
  runtime_monitor::HealthAggregator aggregator(std::chrono::milliseconds(2000));
  const runtime_monitor::SteadyTime now{};
  EXPECT_FALSE(aggregator.update(status("other/component", 0), now));
  EXPECT_FALSE(aggregator.update(status(runtime_monitor::kSystemName, 0), now));
}
