// Joint command encoding for the SocketCAN system interface (issue #541).
//
// This file is deliberately ROS-free. The frame layout and the refusals are the
// part worth testing on any host, and a test that needs a ROS installation to run
// is a test that does not run in most places. `can_system_interface.cpp` wraps
// this; everything about *what may be sent* lives here.
//
// The vocabulary is copied from `interfaces/json_schema/mcu_protocol.schema.json`
// rather than invented: `opcode`, `device_mode` and `fault_code` are the schema's
// enums, and a value outside them is refused rather than passed through.
//
// What this does NOT do: it does not prove a frame reached an MCU. It proves a
// command was valid and was handed to a bus. Hardware stays NOT_EXECUTED.

#ifndef WORKBENCH_MOBILE_BASE_HARDWARE__JOINT_FRAME_CODEC_HPP_
#define WORKBENCH_MOBILE_BASE_HARDWARE__JOINT_FRAME_CODEC_HPP_

#include <cstdint>
#include <string>
#include <vector>

namespace workbench_mobile_base_hardware
{

/// The register a joint's command is written to, and its legal range.
///
/// The range is not a suggestion: an out-of-range command is refused, never
/// clipped. Clipping hides an upper-layer bug by turning "the planner asked for
/// 4.2 rad" into "the arm moved 3.9 rad and nobody said anything".
struct JointLimit
{
  std::string name;
  double min{0.0};
  double max{0.0};
};

/// The arbitration IDs this interface is allowed to use.
///
/// An unset table is a configure-time failure, not a default. A default would let
/// the interface run against a bus whose IDs nobody checked, which is how a
/// command lands on a different device than intended.
struct ArbitrationTable
{
  std::uint32_t command{0};
  std::uint32_t state{0};
  bool set{false};
};

enum class Refusal
{
  kNone = 0,
  kUnknownJoint,
  kOutOfRange,
  kTableUnset,
  kTooManyJoints,
  kFrameTooLong,
};

const char * to_string(Refusal refusal);

/// One decoded joint command, ready to be written as a frame.
struct JointCommand
{
  std::uint32_t arbitration_id{0};
  std::uint8_t sequence_no{0};
  std::vector<std::int16_t> positions;  // milliradians, little-endian on the wire
};

/// The result of an encode attempt. `ok()` is the only thing a caller may branch
/// on; a refusal always carries a reason.
struct EncodeResult
{
  Refusal refusal{Refusal::kNone};
  std::string reason;
  std::vector<std::uint8_t> bytes;

  bool ok() const { return refusal == Refusal::kNone; }
};

/// Encode one command frame.
///
/// Refuses when the arbitration table is unset, when a joint is unknown, when a
/// value is outside its limit, or when the payload would exceed a Classic CAN
/// frame. The last case is a refusal rather than a split: a split command is a
/// partial command, and a partial command that a controller half-applied is worse
/// than a rejected one.
EncodeResult encode_joint_command(
  const ArbitrationTable & table,
  const std::vector<JointLimit> & limits,
  const std::vector<double> & positions,
  std::uint8_t sequence_no);

/// Decode a state frame into joint positions.
///
/// Returns false when the frame is short or the arbitration id does not match the
/// table, so a frame from another device cannot be read as joint state.
bool decode_joint_state(
  const ArbitrationTable & table,
  const std::vector<std::uint8_t> & bytes,
  std::vector<std::int16_t> & positions_out);

/// The stale-state decision, stated as a function so a caller cannot decide for
/// itself that a slightly old frame is fine.
///
/// A stale frame stops the interface accepting commands and reports an error. It
/// never repeats the last command: repeating a command nobody re-issued is how a
/// stopped robot starts moving again.
struct StateAgeVerdict
{
  bool accept_commands{false};
  bool report_error{false};
  std::string reason;
};

StateAgeVerdict judge_state_age(double age_seconds, double timeout_seconds);

}  // namespace workbench_mobile_base_hardware

#endif  // WORKBENCH_MOBILE_BASE_HARDWARE__JOINT_FRAME_CODEC_HPP_
