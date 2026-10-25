#include "task_executor/diagnostic_state.hpp"
#include "task_executor/task_event.hpp"
#include "task_executor/target_validator.hpp"
#include "task_executor_node.hpp"
#include "runtime_history/stage_event_log.hpp"

#include "diagnostic_msgs/msg/diagnostic_array.hpp"
#include "robot_task_interfaces/action/execute_device_command.hpp"
#include "robot_task_interfaces/action/execute_task.hpp"

#include <algorithm>
#include <cctype>
#include <atomic>
#include <chrono>
#include <csignal>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"

namespace
{
volatile std::sig_atomic_t process_shutdown_requested{};
}  // namespace

class TaskExecutorNode : public rclcpp::Node
{
public:
  using ExecuteTask = robot_task_interfaces::action::ExecuteTask;
  using ExecuteDeviceCommand = robot_task_interfaces::action::ExecuteDeviceCommand;
  using GoalHandle = rclcpp_action::ServerGoalHandle<ExecuteTask>;
  using DeviceGoalHandle = rclcpp_action::ClientGoalHandle<ExecuteDeviceCommand>;

  TaskExecutorNode(const rclcpp::NodeOptions & options, task_executor::WorkerHook worker_hook)
  : Node("task_executor", options), worker_hook_(std::move(worker_hook))
  {
    const auto targets = declare_parameter<std::vector<std::string>>(
      "targets", {"dock_a", "home"});
    const auto target_arguments = declare_parameter<std::vector<std::int64_t>>(
      "target_arguments", {42, 0});
    const auto configured_opcode = declare_parameter<std::int64_t>("device_opcode", 1);
    const auto configured_ack_timeout = declare_parameter<std::int64_t>("ack_timeout_ms", 500);
    const auto configured_cancel_timeout = declare_parameter<std::int64_t>(
      "cancel_timeout_ms", 1000);
    const auto configured_validation_delay = declare_parameter<std::int64_t>(
      "validation_delay_ms", 200);
    const auto configured_event_history_depth = declare_parameter<std::int64_t>(
      "task_event_history_depth", 128);
    diagnostic_period_ms_ = declare_parameter<std::int64_t>("diagnostic_period_ms", 1000);
    stage_log_ = std::make_unique<runtime_history::StageEventLog>(
      declare_parameter<std::string>("stage_event_log_path", ""));

    if (targets.size() != target_arguments.size() || targets.empty()) {
      throw std::invalid_argument("targets and target_arguments must be non-empty and aligned");
    }
    if (configured_opcode < 0 || configured_opcode > 255) {
      throw std::invalid_argument("device_opcode must be in [0, 255]");
    }
    if (configured_ack_timeout <= 0 || configured_ack_timeout > 60000 ||
      configured_cancel_timeout <= 0 || configured_cancel_timeout > 60000)
    {
      throw std::invalid_argument("ACK and cancel timeouts must be in [1, 60000] ms");
    }
    if (diagnostic_period_ms_ <= 0 || diagnostic_period_ms_ > 60000) {
      throw std::invalid_argument("diagnostic_period_ms must be in [1, 60000]");
    }
    if (configured_event_history_depth <= 0 || configured_event_history_depth > 1000) {
      throw std::invalid_argument("task_event_history_depth must be in [1, 1000]");
    }

    for (std::size_t index = 0; index < targets.size(); ++index) {
      if (target_arguments[index] < std::numeric_limits<std::int32_t>::min() ||
        target_arguments[index] > std::numeric_limits<std::int32_t>::max())
      {
        throw std::invalid_argument("target argument exceeds int32 range");
      }
      target_arguments_.emplace(
        targets[index], static_cast<std::int32_t>(target_arguments[index]));
    }

    device_opcode_ = static_cast<std::uint8_t>(configured_opcode);
    ack_timeout_ms_ = configured_ack_timeout;
    cancel_timeout_ms_ = configured_cancel_timeout;
    task_event_history_depth_ = static_cast<std::size_t>(configured_event_history_depth);
    validation_delay_ms_ = static_cast<int>(
      std::clamp<std::int64_t>(configured_validation_delay, 1, 60000));
    validator_ = std::make_unique<task_executor::TargetValidator>(targets);
    device_client_ = rclcpp_action::create_client<ExecuteDeviceCommand>(
      this, "execute_device_command");

    diagnostics_publisher_ = create_publisher<diagnostic_msgs::msg::DiagnosticArray>(
      "/diagnostics", rclcpp::QoS(10).reliable());
    task_event_publisher_ = create_publisher<robot_task_interfaces::msg::TaskEvent>(
      "/runtime/task_events",
      rclcpp::QoS(rclcpp::KeepLast(task_event_history_depth_)).reliable().transient_local());
    diagnostics_timer_ = create_wall_timer(
      std::chrono::milliseconds(diagnostic_period_ms_),
      [this]() {publish_diagnostics();});

    action_server_ = rclcpp_action::create_server<ExecuteTask>(
      this,
      "execute_task",
      std::bind(&TaskExecutorNode::handle_goal, this, std::placeholders::_1,
        std::placeholders::_2),
      std::bind(&TaskExecutorNode::handle_cancel, this, std::placeholders::_1),
      std::bind(&TaskExecutorNode::handle_accepted, this, std::placeholders::_1));

    RCLCPP_INFO(
      get_logger(),
      "Orchestrating Task Executor ready targets=%zu ack_timeout_ms=%ld "
      "diagnostic_period_ms=%ld task_event_history_depth=%zu",
      validator_->size(), ack_timeout_ms_, diagnostic_period_ms_, task_event_history_depth_);
  }

  ~TaskExecutorNode() override
  {
    std::thread worker;
    {
      std::lock_guard<std::mutex> lock(workers_mutex_);
      if (workers_active_ != 0) {
        // Destruction cannot safely abandon an in-flight Action worker or its
        // device-cancel path, so fail loudly instead of leaving a live thread
        // with references to a destroyed ROS node.
        RCLCPP_FATAL(get_logger(), "Task Executor destroyed before workers drained");
        std::terminate();
      }
      worker.swap(worker_);
    }
    if (worker.joinable()) {
      worker.join();
    }
  }

  void request_shutdown() {running_ = false;}

  void wait_for_shutdown()
  {
    std::thread worker;
    {
      std::unique_lock<std::mutex> lock(workers_mutex_);
      workers_changed_.wait(lock, [this]() {return workers_active_ == 0 && !active_goal_;});
      worker.swap(worker_);
    }
    if (worker.joinable()) {
      worker.join();
    }
  }

  bool wait_for_shutdown_for(const std::chrono::milliseconds timeout)
  {
    std::thread worker;
    {
      std::unique_lock<std::mutex> lock(workers_mutex_);
      if (!workers_changed_.wait_for(
          lock, timeout, [this]() {return workers_active_ == 0 && !active_goal_;}))
      {
        return false;
      }
      worker.swap(worker_);
    }
    if (worker.joinable()) {
      worker.join();
    }
    return true;
  }

private:
  enum class StopCause
  {
    kNone,
    kUserCancel,
    kDeadline,
    kShutdown
  };

  struct ChildExecutionState
  {
    std::mutex mutex;
    std::condition_variable changed;
    bool dispatch_started{false};
    bool goal_response_received{false};
    DeviceGoalHandle::SharedPtr handle;
    bool terminal{false};
    std::optional<DeviceGoalHandle::WrappedResult> result;
    bool cancel_requested{false};
    bool cancel_dispatched{false};
    std::chrono::steady_clock::time_point cancel_requested_at;
    std::chrono::steady_clock::time_point cancel_started_at;
    bool cancel_timed_out{false};
  };

  static constexpr std::uint16_t kErrorBridgeRejected = 110;
  static constexpr std::uint16_t kErrorBridgeUnavailable = 111;
  static constexpr std::uint16_t kErrorTaskDeadline = 112;
  static constexpr std::uint16_t kErrorCancelUnconfirmed = 113;
  static constexpr std::uint16_t kErrorUnexpectedChildResult = 114;
  static constexpr std::uint16_t kMaxApplicationCommandId = 0x7FFF;
  static constexpr auto kLateGoalResponseGrace = std::chrono::milliseconds(2000);

  static bool duration_is_valid(const builtin_interfaces::msg::Duration & duration)
  {
    return duration.sec >= 0 && duration.nanosec < 1000000000U &&
           (duration.sec > 0 || duration.nanosec > 0);
  }

  static bool has_non_whitespace(const std::string_view value)
  {
    return std::any_of(value.begin(), value.end(), [](const unsigned char character) {
               return std::isspace(character) == 0;
    });
  }

  static std::chrono::steady_clock::duration to_steady_duration(
    const builtin_interfaces::msg::Duration & duration)
  {
    const auto value = std::chrono::seconds(duration.sec) +
      std::chrono::nanoseconds(duration.nanosec);
    return std::chrono::duration_cast<std::chrono::steady_clock::duration>(value);
  }

  rclcpp_action::GoalResponse handle_goal(
    const rclcpp_action::GoalUUID & goal_id,
    std::shared_ptr<const ExecuteTask::Goal> goal)
  {
    if (!running_) {
      RCLCPP_WARN(get_logger(), "Rejecting goal: EXECUTOR_SHUTTING_DOWN");
      return rclcpp_action::GoalResponse::REJECT;
    }
    if (!has_non_whitespace(goal->task_id)) {
      RCLCPP_WARN(get_logger(), "Rejecting goal: EMPTY_TASK_ID");
      return rclcpp_action::GoalResponse::REJECT;
    }
    if (!duration_is_valid(goal->allowed_duration)) {
      RCLCPP_WARN(get_logger(), "Rejecting task_id=%s: INVALID_DURATION", goal->task_id.c_str());
      return rclcpp_action::GoalResponse::REJECT;
    }
    if (!has_non_whitespace(goal->target_id) || !validator_->is_known(goal->target_id)) {
      RCLCPP_WARN(
        get_logger(), "Rejecting task_id=%s target_id=%s: INVALID_TARGET",
        goal->task_id.c_str(), goal->target_id.c_str());
      return rclcpp_action::GoalResponse::REJECT;
    }
    if (!device_client_->action_server_is_ready()) {
      update_diagnostics([this]() {diagnostic_state_.set_bridge_ready(false);});
      RCLCPP_WARN(
        get_logger(), "Rejecting task_id=%s: DEVICE_BRIDGE_NOT_READY", goal->task_id.c_str());
      return rclcpp_action::GoalResponse::REJECT;
    }

    bool expected = false;
    if (!active_goal_.compare_exchange_strong(expected, true)) {
      RCLCPP_WARN(get_logger(), "Rejecting task_id=%s: EXECUTOR_BUSY", goal->task_id.c_str());
      return rclcpp_action::GoalResponse::REJECT;
    }

    // Bound to the accepted goal, which is the one the terminal event belongs
    // to; reassigning it per execution keeps that true without a second field.
    goal_id_ = rclcpp_action::to_string(goal_id);
    RCLCPP_INFO(
      get_logger(), "Accepting task_id=%s target_id=%s",
      goal->task_id.c_str(), goal->target_id.c_str());
    return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
  }

  rclcpp_action::CancelResponse handle_cancel(const std::shared_ptr<GoalHandle> goal_handle)
  {
    RCLCPP_INFO(
      get_logger(), "Accepting task cancel task_id=%s", goal_handle->get_goal()->task_id.c_str());
    return rclcpp_action::CancelResponse::ACCEPT;
  }

  void handle_accepted(const std::shared_ptr<GoalHandle> goal_handle)
  {
    join_completed_worker();
    started_at_ = std::chrono::steady_clock::now();
    parent_terminal_ = false;
    terminal_event_published_ = false;
    emit_stage(goal_handle->get_goal(), "action_receive");
    update_diagnostics(
      [this, &goal_handle]() {
        diagnostic_state_.begin_task(goal_handle->get_goal()->task_id);
      });
    std::lock_guard<std::mutex> lock(workers_mutex_);
    ++workers_active_;
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

  void execute(const std::shared_ptr<GoalHandle> & goal_handle)
  {
    struct WorkerGuard
    {
      TaskExecutorNode & node;
      ~WorkerGuard()
      {
        {
          std::lock_guard<std::mutex> lock(node.workers_mutex_);
          --node.workers_active_;
        }
        node.workers_changed_.notify_all();
      }
    };
    struct ActiveGuard
    {
      std::atomic_bool & active;
      std::atomic_bool & parent_terminal;
      std::shared_ptr<GoalHandle> goal_handle;
      ~ActiveGuard()
      {
        if (parent_terminal || !goal_handle->is_active()) {
          active = false;
        }
      }
    };
    WorkerGuard worker_guard{*this};
    ActiveGuard active_guard{active_goal_, parent_terminal_, goal_handle};

    const auto child = std::make_shared<ChildExecutionState>();

    try {
      execute_worker(goal_handle, child);
    } catch (const std::exception & error) {
      finish_worker_exception(goal_handle, child, error.what());
    } catch (...) {
      finish_worker_exception(goal_handle, child, "unknown exception");
    }
  }

  void execute_worker(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const std::shared_ptr<ChildExecutionState> & child)
  {

    const auto goal = goal_handle->get_goal();
    const auto deadline = std::chrono::steady_clock::now() +
      to_steady_duration(goal->allowed_duration);
    const int validation_step_ms = std::max(validation_delay_ms_ / 5, 1);
    emit_stage(goal, "action_execute_start");

    for (int step = 1; step <= 5; ++step) {
      if (!running_ || !rclcpp::ok()) {
        finish_aborted(
          goal_handle, ExecuteTask::Result::SAFE_STOP,
          kErrorBridgeUnavailable, "executor shutdown during validation");
        return;
      }
      if (goal_handle->is_canceling()) {
        finish_canceled(goal_handle, "task canceled before device command");
        return;
      }
      if (std::chrono::steady_clock::now() >= deadline) {
        finish_aborted(
          goal_handle, ExecuteTask::Result::SAFE_STOP,
          kErrorTaskDeadline, "task deadline expired during validation");
        return;
      }
      publish_feedback(
        goal_handle, ExecuteTask::Feedback::VALIDATING,
        static_cast<float>(step) * 0.04F);
      std::this_thread::sleep_for(std::chrono::milliseconds(validation_step_ms));
    }

    const auto now = std::chrono::steady_clock::now();
    const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
    if (remaining.count() < 3) {
      finish_aborted(
        goal_handle, ExecuteTask::Result::SAFE_STOP,
        kErrorTaskDeadline, "insufficient task budget for device command");
      return;
    }

    // Reserve bounded time for the child ACK path and the later STOP recovery;
    // an ACK timeout must never consume the parent task's whole deadline.
    const auto per_ack_budget = std::max<std::int64_t>(remaining.count() / 3, 1);
    const auto child_ack_timeout_ms = std::min(ack_timeout_ms_, per_ack_budget);
    ExecuteDeviceCommand::Goal device_goal;
    device_goal.command_id = allocate_command_id();
    device_goal.opcode = device_opcode_;
    device_goal.argument = target_arguments_.at(goal->target_id);
    device_goal.ack_timeout.sec = static_cast<std::int32_t>(child_ack_timeout_ms / 1000);
    device_goal.ack_timeout.nanosec = static_cast<std::uint32_t>(
      (child_ack_timeout_ms % 1000) * 1000000);
    // The bridge cannot see ExecuteTask, so the trace crosses on the wire here.
    device_goal.trace_id = goal->task_id;

    // The action layer's own work ends where it hands the command to the
    // device; the diagnoser orders action_execute_end before can_receive.
    emit_stage(goal, "action_execute_end");
    emit_stage(
      goal, "can_receive", 0,
      "{\"command_id\":" + std::to_string(device_goal.command_id) +
      ",\"opcode\":" + std::to_string(device_goal.opcode) +
      ",\"ack_timeout_ms\":" + std::to_string(child_ack_timeout_ms) +
      ",\"target_id\":" + runtime_history::json_quote(goal->target_id) + "}");

    publish_feedback(goal_handle, ExecuteTask::Feedback::RUNNING, 0.25F);
    RCLCPP_INFO(
      get_logger(),
      "Dispatching task_id=%s command_id=%u opcode=%u argument=%d ack_timeout_ms=%ld",
      goal->task_id.c_str(), device_goal.command_id, device_goal.opcode,
      device_goal.argument, child_ack_timeout_ms);

    rclcpp_action::Client<ExecuteDeviceCommand>::SendGoalOptions options;
    const auto device_client = device_client_;
    const auto worker_hook = worker_hook_;
    options.goal_response_callback = [child, device_client, worker_hook](
      const DeviceGoalHandle::SharedPtr handle) {
        bool dispatch_late_cancel = false;
        {
          const std::lock_guard<std::mutex> lock(child->mutex);
          child->goal_response_received = true;
          child->handle = handle;
          if (handle && child->cancel_requested && !child->cancel_dispatched) {
            child->cancel_dispatched = true;
            child->cancel_started_at = std::chrono::steady_clock::now();
            dispatch_late_cancel = true;
          }
        }
        child->changed.notify_all();
        if (dispatch_late_cancel) {
          try {
            if (worker_hook) {
              worker_hook(task_executor::WorkerPhase::kBeforeCancelGoal);
            }
            device_client->async_cancel_goal(handle);
          } catch (...) {
            const std::lock_guard<std::mutex> lock(child->mutex);
            child->cancel_dispatched = false;
            child->changed.notify_all();
          }
        }
      };
    options.result_callback = [child](const DeviceGoalHandle::WrappedResult & result) {
        {
          const std::lock_guard<std::mutex> lock(child->mutex);
          child->result = result;
          child->terminal = true;
        }
        child->changed.notify_all();
      };
    run_worker_hook(task_executor::WorkerPhase::kBeforeSendGoal);
    {
      const std::lock_guard<std::mutex> lock(child->mutex);
      child->dispatch_started = true;
    }
    (void)device_client_->async_send_goal(device_goal, options);
    run_worker_hook(task_executor::WorkerPhase::kAfterSendGoal);

    StopCause stop_cause = StopCause::kNone;
    while (true) {
      bool goal_response_received = false;
      {
        std::unique_lock<std::mutex> lock(child->mutex);
        goal_response_received = child->changed.wait_for(
          lock, std::chrono::milliseconds(25),
          [&]() {return child->goal_response_received;});
      }
      if (goal_response_received) {
        break;
      }
      if (!running_ || !rclcpp::ok()) {
        stop_cause = StopCause::kShutdown;
        break;
      } else if (goal_handle->is_canceling()) {
        stop_cause = StopCause::kUserCancel;
        break;
      } else if (stop_cause == StopCause::kNone &&
        std::chrono::steady_clock::now() >= deadline)
      {
        stop_cause = StopCause::kDeadline;
        break;
      }
    }

    DeviceGoalHandle::SharedPtr child_goal_handle;
    if (!running_ || !rclcpp::ok()) {
      stop_cause = StopCause::kShutdown;
    }
    {
      const std::lock_guard<std::mutex> lock(child->mutex);
      child_goal_handle = child->handle;
    }
    if (stop_cause != StopCause::kNone) {
      publish_feedback(goal_handle, ExecuteTask::Feedback::RECOVERING, 0.6F);
      cancel_child_once(child);
      const auto late_response_deadline = std::chrono::steady_clock::now() +
        std::max(kLateGoalResponseGrace,
        std::chrono::milliseconds(cancel_timeout_ms_));
      std::unique_lock<std::mutex> lock(child->mutex);
      child->changed.wait_until(
        lock, late_response_deadline, [&]() {return child->goal_response_received;});
      child_goal_handle = child->handle;
    }
    if (!child_goal_handle) {
      if (stop_cause == StopCause::kShutdown) {
        finish_aborted(
          goal_handle, ExecuteTask::Result::SAFE_STOP,
          kErrorBridgeUnavailable, "executor shutdown before Device Bridge accepted command");
      } else if (stop_cause == StopCause::kUserCancel) {
        finish_canceled(goal_handle, "task canceled before Device Bridge accepted command");
      } else if (stop_cause == StopCause::kDeadline) {
        finish_aborted(
          goal_handle, ExecuteTask::Result::SAFE_STOP,
          kErrorTaskDeadline, "task deadline expired before bridge rejected command");
      } else {
        finish_aborted(
          goal_handle, ExecuteTask::Result::DEVICE_FAULT,
          kErrorBridgeRejected, "Device Bridge rejected command");
      }
      return;
    }
    run_worker_hook(task_executor::WorkerPhase::kAfterChildAccepted);

    if (stop_cause != StopCause::kNone) {
      publish_feedback(goal_handle, ExecuteTask::Feedback::RECOVERING, 0.6F);
      cancel_child_once(child);
    } else {
      publish_feedback(goal_handle, ExecuteTask::Feedback::WAITING_ACK, 0.5F);
    }

    while (true) {
      {
        std::unique_lock<std::mutex> lock(child->mutex);
        if (child->changed.wait_for(
            lock, std::chrono::milliseconds(25), [&]() {return child->terminal;}))
        {
          break;
        }
      }
      const auto current_time = std::chrono::steady_clock::now();
      record_cancel_timeout(child, current_time);
      bool cancel_drain_expired = false;
      {
        const std::lock_guard<std::mutex> lock(child->mutex);
        cancel_drain_expired = child->cancel_requested &&
          current_time - child->cancel_requested_at >=
          std::chrono::milliseconds(std::max<std::int64_t>(cancel_timeout_ms_, 1000));
      }
      if (cancel_drain_expired) {
        finish_aborted(
          goal_handle, ExecuteTask::Result::SAFE_STOP,
          kErrorCancelUnconfirmed, "device cancel exceeded drain timeout");
        return;
      }
      if (stop_cause != StopCause::kNone) {
        cancel_child_once(child);
      }
      if (!running_ && stop_cause != StopCause::kShutdown) {
        stop_cause = StopCause::kShutdown;
        publish_feedback(goal_handle, ExecuteTask::Feedback::RECOVERING, 0.6F);
        cancel_child_once(child);
      } else if (stop_cause == StopCause::kNone && goal_handle->is_canceling()) {
        stop_cause = StopCause::kUserCancel;
        publish_feedback(goal_handle, ExecuteTask::Feedback::RECOVERING, 0.6F);
        cancel_child_once(child);
      } else if (stop_cause == StopCause::kNone && current_time >= deadline) {
        stop_cause = StopCause::kDeadline;
        publish_feedback(goal_handle, ExecuteTask::Feedback::RECOVERING, 0.6F);
        cancel_child_once(child);
      }
    }
    record_cancel_timeout(child, std::chrono::steady_clock::now());

    DeviceGoalHandle::WrappedResult child_result;
    bool cancel_timed_out = false;
    {
      const std::lock_guard<std::mutex> lock(child->mutex);
      if (!child->result) {
        throw std::runtime_error("child terminal callback returned no result wrapper");
      }
      child_result = *child->result;
      cancel_timed_out = child->cancel_timed_out;
    }
    if (stop_cause == StopCause::kNone &&
      child_result.result &&
      child_result.code == rclcpp_action::ResultCode::SUCCEEDED &&
      child_result.result->outcome == ExecuteDeviceCommand::Result::COMPLETED)
    {
      run_worker_hook(task_executor::WorkerPhase::kBeforeSuccess);
      if (!running_) {
        stop_cause = StopCause::kShutdown;
        cancel_child_once(child);
      } else if (goal_handle->is_canceling()) {
        stop_cause = StopCause::kUserCancel;
        cancel_child_once(child);
      } else if (std::chrono::steady_clock::now() >= deadline) {
        stop_cause = StopCause::kDeadline;
        cancel_child_once(child);
      }
    }
    finish_from_child(goal_handle, child_result, stop_cause, cancel_timed_out);
  }

  void finish_from_child(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const DeviceGoalHandle::WrappedResult & child_result,
    const StopCause stop_cause,
    const bool cancel_timed_out)
  {
    if (!child_result.result) {
      finish_aborted(
        goal_handle, ExecuteTask::Result::SAFE_STOP,
        kErrorUnexpectedChildResult, "Device Bridge returned no result");
      return;
    }

    if (stop_cause == StopCause::kShutdown) {
      finish_aborted(
        goal_handle, ExecuteTask::Result::SAFE_STOP,
        kErrorBridgeUnavailable, "executor shutdown after device command drain");
      return;
    }

    if (stop_cause == StopCause::kUserCancel) {
      if (cancel_timed_out) {
        finish_aborted(
          goal_handle, ExecuteTask::Result::SAFE_STOP,
          kErrorCancelUnconfirmed, "device cancel exceeded confirmation timeout");
      } else if (child_result.code == rclcpp_action::ResultCode::CANCELED &&
        child_result.result->outcome == ExecuteDeviceCommand::Result::CANCELED)
      {
        finish_canceled(goal_handle, "task and device command canceled");
      } else if (child_result.result->outcome == ExecuteDeviceCommand::Result::SAFE_STOP) {
        finish_aborted(
          goal_handle, ExecuteTask::Result::SAFE_STOP,
          child_result.result->error_code,
          "device stop was not confirmed: " + child_result.result->message);
      } else {
        finish_aborted(
          goal_handle, ExecuteTask::Result::SAFE_STOP,
          kErrorCancelUnconfirmed,
          "task cancel ended without confirmed device stop: " + child_result.result->message);
      }
      return;
    }

    if (stop_cause == StopCause::kDeadline) {
      finish_aborted(
        goal_handle, ExecuteTask::Result::SAFE_STOP,
        kErrorTaskDeadline,
        "task deadline exceeded: " + child_result.result->message);
      return;
    }

    if (child_result.code == rclcpp_action::ResultCode::SUCCEEDED &&
      child_result.result->outcome == ExecuteDeviceCommand::Result::COMPLETED)
    {
      auto result = std::make_shared<ExecuteTask::Result>();
      result->outcome = ExecuteTask::Result::COMPLETED;
      result->error_code = 0;
      result->message = "task completed through Device Bridge";
      update_diagnostics(
        [this]() {diagnostic_state_.record_success("COMPLETED");}, true);
      if (commit_parent_terminal(goal_handle, [&]() {goal_handle->succeed(result);})) {
        publish_terminal_event_once(
          goal_handle, *result, robot_task_interfaces::msg::TaskEvent::SUCCEEDED);
      }
      return;
    }

    if (child_result.result->outcome == ExecuteDeviceCommand::Result::DEVICE_FAULT) {
      finish_aborted(
        goal_handle, ExecuteTask::Result::DEVICE_FAULT,
        child_result.result->error_code,
        "device command failed: " + child_result.result->message);
      return;
    }

    if (child_result.result->outcome == ExecuteDeviceCommand::Result::SAFE_STOP) {
      finish_aborted(
        goal_handle, ExecuteTask::Result::SAFE_STOP,
        child_result.result->error_code,
        "device command entered safe stop: " + child_result.result->message);
      return;
    }

    finish_aborted(
      goal_handle, ExecuteTask::Result::SAFE_STOP,
      kErrorUnexpectedChildResult, "unexpected Device Bridge result");
  }

  void emit_stage(
    const std::shared_ptr<const ExecuteTask::Goal> & goal,
    const std::string & stage,
    const std::uint64_t duration_ns = 0,
    const std::string & extra_json = "{}")
  {
    if (stage_log_ == nullptr || !stage_log_->enabled()) {
      return;
    }
    runtime_history::StageEvent event;
    // task_id is the trace identity: it is what the caller, the history store
    // and the TaskEvent all key on, so evidence joins on the same value.
    event.trace_id = goal->task_id;
    event.source_node = "task_executor";
    event.stage = stage;
    event.event_name = stage;
    event.event_type = "action";
    event.duration_ns = duration_ns;
    event.extra_json = extra_json;
    stage_log_->emit(event);
  }

  void publish_feedback(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const std::uint8_t state,
    const float progress)
  {
    run_worker_hook(task_executor::WorkerPhase::kBeforeFeedback);
    auto feedback = std::make_shared<ExecuteTask::Feedback>();
    feedback->state = state;
    feedback->progress = progress;
    goal_handle->publish_feedback(feedback);

    bool publish_immediately = false;
    update_diagnostics(
      [this, state, &publish_immediately]() {
        switch (state) {
          case ExecuteTask::Feedback::VALIDATING:
            diagnostic_state_.mark_validating();
            break;
          case ExecuteTask::Feedback::RUNNING:
            diagnostic_state_.mark_running();
            break;
          case ExecuteTask::Feedback::WAITING_ACK:
            diagnostic_state_.mark_waiting_ack();
            break;
          case ExecuteTask::Feedback::RECOVERING:
            diagnostic_state_.mark_recovering();
            publish_immediately = true;
            break;
          default:
            break;
        }
      });
    if (publish_immediately) {
      publish_diagnostics();
    }
  }

  template<typename Update>
  void update_diagnostics(Update update, const bool publish_immediately = false)
  {
    try {
      update();
      if (publish_immediately) {
        publish_diagnostics();
      }
    } catch (const std::exception & error) {
      RCLCPP_ERROR(get_logger(), "Failed to update diagnostics: %s", error.what());
    } catch (...) {
      RCLCPP_ERROR(get_logger(), "Failed to update diagnostics: unknown exception");
    }
  }

  void publish_diagnostics()
  {
    try {
      diagnostic_state_.set_bridge_ready(device_client_->action_server_is_ready());
      diagnostic_msgs::msg::DiagnosticArray message;
      message.header.stamp = now();
      message.status.push_back(diagnostic_state_.snapshot());
      diagnostics_publisher_->publish(message);
    } catch (const std::exception & error) {
      RCLCPP_ERROR(get_logger(), "Failed to publish diagnostics: %s", error.what());
    } catch (...) {
      RCLCPP_ERROR(get_logger(), "Failed to publish diagnostics: unknown exception");
    }
  }

  void finish_canceled(const std::shared_ptr<GoalHandle> & goal_handle, const std::string & message)
  {
    update_diagnostics([this]() {diagnostic_state_.record_canceled();}, true);
    auto result = std::make_shared<ExecuteTask::Result>();
    result->outcome = ExecuteTask::Result::CANCELED;
    result->error_code = 0;
    result->message = message;
    if (commit_parent_terminal(goal_handle, [&]() {goal_handle->canceled(result);})) {
      publish_terminal_event_once(
        goal_handle, *result, robot_task_interfaces::msg::TaskEvent::CANCELED);
    }
  }

  void finish_aborted(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const std::uint8_t outcome,
    const std::uint16_t error_code,
    const std::string & message)
  {
    const char * diagnostic_outcome = outcome == ExecuteTask::Result::DEVICE_FAULT ?
      "DEVICE_FAULT" : "SAFE_STOP";
    update_diagnostics(
      [this, diagnostic_outcome, error_code]() {
        diagnostic_state_.record_failure(diagnostic_outcome, error_code);
      }, true);
    auto result = std::make_shared<ExecuteTask::Result>();
    result->outcome = outcome;
    result->error_code = error_code;
    result->message = message;
    if (commit_parent_terminal(goal_handle, [&]() {goal_handle->abort(result);})) {
      publish_terminal_event_once(
        goal_handle, *result, robot_task_interfaces::msg::TaskEvent::ABORTED);
    }
  }

  template<typename Transition>
  bool commit_parent_terminal(
    const std::shared_ptr<GoalHandle> & goal_handle, Transition transition)
  {
    bool expected = false;
    // Only the first terminal transition may publish the result and TaskEvent;
    // cancel, deadline, shutdown, and the child callback can race here.
    if (!parent_terminal_.compare_exchange_strong(expected, true)) {
      return false;
    }
    try {
      run_worker_hook(task_executor::WorkerPhase::kBeforeParentTransition);
      transition();
    } catch (...) {
      if (!goal_handle->is_active()) {
        return true;
      }
      parent_terminal_ = false;
      throw;
    }
    return true;
  }

  void cancel_child_once(const std::shared_ptr<ChildExecutionState> & child)
  {
    DeviceGoalHandle::SharedPtr handle;
    {
      const std::lock_guard<std::mutex> lock(child->mutex);
      if (!child->cancel_requested) {
        child->cancel_requested = true;
        child->cancel_requested_at = std::chrono::steady_clock::now();
      }
      if (!child->handle || child->cancel_dispatched) {
        return;
      }
      handle = child->handle;
      child->cancel_dispatched = true;
      child->cancel_started_at = std::chrono::steady_clock::now();
    }
    try {
      run_worker_hook(task_executor::WorkerPhase::kBeforeCancelGoal);
      device_client_->async_cancel_goal(handle);
    } catch (const std::exception & error) {
      {
        const std::lock_guard<std::mutex> lock(child->mutex);
        child->cancel_dispatched = false;
      }
      child->changed.notify_all();
      RCLCPP_WARN(get_logger(), "Child cancel dispatch was not confirmed: %s", error.what());
    } catch (...) {
      {
        const std::lock_guard<std::mutex> lock(child->mutex);
        child->cancel_dispatched = false;
      }
      child->changed.notify_all();
      RCLCPP_WARN(get_logger(), "Child cancel dispatch was not confirmed: unknown exception");
    }
  }

  void record_cancel_timeout(
    const std::shared_ptr<ChildExecutionState> & child,
    const std::chrono::steady_clock::time_point now)
  {
    std::lock_guard<std::mutex> lock(child->mutex);
    if (child->cancel_dispatched && !child->cancel_timed_out &&
      now - child->cancel_started_at >= std::chrono::milliseconds(cancel_timeout_ms_))
    {
      child->cancel_timed_out = true;
    }
  }

  void settle_child_after_exception(const std::shared_ptr<ChildExecutionState> & child)
  {
    cancel_child_once(child);
    const auto deadline = std::chrono::steady_clock::now() +
      std::chrono::milliseconds(std::max<std::int64_t>(cancel_timeout_ms_, 1000));
    {
      std::unique_lock<std::mutex> lock(child->mutex);
      if (!child->dispatch_started) {
        return;
      }
      child->changed.wait_until(lock, deadline, [&]() {return child->goal_response_received;});
      if (!child->handle) {
        return;
      }
      if (child->terminal) {
        return;
      }
    }
    while (true) {
      cancel_child_once(child);
      std::unique_lock<std::mutex> lock(child->mutex);
      if (child->changed.wait_for(
          lock, std::chrono::milliseconds(25), [&]() {return child->terminal;}))
      {
        return;
      }
      if (std::chrono::steady_clock::now() >= deadline) {
        return;
      }
    }
  }

  void finish_worker_exception(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const std::shared_ptr<ChildExecutionState> & child,
    const std::string & detail)
  {
    const std::string message = "executor worker exception: " + detail;
    RCLCPP_ERROR(get_logger(), "%s", message.c_str());
    settle_child_after_exception(child);
    try {
      finish_aborted(
        goal_handle, ExecuteTask::Result::SAFE_STOP,
        kErrorUnexpectedChildResult, message);
    } catch (const std::exception & error) {
      RCLCPP_ERROR(get_logger(), "Failed to abort parent Action: %s", error.what());
    } catch (...) {
      RCLCPP_ERROR(get_logger(), "Failed to abort parent Action: unknown exception");
    }
  }

  void publish_terminal_event_once(
    const std::shared_ptr<GoalHandle> & goal_handle,
    const ExecuteTask::Result & result,
    const std::uint8_t action_status)
  {
    if (terminal_event_published_.exchange(true)) {
      return;
    }
    try {
      const auto duration_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - started_at_).count();
      const auto goal = goal_handle->get_goal();
      task_event_publisher_->publish(task_executor::make_task_event(
          goal->task_id, goal->target_id, goal->request_id, goal->workflow_id, goal_id_,
          action_status, result.outcome, result.error_code,
          static_cast<std::uint64_t>(duration_ms), result.message, now()));
    } catch (const std::exception & error) {
      RCLCPP_ERROR(get_logger(), "Failed to publish terminal task event: %s", error.what());
    } catch (...) {
      RCLCPP_ERROR(get_logger(), "Failed to publish terminal task event: unknown exception");
    }
  }

  std::uint16_t allocate_command_id()
  {
    if (next_command_id_ == 0 || next_command_id_ > kMaxApplicationCommandId) {
      next_command_id_ = 1;
    }
    return next_command_id_++;
  }

  void run_worker_hook(const task_executor::WorkerPhase phase)
  {
    if (worker_hook_) {
      worker_hook_(phase);
    }
  }

  std::string goal_id_;
  int validation_delay_ms_{200};
  std::int64_t ack_timeout_ms_{500};
  std::int64_t cancel_timeout_ms_{1000};
  std::int64_t diagnostic_period_ms_{1000};
  std::size_t task_event_history_depth_{128};
  std::uint8_t device_opcode_{1};
  std::uint16_t next_command_id_{1};
  std::unique_ptr<task_executor::TargetValidator> validator_;
  std::unique_ptr<runtime_history::StageEventLog> stage_log_;
  std::unordered_map<std::string, std::int32_t> target_arguments_;
  std::atomic_bool running_{true};
  std::atomic_bool active_goal_{false};
  std::atomic_bool parent_terminal_{false};
  std::atomic_bool terminal_event_published_{false};
  std::chrono::steady_clock::time_point started_at_;
  task_executor::TaskDiagnosticState diagnostic_state_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diagnostics_publisher_;
  rclcpp::Publisher<robot_task_interfaces::msg::TaskEvent>::SharedPtr task_event_publisher_;
  rclcpp::TimerBase::SharedPtr diagnostics_timer_;
  rclcpp_action::Client<ExecuteDeviceCommand>::SharedPtr device_client_;
  rclcpp_action::Server<ExecuteTask>::SharedPtr action_server_;
  std::mutex workers_mutex_;
  std::condition_variable workers_changed_;
  std::thread worker_;
  std::size_t workers_active_{0};
  task_executor::WorkerHook worker_hook_;
};

namespace task_executor
{

std::shared_ptr<rclcpp::Node> make_task_executor_node(
  const rclcpp::NodeOptions & options, WorkerHook worker_hook)
{
  return std::make_shared<TaskExecutorNode>(options, std::move(worker_hook));
}

void request_task_executor_shutdown(const std::shared_ptr<rclcpp::Node> & node)
{
  const auto executor = std::dynamic_pointer_cast<TaskExecutorNode>(node);
  if (!executor) {
    throw std::invalid_argument("node is not a Task Executor");
  }
  executor->request_shutdown();
}

void wait_for_task_executor_shutdown(const std::shared_ptr<rclcpp::Node> & node)
{
  const auto executor = std::dynamic_pointer_cast<TaskExecutorNode>(node);
  if (!executor) {
    throw std::invalid_argument("node is not a Task Executor");
  }
  executor->wait_for_shutdown();
}

bool wait_for_task_executor_shutdown_for(
  const std::shared_ptr<rclcpp::Node> & node, const std::chrono::milliseconds timeout)
{
  const auto executor = std::dynamic_pointer_cast<TaskExecutorNode>(node);
  if (!executor) {
    throw std::invalid_argument("node is not a Task Executor");
  }
  return executor->wait_for_shutdown_for(timeout);
}

void request_task_executor_process_shutdown(int) {process_shutdown_requested = 1;}

int run_task_executor_node(
  const std::shared_ptr<rclcpp::Node> & node, SpinFunction primary_spin,
  SpinFunction recovery_spin)
{
  process_shutdown_requested = 0;
  rclcpp::executors::MultiThreadedExecutor executor;
  executor.add_node(node);
  std::exception_ptr spin_error;
  std::atomic_bool spin_finished{false};
  std::thread spin_thread([&]() {
      try {
        if (primary_spin) {
          primary_spin(executor);
        } else {
          executor.spin();
        }
      } catch (...) {
        spin_error = std::current_exception();
      }
      spin_finished = true;
    });
  while (process_shutdown_requested == 0 && !spin_finished) {
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }

  if (process_shutdown_requested != 0) {
    request_task_executor_shutdown(node);
    while (!wait_for_task_executor_shutdown_for(node, std::chrono::milliseconds(25))) {
      if (spin_finished) {
        break;
      }
    }
    if (!spin_finished) {
      executor.cancel();
      spin_thread.join();
      executor.remove_node(node);
      return 0;
    }
  }

  spin_thread.join();
  executor.remove_node(node);
  if (!rclcpp::ok()) {
    RCLCPP_FATAL(node->get_logger(), "Primary executor stopped after ROS context shutdown");
    return 2;
  }
  if (!spin_error) {
    spin_error = std::make_exception_ptr(std::runtime_error("executor stopped unexpectedly"));
  }

  try {
    std::rethrow_exception(spin_error);
  } catch (const std::exception & error) {
    RCLCPP_ERROR(node->get_logger(), "Primary executor failed: %s", error.what());
  } catch (...) {
    RCLCPP_ERROR(node->get_logger(), "Primary executor failed: unknown exception");
  }

  rclcpp::executors::MultiThreadedExecutor recovery_executor;
  recovery_executor.add_node(node);
  std::exception_ptr recovery_error;
  std::atomic_bool recovery_finished{false};
  std::thread recovery_thread([&]() {
      try {
        if (recovery_spin) {
          recovery_spin(recovery_executor);
        } else {
          recovery_executor.spin();
        }
      } catch (...) {
        recovery_error = std::current_exception();
      }
      recovery_finished = true;
    });
  request_task_executor_shutdown(node);
  while (!wait_for_task_executor_shutdown_for(node, std::chrono::milliseconds(25))) {
    if (recovery_finished) {
      recovery_thread.join();
      recovery_executor.remove_node(node);
      RCLCPP_FATAL(node->get_logger(), "Recovery executor stopped before child drain");
      return 3;
    }
  }
  const bool recovery_lost = recovery_finished;
  recovery_executor.cancel();
  recovery_thread.join();
  recovery_executor.remove_node(node);
  if (recovery_lost || recovery_error) {
    RCLCPP_FATAL(node->get_logger(), "Recovery executor failed before clean shutdown");
    return 3;
  }
  return 1;
}

}  // namespace task_executor
