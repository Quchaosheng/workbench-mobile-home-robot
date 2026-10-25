#include "device_bridge/command_policy.hpp"

#include <gtest/gtest.h>

TEST(CommandPolicyTest, RoundsPositiveSubMillisecondDurationUp)
{
  builtin_interfaces::msg::Duration duration;
  duration.nanosec = 1;
  std::chrono::milliseconds timeout;

  EXPECT_TRUE(device_bridge::duration_to_milliseconds(duration, 10, timeout));
  EXPECT_EQ(timeout.count(), 1);
}

TEST(CommandPolicyTest, RejectsZeroNegativeMalformedAndTooLargeDurations)
{
  builtin_interfaces::msg::Duration duration;
  std::chrono::milliseconds timeout;

  EXPECT_FALSE(device_bridge::duration_to_milliseconds(duration, 10, timeout));

  duration.sec = -1;
  EXPECT_FALSE(device_bridge::duration_to_milliseconds(duration, 10, timeout));

  duration.sec = 0;
  duration.nanosec = 1000000000U;
  EXPECT_FALSE(device_bridge::duration_to_milliseconds(duration, 10, timeout));

  duration.nanosec = 100000000U;
  EXPECT_FALSE(device_bridge::duration_to_milliseconds(duration, 10, timeout));
}

TEST(CommandPolicyTest, MapsDeviceResultToStableErrorNamespace)
{
  EXPECT_EQ(device_bridge::device_error_code(1), 301);
  EXPECT_EQ(device_bridge::device_error_code(2), 302);
}
