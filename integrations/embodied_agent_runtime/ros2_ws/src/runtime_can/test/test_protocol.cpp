#include "runtime_can/protocol.hpp"

#include <gtest/gtest.h>

TEST(ProtocolTest, EncodesCommandUsingBigEndianFields)
{
  const runtime_can::Command command{0x1234, 0x56, -2};

  const auto frame = runtime_can::encode_command(command);

  EXPECT_EQ(frame.id, runtime_can::kCommandFrameId);
  EXPECT_EQ(frame.size, runtime_can::kPayloadSize);
  EXPECT_EQ(frame.data, (std::array<std::uint8_t, 8>{
    0x01, 0x12, 0x34, 0x56, 0xFF, 0xFF, 0xFF, 0xFE}));
}

TEST(ProtocolTest, RoundTripsCommandAndResponse)
{
  runtime_can::DecodeError error;
  const runtime_can::Command command{42, 7, 123456};
  const runtime_can::Response response{42, 2, 3, {4, 5, 6}};

  const auto decoded_command = runtime_can::decode_command(
    runtime_can::encode_command(command), error);
  ASSERT_TRUE(decoded_command.has_value());
  EXPECT_EQ(error, runtime_can::DecodeError::kNone);
  EXPECT_TRUE(*decoded_command == command);

  const auto decoded_response = runtime_can::decode_response(
    runtime_can::encode_response(response), error);
  ASSERT_TRUE(decoded_response.has_value());
  EXPECT_EQ(decoded_response->command_id, response.command_id);
  EXPECT_EQ(decoded_response->result_code, response.result_code);
  EXPECT_EQ(decoded_response->device_mode, response.device_mode);
  EXPECT_EQ(decoded_response->detail, response.detail);
}

TEST(ProtocolTest, RejectsWrongIdSizeAndVersion)
{
  runtime_can::DecodeError error;
  auto frame = runtime_can::encode_command({1, 2, 3});

  frame.id = runtime_can::kResponseFrameId;
  EXPECT_FALSE(runtime_can::decode_command(frame, error).has_value());
  EXPECT_EQ(error, runtime_can::DecodeError::kWrongFrameId);

  frame = runtime_can::encode_command({1, 2, 3});
  frame.size = 7;
  EXPECT_FALSE(runtime_can::decode_command(frame, error).has_value());
  EXPECT_EQ(error, runtime_can::DecodeError::kWrongPayloadSize);

  frame = runtime_can::encode_command({1, 2, 3});
  frame.data[0] = 99;
  EXPECT_FALSE(runtime_can::decode_command(frame, error).has_value());
  EXPECT_EQ(error, runtime_can::DecodeError::kUnsupportedVersion);
}

TEST(ProtocolTest, ReservesSeparateStopCommandNamespace)
{
  EXPECT_EQ(runtime_can::kStopOpcode, 0xFF);
  EXPECT_LT(runtime_can::kApplicationCommandIdMax, runtime_can::kStopCommandIdMin);
  EXPECT_EQ(runtime_can::kApplicationCommandIdMax + 1, runtime_can::kStopCommandIdMin);
}
