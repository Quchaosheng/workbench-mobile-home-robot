#pragma once

#include <chrono>
#include <functional>
#include <memory>

#include "rclcpp/rclcpp.hpp"

namespace task_executor
{

enum class WorkerPhase
{
  kBeforeFeedback,
  kBeforeSendGoal,
  kAfterSendGoal,
  kBeforeCancelGoal,
  kBeforeSuccess,
  kAfterChildAccepted,
  kBeforeParentTransition
};

using WorkerHook = std::function<void(WorkerPhase)>;
using SpinFunction = std::function<void(rclcpp::executors::MultiThreadedExecutor &)>;

std::shared_ptr<rclcpp::Node> make_task_executor_node(
  const rclcpp::NodeOptions & options = rclcpp::NodeOptions(),
  WorkerHook worker_hook = {});
void request_task_executor_shutdown(const std::shared_ptr<rclcpp::Node> & node);
void wait_for_task_executor_shutdown(const std::shared_ptr<rclcpp::Node> & node);
bool wait_for_task_executor_shutdown_for(
  const std::shared_ptr<rclcpp::Node> & node, std::chrono::milliseconds timeout);
void request_task_executor_process_shutdown(int);
int run_task_executor_node(
  const std::shared_ptr<rclcpp::Node> & node, SpinFunction primary_spin = {},
  SpinFunction recovery_spin = {});

}  // namespace task_executor
