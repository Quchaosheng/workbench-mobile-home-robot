#pragma once

#include <functional>
#include <memory>

#include "rclcpp/rclcpp.hpp"

namespace task_orchestrator
{

enum class FaultPoint
{
  kBeforeCancelDispatch,
  kBeforeTerminalCommit,
  kBeforeTerminalTransition,
  kBeforeFallbackAbort
};

using FaultHook = std::function<void(FaultPoint)>;
using SpinFunction = std::function<void(rclcpp::executors::MultiThreadedExecutor &)>;
using StopRequested = std::function<bool()>;

std::shared_ptr<rclcpp::Node> make_task_orchestrator_node(
  const rclcpp::NodeOptions & options = rclcpp::NodeOptions(), FaultHook fault_hook = {});

int run_task_orchestrator_node(
  const std::shared_ptr<rclcpp::Node> & node, SpinFunction primary_spin = {},
  SpinFunction recovery_spin = {}, StopRequested stop_requested = {});

}  // namespace task_orchestrator
