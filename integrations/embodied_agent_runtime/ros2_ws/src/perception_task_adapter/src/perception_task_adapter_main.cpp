#include "perception_task_adapter/perception_task_adapter_node.hpp"

#include <chrono>
#include <cstdio>
#include <exception>
#include <string>
#include <thread>

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  try {
    auto node = std::make_shared<perception_task_adapter::PerceptionTaskAdapterNode>();
    const auto input_mode = node->get_parameter("input_mode").as_string();
    int run_code = 0;
    if (input_mode == "image") {
      run_code = node->run_image();
      if (run_code != 0) {
        node->cancel_active();
        rclcpp::shutdown();
        return run_code;
      }
      while (rclcpp::ok() && !node->finished()) {
        rclcpp::spin_some(node);
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      }
    } else {
      run_code = node->run_camera();
    }
    node->cancel_active();
    const int exit_code = run_code == 0 ? node->exit_code() : run_code;
    rclcpp::shutdown();
    return exit_code;
  } catch (const std::exception & error) {
    fprintf(stderr, "perception_task_adapter startup failed: %s\n", error.what());
    rclcpp::shutdown();
    return 2;
  }
}
