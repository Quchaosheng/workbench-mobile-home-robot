#include "task_orchestrator/ready_state.hpp"
#include "task_orchestrator_node.hpp"

#include "ament_index_cpp/get_package_share_directory.hpp"
#include "behaviortree_cpp/action_node.h"
#include "behaviortree_cpp/bt_factory.h"
#include "behaviortree_cpp/condition_node.h"
#include "robot_task_interfaces/action/execute_task.hpp"
#include "robot_task_interfaces/action/execute_workflow.hpp"
#include "std_msgs/msg/bool.hpp"

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <exception>
#include <limits>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <optional>
#include <cstdlib>
#include <string>
#include <thread>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"

namespace task_orchestrator
{

using namespace std::chrono_literals;
using ExecuteTask = robot_task_interfaces::action::ExecuteTask;
using ExecuteWorkflow = robot_task_interfaces::action::ExecuteWorkflow;
using ChildGoalHandle = rclcpp_action::ClientGoalHandle<ExecuteTask>;

struct WorkflowState
{
  WorkflowState(const ExecuteWorkflow::Goal & workflow_goal, const SteadyTime workflow_deadline)
  : deadline(workflow_deadline)
  {
    child_goal.task_id = workflow_goal.task_id;
    child_goal.target_id = workflow_goal.target_id;
    child_goal.request_id = workflow_goal.request_id;
    child_goal.workflow_id = workflow_goal.workflow_id;
  }

  void mark(std::string node, const float value)
  {
    std::lock_guard<std::mutex> lock(mutex);
    active_node = std::move(node);
    progress = value;
  }

  std::mutex mutex;
  std::condition_variable changed;
  ExecuteTask::Goal child_goal;
  ChildGoalHandle::SharedPtr child_handle;
  std::optional<ChildGoalHandle::WrappedResult> child_result;
  SteadyTime deadline;
  bool goal_response_received{false};
  bool child_goal_dispatched{false};
  bool halt_requested{false};
  bool cancel_in_flight{false};
  bool cancel_dispatched{false};
  bool cancel_timed_out{false};
  std::string active_node{"RuntimeReady"};
  float progress{0.0F};
};

class RuntimeReadyNode : public BT::ConditionNode
{
public:
  RuntimeReadyNode(
    const std::string & name, const BT::NodeConfig & config,
    std::shared_ptr<ReadyState> ready, const std::chrono::milliseconds stale_after)
  : BT::ConditionNode(name, config), ready_(std::move(ready)), stale_after_(stale_after)
  {}

  static BT::PortsList providedPorts() {return {};}

private:
  BT::NodeStatus tick() override
  {
    return ready_->usable(std::chrono::steady_clock::now(), stale_after_) ?
           BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
  }

  std::shared_ptr<ReadyState> ready_;
  std::chrono::milliseconds stale_after_;
};

class RetryReadyNode : public BT::StatefulActionNode
{
public:
  RetryReadyNode(
    const std::string & name, const BT::NodeConfig & config,
    std::shared_ptr<ReadyState> ready, const std::chrono::milliseconds stale_after)
  : BT::StatefulActionNode(name, config), ready_(std::move(ready)), stale_after_(stale_after)
  {}

  static BT::PortsList providedPorts()
  {
    return {BT::InputPort<unsigned>("attempts"), BT::InputPort<unsigned>("delay_ms")};
  }

private:
  BT::NodeStatus onStart() override
  {
    attempts_ = getInput<unsigned>("attempts").value();
    delay_ = std::chrono::milliseconds(getInput<unsigned>("delay_ms").value());
    used_ = 1;
    if (ready_->usable(std::chrono::steady_clock::now(), stale_after_)) {
      return BT::NodeStatus::SUCCESS;
    }
    if (attempts_ <= 1) {
      return BT::NodeStatus::FAILURE;
    }
    next_check_ = std::chrono::steady_clock::now() + delay_;
    return BT::NodeStatus::RUNNING;
  }

  BT::NodeStatus onRunning() override
  {
    if (std::chrono::steady_clock::now() < next_check_) {
      return BT::NodeStatus::RUNNING;
    }
    ++used_;
    if (ready_->usable(std::chrono::steady_clock::now(), stale_after_)) {
      return BT::NodeStatus::SUCCESS;
    }
    if (used_ >= attempts_) {
      return BT::NodeStatus::FAILURE;
    }
    next_check_ = std::chrono::steady_clock::now() + delay_;
    return BT::NodeStatus::RUNNING;
  }

  void onHalted() override {}

  std::shared_ptr<ReadyState> ready_;
  std::chrono::milliseconds stale_after_;
  std::chrono::milliseconds delay_{0};
  SteadyTime next_check_;
  unsigned attempts_{0};
  unsigned used_{0};
};

class ExecuteTaskNode : public BT::StatefulActionNode
{
public:
  ExecuteTaskNode(
    const std::string & name, const BT::NodeConfig & config,
    std::shared_ptr<WorkflowState> state,
    rclcpp_action::Client<ExecuteTask>::SharedPtr client,
    const std::chrono::milliseconds cancel_timeout, FaultHook fault_hook)
  : BT::StatefulActionNode(name, config), state_(std::move(state)),
    client_(std::move(client)), cancel_timeout_(cancel_timeout),
    fault_hook_(std::move(fault_hook))
  {}

  static BT::PortsList providedPorts() {return {};}

private:
  static bool dispatch_cancel(
    const std::shared_ptr<WorkflowState> & state,
    const rclcpp_action::Client<ExecuteTask>::SharedPtr & client,
    const ChildGoalHandle::SharedPtr & handle, const FaultHook & fault_hook) noexcept
  {
    bool dispatched = false;
    try {
      if (fault_hook) {
        fault_hook(FaultPoint::kBeforeCancelDispatch);
      }
      client->async_cancel_goal(handle);
      dispatched = true;
    } catch (const std::exception & error) {
      RCLCPP_ERROR(
        rclcpp::get_logger("task_orchestrator"), "Child cancel dispatch failed: %s",
        error.what());
    } catch (...) {
      RCLCPP_ERROR(
        rclcpp::get_logger("task_orchestrator"),
        "Child cancel dispatch failed: unknown exception");
    }
    {
      std::lock_guard<std::mutex> lock(state->mutex);
      state->cancel_in_flight = false;
      state->cancel_dispatched = dispatched;
    }
    state->changed.notify_all();
    return dispatched;
  }

  BT::NodeStatus onStart() override
  {
    state_->mark("ExecuteTask", 0.25F);
    // Derive the child budget from the parent's remaining steady-clock time so
    // a late Action dispatch cannot extend the workflow deadline.
    const auto remaining = std::chrono::duration_cast<std::chrono::nanoseconds>(
      state_->deadline - std::chrono::steady_clock::now());
    if (remaining <= 0ns) {
      return BT::NodeStatus::FAILURE;
    }
    const auto seconds = std::chrono::duration_cast<std::chrono::seconds>(remaining);
    if (seconds.count() > std::numeric_limits<std::int32_t>::max()) {
      return BT::NodeStatus::FAILURE;
    }
    state_->child_goal.allowed_duration.sec = static_cast<std::int32_t>(seconds.count());
    state_->child_goal.allowed_duration.nanosec = static_cast<std::uint32_t>(
      (remaining - seconds).count());
    {
      std::lock_guard<std::mutex> lock(state_->mutex);
      state_->child_goal_dispatched = true;
    }
    typename rclcpp_action::Client<ExecuteTask>::SendGoalOptions options;
    const auto state = state_;
    const auto client = client_;
    const auto fault_hook = fault_hook_;
    options.goal_response_callback = [state, client, fault_hook](
      const ChildGoalHandle::SharedPtr handle) {
        ChildGoalHandle::SharedPtr cancel_handle;
        {
          std::lock_guard<std::mutex> lock(state->mutex);
          state->goal_response_received = true;
          state->child_handle = handle;
          if (handle && state->halt_requested && !state->cancel_dispatched &&
            !state->cancel_in_flight)
          {
            state->cancel_in_flight = true;
            cancel_handle = handle;
          }
        }
        state->changed.notify_all();
        if (cancel_handle) {
          (void)dispatch_cancel(state, client, cancel_handle, fault_hook);
        }
      };
    options.feedback_callback = [state](
      ChildGoalHandle::SharedPtr, const std::shared_ptr<const ExecuteTask::Feedback> feedback) {
        state->mark("ExecuteTask", feedback->progress);
      };
    options.result_callback = [state](const ChildGoalHandle::WrappedResult & result) {
        {
          std::lock_guard<std::mutex> lock(state->mutex);
          state->child_result = result;
        }
        state->changed.notify_all();
      };
    client_->async_send_goal(state_->child_goal, options);
    return BT::NodeStatus::RUNNING;
  }

  BT::NodeStatus onRunning() override
  {
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (state_->child_result) {
      const auto & result = *state_->child_result;
      return result.result &&
             result.code == rclcpp_action::ResultCode::SUCCEEDED &&
             result.result->outcome == ExecuteTask::Result::COMPLETED ?
             BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
    }
    if (state_->goal_response_received && !state_->child_handle) {
      return BT::NodeStatus::FAILURE;
    }
    return BT::NodeStatus::RUNNING;
  }

  void onHalted() override
  {
    const auto cancel_deadline = std::chrono::steady_clock::now() + cancel_timeout_;
    std::unique_lock<std::mutex> lock(state_->mutex);
    state_->halt_requested = true;
    const auto mark_timeout = [this, cancel_deadline, &lock](const auto & predicate) {
        if (!state_->cancel_timed_out && !predicate() &&
          !state_->changed.wait_until(lock, cancel_deadline, predicate))
        {
          state_->cancel_timed_out = true;
        }
      };

    const auto response_ready = [this]() {
        return state_->child_result.has_value() || state_->goal_response_received;
      };
    mark_timeout(response_ready);
    if (!response_ready()) {
      return;
    }
    if (state_->child_result || !state_->child_handle) {
      return;
    }

    while (!state_->child_result && !state_->cancel_dispatched) {
      if (state_->cancel_in_flight) {
        if (!state_->changed.wait_until(lock, cancel_deadline, [this]() {
            return state_->child_result.has_value() || !state_->cancel_in_flight;
            }))
        {
          state_->cancel_timed_out = true;
          return;
        }
        continue;
      }
      if (std::chrono::steady_clock::now() >= cancel_deadline) {
        state_->cancel_timed_out = true;
        return;
      }
      state_->cancel_in_flight = true;
      const auto child_handle = state_->child_handle;
      lock.unlock();
      const bool dispatched = dispatch_cancel(state_, client_, child_handle, fault_hook_);
      lock.lock();
      if (!dispatched && !state_->child_result) {
        state_->changed.wait_for(lock, 20ms, [this]() {
            return state_->child_result.has_value() || state_->cancel_dispatched;
          });
      }
    }

    const auto child_terminal = [this]() {return state_->child_result.has_value();};
    mark_timeout(child_terminal);
    if (!child_terminal()) {
      return;
    }
  }

  std::shared_ptr<WorkflowState> state_;
  rclcpp_action::Client<ExecuteTask>::SharedPtr client_;
  std::chrono::milliseconds cancel_timeout_;
  FaultHook fault_hook_;
};

class TaskOrchestratorNode : public rclcpp::Node
{
public:
  using WorkflowGoalHandle = rclcpp_action::ServerGoalHandle<ExecuteWorkflow>;

  TaskOrchestratorNode(const rclcpp::NodeOptions & options, FaultHook fault_hook)
  : Node("task_orchestrator", options), ready_(std::make_shared<ReadyState>()),
    fault_hook_(std::move(fault_hook))
  {
    const auto ready_stale_ms = declare_parameter<int>("ready_stale_ms", 2000);
    const auto cancel_timeout_ms = declare_parameter<int>("cancel_timeout_ms", 1000);
    if (ready_stale_ms <= 0 || cancel_timeout_ms <= 0) {
      throw std::invalid_argument("ready_stale_ms and cancel_timeout_ms must be positive");
    }
    ready_stale_ = std::chrono::milliseconds(ready_stale_ms);
    cancel_timeout_ = std::chrono::milliseconds(cancel_timeout_ms);
    workflow_xml_ = ament_index_cpp::get_package_share_directory("task_orchestrator") +
      "/config/workflows.xml";

    ready_subscription_ = create_subscription<std_msgs::msg::Bool>(
      "/runtime/ready", 10,
      [this](const std_msgs::msg::Bool & message) {
        ready_->update(message.data, std::chrono::steady_clock::now());
      });
    task_client_ = rclcpp_action::create_client<ExecuteTask>(this, "execute_task");
    workflow_server_ = rclcpp_action::create_server<ExecuteWorkflow>(
      this, "execute_workflow",
      std::bind(&TaskOrchestratorNode::handle_goal, this, std::placeholders::_1,
        std::placeholders::_2),
      std::bind(&TaskOrchestratorNode::handle_cancel, this, std::placeholders::_1),
      std::bind(&TaskOrchestratorNode::handle_accepted, this, std::placeholders::_1));
  }

  ~TaskOrchestratorNode() override
  {
    drain_and_stop();
  }

  void request_stop()
  {
    accepting_ = false;
    running_ = false;
  }

  bool wait_drained_for(const std::chrono::milliseconds timeout)
  {
    std::thread worker;
    {
      std::unique_lock<std::mutex> lock(worker_mutex_);
      if (!worker_changed_.wait_for(lock, timeout, [this]() {return !active_;})) {
        return false;
      }
      worker.swap(worker_);
    }
    if (worker.joinable()) {
      worker.join();
    }
    return true;
  }

  void drain_and_stop()
  {
    request_stop();
    while (!wait_drained_for(100ms)) {
    }
  }

private:
  static constexpr std::uint16_t kErrorShutdown = 205;
  static constexpr std::uint16_t kErrorPrecondition = 206;
  static constexpr std::uint16_t kErrorCancelUnconfirmed = 207;
  static constexpr std::uint16_t kErrorInternal = 208;
  static constexpr std::uint16_t kErrorDeadline = 209;

  rclcpp_action::GoalResponse handle_goal(
    const rclcpp_action::GoalUUID &,
    const std::shared_ptr<const ExecuteWorkflow::Goal> goal)
  {
    const auto duration = goal->allowed_duration;
    if (!accepting_ || duration.sec < 0 || duration.nanosec >= 1000000000U ||
      (duration.sec == 0 && duration.nanosec == 0))
    {
      RCLCPP_WARN(get_logger(), "Rejecting workflow_id=%s: INVALID_DURATION_OR_SHUTDOWN",
        goal->workflow_id.c_str());
      return rclcpp_action::GoalResponse::REJECT;
    }
    if (goal->workflow_id != "single_task" && goal->workflow_id != "ready_then_task") {
      RCLCPP_WARN(
        get_logger(), "Rejecting workflow_id=%s: WORKFLOW_NOT_ALLOWLISTED",
        goal->workflow_id.c_str());
      return rclcpp_action::GoalResponse::REJECT;
    }
    bool expected = false;
    if (!active_.compare_exchange_strong(expected, true)) {
      RCLCPP_WARN(get_logger(), "Rejecting workflow_id=%s: WORKFLOW_BUSY",
          goal->workflow_id.c_str());
      return rclcpp_action::GoalResponse::REJECT;
    }
    active_generation_.store(generation_counter_.fetch_add(1) + 1);
    if (!accepting_) {
      active_ = false;
      worker_changed_.notify_all();
      return rclcpp_action::GoalResponse::REJECT;
    }
    terminal_started_ = false;
    fallback_attempted_ = false;
    terminal_transition_failed_ = false;
    {
      std::lock_guard<std::mutex> lock(terminal_mutex_);
      cancel_accepted_ = false;
      terminal_reserved_ = false;
      const auto allowed = std::chrono::seconds(duration.sec) +
        std::chrono::nanoseconds(duration.nanosec);
      const auto started_at = std::chrono::steady_clock::now();
      active_deadline_ = allowed > SteadyTime::max() - started_at ?
        SteadyTime::max() : started_at + allowed;
    }
    return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
  }

  rclcpp_action::CancelResponse handle_cancel(const std::shared_ptr<WorkflowGoalHandle>)
  {
    std::lock_guard<std::mutex> lock(terminal_mutex_);
    if (terminal_reserved_) {
      return rclcpp_action::CancelResponse::REJECT;
    }
    cancel_accepted_ = true;
    return rclcpp_action::CancelResponse::ACCEPT;
  }

  void handle_accepted(const std::shared_ptr<WorkflowGoalHandle> goal_handle)
  {
    const auto generation = active_generation_.load();
    std::lock_guard<std::mutex> lock(worker_mutex_);
    if (worker_.joinable()) {
      worker_.join();
    }
    worker_ = std::thread([this, goal_handle, generation]() {
          run_worker(goal_handle, generation);
      });
    worker_changed_.notify_all();
  }

  void release_active_for_generation(const std::uint64_t generation) noexcept
  {
    if (active_generation_.load() == generation) {
      active_.store(false);
      worker_changed_.notify_all();
    }
  }

  void finish_worker(const std::uint64_t generation) noexcept
  {
    if (!terminal_transition_failed_ && active_generation_.load() == generation) {
      release_active_for_generation(generation);
    }
  }

  void run_worker(
    const std::shared_ptr<WorkflowGoalHandle> & goal_handle,
    const std::uint64_t generation) noexcept
  {
    struct ActiveGuard
    {
      TaskOrchestratorNode & node;
      const std::uint64_t generation;
      ~ActiveGuard() {node.finish_worker(generation);}
    } guard{*this, generation};

    try {
      execute(goal_handle);
    } catch (const std::exception & error) {
      RCLCPP_ERROR(get_logger(), "Workflow worker exception: %s", error.what());
      (void)terminal(
        goal_handle, ExecuteWorkflow::Result::SAFE_STOP, kErrorInternal,
        "workflow worker internal error", Terminal::kAbort);
    } catch (...) {
      RCLCPP_ERROR(get_logger(), "Workflow worker exception: unknown exception");
      (void)terminal(
        goal_handle, ExecuteWorkflow::Result::SAFE_STOP, kErrorInternal,
        "workflow worker internal error", Terminal::kAbort);
    }
  }

  void execute(const std::shared_ptr<WorkflowGoalHandle> & goal_handle)
  {
    SteadyTime deadline;
    {
      std::lock_guard<std::mutex> lock(terminal_mutex_);
      deadline = active_deadline_;
    }
    auto state = std::make_shared<WorkflowState>(*goal_handle->get_goal(), deadline);
    BT::BehaviorTreeFactory factory;
    factory.registerNodeType<RuntimeReadyNode>("RuntimeReady", ready_, ready_stale_);
    factory.registerNodeType<RetryReadyNode>("RetryReady", ready_, ready_stale_);
    factory.registerNodeType<ExecuteTaskNode>(
      "ExecuteTask", state, task_client_, cancel_timeout_, fault_hook_);
    factory.registerBehaviorTreeFromFile(workflow_xml_);
    auto tree = factory.createTree(goal_handle->get_goal()->workflow_id);

    while (running_ && rclcpp::ok()) {
      if (goal_handle->is_canceling()) {
        tree.haltTree();
        finish(goal_handle, state);
        return;
      }
      if (std::chrono::steady_clock::now() >= state->deadline) {
        tree.haltTree();
        (void)terminal(
          goal_handle, ExecuteWorkflow::Result::SAFE_STOP, kErrorDeadline,
          "workflow allowed duration exceeded", Terminal::kAbort);
        return;
      }
      const auto status = tree.tickOnce();
      publish_feedback(goal_handle, state);
      if (std::chrono::steady_clock::now() >= state->deadline) {
        tree.haltTree();
        (void)terminal(
          goal_handle, ExecuteWorkflow::Result::SAFE_STOP, kErrorDeadline,
          "workflow allowed duration exceeded", Terminal::kAbort);
        return;
      }
      if (status == BT::NodeStatus::SUCCESS || status == BT::NodeStatus::FAILURE) {
        finish(goal_handle, state);
        return;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    tree.haltTree();
    (void)terminal(
      goal_handle, ExecuteWorkflow::Result::SAFE_STOP, kErrorShutdown,
      "orchestrator shutdown", Terminal::kAbort);
  }

  static void publish_feedback(
    const std::shared_ptr<WorkflowGoalHandle> & goal_handle,
    const std::shared_ptr<WorkflowState> & state)
  {
    auto feedback = std::make_shared<ExecuteWorkflow::Feedback>();
    {
      std::lock_guard<std::mutex> lock(state->mutex);
      feedback->active_node = state->active_node;
      feedback->progress = state->progress;
    }
    goal_handle->publish_feedback(feedback);
  }

  void finish(
    const std::shared_ptr<WorkflowGoalHandle> & goal_handle,
    const std::shared_ptr<WorkflowState> & state)
  {
    const bool parent_canceling = goal_handle->is_canceling();
    std::optional<ChildGoalHandle::WrappedResult> wrapped;
    bool child_goal_dispatched;
    bool cancel_timed_out;
    {
      std::lock_guard<std::mutex> lock(state->mutex);
      wrapped = state->child_result;
      child_goal_dispatched = state->child_goal_dispatched;
      cancel_timed_out = state->cancel_timed_out;
    }
    if (parent_canceling && cancel_timed_out) {
      terminal(
        goal_handle, ExecuteWorkflow::Result::SAFE_STOP, kErrorCancelUnconfirmed,
        "child task cancel exceeded the confirmation budget", Terminal::kAbort);
      return;
    }
    if (!wrapped || !wrapped->result) {
      if (parent_canceling && child_goal_dispatched) {
        terminal(
          goal_handle, ExecuteWorkflow::Result::SAFE_STOP, kErrorCancelUnconfirmed,
          "child task cancel was not confirmed in time", Terminal::kAbort);
      } else if (parent_canceling) {
        terminal(
          goal_handle, ExecuteWorkflow::Result::CANCELED, 0,
          "workflow canceled before child task dispatch", Terminal::kCanceled);
      } else {
        terminal(
          goal_handle, ExecuteWorkflow::Result::SAFE_STOP, kErrorPrecondition,
          "workflow precondition failed", Terminal::kAbort);
      }
      return;
    }

    const auto & child = *wrapped->result;
    if (parent_canceling) {
      if (child.outcome == ExecuteWorkflow::Result::CANCELED) {
        terminal(
          goal_handle, child.outcome, child.error_code, child.message, Terminal::kCanceled);
      } else if (child.outcome == ExecuteWorkflow::Result::SAFE_STOP ||
        child.outcome == ExecuteWorkflow::Result::DEVICE_FAULT)
      {
        terminal(
          goal_handle, child.outcome, child.error_code, child.message, Terminal::kAbort);
      } else {
        terminal(
          goal_handle, ExecuteWorkflow::Result::SAFE_STOP, kErrorInternal,
          "parent cancel raced with non-canceled child result", Terminal::kAbort);
      }
      return;
    }

    if (child.outcome == ExecuteWorkflow::Result::COMPLETED &&
      wrapped->code == rclcpp_action::ResultCode::SUCCEEDED)
    {
      terminal(
        goal_handle, child.outcome, child.error_code, child.message, Terminal::kSucceed);
    } else if (child.outcome == ExecuteWorkflow::Result::CANCELED) {
      terminal(
        goal_handle, ExecuteWorkflow::Result::SAFE_STOP, kErrorInternal,
        "child canceled without parent cancellation", Terminal::kAbort);
    } else {
      terminal(
        goal_handle, child.outcome, child.error_code, child.message, Terminal::kAbort);
    }
  }

  enum class Terminal {kSucceed, kAbort, kCanceled};

  bool terminal(
    const std::shared_ptr<WorkflowGoalHandle> & goal_handle, const std::uint8_t outcome,
    const std::uint16_t error_code, const std::string & message,
    const Terminal terminal_state) noexcept
  {
    if (terminal_started_.exchange(true)) {
      return !terminal_transition_failed_;
    }
    std::uint8_t committed_outcome = outcome;
    std::uint16_t committed_error_code = error_code;
    std::string committed_message = message;
    Terminal committed_terminal = terminal_state;
    if (terminal_state == Terminal::kSucceed && fault_hook_) {
      try {
        fault_hook_(FaultPoint::kBeforeTerminalCommit);
      } catch (const std::exception & error) {
        RCLCPP_ERROR(get_logger(), "Before-terminal-commit hook failed: %s", error.what());
      } catch (...) {
        RCLCPP_ERROR(get_logger(), "Before-terminal-commit hook failed: unknown exception");
      }
    }
    {
      std::lock_guard<std::mutex> lock(terminal_mutex_);
      if (terminal_reserved_) {
        return !terminal_transition_failed_;
      }
      if (terminal_state == Terminal::kSucceed && cancel_accepted_) {
        committed_outcome = ExecuteWorkflow::Result::SAFE_STOP;
        committed_error_code = kErrorInternal;
        committed_message = "parent cancel raced with terminal child result";
        committed_terminal = Terminal::kAbort;
      } else if (terminal_state == Terminal::kSucceed &&
        std::chrono::steady_clock::now() >= active_deadline_)
      {
        committed_outcome = ExecuteWorkflow::Result::SAFE_STOP;
        committed_error_code = kErrorDeadline;
        committed_message = "workflow allowed duration exceeded at terminal commit";
        committed_terminal = Terminal::kAbort;
      }
      terminal_reserved_ = true;
    }
    try {
      if (!goal_handle->is_active()) {
        RCLCPP_WARN(get_logger(), "Skipping terminal transition: workflow goal is not active");
        return true;
      }
      auto result = std::make_shared<ExecuteWorkflow::Result>();
      result->outcome = committed_outcome;
      result->error_code = committed_error_code;
      result->message = committed_message;
      if (fault_hook_) {
        fault_hook_(FaultPoint::kBeforeTerminalTransition);
      }
      switch (committed_terminal) {
        case Terminal::kSucceed:
          goal_handle->succeed(result);
          break;
        case Terminal::kAbort:
          goal_handle->abort(result);
          break;
        case Terminal::kCanceled:
          goal_handle->canceled(result);
          break;
      }
      release_active_for_generation(active_generation_.load());
      return true;
    } catch (const std::exception & error) {
      RCLCPP_ERROR(get_logger(), "Workflow terminal transition failed: %s", error.what());
    } catch (...) {
      RCLCPP_ERROR(get_logger(), "Workflow terminal transition failed: unknown exception");
    }

    if (goal_handle->is_active() && !fallback_attempted_.exchange(true)) {
      try {
        if (fault_hook_) {
          fault_hook_(FaultPoint::kBeforeFallbackAbort);
        }
        auto fallback = std::make_shared<ExecuteWorkflow::Result>();
        fallback->outcome = ExecuteWorkflow::Result::SAFE_STOP;
        fallback->error_code = kErrorInternal;
        fallback->message = "workflow terminal transition fallback";
        goal_handle->abort(fallback);
        release_active_for_generation(active_generation_.load());
        return true;
      } catch (const std::exception & error) {
        RCLCPP_ERROR(get_logger(), "Workflow fallback abort failed: %s", error.what());
      } catch (...) {
        RCLCPP_ERROR(get_logger(), "Workflow fallback abort failed: unknown exception");
      }
    }
    if (goal_handle->is_active()) {
      terminal_transition_failed_ = true;
      return false;
    }
    return true;
  }

  std::atomic_bool accepting_{true};
  std::atomic_bool running_{true};
  std::atomic_bool active_{false};
  std::atomic_uint64_t generation_counter_{0};
  std::atomic_uint64_t active_generation_{0};
  std::atomic_bool terminal_started_{false};
  std::atomic_bool fallback_attempted_{false};
  std::atomic_bool terminal_transition_failed_{false};
  std::mutex terminal_mutex_;
  bool cancel_accepted_{false};
  bool terminal_reserved_{false};
  SteadyTime active_deadline_{};
  std::shared_ptr<ReadyState> ready_;
  std::chrono::milliseconds ready_stale_{2000};
  std::chrono::milliseconds cancel_timeout_{1000};
  std::string workflow_xml_;
  rclcpp::Subscription<std_msgs::msg::Bool>::SharedPtr ready_subscription_;
  rclcpp_action::Client<ExecuteTask>::SharedPtr task_client_;
  rclcpp_action::Server<ExecuteWorkflow>::SharedPtr workflow_server_;
  std::mutex worker_mutex_;
  std::condition_variable worker_changed_;
  std::thread worker_;
  FaultHook fault_hook_;
};

std::shared_ptr<rclcpp::Node> make_task_orchestrator_node(
  const rclcpp::NodeOptions & options, FaultHook fault_hook)
{
  return std::make_shared<TaskOrchestratorNode>(options, std::move(fault_hook));
}

}  // namespace task_orchestrator

namespace
{

volatile std::sig_atomic_t shutdown_requested = 0;

#ifndef TASK_ORCHESTRATOR_NO_MAIN
void request_shutdown(int) {shutdown_requested = 1;}
#endif

}  // namespace

namespace task_orchestrator
{

int run_task_orchestrator_node(
  const std::shared_ptr<rclcpp::Node> & base_node, SpinFunction primary_spin,
  SpinFunction recovery_spin, StopRequested stop_requested)
{
  shutdown_requested = 0;
  auto node = std::static_pointer_cast<TaskOrchestratorNode>(base_node);
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
  const auto stop = [&]() {
      return stop_requested ? stop_requested() : shutdown_requested != 0;
    };
  while (!stop() && !spin_finished) {
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }

  const bool shutdown = stop();
  if (shutdown) {
    node->request_stop();
    while (!spin_finished) {
      if (node->wait_drained_for(20ms)) {
        executor.cancel();
        spin_thread.join();
        executor.remove_node(node);
        return 0;
      }
    }
  }

  spin_thread.join();
  executor.remove_node(node);
  if (!rclcpp::ok()) {
    return 2;
  }
  if (!spin_error) {
    spin_error = std::make_exception_ptr(std::runtime_error("executor stopped unexpectedly"));
  }

  node->request_stop();
  rclcpp::executors::MultiThreadedExecutor recovery_executor;
  recovery_executor.add_node(node);
  std::atomic_bool recovery_finished{false};
  std::thread recovery_thread([&]() {
      try {
        if (recovery_spin) {
          recovery_spin(recovery_executor);
        } else {
          recovery_executor.spin();
        }
      } catch (const std::exception & error) {
        RCLCPP_ERROR(base_node->get_logger(), "Recovery executor failed: %s", error.what());
      } catch (...) {
        RCLCPP_ERROR(base_node->get_logger(), "Recovery executor failed: unknown exception");
      }
      recovery_finished = true;
    });
  while (true) {
    if (node->wait_drained_for(20ms)) {
      recovery_executor.cancel();
      recovery_thread.join();
      recovery_executor.remove_node(node);
      std::rethrow_exception(spin_error);
    }
    if (recovery_finished) {
      recovery_thread.join();
      recovery_executor.remove_node(node);
      return 2;
    }
  }
}

}  // namespace task_orchestrator

#ifndef TASK_ORCHESTRATOR_NO_MAIN

int main(int argc, char ** argv)
{
  rclcpp::init(
    argc, argv, rclcpp::InitOptions(), rclcpp::SignalHandlerOptions::None);
  std::signal(SIGINT, request_shutdown);
  std::signal(SIGTERM, request_shutdown);
  try {
    auto node = task_orchestrator::make_task_orchestrator_node();
    const int result = task_orchestrator::run_task_orchestrator_node(node);
    if (result == 2) {
      std::_Exit(result);
    }
    rclcpp::shutdown();
    return result;
  } catch (const std::exception & error) {
    RCLCPP_FATAL(rclcpp::get_logger("task_orchestrator"), "%s", error.what());
    rclcpp::shutdown();
    return 1;
  }
}
#endif
