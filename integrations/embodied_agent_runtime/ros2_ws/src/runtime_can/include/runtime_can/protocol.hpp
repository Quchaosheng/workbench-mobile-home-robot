#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string_view>

namespace runtime_can
{

constexpr std::uint32_t kCommandFrameId = 0x100;
constexpr std::uint32_t kResponseFrameId = 0x101;
constexpr std::uint8_t kProtocolVersion = 1;
constexpr std::uint8_t kPayloadSize = 8;
constexpr std::uint8_t kStopOpcode = 0xFF;
constexpr std::uint16_t kApplicationCommandIdMax = 0x7FFF;
// STOP ACKs use a disjoint command-id range and cannot match an application ACK.
constexpr std::uint16_t kStopCommandIdMin = 0x8000;

struct RawFrame
{
  std::uint32_t id{0};
  std::uint8_t size{0};
  std::array<std::uint8_t, kPayloadSize> data{};
};

struct Command
{
  std::uint16_t command_id{0};
  std::uint8_t opcode{0};
  std::int32_t argument{0};

  bool operator==(const Command & other) const;
};

struct Response
{
  std::uint16_t command_id{0};
  std::uint8_t result_code{0};
  std::uint8_t device_mode{0};
  std::array<std::uint8_t, 3> detail{};
};

enum class DecodeError
{
  kNone,
  kWrongFrameId,
  kWrongPayloadSize,
  kUnsupportedVersion
};

RawFrame encode_command(const Command & command);
RawFrame encode_response(const Response & response);
std::optional<Command> decode_command(const RawFrame & frame, DecodeError & error);
std::optional<Response> decode_response(const RawFrame & frame, DecodeError & error);
std::string_view to_string(DecodeError error);

}
