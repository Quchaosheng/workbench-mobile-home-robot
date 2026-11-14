// Tests for the joint frame codec (issue #541).
//
// Deliberately ROS-free, like the codec: these run with a C++ compiler and nothing
// else, so the refusals are checked on any host rather than only where a ROS 2
// installation exists. Each test asserts a refusal that would otherwise be a silent
// wrong command.

#include <gtest/gtest.h>

#include <cmath>
#include <limits>
#include <vector>

#include "workbench_mobile_base_hardware/joint_frame_codec.hpp"

namespace wmbh = workbench_mobile_base_hardware;

namespace
{

wmbh::ArbitrationTable configured_table()
{
  wmbh::ArbitrationTable table;
  table.command = 0x100;
  table.state = 0x180;
  table.set = true;
  return table;
}

wmbh::ArbitrationTable unset_table()
{
  wmbh::ArbitrationTable table;  // set is false
  return table;
}

std::vector<wmbh::JointLimit> three_joints()
{
  return {
    {"lift", 0.0, 1.0},
    {"neck_yaw", -1.5, 1.5},
    {"neck_pitch", -0.5, 0.5},
  };
}

}  // namespace

TEST(JointFrameCodec, encodes_a_three_joint_command)
{
  const auto result = wmbh::encode_joint_command(
    configured_table(), three_joints(), {0.5, -1.0, 0.25}, 7);

  ASSERT_TRUE(result.ok()) << result.reason;
  // One sequence byte plus two bytes per joint.
  EXPECT_EQ(result.bytes.size(), 7u);
  EXPECT_EQ(result.bytes[0], 7u);
}

TEST(JointFrameCodec, refuses_an_unset_arbitration_table)
{
  // A default id is how a command reaches a device nobody meant to address.
  const auto result = wmbh::encode_joint_command(
    unset_table(), three_joints(), {0.5, 0.0, 0.0}, 1);

  EXPECT_FALSE(result.ok());
  EXPECT_EQ(result.refusal, wmbh::Refusal::kTableUnset);
  EXPECT_NE(result.reason.find("not set"), std::string::npos);
  EXPECT_TRUE(result.bytes.empty());
}

TEST(JointFrameCodec, refuses_an_out_of_range_command_instead_of_clipping)
{
  // Clipping turns a planner bug into an arm that moved and nobody said anything.
  const auto result = wmbh::encode_joint_command(
    configured_table(), three_joints(), {0.5, 9.0, 0.0}, 1);

  EXPECT_FALSE(result.ok());
  EXPECT_EQ(result.refusal, wmbh::Refusal::kOutOfRange);
  EXPECT_NE(result.reason.find("refused rather than clipped"), std::string::npos);
  EXPECT_TRUE(result.bytes.empty());
}

TEST(JointFrameCodec, refuses_a_non_finite_command)
{
  // NaN compares false against every limit, so it would slip through a naive range
  // check and become an unspecified integer cast.
  const auto result = wmbh::encode_joint_command(
    configured_table(), three_joints(),
    {0.5, std::numeric_limits<double>::quiet_NaN(), 0.0}, 1);

  EXPECT_FALSE(result.ok());
  EXPECT_EQ(result.refusal, wmbh::Refusal::kOutOfRange);
}

TEST(JointFrameCodec, refuses_a_position_count_that_does_not_match_the_joint_set)
{
  const auto result = wmbh::encode_joint_command(
    configured_table(), three_joints(), {0.5, 0.0}, 1);

  EXPECT_FALSE(result.ok());
  EXPECT_EQ(result.refusal, wmbh::Refusal::kUnknownJoint);
}

TEST(JointFrameCodec, refuses_more_joints_than_a_classic_can_frame_carries)
{
  // A split command is a partial command, and a half-applied command is worse than
  // a rejected one.
  std::vector<wmbh::JointLimit> limits;
  std::vector<double> positions;
  for (int index = 0; index < 5; ++index) {
    limits.push_back({"joint_" + std::to_string(index), 0.0, 1.0});
    positions.push_back(0.5);
  }

  const auto result = wmbh::encode_joint_command(configured_table(), limits, positions, 1);

  EXPECT_FALSE(result.ok());
  EXPECT_EQ(result.refusal, wmbh::Refusal::kTooManyJoints);
  EXPECT_TRUE(result.bytes.empty());
}

TEST(JointFrameCodec, encodes_and_decodes_a_round_trip)
{
  const auto encoded = wmbh::encode_joint_command(
    configured_table(), three_joints(), {0.5, -1.0, 0.25}, 3);
  ASSERT_TRUE(encoded.ok()) << encoded.reason;

  // The state frame carries positions only, so drop the sequence byte.
  const std::vector<std::uint8_t> payload(encoded.bytes.begin() + 1, encoded.bytes.end());
  std::vector<std::int16_t> positions;
  ASSERT_TRUE(wmbh::decode_joint_state(configured_table(), payload, positions));

  ASSERT_EQ(positions.size(), 3u);
  EXPECT_EQ(positions[0], 500);    // 0.5 rad
  EXPECT_EQ(positions[1], -1000);  // -1.0 rad
  EXPECT_EQ(positions[2], 250);    // 0.25 rad
}

TEST(JointFrameCodec, refuses_to_decode_with_an_unset_table)
{
  std::vector<std::int16_t> positions;
  EXPECT_FALSE(wmbh::decode_joint_state(unset_table(), {0x01, 0x00}, positions));
  EXPECT_TRUE(positions.empty());
}

TEST(JointFrameCodec, refuses_an_odd_length_state_frame)
{
  // An odd payload means a truncated frame, and reading it would shift every joint.
  std::vector<std::int16_t> positions;
  EXPECT_FALSE(wmbh::decode_joint_state(configured_table(), {0x01, 0x00, 0x02}, positions));
}

TEST(StateAge, accepts_a_fresh_state)
{
  const auto verdict = wmbh::judge_state_age(0.05, 0.2);
  EXPECT_TRUE(verdict.accept_commands);
  EXPECT_FALSE(verdict.report_error);
}

TEST(StateAge, refuses_a_stale_state_and_does_not_repeat_the_last_command)
{
  const auto verdict = wmbh::judge_state_age(0.5, 0.2);

  EXPECT_FALSE(verdict.accept_commands);
  EXPECT_TRUE(verdict.report_error);
  // The sentence that matters: a stale state must not resurrect a command.
  EXPECT_NE(verdict.reason.find("NOT repeated"), std::string::npos);
}

TEST(StateAge, refuses_a_zero_timeout)
{
  // A timeout of zero would accept any frame, including one from a previous run.
  const auto verdict = wmbh::judge_state_age(0.0, 0.0);
  EXPECT_FALSE(verdict.accept_commands);
  EXPECT_TRUE(verdict.report_error);
}

TEST(StateAge, refuses_a_negative_age)
{
  const auto verdict = wmbh::judge_state_age(-1.0, 0.2);
  EXPECT_FALSE(verdict.accept_commands);
  EXPECT_TRUE(verdict.report_error);
}
