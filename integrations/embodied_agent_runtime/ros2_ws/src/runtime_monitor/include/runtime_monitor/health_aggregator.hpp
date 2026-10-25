#pragma once

#include <chrono>
#include <cstdint>
#include <string>

#include "diagnostic_msgs/msg/diagnostic_status.hpp"

namespace runtime_monitor
{
inline constexpr char kDeviceBridgeName[] = "runtime/device_bridge";
inline constexpr char kTaskExecutorName[] = "runtime/task_executor";
inline constexpr char kSystemName[] = "runtime/system";

using SteadyTime = std::chrono::steady_clock::time_point;

struct ComponentHealth
{
  bool seen{false};
  std::uint8_t level{diagnostic_msgs::msg::DiagnosticStatus::STALE};
  std::string message{"not received"};
  std::int64_t age_ms{-1};
};

struct SystemHealth
{
  bool ready{false};
  std::uint8_t level{diagnostic_msgs::msg::DiagnosticStatus::STALE};
  std::string message{"waiting for core diagnostics"};
  ComponentHealth device_bridge;
  ComponentHealth task_executor;
};

class HealthAggregator
{
public:
  explicit HealthAggregator(std::chrono::milliseconds stale_timeout);
  bool update(const diagnostic_msgs::msg::DiagnosticStatus & status, SteadyTime received_at);
  SystemHealth evaluate(SteadyTime now) const;

private:
  struct StoredStatus
  {
    bool seen{false};
    std::uint8_t level{diagnostic_msgs::msg::DiagnosticStatus::STALE};
    std::string message{"not received"};
    SteadyTime received_at{};
  };

  ComponentHealth evaluate_component(const StoredStatus & stored, SteadyTime now) const;

  std::chrono::milliseconds stale_timeout_;
  StoredStatus device_bridge_;
  StoredStatus task_executor_;
};
}  // namespace runtime_monitor
