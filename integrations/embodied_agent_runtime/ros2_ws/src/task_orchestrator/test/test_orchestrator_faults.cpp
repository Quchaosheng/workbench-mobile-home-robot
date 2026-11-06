#include "task_orchestrator_node.hpp"

#include "robot_task_interfaces/action/execute_task.hpp"
#include "robot_task_interfaces/action/execute_workflow.hpp"
#include "std_msgs/msg/bool.hpp"

#include <gtest/gtest.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"

namespace
{
using namespace std::chrono_literals;
using ExecuteTask = robot_task_interfaces::action::ExecuteTask;
using ExecuteWorkflow = robot_task_interfaces::action::ExecuteWorkflow;
using TaskServerHandle = rclcpp_action::ServerGoalHandle<ExecuteTask>;
using WorkflowClientHandle = rclcpp_action::ClientGoalHandle<ExecuteWorkflow>;

class FakeTaskServer : public rclcpp::Node
{
public:
  FakeTaskServer(
    const std::string & name_space, const bool hold,
    std::function<void()> before_goal_response = {})
  : Node("fake_task_server", name_space), hold_(hold),
    before_goal_response_(std::move(before_goal_response))
  {
    server_ = rclcpp_action::create_server<ExecuteTask>(
      this, "execute_task",
      [this](const rclcpp_action::GoalUUID &, const std::shared_ptr<const ExecuteTask::Goal>) {
        if (before_goal_response_) {
          before_goal_response_();
        }
        return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
      },
      [this](const std::shared_ptr<TaskServerHandle>) {
        ++cancel_requests_;
        return rclcpp_action::CancelResponse::ACCEPT;
      },
      [this](const std::shared_ptr<TaskServerHandle> handle) {
        std::lock_guard<std::mutex> lock(mutex_);
        workers_.emplace_back([this, handle]() {
          auto result = std::make_shared<ExecuteTask::Result>();
          if (!hold_) {
            result->outcome = ExecuteTask::Result::COMPLETED;
            handle->succeed(result);
            return;
          }
          while (rclcpp::ok() && !handle->is_canceling()) {
            std::this_thread::sleep_for(2ms);
          }
          result->outcome = ExecuteTask::Result::CANCELED;
          handle->canceled(result);
          });
      });
  }

  ~FakeTaskServer() override
  {
    std::vector<std::thread> workers;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      workers.swap(workers_);
    }
    for (auto & worker : workers) {
      if (worker.joinable()) {
        worker.join();
      }
    }
  }

  int cancel_requests() const {return cancel_requests_;}

  bool wait_for_cancel(const std::chrono::milliseconds timeout = 2s) const
  {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (std::chrono::steady_clock::now() < deadline) {
      if (cancel_requests_ > 0) {
        return true;
      }
      std::this_thread::sleep_for(2ms);
    }
    return cancel_requests_ > 0;
  }

private:
  bool hold_;
  std::function<void()> before_goal_response_;
  std::atomic_int cancel_requests_{0};
  std::mutex mutex_;
  std::vector<std::thread> workers_;
  rclcpp_action::Server<ExecuteTask>::SharedPtr server_;
};

class OrchestratorFaultTest : public ::testing::Test
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

  void start(const bool hold, task_orchestrator::FaultHook hook)
  {
    static std::atomic_int next_id{1};
    name_space_ = "/orchestrator_fault_" + std::to_string(next_id++);
    server_ = std::make_shared<FakeTaskServer>(name_space_, hold);
    rclcpp::NodeOptions options;
    options.arguments({"--ros-args", "-r", "__ns:=" + name_space_});
    options.parameter_overrides({rclcpp::Parameter("cancel_timeout_ms", 50)});
    orchestrator_ = task_orchestrator::make_task_orchestrator_node(options, std::move(hook));
    client_node_ = std::make_shared<rclcpp::Node>("fault_test_client", name_space_);
    client_ = rclcpp_action::create_client<ExecuteWorkflow>(client_node_, "execute_workflow");
    ready_ = client_node_->create_publisher<std_msgs::msg::Bool>("/runtime/ready", 10);
    executor_.add_node(server_);
    executor_.add_node(orchestrator_);
    executor_.add_node(client_node_);
    spin_thread_ = std::thread([this]() {executor_.spin();});
    ASSERT_TRUE(client_->wait_for_action_server(2s));
    std_msgs::msg::Bool message;
    message.data = true;
    for (int count = 0; count < 5; ++count) {
      ready_->publish(message);
      std::this_thread::sleep_for(10ms);
    }
  }

  void TearDown() override
  {
    executor_.cancel();
    if (spin_thread_.joinable()) {
      spin_thread_.join();
    }
    executor_.remove_node(client_node_);
    executor_.remove_node(orchestrator_);
    executor_.remove_node(server_);
    ready_.reset();
    client_.reset();
    client_node_.reset();
    orchestrator_.reset();
    server_.reset();
  }

  WorkflowClientHandle::WrappedResult run(const std::string & task_id, const int duration_ms)
  {
    return wait_result(send(task_id, duration_ms));
  }

  std::shared_ptr<WorkflowClientHandle> send(
    const std::string & task_id, const int duration_ms)
  {
    ExecuteWorkflow::Goal goal;
    goal.request_id = "request-" + task_id;
    goal.workflow_id = "single_task";
    goal.task_id = task_id;
    goal.target_id = "dock_a";
    goal.allowed_duration.sec = duration_ms / 1000;
    goal.allowed_duration.nanosec = (duration_ms % 1000) * 1000000U;
    auto goal_future = client_->async_send_goal(goal);
    EXPECT_EQ(goal_future.wait_for(2s), std::future_status::ready);
    const auto handle = goal_future.get();
    EXPECT_NE(handle, nullptr);
    return handle;
  }

  WorkflowClientHandle::WrappedResult wait_result(
    const std::shared_ptr<WorkflowClientHandle> & handle)
  {
    auto result_future = client_->async_get_result(handle);
    EXPECT_EQ(result_future.wait_for(3s), std::future_status::ready);
    return result_future.get();
  }

  std::string name_space_;
  rclcpp::executors::MultiThreadedExecutor executor_{rclcpp::ExecutorOptions(), 4};
  std::shared_ptr<FakeTaskServer> server_;
  std::shared_ptr<rclcpp::Node> orchestrator_;
  std::shared_ptr<rclcpp::Node> client_node_;
  rclcpp_action::Client<ExecuteWorkflow>::SharedPtr client_;
  rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr ready_;
  std::thread spin_thread_;
};

TEST_F(OrchestratorFaultTest, CancelDispatchExceptionRetriesWithoutDuplicateRpc)
{
  std::atomic_int attempts{0};
  start(true, [&](const task_orchestrator::FaultPoint point) {
      if (point == task_orchestrator::FaultPoint::kBeforeCancelDispatch && attempts++ == 0) {
        throw std::runtime_error("injected cancel dispatch failure");
      }
    });

  const auto result = run("cancel-retry", 100);
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteWorkflow::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 209);
  EXPECT_EQ(attempts, 2);
  EXPECT_EQ(server_->cancel_requests(), 1);
}

TEST_F(OrchestratorFaultTest, TerminalExceptionUsesOneFallbackAndReleasesBusyGate)
{
  std::atomic_bool fail_primary{true};
  std::atomic_int fallbacks{0};
  start(false, [&](const task_orchestrator::FaultPoint point) {
      if (point == task_orchestrator::FaultPoint::kBeforeTerminalTransition &&
      fail_primary.exchange(false))
      {
        throw std::runtime_error("injected terminal failure");
      }
      if (point == task_orchestrator::FaultPoint::kBeforeFallbackAbort) {
        ++fallbacks;
      }
    });

  const auto failed = run("terminal-fallback", 1000);
  EXPECT_EQ(failed.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(failed.result, nullptr);
  EXPECT_EQ(failed.result->outcome, ExecuteWorkflow::Result::SAFE_STOP);
  EXPECT_EQ(failed.result->error_code, 208);
  EXPECT_EQ(fallbacks, 1);

  const auto next = run("terminal-next", 1000);
  EXPECT_EQ(next.code, rclcpp_action::ResultCode::SUCCEEDED);
}

TEST_F(OrchestratorFaultTest, CancelAfterTerminalCommitIsRejected)
{
  std::mutex gate_mutex;
  std::condition_variable gate_changed;
  bool terminal_reserved = false;
  bool release_terminal = false;
  start(false, [&](const task_orchestrator::FaultPoint point) {
      if (point != task_orchestrator::FaultPoint::kBeforeTerminalTransition) {
        return;
      }
      std::unique_lock<std::mutex> lock(gate_mutex);
      terminal_reserved = true;
      gate_changed.notify_all();
      gate_changed.wait(lock, [&]() {return release_terminal;});
    });

  const auto handle = send("terminal-wins", 1000);
  {
    std::unique_lock<std::mutex> lock(gate_mutex);
    ASSERT_TRUE(gate_changed.wait_for(lock, 2s, [&]() {return terminal_reserved;}));
  }
  auto cancel_future = client_->async_cancel_goal(handle);
  ASSERT_EQ(cancel_future.wait_for(1s), std::future_status::ready);
  EXPECT_TRUE(cancel_future.get()->goals_canceling.empty());
  {
    std::lock_guard<std::mutex> lock(gate_mutex);
    release_terminal = true;
  }
  gate_changed.notify_all();

  const auto result = wait_result(handle);
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::SUCCEEDED);
}

TEST_F(OrchestratorFaultTest, CancelBeforeTerminalCommitPreventsSuccess)
{
  std::mutex gate_mutex;
  std::condition_variable gate_changed;
  bool before_terminal_commit = false;
  bool release_terminal = false;
  start(false, [&](const task_orchestrator::FaultPoint point) {
      if (point != task_orchestrator::FaultPoint::kBeforeTerminalCommit) {
        return;
      }
      std::unique_lock<std::mutex> lock(gate_mutex);
      before_terminal_commit = true;
      gate_changed.notify_all();
      gate_changed.wait(lock, [&]() {return release_terminal;});
    });

  const auto handle = send("cancel-wins", 1000);
  {
    std::unique_lock<std::mutex> lock(gate_mutex);
    ASSERT_TRUE(gate_changed.wait_for(lock, 2s, [&]() {return before_terminal_commit;}));
  }
  auto cancel_future = client_->async_cancel_goal(handle);
  ASSERT_EQ(cancel_future.wait_for(1s), std::future_status::ready);
  EXPECT_EQ(cancel_future.get()->goals_canceling.size(), 1U);
  {
    std::lock_guard<std::mutex> lock(gate_mutex);
    release_terminal = true;
  }
  gate_changed.notify_all();

  const auto result = wait_result(handle);
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteWorkflow::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 208);
  EXPECT_EQ(server_->cancel_requests(), 0);
}

class OrchestratorRuntimeTest : public ::testing::TestWithParam<bool>
{};

TEST_P(OrchestratorRuntimeTest, SignalDrainSupervisesPrimaryAndRecoveryExecutors)
{
  if (!rclcpp::ok()) {
    int argc = 0;
    rclcpp::init(argc, nullptr);
  }
  const std::string name_space = "/orchestrator_spin_recovery";
  std::mutex response_mutex;
  std::condition_variable response_changed;
  bool response_entered = false;
  bool release_response = false;
  auto server = std::make_shared<FakeTaskServer>(
    name_space, true, [&]() {
      std::unique_lock<std::mutex> lock(response_mutex);
      response_entered = true;
      response_changed.notify_all();
      response_changed.wait(lock, [&]() {return release_response;});
    });
  rclcpp::NodeOptions options;
  options.arguments({"--ros-args", "-r", "__ns:=" + name_space});
  options.parameter_overrides({rclcpp::Parameter("cancel_timeout_ms", 50)});
  auto orchestrator = task_orchestrator::make_task_orchestrator_node(options);
  auto client_node = std::make_shared<rclcpp::Node>("recovery_client", name_space);
  auto client = rclcpp_action::create_client<ExecuteWorkflow>(client_node, "execute_workflow");
  auto ready = client_node->create_publisher<std_msgs::msg::Bool>("/runtime/ready", 10);

  rclcpp::executors::MultiThreadedExecutor external_executor(
    rclcpp::ExecutorOptions(), 3);
  external_executor.add_node(server);
  external_executor.add_node(client_node);
  std::thread external_spin([&]() {external_executor.spin();});

  std::atomic_int runtime_result{-1};
  std::atomic_bool stop_requested{false};
  const bool fail_recovery = GetParam();
  std::thread runtime([&]() {
      try {
        runtime_result = task_orchestrator::run_task_orchestrator_node(
          orchestrator, [&](rclcpp::executors::MultiThreadedExecutor & executor) {
            while (true) {
              executor.spin_some(5ms);
              std::lock_guard<std::mutex> lock(response_mutex);
              if (response_entered) {
                stop_requested = true;
                throw std::runtime_error("injected primary spin failure");
              }
            }
          },
          fail_recovery ? task_orchestrator::SpinFunction(
            [](rclcpp::executors::MultiThreadedExecutor &) {
              throw std::runtime_error("injected recovery spin failure");
            }) : task_orchestrator::SpinFunction{},
          [&]() {return stop_requested.load();});
      } catch (const std::runtime_error &) {
        runtime_result = 1;
      }
    });

  ASSERT_TRUE(client->wait_for_action_server(2s));
  std_msgs::msg::Bool ready_message;
  ready_message.data = true;
  for (int count = 0; count < 5; ++count) {
    ready->publish(ready_message);
    std::this_thread::sleep_for(10ms);
  }
  ExecuteWorkflow::Goal goal;
  goal.request_id = "request-spin-recovery";
  goal.workflow_id = "single_task";
  goal.task_id = "spin-recovery";
  goal.target_id = "dock_a";
  goal.allowed_duration.sec = 5;
  auto goal_future = client->async_send_goal(goal);
  ASSERT_EQ(goal_future.wait_for(2s), std::future_status::ready);
  const auto handle = goal_future.get();
  ASSERT_NE(handle, nullptr);
  auto result_future = client->async_get_result(handle);
  {
    std::unique_lock<std::mutex> lock(response_mutex);
    ASSERT_TRUE(response_changed.wait_for(lock, 2s, [&]() {return response_entered;}));
  }

  std::unique_ptr<rclcpp::executors::MultiThreadedExecutor> rescue_executor;
  std::thread rescue_spin;
  if (fail_recovery) {
    runtime.join();
    EXPECT_EQ(runtime_result, 2);
    rescue_executor = std::make_unique<rclcpp::executors::MultiThreadedExecutor>();
    rescue_executor->add_node(orchestrator);
    rescue_spin = std::thread([&]() {rescue_executor->spin();});
  }
  {
    std::lock_guard<std::mutex> lock(response_mutex);
    release_response = true;
  }
  response_changed.notify_all();

  ASSERT_EQ(result_future.wait_for(3s), std::future_status::ready);
  const auto result = result_future.get();
  EXPECT_EQ(result.code, rclcpp_action::ResultCode::ABORTED);
  ASSERT_NE(result.result, nullptr);
  EXPECT_EQ(result.result->outcome, ExecuteWorkflow::Result::SAFE_STOP);
  EXPECT_EQ(result.result->error_code, 205);
  ASSERT_TRUE(server->wait_for_cancel());
  EXPECT_EQ(server->cancel_requests(), 1);
  if (fail_recovery) {
    rescue_executor->cancel();
    rescue_spin.join();
    rescue_executor->remove_node(orchestrator);
  } else {
    runtime.join();
    EXPECT_EQ(runtime_result, 1);
  }

  external_executor.cancel();
  external_spin.join();
  external_executor.remove_node(client_node);
  external_executor.remove_node(server);
  ready.reset();
  client.reset();
  client_node.reset();
  orchestrator.reset();
  server.reset();
  rclcpp::shutdown();
}

INSTANTIATE_TEST_SUITE_P(
  ExecutorSupervision, OrchestratorRuntimeTest, ::testing::Values(false, true));

}  // namespace
