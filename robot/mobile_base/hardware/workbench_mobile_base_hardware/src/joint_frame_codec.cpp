// See joint_frame_codec.hpp. ROS-free on purpose: the refusals are the part worth
// testing anywhere, and this file needs no ROS installation to compile.

#include "workbench_mobile_base_hardware/joint_frame_codec.hpp"

#include <cmath>
#include <limits>
#include <sstream>

namespace workbench_mobile_base_hardware
{

namespace
{

//: A Classic CAN frame carries eight data bytes. The first is the sequence number,
//: the rest are two-byte joint positions, so three joints fit.
constexpr std::size_t kClassicCanPayload = 8;
constexpr std::size_t kMaxJoints = (kClassicCanPayload - 1) / 2;

//: Milliradians per radian. Integer positions keep the wire format exact and the
//: comparison in the tests exact; a float payload would make the round trip depend
//: on the compiler's libm.
constexpr double kMilliradPerRad = 1000.0;

}  // namespace

const char * to_string(Refusal refusal)
{
  switch (refusal) {
    case Refusal::kNone:
      return "none";
    case Refusal::kUnknownJoint:
      return "unknown_joint";
    case Refusal::kOutOfRange:
      return "out_of_range";
    case Refusal::kTableUnset:
      return "arbitration_table_unset";
    case Refusal::kTooManyJoints:
      return "too_many_joints";
    case Refusal::kFrameTooLong:
      return "frame_too_long";
  }
  return "unrecognised";
}

EncodeResult encode_joint_command(
  const ArbitrationTable & table,
  const std::vector<JointLimit> & limits,
  const std::vector<double> & positions,
  std::uint8_t sequence_no)
{
  EncodeResult result;

  // The table first: a frame with no arbitration id is not a frame, and a default
  // id is how a command reaches a device nobody meant to address.
  if (!table.set || table.command == 0) {
    result.refusal = Refusal::kTableUnset;
    result.reason =
      "the arbitration id table is not set, so a command frame has no destination; "
      "configure must refuse rather than fall back to a default id";
    return result;
  }

  if (positions.size() != limits.size()) {
    result.refusal = Refusal::kUnknownJoint;
    std::ostringstream message;
    message << "got " << positions.size() << " positions for " << limits.size()
            << " configured joints; a command must name every joint it drives";
    result.reason = message.str();
    return result;
  }

  if (positions.size() > kMaxJoints) {
    result.refusal = Refusal::kTooManyJoints;
    std::ostringstream message;
    message << positions.size() << " joints do not fit a Classic CAN frame with a sequence "
            << "number (" << kMaxJoints << " maximum); a split command is a partial command";
    result.reason = message.str();
    return result;
  }

  // Range is checked against the limit, and a non-finite value is out of range for
  // every limit rather than becoming an unspecified integer cast.
  for (std::size_t index = 0; index < positions.size(); ++index) {
    const double value = positions[index];
    if (!std::isfinite(value)) {
      result.refusal = Refusal::kOutOfRange;
      result.reason = "joint " + limits[index].name + " has a non-finite command";
      return result;
    }
    if (value < limits[index].min || value > limits[index].max) {
      result.refusal = Refusal::kOutOfRange;
      std::ostringstream message;
      message << "joint " << limits[index].name << " command " << value << " is outside ["
              << limits[index].min << ", " << limits[index].max
              << "]; refused rather than clipped";
      result.reason = message.str();
      return result;
    }
  }

  result.bytes.reserve(1 + positions.size() * 2);
  result.bytes.push_back(sequence_no);
  for (const double value : positions) {
    const auto milli = static_cast<std::int32_t>(std::lround(value * kMilliradPerRad));
    result.bytes.push_back(static_cast<std::uint8_t>(milli & 0xFF));
    result.bytes.push_back(static_cast<std::uint8_t>((milli >> 8) & 0xFF));
  }

  if (result.bytes.size() > kClassicCanPayload) {
    // Unreachable given kMaxJoints, and kept because a future payload change must
    // hit a refusal rather than silently truncate.
    result.refusal = Refusal::kFrameTooLong;
    result.reason = "the encoded payload exceeds a Classic CAN frame";
    result.bytes.clear();
    return result;
  }
  return result;
}

bool decode_joint_state(
  const ArbitrationTable & table,
  const std::vector<std::uint8_t> & bytes,
  std::vector<std::int16_t> & positions_out)
{
  positions_out.clear();
  if (!table.set || table.state == 0) {
    return false;
  }
  // A state frame carries no sequence number, so the payload is entirely positions
  // and must be an even length.
  if (bytes.size() < 2 || bytes.size() % 2 != 0) {
    return false;
  }
  positions_out.reserve(bytes.size() / 2);
  for (std::size_t index = 0; index + 1 < bytes.size(); index += 2) {
    const auto raw = static_cast<std::int32_t>(
      static_cast<std::uint16_t>(bytes[index]) |
      (static_cast<std::uint16_t>(bytes[index + 1]) << 8));
    // Sign-extend from 16 bits.
    const auto signed_value = static_cast<std::int16_t>(raw);
    positions_out.push_back(signed_value);
  }
  return true;
}

StateAgeVerdict judge_state_age(double age_seconds, double timeout_seconds)
{
  StateAgeVerdict verdict;
  if (!std::isfinite(timeout_seconds) || timeout_seconds <= 0.0) {
    verdict.accept_commands = false;
    verdict.report_error = true;
    verdict.reason = "state_timeout_s must be positive; a timeout of zero would accept any frame";
    return verdict;
  }
  if (!std::isfinite(age_seconds) || age_seconds < 0.0) {
    verdict.accept_commands = false;
    verdict.report_error = true;
    verdict.reason = "a negative or non-finite state age is not a measurement; refusing";
    return verdict;
  }
  if (age_seconds > timeout_seconds) {
    verdict.accept_commands = false;
    verdict.report_error = true;
    std::ostringstream message;
    message << "the last state frame is " << age_seconds << "s old, beyond the "
            << timeout_seconds << "s timeout; commands are refused and the last command is NOT repeated";
    verdict.reason = message.str();
    return verdict;
  }
  verdict.accept_commands = true;
  verdict.report_error = false;
  verdict.reason = "state is fresh";
  return verdict;
}

}  // namespace workbench_mobile_base_hardware
