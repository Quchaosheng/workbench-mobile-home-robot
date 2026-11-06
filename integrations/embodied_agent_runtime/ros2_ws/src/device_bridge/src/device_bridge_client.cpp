#include "robot_task_interfaces/action/execute_device_command.hpp"

#include <chrono>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"

class DeviceBridgeClient : public rclcpp::Node
{
public:
  using ExecuteDeviceCommand = robot_task_interfaces::action::ExecuteDeviceCommand;
  using GoalHandle = rclcpp_action::ClientGoalHandle<ExecuteDeviceCommand>;

  DeviceBridgeClient()
  : Node("device_bridge_client")
  {
    command_id_ = declare_parameter<std::int64_t>("command_id", 1);
    opcode_ = declare_parameter<std::int64_t>("opcode", 1);
    argument_ = declare_parameter<std::int64_t>("argument", 0);
    ack_timeout_ms_ = declare_parameter<std::int64_t>("ack_timeout_ms", 1000);
    cancel_after_ms_ = declare_parameter<std::int64_t>("cancel_after_ms", -1);
    action_client_ = rclcpp_action::create_client<ExecuteDeviceCommand>(
      this, "execute_device_command");
  }

  bool send_goal()
  {
    using namespace std::chrono_literals;
    if (!parameters_fit_interface()) {
      exit_code_ = 6;
      return false;
    }
    if (!action_client_->wait_for_action_server(5s)) {
      RCLCPP_ERROR(get_logger(), "Action server not available after 5 seconds");
      exit_code_ = 1;
      return false;
    }

    ExecuteDeviceCommand::Goal goal;
    goal.command_id = static_cast<std::uint32_t>(command_id_);
    goal.opcode = static_cast<std::uint8_t>(opcode_);
    goal.argument = static_cast<std::int32_t>(argument_);
    goal.ack_timeout.sec = static_cast<std::int32_t>(ack_timeout_ms_ / 1000);
    goal.ack_timeout.nanosec = static_cast<std::uint32_t>(
      (ack_timeout_ms_ % 1000) * 1000000);

    rclcpp_action::Client<ExecuteDeviceCommand>::SendGoalOptions options;
    options.goal_response_callback = std::bind(
      &DeviceBridgeClient::goal_response_callback, this, std::placeholders::_1);
    options.feedback_callback = std::bind(
      &DeviceBridgeClient::feedback_callback, this, std::placeholders::_1,
      std::placeholders::_2);
    options.result_callback = std::bind(
      &DeviceBridgeClient::result_callback, this, std::placeholders::_1);

    RCLCPP_INFO(
      get_logger(),
      "Sending command_id=%ld opcode=%ld argument=%ld ack_timeout_ms=%ld cancel_after_ms=%ld",
      command_id_, opcode_, argument_, ack_timeout_ms_, cancel_after_ms_);
    action_client_->async_send_goal(goal, options);
    return true;
  }

  int exit_code() const
  {
    return exit_code_;
  }

private:
  bool parameters_fit_interface()
  {
    const bool valid =
      command_id_ >= 0 &&
      command_id_ <= std::numeric_limits<std::uint32_t>::max() &&
      opcode_ >= 0 && opcode_ <= std::numeric_limits<std::uint8_t>::max() &&
      argument_ >= std::numeric_limits<std::int32_t>::min() &&
      argument_ <= std::numeric_limits<std::int32_t>::max() &&
      ack_timeout_ms_ >= 0 &&
      ack_timeout_ms_ <= static_cast<std::int64_t>(std::numeric_limits<std::int32_t>::max()) * 1000;
    if (!valid) {
      RCLCPP_ERROR(get_logger(), "Client parameters do not fit Action field ranges");
    }
    return valid;
  }

  void goal_response_callback(const GoalHandle::SharedPtr & goal_handle)
  {
    if (!goal_handle) {
      RCLCPP_WARN(get_logger(), "Goal rejected");
      exit_code_ = 2;
      rclcpp::shutdown();
      return;
    }

    RCLCPP_INFO(get_logger(), "Goal accepted");
    goal_handle_ = goal_handle;
    if (cancel_after_ms_ >= 0) {
      cancel_timer_ = create_wall_timer(
        std::chrono::milliseconds(cancel_after_ms_),
        [this]() {
          cancel_timer_->cancel();
          RCLCPP_INFO(get_logger(), "Sending cancel request");
          action_client_->async_cancel_goal(goal_handle_);
        });
    }
  }

  void feedback_callback(
    GoalHandle::SharedPtr,
    const std::shared_ptr<const ExecuteDeviceCommand::Feedback> feedback)
  {
    RCLCPP_INFO(
      get_logger(), "Feedback state=%u retry_count=%u",
      feedback->state, feedback->retry_count);
  }

  void result_callback(const GoalHandle::WrappedResult & wrapped_result)
  {
    if (cancel_timer_) {
      cancel_timer_->cancel();
    }

    switch (wrapped_result.code) {
      case rclcpp_action::ResultCode::SUCCEEDED:
        exit_code_ = 0;
        break;
      case rclcpp_action::ResultCode::CANCELED:
        exit_code_ = 3;
        break;
      case rclcpp_action::ResultCode::ABORTED:
        exit_code_ = 4;
        break;
      default:
        exit_code_ = 5;
        break;
    }

    RCLCPP_INFO(
      get_logger(), "Result code=%d outcome=%u error_code=%u message=%s",
      static_cast<int>(wrapped_result.code), wrapped_result.result->outcome,
      wrapped_result.result->error_code, wrapped_result.result->message.c_str());
    rclcpp::shutdown();
  }

  std::int64_t command_id_;
  std::int64_t opcode_;
  std::int64_t argument_;
  std::int64_t ack_timeout_ms_;
  std::int64_t cancel_after_ms_;
  int exit_code_{5};
  rclcpp_action::Client<ExecuteDeviceCommand>::SharedPtr action_client_;
  GoalHandle::SharedPtr goal_handle_;
  rclcpp::TimerBase::SharedPtr cancel_timer_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto node = std::make_shared<DeviceBridgeClient>();
  if (!node->send_goal()) {
    rclcpp::shutdown();
    return node->exit_code();
  }
  rclcpp::spin(node);
  return node->exit_code();
}
