#include "runtime_monitor/health_aggregator.hpp"

#include <chrono>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>

#include "diagnostic_msgs/msg/diagnostic_array.hpp"
#include "diagnostic_msgs/msg/key_value.hpp"
#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/bool.hpp"

class RuntimeMonitorNode : public rclcpp::Node
{
public:
  RuntimeMonitorNode()
  : Node("runtime_monitor"),
    aggregate_period_(declare_positive_ms("aggregate_period_ms", 500)),
    stale_timeout_(declare_positive_ms("stale_timeout_ms", 2000)),
    aggregator_(stale_timeout_)
  {
    diagnostics_publisher_ = create_publisher<diagnostic_msgs::msg::DiagnosticArray>(
      "/diagnostics", rclcpp::QoS(10).reliable());
    ready_publisher_ = create_publisher<std_msgs::msg::Bool>(
      "/runtime/ready", rclcpp::QoS(1).reliable());
    diagnostics_subscription_ = create_subscription<diagnostic_msgs::msg::DiagnosticArray>(
      "/diagnostics", rclcpp::QoS(10).reliable(),
      [this](const diagnostic_msgs::msg::DiagnosticArray::SharedPtr message) {
        const auto received_at = std::chrono::steady_clock::now();
        bool source_updated = false;
        for (const auto & status : message->status) {
          source_updated = aggregator_.update(status, received_at) || source_updated;
        }
        if (source_updated) {
          publish_health();
        }
      });
    timer_ = create_wall_timer(aggregate_period_, [this]() {publish_health();});
  }

private:
  std::chrono::milliseconds declare_positive_ms(
    const std::string & name, const std::int64_t default_value)
  {
    const auto value = declare_parameter<std::int64_t>(name, default_value);
    if (value <= 0 || value > 60000) {
      throw std::invalid_argument(name + " must be in [1, 60000] ms");
    }
    return std::chrono::milliseconds(value);
  }

  static diagnostic_msgs::msg::KeyValue key_value(
    const std::string & key, const std::string & value)
  {
    diagnostic_msgs::msg::KeyValue result;
    result.key = key;
    result.value = value;
    return result;
  }

  void publish_health()
  {
    const auto health = aggregator_.evaluate(std::chrono::steady_clock::now());
    diagnostic_msgs::msg::DiagnosticStatus status;
    status.name = runtime_monitor::kSystemName;
    status.hardware_id = "robot_runtime";
    status.level = health.level;
    status.message = health.message;
    status.values = {
      key_value("ready", health.ready ? "true" : "false"),
      key_value("device_bridge_level", std::to_string(health.device_bridge.level)),
      key_value("device_bridge_age_ms", std::to_string(health.device_bridge.age_ms)),
      key_value("task_executor_level", std::to_string(health.task_executor.level)),
      key_value("task_executor_age_ms", std::to_string(health.task_executor.age_ms))};

    diagnostic_msgs::msg::DiagnosticArray array;
    array.header.stamp = now();
    array.status.push_back(status);
    diagnostics_publisher_->publish(array);

    std_msgs::msg::Bool ready;
    ready.data = health.ready;
    ready_publisher_->publish(ready);
  }

  std::chrono::milliseconds aggregate_period_;
  std::chrono::milliseconds stale_timeout_;
  runtime_monitor::HealthAggregator aggregator_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diagnostics_publisher_;
  rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr ready_publisher_;
  rclcpp::Subscription<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diagnostics_subscription_;
  rclcpp::TimerBase::SharedPtr timer_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  try {
    rclcpp::spin(std::make_shared<RuntimeMonitorNode>());
  } catch (const std::exception & error) {
    RCLCPP_FATAL(rclcpp::get_logger("runtime_monitor"), "%s", error.what());
    rclcpp::shutdown();
    return 1;
  }
  rclcpp::shutdown();
  return 0;
}
