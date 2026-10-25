#include "task_executor_node.hpp"

#include "robot_task_interfaces/action/execute_device_command.hpp"
#include "robot_task_interfaces/action/execute_task.hpp"
#include "robot_task_interfaces/msg/task_event.hpp"

#include <gtest/gtest.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <functional>
#include <future>
#include <iterator>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"

namespace
{
using namespace std::chrono_literals;
using ExecuteTask = robot_task_interfaces::action::ExecuteTask;
using ExecuteDeviceCommand = robot_task_interfaces::action::ExecuteDeviceCommand;
using TaskEvent = robot_task_interfaces::msg::TaskEvent;
using TaskGoalHandle = rclcpp_action::ClientGoalHandle<ExecuteTask>;
using DeviceGoalHandle = rclcpp_action::ServerGoalHandle<ExecuteDeviceCommand>;

class FakeDeviceBridge : public rclcpp::Node
{
public:
  struct Behavior
  {
    std::chrono::milliseconds goal_delay{0};
    bool accept{true};
    bool complete{true};
    bool hold_terminal_after_cancel{false};
  };

  FakeDeviceBridge(const std::string & name_space, Behavior behavior)
  : Node("fake_device_bridge", name_space), behavior_(behavior)
  {
    server_ = rclcpp_action::create_server<ExecuteDeviceCommand>(
      this, "execute_device_command",
      [this](const rclcpp_action::GoalUUID &, std::shared_ptr<const ExecuteDeviceCommand::Goal>) {
        std::this_thread::sleep_for(behavior_.goal_delay);
        return behavior_.accept ? rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE :
               rclcpp_action::GoalResponse::REJECT;
      },
      [this](const std::shared_ptr<DeviceGoalHandle>) {
        ++cancel_count_;
        return rclcpp_action::CancelResponse::ACCEPT;
      },
      [this](const std::shared_ptr<DeviceGoalHandle> goal_handle) {
        ++accepted_count_;
        std::lock_guard<std::mutex> lock(workers_mutex_);
        workers_.emplace_back([this, goal_handle]() {
          auto result = std::make_shared<ExecuteDeviceCommand::Result>();
          if (behavior_.complete) {
            result->outcome = ExecuteDeviceCommand::Result::COMPLETED;
            result->message = "completed";
            goal_handle->succeed(result);
            return;
          }
          while (running_ && rclcpp::ok() && !goal_handle->is_canceling()) {
            std::this_thread::sleep_for(2ms);
          }
          if (!running_) {
            return;
          }
          if (behavior_.hold_terminal_after_cancel) {
            std::unique_lock<std::mutex> lock(terminal_mutex_);
            terminal_changed_.wait(lock, [this]() {return terminal_released_ || !running_;});
            if (!running_) {
              return;
            }
          }
          result->outcome = ExecuteDeviceCommand::Result::CANCELED;
          result->message = "stopped";
          goal_handle->canceled(result);
          {
            const std::lock_guard<std::mutex> lock(terminal_mutex_);
            terminal_count_++;
          }
          terminal_changed_.notify_all();
        });
      });
  }

  ~FakeDeviceBridge() override
  {
    running_ = false;
    terminal_changed_.notify_all();
    std::vector<std::thread> workers;
    {
      std::lock_guard<std::mutex> lock(workers_mutex_);
      workers.swap(workers_);
    }
    for (auto & worker : workers) {
      if (worker.joinable()) {
        worker.join();
      }
    }
  }

  int accepted_count() const {return accepted_count_;}
  int cancel_count() const {return cancel_count_;}

  bool wait_for_cancel(const std::chrono::milliseconds timeout = 2s) const
  {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (std::chrono::steady_clock::now() < deadline) {
      if (cancel_count_ > 0) {
        return true;
      }
      std::this_thread::sleep_for(2ms);
    }
    return cancel_count_ > 0;
  }

  void release_terminal()
  {
    {
      const std::lock_guard<std::mutex> lock(terminal_mutex_);
      terminal_released_ = true;
    }
    terminal_changed_.notify_all();
  }

  bool wait_for_terminal(const std::chrono::milliseconds timeout = 2s)
  {
    std::unique_lock<std::mutex> lock(terminal_mutex_);
    return terminal_changed_.wait_for(lock, timeout, [this]() {return terminal_count_ > 0;});
  }

private:
  Behavior behavior_;
  std::atomic_int accepted_count_{0};
  std::atomic_int cancel_count_{0};
  std::atomic_bool running_{true};
  std::mutex workers_mutex_;
  std::vector<std::thread> workers_;
  std::mutex terminal_mutex_;
  std::condition_variable terminal_changed_;
  bool terminal_released_{false};
  int terminal_count_{0};
  rclcpp_action::Server<ExecuteDeviceCommand>::SharedPtr server_;
};

class ExecutorSafetyTest : public ::testing::Test
{
protected:
  static void SetUpTestSuite()
  {
    if (!rclcpp::ok()) {
      int argc = 0;
      rclcpp::init(argc, nullptr);
    }
  }

  static void TearDownTestSuite()
  {
    if (rclcpp::ok()) {
      rclcpp::shutdown();
    }
  }

  void start(
    FakeDeviceBridge::Behavior behavior,
    task_executor::WorkerHook hook = {},
    const std::int64_t cancel_timeout_ms = 100,
    const std::string & stage_event_log_path = "")
  {
    static std::atomic_int next_id{1};
    name_space_ = "/executor_safety_" + std::to_string(next_id++);
    bridge_ = std::make_shared<FakeDeviceBridge>(name_space_, behavior);
    rclcpp::NodeOptions options;
    options.arguments({"--ros-args", "-r", "__ns:=" + name_space_});
    options.parameter_overrides({
        rclcpp::Parameter("validation_delay_ms", 5),
        rclcpp::Parameter("ack_timeout_ms", 50),
        rclcpp::Parameter("cancel_timeout_ms", cancel_timeout_ms),
        rclcpp::Parameter("stage_event_log_path", stage_event_log_path)});
    executor_node_ = task_executor::make_task_executor_node(options, std::move(hook));
    client_node_ = std::make_shared<rclcpp::Node>("test_client", name_space_);
    task_client_ = rclcpp_action::create_client<ExecuteTask>(client_node_, "execute_task");
    event_subscription_ = client_node_->create_subscription<TaskEvent>(
      "/runtime/task_events", rclcpp::QoS(32).reliable(),
      [this](const TaskEvent & event) {
        std::lock_guard<std::mutex> lock(events_mutex_);
        events_.push_back(event);
        events_changed_.notify_all();
      });

    executor_.add_node(bridge_);
    executor_.add_node(executor_node_);
    executor_.add_node(client_node_);
    spin_thread_ = std::thread([this]() {executor_.spin();});
    ASSERT_TRUE(task_client_->wait_for_action_server(2s));
  }

  void TearDown() override
  {
    executor_.cancel();
    if (spin_thread_.joinable()) {
      spin_thread_.join();
    }
    executor_.remove_node(client_node_);
    executor_.remove_node(executor_node_);
    executor_.remove_node(bridge_);
    event_subscription_.reset();
    task_client_.reset();
    client_node_.reset();
    executor_node_.reset();
    bridge_.reset();
  }

  std::shared_ptr<TaskGoalHandle> send_task(
    const std::string & task_id, const std::int32_t duration_ms = 1000)
  {
    ExecuteTask::Goal goal;
    goal.task_id = task_id;
    goal.target_id = "dock_a";
    goal.allowed_duration.sec = duration_ms / 1000;
    goal.allowed_duration.nanosec =
      static_cast<std::uint32_t>((duration_ms % 1000) * 1000000);
    auto future = task_client_->async_send_goal(goal);
    EXPECT_EQ(future.wait_for(2s), std::future_status::ready);
    return future.get();
  }

  TaskGoalHandle::WrappedResult wait_result(
    const std::shared_ptr<TaskGoalHandle> & goal_handle,
    const std::chrono::seconds timeout = 3s)
  {
    auto future = task_client_->async_get_result(goal_handle);
    EXPECT_EQ(future.wait_for(timeout), std::future_status::ready);
    return future.get();
  }

  std::vector<TaskEvent> wait_events(
    const std::string & task_id, const std::size_t count,
    const std::chrono::milliseconds timeout)
  {
    std::unique_lock<std::mutex> lock(events_mutex_);
    events_changed_.wait_for(lock, timeout, [&]() {
        return std::count_if(events_.begin(), events_.end(), [&](const TaskEvent & event) {
                 return event.task_id == task_id;
        }) >= static_cast<std::ptrdiff_t>(count);
    });
    std::vector<TaskEvent> matches;
    std::copy_if(events_.begin(), events_.end(), std::back_inserter(matches),
      [&](const TaskEvent & event) {return event.task_id == task_id;});
    return matches;
  }

  std::vector<TaskEvent> wait_one_event_after_quiet(const std::string & task_id)
  {
    auto events = wait_events(task_id, 1, 1s);
    EXPECT_EQ(events.size(), 1U);
    std::this_thread::sleep_for(100ms);
    events = wait_events(task_id, 1, 0ms);
    EXPECT_EQ(events.size(), 1U);
    return events;
  }

  std::string name_space_;
  rclcpp::executors::MultiThreadedExecutor executor_{rclcpp::ExecutorOptions(), 4};
  std::shared_ptr<FakeDeviceBridge> bridge_;
  std::shared_ptr<rclcpp::Node> executor_node_;
  std::shared_ptr<rclcpp::Node> client_node_;
  rclcpp_action::Client<ExecuteTask>::SharedPtr task_client_;
  rclcpp::Subscription<TaskEvent>::SharedPtr event_subscription_;
  std::thread spin_thread_;
  std::mutex events_mutex_;
  std::condition_variable events_changed_;
  std::vector<TaskEvent> events_;
};

TEST_F(ExecutorSafetyTest, LateAcceptedGoalIsCanceledOnceBeforeParentTerminal)
{
  std::atomic_int cancel_attempts{0};
  start({250ms, true, false}, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kBeforeCancelGoal) {
        ++cancel_attempts;
      }
  });

  const auto goal_handle = send_task("late-accepted", 100);
  ASSERT_NE(goal_handle, nullptr);
  EXPECT_TRUE(wait_events("late-accepted", 1, 150ms).empty());

  const auto result = wait_result(goal_handle);
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(cancel_attempts, 1);
  EXPECT_EQ(bridge_->accepted_count(), 1);
  EXPECT_EQ(bridge_->cancel_count(), 1);
  const auto events = wait_one_event_after_quiet("late-accepted");
  ASSERT_EQ(events.size(), 1U);
  EXPECT_EQ(events.front().action_status, TaskEvent::ABORTED);
  EXPECT_EQ(events.front().outcome, ExecuteTask::Result::SAFE_STOP);
}

// The offline diagnoser groups stage windows by (trace_id, sequence_id) and
// requires the ordered stage chain; a silent break here would only surface as a
// useless capture much later.
TEST_F(ExecutorSafetyTest, EmitsOrderedStageEvidenceForOneTrace)
{
  const std::string path = "/tmp/executor_stage_events_" +
    std::to_string(::getpid()) + ".jsonl";
  std::remove(path.c_str());

  start({0ms, true, true}, {}, 100, path);
  const auto goal_handle = send_task("stage-evidence", 1000);
  ASSERT_NE(goal_handle, nullptr);
  const auto result = wait_result(goal_handle);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::COMPLETED);

  std::ifstream input(path);
  ASSERT_TRUE(input.is_open()) << "stage event log was not created";
  std::vector<std::string> lines;
  for (std::string line; std::getline(input, line); ) {
    if (!line.empty()) {
      lines.push_back(line);
    }
  }
  std::remove(path.c_str());
  ASSERT_FALSE(lines.empty());

  // Every line must carry the trace identity and a single sequence_id; the
  // diagnoser rejects a trace whose events disagree on either.
  std::string sequence_id;
  std::vector<std::string> stages;
  for (const auto & line : lines) {
    EXPECT_NE(line.find("\"trace_id\":\"stage-evidence\""), std::string::npos) << line;
    EXPECT_NE(line.find("\"clock_id\":\"monotonic\""), std::string::npos) << line;
    EXPECT_EQ(line.find("\"host_id\":\"unknown\""), std::string::npos) << line;
    const auto key = std::string("\"sequence_id\":");
    const auto begin = line.find(key);
    ASSERT_NE(begin, std::string::npos) << line;
    const auto value_begin = begin + key.size();
    const auto value_end = line.find(',', value_begin);
    const auto value = line.substr(value_begin, value_end - value_begin);
    if (sequence_id.empty()) {
      sequence_id = value;
    }
    EXPECT_EQ(value, sequence_id) << line;
    for (const char * stage : {
        "action_receive", "action_execute_start", "action_execute_end", "can_receive"})
    {
      if (line.find(std::string("\"stage\":\"") + stage + "\"") != std::string::npos) {
        stages.emplace_back(stage);
      }
    }
  }
  // Order matters: the diagnoser rebuilds windows from adjacent events, and its
  // w1 topology contract requires action_execute_end before can_receive.
  const std::vector<std::string> expected{
    "action_receive", "action_execute_start", "action_execute_end", "can_receive"};
  EXPECT_EQ(stages, expected);
}

TEST_F(ExecutorSafetyTest, LateRejectedGoalHasOneExplicitParentTerminal) {
  start({250ms, false, false});
  const auto goal_handle = send_task("late-rejected", 100);
  ASSERT_NE(goal_handle, nullptr);
  EXPECT_TRUE(wait_events("late-rejected", 1, 150ms).empty());

  const auto result = wait_result(goal_handle);
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  const auto events = wait_one_event_after_quiet("late-rejected");
  ASSERT_EQ(events.size(), 1U);
  EXPECT_EQ(events.front().action_status, TaskEvent::ABORTED);
}

class WorkerExceptionTest : public ExecutorSafetyTest,
  public ::testing::WithParamInterface<task_executor::WorkerPhase>
{};

TEST_P(WorkerExceptionTest, ExceptionBecomesOneSafeStopAndExecutorStaysAlive)
{
  const auto throwing_phase = GetParam();
  std::atomic_bool throw_once{true};
  start({0ms, true, true}, [&](const task_executor::WorkerPhase phase) {
      if (phase == throwing_phase && throw_once.exchange(false)) {
        throw std::runtime_error("injected worker exception");
      }
  });

  const auto failed_goal = send_task("worker-exception");
  ASSERT_NE(failed_goal, nullptr);
  const auto failed_result = wait_result(failed_goal);
  EXPECT_EQ(failed_result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(failed_result.result, nullptr);
  EXPECT_EQ(failed_result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(failed_result.result->error_code, 114);
  const auto failed_events = wait_one_event_after_quiet("worker-exception");
  ASSERT_EQ(failed_events.size(), 1U);
  EXPECT_EQ(failed_events.front().action_status, TaskEvent::ABORTED);
  EXPECT_EQ(failed_events.front().outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(failed_events.front().error_code, 114);

  const auto next_goal = send_task("worker-survived");
  ASSERT_NE(next_goal, nullptr);
  const auto next_result = wait_result(next_goal);
  EXPECT_EQ(next_result.code, rclcpp_action::ResultCode::SUCCEEDED);
  ASSERT_NE(next_result.result, nullptr);
  EXPECT_EQ(next_result.result->outcome, ExecuteTask::Result::COMPLETED);
  const auto next_events = wait_one_event_after_quiet("worker-survived");
  ASSERT_EQ(next_events.size(), 1U);
  EXPECT_EQ(next_events.front().action_status, TaskEvent::SUCCEEDED);
  // The event must name the goal it belongs to, or a product-side consumer
  // cannot bind the terminal result to the execution it started.
  EXPECT_EQ(next_events.front().goal_id, rclcpp_action::to_string(next_goal->get_goal_id()));
  EXPECT_EQ(next_events.front().request_id, "");
}

INSTANTIATE_TEST_SUITE_P(
  WorkerBoundaries, WorkerExceptionTest,
  ::testing::Values(
    task_executor::WorkerPhase::kBeforeFeedback,
    task_executor::WorkerPhase::kBeforeSendGoal,
    task_executor::WorkerPhase::kBeforeSuccess));

TEST_F(ExecutorSafetyTest, CancelAtFinalSuccessGateCannotPublishCompleted)
{
  std::mutex gate_mutex;
  std::condition_variable gate_changed;
  bool at_success_gate = false;
  bool release_success_gate = false;
  std::atomic_int cancel_attempts{0};
  start({0ms, true, true}, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kBeforeCancelGoal) {
        ++cancel_attempts;
      }
      if (phase != task_executor::WorkerPhase::kBeforeSuccess) {
        return;
      }
      std::unique_lock<std::mutex> lock(gate_mutex);
      at_success_gate = true;
      gate_changed.notify_all();
      gate_changed.wait(lock, [&]() {return release_success_gate;});
  });

  const auto goal_handle = send_task("cancel-at-success");
  ASSERT_NE(goal_handle, nullptr);
  {
    std::unique_lock<std::mutex> lock(gate_mutex);
    ASSERT_TRUE(gate_changed.wait_for(lock, 2s, [&]() {return at_success_gate;}));
  }
  const auto cancel_future = task_client_->async_cancel_goal(goal_handle);
  ASSERT_EQ(cancel_future.wait_for(1s), std::future_status::ready);
  {
    std::lock_guard<std::mutex> lock(gate_mutex);
    release_success_gate = true;
  }
  gate_changed.notify_all();

  const auto result = wait_result(goal_handle);
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 113);
  EXPECT_EQ(cancel_attempts, 1);
  const auto events = wait_one_event_after_quiet("cancel-at-success");
  ASSERT_EQ(events.size(), 1U);
  EXPECT_EQ(events.front().action_status, TaskEvent::ABORTED);
  EXPECT_EQ(events.front().outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(events.front().error_code, 113);
}

TEST_F(ExecutorSafetyTest, ExceptionAfterChildAcceptedCancelsAndWaitsForTerminal)
{
  std::atomic_bool throw_once{true};
  start({0ms, true, false, true}, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kAfterChildAccepted && throw_once.exchange(false)) {
        throw std::runtime_error("post-accept exception");
      }
  });

  const auto goal_handle = send_task("post-accept-exception");
  ASSERT_NE(goal_handle, nullptr);
  auto result_future = task_client_->async_get_result(goal_handle);
  ASSERT_TRUE(bridge_->wait_for_cancel());
  EXPECT_TRUE(wait_events("post-accept-exception", 1, 150ms).empty());
  EXPECT_EQ(result_future.wait_for(0ms), std::future_status::timeout);
  EXPECT_EQ(bridge_->cancel_count(), 1);

  bridge_->release_terminal();
  ASSERT_TRUE(bridge_->wait_for_terminal());
  ASSERT_EQ(result_future.wait_for(2s), std::future_status::ready);
  const auto result = result_future.get();
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 114);
  const auto events = wait_one_event_after_quiet("post-accept-exception");
  ASSERT_EQ(events.size(), 1U);
  EXPECT_EQ(events.front().action_status, TaskEvent::ABORTED);
  EXPECT_EQ(events.front().error_code, 114);
}

TEST_F(ExecutorSafetyTest, ExceptionAfterSendWaitsForLateAcceptedChildTerminal)
{
  std::atomic_bool throw_once{true};
  std::atomic_int cancel_attempts{0};
  start({250ms, true, false, true}, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kBeforeCancelGoal) {
        ++cancel_attempts;
      }
      if (phase == task_executor::WorkerPhase::kAfterSendGoal && throw_once.exchange(false)) {
        throw std::runtime_error("post-send exception");
      }
  });

  const auto goal_handle = send_task("post-send-exception", 2000);
  ASSERT_NE(goal_handle, nullptr);
  auto result_future = task_client_->async_get_result(goal_handle);
  EXPECT_TRUE(wait_events("post-send-exception", 1, 150ms).empty());
  EXPECT_EQ(result_future.wait_for(0ms), std::future_status::timeout);
  ASSERT_TRUE(bridge_->wait_for_cancel());
  EXPECT_EQ(cancel_attempts, 1);
  EXPECT_EQ(bridge_->cancel_count(), 1);
  EXPECT_TRUE(wait_events("post-send-exception", 1, 100ms).empty());

  bridge_->release_terminal();
  ASSERT_TRUE(bridge_->wait_for_terminal());
  ASSERT_EQ(result_future.wait_for(2s), std::future_status::ready);
  const auto result = result_future.get();
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 114);
  const auto events = wait_one_event_after_quiet("post-send-exception");
  ASSERT_EQ(events.size(), 1U);
  EXPECT_EQ(events.front().action_status, TaskEvent::ABORTED);
  EXPECT_EQ(events.front().error_code, 114);
}

TEST_F(ExecutorSafetyTest, ParentCancelWhileGoalResponsePendingCancelsLateAcceptedChildOnce)
{
  std::atomic_int cancel_attempts{0};
  std::promise<void> send_started;
  start({250ms, true, false}, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kAfterSendGoal) {
        send_started.set_value();
      }
      if (phase == task_executor::WorkerPhase::kBeforeCancelGoal) {
        ++cancel_attempts;
      }
  });

  const auto goal_handle = send_task("pending-parent-cancel", 2000);
  ASSERT_NE(goal_handle, nullptr);
  ASSERT_EQ(send_started.get_future().wait_for(1s), std::future_status::ready);
  const auto cancel_future = task_client_->async_cancel_goal(goal_handle);
  ASSERT_EQ(cancel_future.wait_for(1s), std::future_status::ready);
  EXPECT_TRUE(wait_events("pending-parent-cancel", 1, 150ms).empty());

  const auto result = wait_result(goal_handle);
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::CANCELED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::CANCELED);
  EXPECT_EQ(cancel_attempts, 1);
  EXPECT_EQ(bridge_->cancel_count(), 1);
  const auto events = wait_one_event_after_quiet("pending-parent-cancel");
  ASSERT_EQ(events.size(), 1U);
  EXPECT_EQ(events.front().action_status, TaskEvent::CANCELED);
}

TEST_F(ExecutorSafetyTest, ShutdownWaitsForLateAcceptedChildTerminal)
{
  std::promise<void> send_started;
  std::atomic_int cancel_attempts{0};
  start({250ms, true, false, true}, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kAfterSendGoal) {
        send_started.set_value();
      }
      if (phase == task_executor::WorkerPhase::kBeforeCancelGoal) {
        ++cancel_attempts;
      }
  });

  const auto goal_handle = send_task("shutdown-pending", 2000);
  ASSERT_NE(goal_handle, nullptr);
  ASSERT_EQ(send_started.get_future().wait_for(1s), std::future_status::ready);
  task_executor::request_task_executor_shutdown(executor_node_);
  auto shutdown = std::async(std::launch::async, [this]() {
        task_executor::wait_for_task_executor_shutdown(executor_node_);
  });
  EXPECT_TRUE(wait_events("shutdown-pending", 1, 150ms).empty());
  EXPECT_EQ(shutdown.wait_for(0ms), std::future_status::timeout);
  ASSERT_TRUE(bridge_->wait_for_cancel());
  EXPECT_EQ(cancel_attempts, 1);
  EXPECT_EQ(bridge_->cancel_count(), 1);

  bridge_->release_terminal();
  ASSERT_TRUE(bridge_->wait_for_terminal());
  const auto result = wait_result(goal_handle);
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 111);
  ASSERT_EQ(shutdown.wait_for(2s), std::future_status::ready);
  shutdown.get();
  EXPECT_EQ(send_task("shutdown-pending-second"), nullptr);
}

TEST_F(ExecutorSafetyTest, ShutdownWaitsForAcceptedChildTerminal)
{
  std::promise<void> child_accepted;
  start({0ms, true, false, true}, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kAfterChildAccepted) {
        child_accepted.set_value();
      }
  });

  const auto goal_handle = send_task("shutdown-accepted", 2000);
  ASSERT_NE(goal_handle, nullptr);
  ASSERT_EQ(child_accepted.get_future().wait_for(1s), std::future_status::ready);
  task_executor::request_task_executor_shutdown(executor_node_);
  auto shutdown = std::async(std::launch::async, [this]() {
        task_executor::wait_for_task_executor_shutdown(executor_node_);
  });
  ASSERT_TRUE(bridge_->wait_for_cancel());
  EXPECT_EQ(shutdown.wait_for(150ms), std::future_status::timeout);
  EXPECT_TRUE(wait_events("shutdown-accepted", 1, 0ms).empty());

  bridge_->release_terminal();
  ASSERT_TRUE(bridge_->wait_for_terminal());
  const auto result = wait_result(goal_handle);
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 111);
  ASSERT_EQ(shutdown.wait_for(2s), std::future_status::ready);
  shutdown.get();
}

TEST_F(ExecutorSafetyTest, CancelTimeoutStillDrainsChildAndReportsUnconfirmedStop)
{
  std::promise<void> send_started;
  start({0ms, true, false, true}, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kAfterSendGoal) {
        send_started.set_value();
      }
  }, 50);

  const auto goal_handle = send_task("slow-cancel", 2000);
  ASSERT_NE(goal_handle, nullptr);
  ASSERT_EQ(send_started.get_future().wait_for(1s), std::future_status::ready);
  auto result_future = task_client_->async_get_result(goal_handle);
  const auto cancel_future = task_client_->async_cancel_goal(goal_handle);
  ASSERT_EQ(cancel_future.wait_for(1s), std::future_status::ready);
  ASSERT_TRUE(bridge_->wait_for_cancel());
  std::this_thread::sleep_for(100ms);
  EXPECT_TRUE(wait_events("slow-cancel", 1, 50ms).empty());
  EXPECT_EQ(result_future.wait_for(0ms), std::future_status::timeout);

  bridge_->release_terminal();
  ASSERT_TRUE(bridge_->wait_for_terminal());
  ASSERT_EQ(result_future.wait_for(2s), std::future_status::ready);
  const auto result = result_future.get();
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 113);
  const auto events = wait_one_event_after_quiet("slow-cancel");
  ASSERT_EQ(events.size(), 1U);
  EXPECT_EQ(events.front().action_status, TaskEvent::ABORTED);
  EXPECT_EQ(events.front().error_code, 113);
}

TEST_F(ExecutorSafetyTest, CancelDispatchExceptionIsRetriedBeforeParentTerminal)
{
  std::atomic_int cancel_attempts{0};
  std::promise<void> child_accepted;
  start({0ms, true, false, true}, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kAfterChildAccepted) {
        child_accepted.set_value();
      }
      if (phase == task_executor::WorkerPhase::kBeforeCancelGoal &&
      ++cancel_attempts == 1)
      {
        throw std::runtime_error("injected cancel dispatch exception");
      }
  }, 1000);

  const auto goal_handle = send_task("cancel-dispatch-retry", 2000);
  ASSERT_NE(goal_handle, nullptr);
  ASSERT_EQ(child_accepted.get_future().wait_for(1s), std::future_status::ready);
  auto result_future = task_client_->async_get_result(goal_handle);
  const auto cancel_future = task_client_->async_cancel_goal(goal_handle);
  ASSERT_EQ(cancel_future.wait_for(1s), std::future_status::ready);
  ASSERT_TRUE(bridge_->wait_for_cancel());
  EXPECT_EQ(cancel_attempts, 2);
  EXPECT_EQ(bridge_->cancel_count(), 1);
  EXPECT_TRUE(wait_events("cancel-dispatch-retry", 1, 100ms).empty());
  EXPECT_EQ(result_future.wait_for(0ms), std::future_status::timeout);

  bridge_->release_terminal();
  ASSERT_TRUE(bridge_->wait_for_terminal());
  ASSERT_EQ(result_future.wait_for(2s), std::future_status::ready);
  const auto result = result_future.get();
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::CANCELED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::CANCELED);
  const auto events = wait_one_event_after_quiet("cancel-dispatch-retry");
  ASSERT_EQ(events.size(), 1U);
  EXPECT_EQ(events.front().action_status, TaskEvent::CANCELED);
}

TEST_F(ExecutorSafetyTest, TransitionExceptionFallsBackToOneSafeStop)
{
  std::atomic_bool throw_once{true};
  start({0ms, true, true}, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kBeforeParentTransition &&
      throw_once.exchange(false))
      {
        throw std::runtime_error("transition exception");
      }
  });

  const auto goal_handle = send_task("transition-fallback");
  ASSERT_NE(goal_handle, nullptr);
  const auto result = wait_result(goal_handle);
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 114);
  const auto events = wait_one_event_after_quiet("transition-fallback");
  ASSERT_EQ(events.size(), 1U);
  EXPECT_EQ(events.front().action_status, TaskEvent::ABORTED);
  EXPECT_EQ(events.front().error_code, 114);
}

TEST_F(ExecutorSafetyTest, FailedPrimaryAndFallbackTransitionsPublishNoTaskEvent)
{
  start({0ms, true, true}, [](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kBeforeParentTransition) {
        throw std::runtime_error("transition exception");
      }
  });

  const auto goal_handle = send_task("transition-double-failure");
  ASSERT_NE(goal_handle, nullptr);
  EXPECT_TRUE(wait_events("transition-double-failure", 1, 300ms).empty());
  std::this_thread::sleep_for(100ms);
  EXPECT_TRUE(wait_events("transition-double-failure", 1, 0ms).empty());

  const auto second_goal = send_task("transition-double-failure-second");
  EXPECT_EQ(second_goal, nullptr);
}

TEST(ExecutorRuntimeTest, SpinFailureRecoversAndDrainsDelayedGoal)
{
  if (!rclcpp::ok()) {
    int argc = 0;
    rclcpp::init(argc, nullptr);
  }
  const std::string name_space = "/executor_spin_recovery";
  std::atomic_bool dispatch_started{false};
  auto bridge = std::make_shared<FakeDeviceBridge>(
    name_space, FakeDeviceBridge::Behavior{250ms, true, false, true});
  rclcpp::NodeOptions options;
  options.arguments({"--ros-args", "-r", "__ns:=" + name_space});
  options.parameter_overrides({
      rclcpp::Parameter("validation_delay_ms", 5),
      rclcpp::Parameter("ack_timeout_ms", 50),
      rclcpp::Parameter("cancel_timeout_ms", 100)});
  auto executor_node = task_executor::make_task_executor_node(
    options, [&](const task_executor::WorkerPhase phase) {
      if (phase == task_executor::WorkerPhase::kAfterSendGoal) {
        dispatch_started = true;
      }
    });
  auto client_node = std::make_shared<rclcpp::Node>("recovery_client", name_space);
  auto client = rclcpp_action::create_client<ExecuteTask>(client_node, "execute_task");

  rclcpp::executors::MultiThreadedExecutor external_executor(
    rclcpp::ExecutorOptions(), 3);
  external_executor.add_node(bridge);
  external_executor.add_node(client_node);
  std::thread external_spin([&]() {external_executor.spin();});

  std::atomic_int runtime_result{-1};
  std::thread runtime([&]() {
      runtime_result = task_executor::run_task_executor_node(
        executor_node, [&](rclcpp::executors::MultiThreadedExecutor & executor) {
          while (true) {
            executor.spin_some(5ms);
            if (dispatch_started) {
              task_executor::request_task_executor_process_shutdown(0);
              throw std::runtime_error("injected primary spin failure");
            }
          }
        });
    });

  ASSERT_TRUE(client->wait_for_action_server(2s));
  ExecuteTask::Goal goal;
  goal.task_id = "spin-recovery";
  goal.target_id = "dock_a";
  goal.allowed_duration.sec = 2;
  auto goal_future = client->async_send_goal(goal);
  ASSERT_EQ(goal_future.wait_for(2s), std::future_status::ready);
  const auto handle = goal_future.get();
  ASSERT_NE(handle, nullptr);
  auto result_future = client->async_get_result(handle);
  ASSERT_TRUE(bridge->wait_for_cancel(3s));
  std::this_thread::sleep_for(150ms);
  EXPECT_EQ(runtime_result, -1);
  EXPECT_EQ(bridge->cancel_count(), 1);

  bridge->release_terminal();
  ASSERT_TRUE(bridge->wait_for_terminal());
  ASSERT_EQ(result_future.wait_for(2s), std::future_status::ready);
  const auto result = result_future.get();
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteTask::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 111);
  runtime.join();
  EXPECT_EQ(runtime_result, 1);

  external_executor.cancel();
  external_spin.join();
  external_executor.remove_node(client_node);
  external_executor.remove_node(bridge);
  client.reset();
  client_node.reset();
  bridge.reset();
  executor_node.reset();
  rclcpp::shutdown();
}

TEST(ExecutorRuntimeTest, RecoveryFailureWithActiveDelayedGoalReturnsFatal)
{
  testing::FLAGS_gtest_death_test_style = "threadsafe";
  ASSERT_EXIT(
    {
      if (!rclcpp::ok()) {
        int argc = 0;
        rclcpp::init(argc, nullptr);
      }
      const std::string name_space = "/executor_recovery_failure";
      std::atomic_bool dispatch_started{false};
      auto bridge = std::make_shared<FakeDeviceBridge>(
        name_space, FakeDeviceBridge::Behavior{250ms, true, false, true});
      rclcpp::NodeOptions options;
      options.arguments({"--ros-args", "-r", "__ns:=" + name_space});
      options.parameter_overrides({rclcpp::Parameter("validation_delay_ms", 5)});
      auto executor_node = task_executor::make_task_executor_node(
        options, [&](const task_executor::WorkerPhase phase) {
          if (phase == task_executor::WorkerPhase::kAfterSendGoal) {
            dispatch_started = true;
          }
        });
      auto client_node = std::make_shared<rclcpp::Node>("fatal_client", name_space);
      auto client = rclcpp_action::create_client<ExecuteTask>(client_node, "execute_task");
      rclcpp::executors::MultiThreadedExecutor external_executor(
        rclcpp::ExecutorOptions(), 3);
      external_executor.add_node(bridge);
      external_executor.add_node(client_node);
      std::thread external_spin([&]() {external_executor.spin();});

      std::atomic_int runtime_result{-1};
      std::thread runtime([&]() {
        runtime_result = task_executor::run_task_executor_node(
          executor_node,
          [&](rclcpp::executors::MultiThreadedExecutor & executor) {
            while (true) {
              executor.spin_some(5ms);
              if (dispatch_started) {
                throw std::runtime_error("injected primary spin failure");
              }
            }
          },
          [](rclcpp::executors::MultiThreadedExecutor &) {
            throw std::runtime_error("injected recovery spin failure");
          });
      });

      if (!client->wait_for_action_server(2s)) {
        std::_Exit(90);
      }
      ExecuteTask::Goal goal;
      goal.task_id = "recovery-failure";
      goal.target_id = "dock_a";
      goal.allowed_duration.sec = 2;
      auto goal_future = client->async_send_goal(goal);
      if (goal_future.wait_for(2s) != std::future_status::ready || !goal_future.get()) {
        std::_Exit(91);
      }
      runtime.join();
      std::_Exit(runtime_result);
    },
    ::testing::ExitedWithCode(3), "");
}

}  // namespace
