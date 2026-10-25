#include "ai_task_adapter/natural_language_planner.hpp"

#include "robot_task_interfaces/action/execute_task.hpp"

#include <chrono>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <string>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"

class AiTaskAdapterNode : public rclcpp::Node
{
public:
  using ExecuteTask = robot_task_interfaces::action::ExecuteTask;
  using GoalHandle = rclcpp_action::ClientGoalHandle<ExecuteTask>;

  AiTaskAdapterNode()
  : Node("ai_task_adapter")
  {
    request_ = declare_parameter<std::string>("request", "go to dock_a");
    task_id_ = declare_parameter<std::string>("task_id", "ai_demo_1");
    allowed_duration_ms_ = declare_parameter<std::int64_t>("allowed_duration_ms", 1000);
    cancel_after_ms_ = declare_parameter<std::int64_t>("cancel_after_ms", -1);
    const auto targets = declare_parameter<std::vector<std::string>>(
      "targets", {"dock_a", "home"});
    planner_ = std::make_unique<ai_task_adapter::NaturalLanguagePlanner>(targets);
    action_client_ = rclcpp_action::create_client<ExecuteTask>(this, "execute_task");
  }

  bool run()
  {
    using namespace std::chrono_literals;
    if (task_id_.empty() || allowed_duration_ms_ <= 0 ||
      allowed_duration_ms_ >
      static_cast<std::int64_t>(std::numeric_limits<std::int32_t>::max()) * 1000)
    {
      RCLCPP_ERROR(get_logger(), "Invalid task_id or allowed_duration_ms");
      exit_code_ = 2;
      return false;
    }

    builtin_interfaces::msg::Duration duration;
    duration.sec = static_cast<std::int32_t>(allowed_duration_ms_ / 1000);
    duration.nanosec = static_cast<std::uint32_t>(
      (allowed_duration_ms_ % 1000) * 1000000);

    ai_task_adapter::PlanError plan_error;
    const auto plan = planner_->plan(request_, duration, task_id_, plan_error);
    if (!plan) {
      RCLCPP_ERROR(
        get_logger(), "Plan rejected error=%s code=%u request=%s",
        ai_task_adapter::to_string(plan_error).data(),
        ai_task_adapter::error_code(plan_error), request_.c_str());
      exit_code_ = 2;
      return false;
    }

    RCLCPP_INFO(
      get_logger(), "Structured plan task_id=%s target_id=%s duration_ms=%ld",
      plan->task_id.c_str(), plan->target_id.c_str(), allowed_duration_ms_);
    if (!action_client_->wait_for_action_server(5s)) {
      RCLCPP_ERROR(get_logger(), "ExecuteTask server not available after 5 seconds");
      exit_code_ = 1;
      return false;
    }

    ExecuteTask::Goal goal;
    goal.task_id = plan->task_id;
    goal.target_id = plan->target_id;
    goal.allowed_duration = plan->allowed_duration;

    rclcpp_action::Client<ExecuteTask>::SendGoalOptions options;
    options.goal_response_callback = std::bind(
      &AiTaskAdapterNode::goal_response_callback, this, std::placeholders::_1);
    options.feedback_callback = std::bind(
      &AiTaskAdapterNode::feedback_callback, this, std::placeholders::_1,
      std::placeholders::_2);
    options.result_callback = std::bind(
      &AiTaskAdapterNode::result_callback, this, std::placeholders::_1);
    action_client_->async_send_goal(goal, options);
    return true;
  }

  int exit_code() const
  {
    return exit_code_;
  }

private:
  void goal_response_callback(const GoalHandle::SharedPtr & goal_handle)
  {
    if (!goal_handle) {
      RCLCPP_WARN(get_logger(), "ExecuteTask goal rejected");
      exit_code_ = 2;
      rclcpp::shutdown();
      return;
    }
    RCLCPP_INFO(get_logger(), "ExecuteTask goal accepted");
    goal_handle_ = goal_handle;
    if (cancel_after_ms_ >= 0) {
      cancel_timer_ = create_wall_timer(
        std::chrono::milliseconds(cancel_after_ms_),
        [this]() {
          cancel_timer_->cancel();
          RCLCPP_INFO(get_logger(), "Sending ExecuteTask cancel request");
          action_client_->async_cancel_goal(goal_handle_);
        });
    }
  }

  void feedback_callback(
    GoalHandle::SharedPtr,
    const std::shared_ptr<const ExecuteTask::Feedback> feedback)
  {
    RCLCPP_INFO(
      get_logger(), "ExecuteTask feedback state=%u progress=%.2f",
      feedback->state, feedback->progress);
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

    if (wrapped_result.result) {
      RCLCPP_INFO(
        get_logger(), "ExecuteTask result code=%d outcome=%u error_code=%u message=%s",
        static_cast<int>(wrapped_result.code), wrapped_result.result->outcome,
        wrapped_result.result->error_code, wrapped_result.result->message.c_str());
    } else {
      RCLCPP_ERROR(get_logger(), "ExecuteTask returned no result");
    }
    rclcpp::shutdown();
  }

  std::string request_;
  std::string task_id_;
  std::int64_t allowed_duration_ms_;
  std::int64_t cancel_after_ms_;
  int exit_code_{5};
  std::unique_ptr<ai_task_adapter::NaturalLanguagePlanner> planner_;
  rclcpp_action::Client<ExecuteTask>::SharedPtr action_client_;
  GoalHandle::SharedPtr goal_handle_;
  rclcpp::TimerBase::SharedPtr cancel_timer_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto node = std::make_shared<AiTaskAdapterNode>();
  if (!node->run()) {
    rclcpp::shutdown();
    return node->exit_code();
  }
  rclcpp::spin(node);
  return node->exit_code();
}
