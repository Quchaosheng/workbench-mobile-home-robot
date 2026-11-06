#include "device_bridge/command_policy.hpp"
#include "device_bridge/diagnostic_state.hpp"
#include "runtime_history/stage_event_log.hpp"

#include "diagnostic_msgs/msg/diagnostic_array.hpp"
#include "robot_task_interfaces/action/execute_device_command.hpp"
#include "robot_task_interfaces/msg/device_state.hpp"
#include "runtime_can/protocol.hpp"
#include "runtime_can/socket_can.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"

class DeviceBridgeNode : public rclcpp::Node
{
public:
  using ExecuteDeviceCommand = robot_task_interfaces::action::ExecuteDeviceCommand;
  using GoalHandle = rclcpp_action::ServerGoalHandle<ExecuteDeviceCommand>;

  DeviceBridgeNode()
  : Node("device_bridge")
  {
    interface_name_ = declare_parameter<std::string>("interface_name", "vcan0");
    const auto configured_max_timeout = declare_parameter<std::int64_t>(
      "max_ack_timeout_ms", 5000);
    const auto configured_poll = declare_parameter<std::int64_t>("receive_poll_ms", 25);
    const auto configured_stop_timeout = declare_parameter<std::int64_t>(
      "stop_timeout_ms", 500);
    diagnostic_period_ms_ = declare_parameter<std::int64_t>("diagnostic_period_ms", 1000);
    stage_log_ = std::make_unique<runtime_history::StageEventLog>(
      declare_parameter<std::string>("stage_event_log_path", ""));
    if (configured_max_timeout <= 0 || configured_stop_timeout <= 0 ||
      configured_stop_timeout > 60000)
    {
      throw std::invalid_argument("max_ack_timeout_ms must be positive");
    }
    if (diagnostic_period_ms_ <= 0 || diagnostic_period_ms_ > 60000) {
      throw std::invalid_argument("diagnostic_period_ms must be in [1, 60000]");
    }
    max_ack_timeout_ms_ = configured_max_timeout;
    receive_poll_ms_ = std::chrono::milliseconds(std::clamp<std::int64_t>(
        configured_poll, 1, 1000));
    stop_timeout_ms_ = std::chrono::milliseconds(configured_stop_timeout);

    std::string error;
    if (!socket_.open(interface_name_, error)) {
      throw std::runtime_error("failed to open " + interface_name_ + ": " + error);
    }
    if (!socket_.set_filter(runtime_can::kResponseFrameId, error)) {
      throw std::runtime_error("failed to set response filter: " + error);
    }

    diagnostics_publisher_ = create_publisher<diagnostic_msgs::msg::DiagnosticArray>(
      "/diagnostics", rclcpp::QoS(10).reliable());
    diagnostics_timer_ = create_wall_timer(
      std::chrono::milliseconds(diagnostic_period_ms_),
      [this]() {publish_diagnostics();});

    action_server_ = rclcpp_action::create_server<ExecuteDeviceCommand>(
      this,
      "execute_device_command",
      std::bind(&DeviceBridgeNode::handle_goal, this, std::placeholders::_1,
        std::placeholders::_2),
      std::bind(&DeviceBridgeNode::handle_cancel, this, std::placeholders::_1),
      std::bind(&DeviceBridgeNode::handle_accepted, this, std::placeholders::_1));

    RCLCPP_INFO(
      get_logger(),
      "Device Bridge ready on %s max_ack_timeout_ms=%ld stop_timeout_ms=%ld "
      "receive_poll_ms=%ld diagnostic_period_ms=%ld",
      interface_name_.c_str(), max_ack_timeout_ms_, stop_timeout_ms_.count(),
      receive_poll_ms_.count(), diagnostic_period_ms_);
  }

  ~DeviceBridgeNode() override
  {
    running_ = false;
    std::thread worker;
    {
      std::lock_guard<std::mutex> lock(workers_mutex_);
      worker.swap(worker_);
    }
    if (worker.joinable()) {
      worker.join();
    }
  }

private:
  enum class WaitResult
  {
    kResponse,
    kTimeout,
    kCanceled,
    kTransportError,
    kShutdown
  };

  static constexpr std::uint8_t kMaxRetries = 1;

  rclcpp_action::GoalResponse handle_goal(
    const rclcpp_action::GoalUUID &,
    std::shared_ptr<const ExecuteDeviceCommand::Goal> goal)
  {
    std::chrono::milliseconds ack_timeout;
    if (goal->command_id == 0 || goal->command_id > 0xFFFFU) {
      RCLCPP_WARN(
        get_logger(), "Rejecting command_id=%u: command_id must be in [1, 65535]",
        goal->command_id);
      return rclcpp_action::GoalResponse::REJECT;
    }
    if (!device_bridge::duration_to_milliseconds(
        goal->ack_timeout, max_ack_timeout_ms_, ack_timeout))
    {
      RCLCPP_WARN(
        get_logger(), "Rejecting command_id=%u: invalid ack_timeout", goal->command_id);
      return rclcpp_action::GoalResponse::REJECT;
    }
    if (!socket_.is_open()) {
      RCLCPP_WARN(
        get_logger(), "Rejecting command_id=%u: CAN socket is not ready",
        goal->command_id);
      return rclcpp_action::GoalResponse::REJECT;
    }

    bool expected = false;
    // A single active wire transaction keeps response matching unambiguous;
    // concurrent goals could otherwise consume each other's command ACKs.
    if (!active_goal_.compare_exchange_strong(expected, true)) {
      RCLCPP_WARN(get_logger(), "Rejecting command_id=%u: bridge is busy", goal->command_id);
      return rclcpp_action::GoalResponse::REJECT;
    }

    RCLCPP_INFO(
      get_logger(), "Accepting command_id=%u opcode=%u argument=%d ack_timeout_ms=%ld",
      goal->command_id, goal->opcode, goal->argument, ack_timeout.count());
    return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
  }

  rclcpp_action::CancelResponse handle_cancel(const std::shared_ptr<GoalHandle> goal_handle)
  {
    RCLCPP_INFO(
      get_logger(), "Accepting cancel request for command_id=%u",
      goal_handle->get_goal()->command_id);
    return rclcpp_action::CancelResponse::ACCEPT;
  }

  void handle_accepted(const std::shared_ptr<GoalHandle> goal_handle)
  {
    join_completed_worker();
    diagnostic_state_.begin_command(
      static_cast<std::uint16_t>(goal_handle->get_goal()->command_id));
    std::lock_guard<std::mutex> lock(workers_mutex_);
    worker_ = std::thread([this, goal_handle]() {execute(goal_handle);});
  }

  void join_completed_worker()
  {
    std::thread worker;
    {
      const std::lock_guard<std::mutex> lock(workers_mutex_);
      worker.swap(worker_);
    }
    if (worker.joinable()) {
      worker.join();
    }
  }

  WaitResult receive_response(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const std::uint16_t expected_command_id,
    const std::chrono::milliseconds timeout,
    const bool observe_cancel,
    runtime_can::Response & response,
    std::string & error)
  {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (running_ && rclcpp::ok()) {
      if (observe_cancel && goal_handle->is_canceling()) {
        return WaitResult::kCanceled;
      }

      const auto now = std::chrono::steady_clock::now();
      if (now >= deadline) {
        return WaitResult::kTimeout;
      }
      const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
      const auto poll_timeout = std::min(remaining, receive_poll_ms_);

      runtime_can::RawFrame frame;
      std::string receive_error;
      const auto status = socket_.receive(frame, poll_timeout, receive_error);
      if (status == runtime_can::ReceiveStatus::kTimeout) {
        continue;
      }
      if (status == runtime_can::ReceiveStatus::kError) {
        error = receive_error;
        return WaitResult::kTransportError;
      }

      runtime_can::DecodeError decode_error;
      const auto decoded = runtime_can::decode_response(frame, decode_error);
      if (!decoded) {
        RCLCPP_WARN(
          get_logger(), "Ignoring invalid response frame: %s",
          runtime_can::to_string(decode_error).data());
        continue;
      }
      if (decoded->command_id != expected_command_id) {
        RCLCPP_WARN(
          get_logger(), "Ignoring unmatched response command_id=%u expected=%u",
          decoded->command_id, expected_command_id);
        continue;
      }
      response = *decoded;
      return WaitResult::kResponse;
    }
    error = "bridge is shutting down";
    return WaitResult::kShutdown;
  }

  // Trace identity arrives in the goal because this node cannot see ExecuteTask.
  void emit_stage(
    const std::shared_ptr<const ExecuteDeviceCommand::Goal> & goal,
    const std::string & stage,
    const std::string & extra_json = "{}")
  {
    if (stage_log_ == nullptr || !stage_log_->enabled() || goal->trace_id.empty()) {
      return;
    }
    runtime_history::StageEvent event;
    event.trace_id = goal->trace_id;
    event.source_node = "device_bridge";
    event.stage = stage;
    event.event_name = stage;
    event.event_type = "can_bridge";
    event.extra_json = extra_json;
    stage_log_->emit(event);
  }

  void execute(const std::shared_ptr<GoalHandle> & goal_handle)
  {
    struct ActiveGuard
    {
      std::atomic_bool & active;
      ~ActiveGuard() {active = false;}
    } active_guard{active_goal_};

    const auto goal = goal_handle->get_goal();
    std::chrono::milliseconds ack_timeout;
    if (!device_bridge::duration_to_milliseconds(
        goal->ack_timeout, max_ack_timeout_ms_, ack_timeout))
    {
      auto result = std::make_shared<ExecuteDeviceCommand::Result>();
      result->outcome = ExecuteDeviceCommand::Result::DEVICE_FAULT;
      result->error_code = device_bridge::kErrorInvalidGoal;
      result->message = "ack_timeout became invalid before execution";
      goal_handle->abort(result);
      return;
    }

    const auto command = runtime_can::Command{
      static_cast<std::uint16_t>(goal->command_id), goal->opcode, goal->argument};
    bool command_sent = false;
    for (std::uint8_t attempt = 0; attempt <= kMaxRetries; ++attempt) {
      if (goal_handle->is_canceling() && !command_sent) {
        finish_canceled(goal_handle, "command canceled before CAN send");
        return;
      }

      publish_feedback(goal_handle, ExecuteDeviceCommand::Feedback::SENDING, attempt);
      diagnostic_state_.mark_sending();
      std::string send_error;
      // retry_count distinguishes attempts of one command inside the trace.
      const std::string attempt_extra =
        "{\"command_id\":" + std::to_string(command.command_id) +
        ",\"retry_count\":" + std::to_string(attempt) + "}";
      emit_stage(goal, "can_encode_start", attempt_extra);
      const auto frame = runtime_can::encode_command(command);
      emit_stage(goal, "can_encode_end", attempt_extra);
      if (!socket_.send(frame, send_error)) {
        diagnostic_state_.record_transport_error(device_bridge::kErrorTransport);
        publish_diagnostics();
        finish_aborted(
          goal_handle, ExecuteDeviceCommand::Result::SAFE_STOP,
          device_bridge::kErrorTransport, "CAN send failed: " + send_error);
        return;
      }
      command_sent = true;
      publish_feedback(goal_handle, ExecuteDeviceCommand::Feedback::WAITING_ACK, attempt);
      diagnostic_state_.mark_waiting_ack();
      emit_stage(goal, "can_frame_sent", attempt_extra);
      emit_stage(goal, "can_ack_wait_start", attempt_extra);

      runtime_can::Response response;
      std::string wait_error;
      const auto wait_result = receive_response(
        goal_handle, command.command_id, ack_timeout, true, response, wait_error);
      if (wait_result == WaitResult::kResponse) {
        if (goal_handle->is_canceling()) {
          stop_and_finish(goal_handle, command.command_id, true, 0, "");
          return;
        }
        if (response.result_code == 0) {
          diagnostic_state_.record_success();
          publish_diagnostics();
          emit_stage(goal, "can_ack_received", attempt_extra);
          auto result = std::make_shared<ExecuteDeviceCommand::Result>();
          result->outcome = ExecuteDeviceCommand::Result::COMPLETED;
          result->error_code = 0;
          result->message = "device acknowledged command";
          goal_handle->succeed(result);
          return;
        }
        const auto error_code = device_bridge::device_error_code(response.result_code);
        diagnostic_state_.record_device_fault(error_code);
        publish_diagnostics();
        finish_aborted(
          goal_handle, ExecuteDeviceCommand::Result::DEVICE_FAULT,
          error_code,
          "device returned result_code=" + std::to_string(response.result_code));
        return;
      }
      if (wait_result == WaitResult::kCanceled) {
        if (!command_sent) {
          finish_canceled(goal_handle, "command canceled before CAN send");
        } else {
          stop_and_finish(goal_handle, command.command_id, true, 0, "");
        }
        return;
      }
      if (wait_result == WaitResult::kTransportError) {
        diagnostic_state_.record_transport_error(device_bridge::kErrorTransport);
        publish_diagnostics();
        stop_and_finish(
          goal_handle, command.command_id, false, device_bridge::kErrorTransport,
          "CAN receive failed: " + wait_error + "; attempting device STOP");
        return;
      }
      if (wait_result == WaitResult::kShutdown) {
        stop_and_finish(
          goal_handle, command.command_id, false, device_bridge::kErrorShutdown,
          wait_error + "; attempting device STOP");
        return;
      }

      if (attempt < kMaxRetries) {
        publish_feedback(goal_handle, ExecuteDeviceCommand::Feedback::RETRYING, attempt + 1);
        diagnostic_state_.record_retry();
        publish_diagnostics();
        RCLCPP_WARN(
          get_logger(), "ACK timeout command_id=%u; retrying with same command_id",
          command.command_id);
        continue;
      }

      publish_feedback(goal_handle, ExecuteDeviceCommand::Feedback::STOPPING, attempt);
      diagnostic_state_.record_ack_timeout(device_bridge::kErrorAckTimeout);
      publish_diagnostics();
      emit_stage(goal, "can_retry_exhausted", attempt_extra);
      stop_and_finish(
        goal_handle, command.command_id, false, device_bridge::kErrorAckTimeout,
        "command ACK timeout; attempting device STOP");
      return;
    }
  }

  void stop_and_finish(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const std::uint16_t original_command_id,
    const bool cancellation_requested,
    const std::uint16_t safe_stop_error_code,
    const std::string & safe_stop_message)
  {
    using DeviceState = robot_task_interfaces::msg::DeviceState;
    diagnostic_state_.mark_stopping();

    const auto stop_command_id = allocate_stop_command_id();
    const runtime_can::Command stop_command{
      stop_command_id, runtime_can::kStopOpcode,
      static_cast<std::int32_t>(original_command_id)};
    std::string send_error;
    if (!socket_.send(runtime_can::encode_command(stop_command), send_error)) {
      diagnostic_state_.record_transport_error(device_bridge::kErrorTransport);
      publish_diagnostics();
      finish_safe_stop(
        goal_handle, device_bridge::kErrorTransport,
        "STOP send failed: " + send_error);
      return;
    }
    publish_feedback(goal_handle, ExecuteDeviceCommand::Feedback::STOPPING, 0);

    RCLCPP_WARN(
      get_logger(), "STOP sent stop_command_id=%u original_command_id=%u",
      stop_command_id, original_command_id);
    runtime_can::Response stop_response;
    std::string wait_error;
    const auto wait_result = receive_response(
      goal_handle, stop_command_id, stop_timeout_ms_, false, stop_response, wait_error);
    if (wait_result == WaitResult::kResponse && stop_response.result_code == 0 &&
      stop_response.device_mode == DeviceState::STOPPED)
    {
      const auto stop_message = "device STOP acknowledged stop_command_id=" +
        std::to_string(stop_command_id);
      if (cancellation_requested) {
        finish_canceled(goal_handle, stop_message);
      } else {
        finish_safe_stop(
          goal_handle, safe_stop_error_code, safe_stop_message + "; " + stop_message);
      }
      return;
    }
    if (wait_result == WaitResult::kTimeout) {
      diagnostic_state_.record_stop_failure(device_bridge::kErrorStopTimeout);
      publish_diagnostics();
      finish_safe_stop(
        goal_handle, device_bridge::kErrorStopTimeout,
        "device STOP ACK timeout stop_command_id=" + std::to_string(stop_command_id));
      return;
    }
    if (wait_result == WaitResult::kTransportError || wait_result == WaitResult::kShutdown) {
      diagnostic_state_.record_transport_error(device_bridge::kErrorTransport);
      publish_diagnostics();
      finish_safe_stop(
        goal_handle, device_bridge::kErrorTransport,
        "device STOP receive failed: " + wait_error);
      return;
    }
    diagnostic_state_.record_stop_failure(device_bridge::kErrorStopRejected);
    publish_diagnostics();
    finish_safe_stop(
      goal_handle, device_bridge::kErrorStopRejected,
      "device STOP response rejected stop_command_id=" + std::to_string(stop_command_id));
  }

  std::uint16_t allocate_stop_command_id()
  {
    const auto command_id = next_stop_command_id_;
    if (next_stop_command_id_ == std::numeric_limits<std::uint16_t>::max()) {
      next_stop_command_id_ = runtime_can::kStopCommandIdMin;
    } else {
      ++next_stop_command_id_;
    }
    return command_id;
  }

  void publish_feedback(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const std::uint8_t state,
    const std::uint8_t retry_count)
  {
    auto feedback = std::make_shared<ExecuteDeviceCommand::Feedback>();
    feedback->state = state;
    feedback->retry_count = retry_count;
    goal_handle->publish_feedback(feedback);
  }

  void publish_diagnostics()
  {
    try {
      diagnostic_msgs::msg::DiagnosticArray message;
      message.header.stamp = now();
      message.status.push_back(diagnostic_state_.snapshot(socket_.is_open()));
      diagnostics_publisher_->publish(message);
    } catch (const std::exception & error) {
      RCLCPP_ERROR(get_logger(), "Failed to publish diagnostics: %s", error.what());
    } catch (...) {
      RCLCPP_ERROR(get_logger(), "Failed to publish diagnostics: unknown exception");
    }
  }

  void finish_canceled(const std::shared_ptr<GoalHandle> & goal_handle, const std::string & message)
  {
    diagnostic_state_.record_canceled();
    publish_diagnostics();
    auto result = std::make_shared<ExecuteDeviceCommand::Result>();
    result->outcome = ExecuteDeviceCommand::Result::CANCELED;
    result->error_code = 0;
    result->message = message;
    goal_handle->canceled(result);
  }

  void finish_safe_stop(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const std::uint16_t error_code,
    const std::string & message)
  {
    finish_aborted(
      goal_handle, ExecuteDeviceCommand::Result::SAFE_STOP,
      error_code, message);
  }

  void finish_aborted(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const std::uint8_t outcome,
    const std::uint16_t error_code,
    const std::string & message)
  {
    auto result = std::make_shared<ExecuteDeviceCommand::Result>();
    result->outcome = outcome;
    result->error_code = error_code;
    result->message = message;
    goal_handle->abort(result);
  }

  std::string interface_name_;
  std::int64_t max_ack_timeout_ms_{5000};
  std::int64_t diagnostic_period_ms_{1000};
  std::chrono::milliseconds receive_poll_ms_{25};
  std::chrono::milliseconds stop_timeout_ms_{500};
  std::uint16_t next_stop_command_id_{runtime_can::kStopCommandIdMin};
  std::atomic_bool running_{true};
  std::atomic_bool active_goal_{false};
  runtime_can::SocketCan socket_;
  std::unique_ptr<runtime_history::StageEventLog> stage_log_;
  device_bridge::DeviceDiagnosticState diagnostic_state_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diagnostics_publisher_;
  rclcpp::TimerBase::SharedPtr diagnostics_timer_;
  rclcpp_action::Server<ExecuteDeviceCommand>::SharedPtr action_server_;
  std::mutex workers_mutex_;
  std::thread worker_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  try {
    rclcpp::spin(std::make_shared<DeviceBridgeNode>());
  } catch (const std::exception & error) {
    RCLCPP_FATAL(rclcpp::get_logger("device_bridge"), "%s", error.what());
    rclcpp::shutdown();
    return 1;
  }
  rclcpp::shutdown();
  return 0;
}
