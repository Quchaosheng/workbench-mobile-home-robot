#include "runtime_history/recorder.hpp"
#include "runtime_history/defaults.hpp"
#include "runtime_history/store.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <string>
#include <utility>

#include "diagnostic_msgs/msg/diagnostic_array.hpp"
#include "diagnostic_msgs/msg/key_value.hpp"
#include "rclcpp/rclcpp.hpp"
#include "robot_task_interfaces/msg/task_event.hpp"

class RuntimeHistoryNode : public rclcpp::Node
{
public:
  RuntimeHistoryNode()
  : Node("runtime_history"),
    database_path_(declare_parameter<std::string>(
        "database_path", runtime_history::kDefaultDatabasePath)),
    store_(database_path_),
    recorder_([this](const runtime_history::TaskRecord & record) {return store_.insert(record);})
  {
    diagnostics_publisher_ = create_publisher<diagnostic_msgs::msg::DiagnosticArray>(
      "/diagnostics", rclcpp::QoS(10).reliable());
    event_subscription_ = create_subscription<robot_task_interfaces::msg::TaskEvent>(
      "/runtime/task_events", rclcpp::QoS(32).reliable(),
      [this](const robot_task_interfaces::msg::TaskEvent::SharedPtr event) {
        runtime_history::TaskRecord record{
          event->task_id, event->target_id, event->action_status, event->outcome,
          event->error_code, event->duration_ms, event->message,
          static_cast<std::int64_t>(event->stamp.sec) * 1000000000LL + event->stamp.nanosec};
        if (!recorder_.enqueue(std::move(record))) {
          ++queue_rejections_;
          publish_diagnostics();
        }
      });
    diagnostics_timer_ = create_wall_timer(
      std::chrono::milliseconds(500), [this]() {publish_diagnostics();});
  }

private:
  static diagnostic_msgs::msg::KeyValue key_value(
    const std::string & key, const std::uint64_t value)
  {
    diagnostic_msgs::msg::KeyValue result;
    result.key = key;
    result.value = std::to_string(value);
    return result;
  }

  void publish_diagnostics()
  {
    const auto write_errors = recorder_.write_errors();
    const auto queue_rejections = queue_rejections_.load();
    diagnostic_msgs::msg::DiagnosticStatus status;
    status.name = "runtime/history";
    status.hardware_id = "robot_runtime";
    status.level = write_errors == 0 && queue_rejections == 0 ?
      diagnostic_msgs::msg::DiagnosticStatus::OK : diagnostic_msgs::msg::DiagnosticStatus::ERROR;
    status.message = status.level == diagnostic_msgs::msg::DiagnosticStatus::OK ?
      "history available" : "history records lost";
    status.values = {
      key_value("persisted", recorder_.persisted()),
      key_value("write_errors", write_errors),
      key_value("queue_rejections", queue_rejections)};

    diagnostic_msgs::msg::DiagnosticArray message;
    message.header.stamp = now();
    message.status.push_back(std::move(status));
    diagnostics_publisher_->publish(message);
  }

  std::string database_path_;
  runtime_history::Store store_;
  runtime_history::Recorder recorder_;
  std::atomic<std::uint64_t> queue_rejections_{};
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diagnostics_publisher_;
  rclcpp::Subscription<robot_task_interfaces::msg::TaskEvent>::SharedPtr event_subscription_;
  rclcpp::TimerBase::SharedPtr diagnostics_timer_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  try {
    rclcpp::spin(std::make_shared<RuntimeHistoryNode>());
  } catch (const std::exception & error) {
    RCLCPP_FATAL(rclcpp::get_logger("runtime_history"), "%s", error.what());
    rclcpp::shutdown();
    return 1;
  }
  rclcpp::shutdown();
  return 0;
}
