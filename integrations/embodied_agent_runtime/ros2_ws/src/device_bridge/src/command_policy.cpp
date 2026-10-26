#include "device_bridge/command_policy.hpp"

#include <limits>

namespace device_bridge
{

bool duration_to_milliseconds(
  const builtin_interfaces::msg::Duration & duration,
  const std::int64_t max_milliseconds,
  std::chrono::milliseconds & result)
{
  if (max_milliseconds <= 0 || duration.sec < 0 || duration.nanosec >= 1000000000U) {
    return false;
  }

  constexpr std::int64_t kNanosecondsPerSecond = 1000000000LL;
  constexpr std::int64_t kNanosecondsPerMillisecond = 1000000LL;
  const auto seconds = static_cast<std::int64_t>(duration.sec);
  const auto total_nanoseconds = seconds * kNanosecondsPerSecond + duration.nanosec;
  if (total_nanoseconds <= 0) {
    return false;
  }

  const auto rounded_milliseconds =
    (total_nanoseconds + kNanosecondsPerMillisecond - 1) / kNanosecondsPerMillisecond;
  if (rounded_milliseconds <= 0 || rounded_milliseconds > max_milliseconds) {
    return false;
  }

  result = std::chrono::milliseconds(rounded_milliseconds);
  return true;
}

std::uint16_t device_error_code(const std::uint8_t result_code)
{
  const auto code = static_cast<std::uint32_t>(kErrorDeviceRejectedBase) + result_code;
  return code > std::numeric_limits<std::uint16_t>::max() ?
         std::numeric_limits<std::uint16_t>::max() : static_cast<std::uint16_t>(code);
}

}
