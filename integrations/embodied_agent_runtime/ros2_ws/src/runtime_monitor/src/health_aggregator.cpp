#include "runtime_monitor/health_aggregator.hpp"

#include <algorithm>
#include <stdexcept>

namespace runtime_monitor
{
HealthAggregator::HealthAggregator(const std::chrono::milliseconds stale_timeout)
: stale_timeout_(stale_timeout)
{
  if (stale_timeout_.count() <= 0) {
    throw std::invalid_argument("stale_timeout must be positive");
  }
}

bool HealthAggregator::update(
  const diagnostic_msgs::msg::DiagnosticStatus & status, const SteadyTime received_at)
{
  StoredStatus * target = nullptr;
  if (status.name == kDeviceBridgeName) {
    target = &device_bridge_;
  } else if (status.name == kTaskExecutorName) {
    target = &task_executor_;
  } else {
    return false;
  }

  target->seen = true;
  target->level = status.level <= diagnostic_msgs::msg::DiagnosticStatus::STALE ?
    status.level : diagnostic_msgs::msg::DiagnosticStatus::ERROR;
  target->message = status.message;
  target->received_at = received_at;
  return true;
}

ComponentHealth HealthAggregator::evaluate_component(
  const StoredStatus & stored, const SteadyTime now) const
{
  if (!stored.seen) {
    return {};
  }
  const auto raw_age = std::chrono::duration_cast<std::chrono::milliseconds>(
    now - stored.received_at);
  const auto age = std::max<std::int64_t>(raw_age.count(), 0);
  if (age > stale_timeout_.count()) {
    return ComponentHealth{
      true, diagnostic_msgs::msg::DiagnosticStatus::STALE, "diagnostics stale", age};
  }
  return ComponentHealth{true, stored.level, stored.message, age};
}

SystemHealth HealthAggregator::evaluate(const SteadyTime now) const
{
  SystemHealth health;
  health.device_bridge = evaluate_component(device_bridge_, now);
  health.task_executor = evaluate_component(task_executor_, now);

  if (!health.device_bridge.seen || !health.task_executor.seen ||
    health.device_bridge.level == diagnostic_msgs::msg::DiagnosticStatus::STALE ||
    health.task_executor.level == diagnostic_msgs::msg::DiagnosticStatus::STALE)
  {
    health.level = diagnostic_msgs::msg::DiagnosticStatus::STALE;
    health.message = "core diagnostics missing or stale";
  } else if (
    health.device_bridge.level == diagnostic_msgs::msg::DiagnosticStatus::ERROR ||
    health.task_executor.level == diagnostic_msgs::msg::DiagnosticStatus::ERROR)
  {
    health.level = diagnostic_msgs::msg::DiagnosticStatus::ERROR;
    health.message = "core component error";
  } else if (
    health.device_bridge.level == diagnostic_msgs::msg::DiagnosticStatus::WARN ||
    health.task_executor.level == diagnostic_msgs::msg::DiagnosticStatus::WARN)
  {
    health.level = diagnostic_msgs::msg::DiagnosticStatus::WARN;
    health.message = "runtime degraded";
    health.ready = true;
  } else {
    health.level = diagnostic_msgs::msg::DiagnosticStatus::OK;
    health.message = "runtime ready";
    health.ready = true;
  }
  return health;
}
}  // namespace runtime_monitor
