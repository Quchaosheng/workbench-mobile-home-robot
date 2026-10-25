#include "task_executor_node.hpp"

#include <csignal>
#include <cstdlib>
#include <exception>
#include <memory>

#include "rclcpp/rclcpp.hpp"

int main(int argc, char ** argv)
{
  rclcpp::init(
    argc, argv, rclcpp::InitOptions{}, rclcpp::SignalHandlerOptions::None);
  std::signal(SIGINT, task_executor::request_task_executor_process_shutdown);
  std::signal(SIGTERM, task_executor::request_task_executor_process_shutdown);
  try {
    auto node = task_executor::make_task_executor_node();
    const int result = task_executor::run_task_executor_node(node);
    if (result == 2 || result == 3) {
      std::_Exit(result);
    }
    rclcpp::shutdown();
    return result;
  } catch (const std::exception & error) {
    RCLCPP_FATAL(rclcpp::get_logger("task_executor"), "%s", error.what());
    rclcpp::shutdown();
    return 1;
  }
}
