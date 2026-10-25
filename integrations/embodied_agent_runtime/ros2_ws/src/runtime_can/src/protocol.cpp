#include "runtime_can/protocol.hpp"

#include <cstring>

namespace runtime_can
{

namespace
{

std::uint16_t decode_u16(const std::array<std::uint8_t, kPayloadSize> & data)
{
  return static_cast<std::uint16_t>(
    (static_cast<std::uint16_t>(data[1]) << 8U) |
    static_cast<std::uint16_t>(data[2]));
}

}

bool Command::operator==(const Command & other) const
{
  return command_id == other.command_id && opcode == other.opcode && argument == other.argument;
}

RawFrame encode_command(const Command & command)
{
  RawFrame frame;
  frame.id = kCommandFrameId;
  frame.size = kPayloadSize;
  frame.data[0] = kProtocolVersion;
  frame.data[1] = static_cast<std::uint8_t>(command.command_id >> 8U);
  frame.data[2] = static_cast<std::uint8_t>(command.command_id);
  frame.data[3] = command.opcode;

  std::uint32_t argument_bits;
  std::memcpy(&argument_bits, &command.argument, sizeof(argument_bits));
  frame.data[4] = static_cast<std::uint8_t>(argument_bits >> 24U);
  frame.data[5] = static_cast<std::uint8_t>(argument_bits >> 16U);
  frame.data[6] = static_cast<std::uint8_t>(argument_bits >> 8U);
  frame.data[7] = static_cast<std::uint8_t>(argument_bits);
  return frame;
}

RawFrame encode_response(const Response & response)
{
  RawFrame frame;
  frame.id = kResponseFrameId;
  frame.size = kPayloadSize;
  frame.data[0] = kProtocolVersion;
  frame.data[1] = static_cast<std::uint8_t>(response.command_id >> 8U);
  frame.data[2] = static_cast<std::uint8_t>(response.command_id);
  frame.data[3] = response.result_code;
  frame.data[4] = response.device_mode;
  frame.data[5] = response.detail[0];
  frame.data[6] = response.detail[1];
  frame.data[7] = response.detail[2];
  return frame;
}

std::optional<Command> decode_command(const RawFrame & frame, DecodeError & error)
{
  if (frame.id != kCommandFrameId) {
    error = DecodeError::kWrongFrameId;
    return std::nullopt;
  }
  if (frame.size != kPayloadSize) {
    error = DecodeError::kWrongPayloadSize;
    return std::nullopt;
  }
  if (frame.data[0] != kProtocolVersion) {
    error = DecodeError::kUnsupportedVersion;
    return std::nullopt;
  }

  std::uint32_t argument_bits =
    (static_cast<std::uint32_t>(frame.data[4]) << 24U) |
    (static_cast<std::uint32_t>(frame.data[5]) << 16U) |
    (static_cast<std::uint32_t>(frame.data[6]) << 8U) |
    static_cast<std::uint32_t>(frame.data[7]);
  std::int32_t argument;
  std::memcpy(&argument, &argument_bits, sizeof(argument));
  error = DecodeError::kNone;
  return Command{decode_u16(frame.data), frame.data[3], argument};
}

std::optional<Response> decode_response(const RawFrame & frame, DecodeError & error)
{
  if (frame.id != kResponseFrameId) {
    error = DecodeError::kWrongFrameId;
    return std::nullopt;
  }
  if (frame.size != kPayloadSize) {
    error = DecodeError::kWrongPayloadSize;
    return std::nullopt;
  }
  if (frame.data[0] != kProtocolVersion) {
    error = DecodeError::kUnsupportedVersion;
    return std::nullopt;
  }

  error = DecodeError::kNone;
  return Response{
    decode_u16(frame.data), frame.data[3], frame.data[4],
    {frame.data[5], frame.data[6], frame.data[7]}};
}

std::string_view to_string(DecodeError error)
{
  switch (error) {
    case DecodeError::kNone:
      return "none";
    case DecodeError::kWrongFrameId:
      return "wrong_frame_id";
    case DecodeError::kWrongPayloadSize:
      return "wrong_payload_size";
    case DecodeError::kUnsupportedVersion:
      return "unsupported_version";
  }
  return "unknown";
}

}
