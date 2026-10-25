#include "runtime_gateway/gateway_service.hpp"
#include "runtime_gateway/loopback_server.hpp"

#include "runtime_history/store.hpp"

#include "robot_task_interfaces/action/execute_workflow.hpp"

#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <future>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <grpcpp/grpcpp.h>
#include <gtest/gtest.h>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp_action/rclcpp_action.hpp>

namespace
{

using namespace std::chrono_literals;
using ExecuteWorkflow = robot_task_interfaces::action::ExecuteWorkflow;
using ServerGoalHandle = rclcpp_action::ServerGoalHandle<ExecuteWorkflow>;

class FakeOrchestrator : public rclcpp::Node
{
public:
  explicit FakeOrchestrator(std::chrono::milliseconds goal_delay = 0ms)
  : Node("fake_orchestrator"), goal_delay_(goal_delay)
  {
    server_ = rclcpp_action::create_server<ExecuteWorkflow>(
      this, "execute_workflow",
      [this](const rclcpp_action::GoalUUID &, const std::shared_ptr<const ExecuteWorkflow::Goal>) {
        std::this_thread::sleep_for(goal_delay_);
        return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
      },
      [this](const std::shared_ptr<ServerGoalHandle> handle) {
        {
          const std::lock_guard<std::mutex> lock(mutex_);
          canceled_requests_.push_back(handle->get_goal()->request_id);
        }
        changed_.notify_all();
        return rclcpp_action::CancelResponse::ACCEPT;
      },
      [this](const std::shared_ptr<ServerGoalHandle> handle) {
        {
          const std::lock_guard<std::mutex> lock(mutex_);
          handles_.push_back(handle);
        }
        changed_.notify_all();
      });
  }

  bool wait_for_goals(const std::size_t count)
  {
    std::unique_lock<std::mutex> lock(mutex_);
    return changed_.wait_for(lock, 2s, [this, count]() {return handles_.size() >= count;});
  }

  bool wait_for_cancel(
    const std::string & request_id, const std::chrono::milliseconds timeout = 2s)
  {
    std::unique_lock<std::mutex> lock(mutex_);
    return changed_.wait_for(
      lock, timeout, [this, &request_id]() {
        for (const auto & value : canceled_requests_) {
          if (value == request_id) {
            return true;
          }
        }
        return false;
      });
  }

  std::size_t goal_count() const
  {
    const std::lock_guard<std::mutex> lock(mutex_);
    return handles_.size();
  }

  std::size_t cancel_count() const
  {
    const std::lock_guard<std::mutex> lock(mutex_);
    return canceled_requests_.size();
  }

  void complete(const std::string & request_id)
  {
    finish(request_id, ExecuteWorkflow::Result::COMPLETED);
  }

  void finish(const std::string & request_id, const std::uint8_t outcome)
  {
    std::shared_ptr<ServerGoalHandle> handle;
    {
      const std::lock_guard<std::mutex> lock(mutex_);
      for (const auto & value : handles_) {
        if (value->get_goal()->request_id == request_id) {
          handle = value;
        }
      }
    }
    ASSERT_TRUE(handle);
    for (int attempt = 0;
      outcome == ExecuteWorkflow::Result::CANCELED && !handle->is_canceling() && attempt < 200;
      ++attempt)
    {
      std::this_thread::sleep_for(5ms);
    }
    auto result = std::make_shared<ExecuteWorkflow::Result>();
    result->outcome = outcome;
    result->message = outcome == ExecuteWorkflow::Result::CANCELED ? "canceled" : "done";
    if (outcome == ExecuteWorkflow::Result::COMPLETED) {
      handle->succeed(result);
    } else if (outcome == ExecuteWorkflow::Result::CANCELED) {
      handle->canceled(result);
    } else {
      handle->abort(result);
    }
  }

private:
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  std::vector<std::shared_ptr<ServerGoalHandle>> handles_;
  std::vector<std::string> canceled_requests_;
  rclcpp_action::Server<ExecuteWorkflow>::SharedPtr server_;
  std::chrono::milliseconds goal_delay_;
};

class GrpcActionBridgeTest : public testing::Test
{
protected:
  static void SetUpTestSuite()
  {
    int argc = 1;
    char name[] = "test_grpc_action_bridge";
    char * argv[] = {name, nullptr};
    rclcpp::init(argc, argv);
  }

  static void TearDownTestSuite() {rclcpp::shutdown();}

  void SetUp() override
  {
    path_ = (std::filesystem::temp_directory_path() /
      ("runtime_gateway_action_" + std::to_string(counter_++) + ".sqlite3")).string();
    std::filesystem::remove(path_);
    std::filesystem::remove(path_ + "-wal");
    std::filesystem::remove(path_ + "-shm");
    store_ = std::make_unique<runtime_history::Store>(path_);
    gateway_node_ = std::make_shared<rclcpp::Node>("gateway_action_test");
    orchestrator_ = std::make_shared<FakeOrchestrator>(goal_delay());
    executor_.add_node(gateway_node_);
    executor_.add_node(orchestrator_);
    spin_thread_ = std::thread([this]() {executor_.spin();});
    service_ = std::make_unique<runtime_gateway::GatewayService>(
      *gateway_node_, *store_, before_send(), before_cancel_lookup(), before_cancel_dispatch());
    server_ = std::make_unique<runtime_gateway::LoopbackServer>(*service_, 0);
    server_->start();
    stub_ = runtime_gateway::RobotRuntime::NewStub(
      grpc::CreateChannel(server_->address(), grpc::InsecureChannelCredentials()));
  }

  void TearDown() override
  {
    server_->shutdown();
    service_->clear();
    stub_.reset();
    server_.reset();
    service_.reset();
    executor_.cancel();
    spin_thread_.join();
    executor_.remove_node(orchestrator_);
    executor_.remove_node(gateway_node_);
    orchestrator_.reset();
    gateway_node_.reset();
    store_.reset();
    std::filesystem::remove(path_);
    std::filesystem::remove(path_ + "-wal");
    std::filesystem::remove(path_ + "-shm");
  }

  runtime_gateway::SubmitWorkflowReply submit(
    const std::string & request_id = "request-1", const std::string & task_id = "task-1")
  {
    runtime_gateway::SubmitWorkflowRequest request;
    request.set_request_id(request_id);
    request.set_workflow_id("single_task");
    request.set_task_id(task_id);
    request.set_target_id("dock_a");
    request.set_timeout_ms(3000);
    runtime_gateway::SubmitWorkflowReply reply;
    grpc::ClientContext context;
    const auto status = stub_->SubmitWorkflow(&context, request, &reply);
    EXPECT_TRUE(status.ok()) << status.error_message();
    return reply;
  }

  grpc::Status submit_status(
    const std::string & request_id, const std::string & task_id,
    runtime_gateway::SubmitWorkflowReply * reply, std::uint64_t timeout_ms = 3000)
  {
    runtime_gateway::SubmitWorkflowRequest request;
    request.set_request_id(request_id);
    request.set_workflow_id("single_task");
    request.set_task_id(task_id);
    request.set_target_id("dock_a");
    request.set_timeout_ms(timeout_ms);
    grpc::ClientContext context;
    return stub_->SubmitWorkflow(&context, request, reply);
  }

  // The result callback persists the durable terminal state asynchronously.
  bool wait_for_durable(const std::string & request_id, const std::string & state)
  {
    for (int attempt = 0; attempt < 200; ++attempt) {
      const auto record = store_->get_idempotency(request_id);
      if (record && record->state == state) {
        return true;
      }
      std::this_thread::sleep_for(5ms);
    }
    return false;
  }

  virtual std::chrono::milliseconds goal_delay() const {return 0ms;}
  virtual std::function<void()> before_send() {return {};}
  virtual std::function<void()> before_cancel_lookup() {return {};}
  virtual std::function<void()> before_cancel_dispatch() {return {};}

  static inline unsigned counter_{};
  std::string path_;
  std::unique_ptr<runtime_history::Store> store_;
  std::shared_ptr<rclcpp::Node> gateway_node_;
  std::shared_ptr<FakeOrchestrator> orchestrator_;
  rclcpp::executors::MultiThreadedExecutor executor_;
  std::thread spin_thread_;
  std::unique_ptr<runtime_gateway::GatewayService> service_;
  std::unique_ptr<runtime_gateway::LoopbackServer> server_;
  std::unique_ptr<runtime_gateway::RobotRuntime::Stub> stub_;
};

class CancelTerminalRaceTest : public GrpcActionBridgeTest
{
protected:
  std::function<void()> before_cancel_lookup() override
  {
    return [this]() {
             cancel_entered_.set_value();
             release_cancel_.get_future().wait();
           };
  }

  std::promise<void> cancel_entered_;
  std::promise<void> release_cancel_;
};

class PostLookupCancelTerminalRaceTest : public GrpcActionBridgeTest
{
protected:
  std::function<void()> before_cancel_dispatch() override
  {
    return [this]() {
             cancel_entered_.set_value();
             release_cancel_.get_future().wait();
           };
  }

  std::promise<void> cancel_entered_;
  std::promise<void> release_cancel_;
};

class DelayedGrpcActionBridgeTest : public GrpcActionBridgeTest
{
protected:
  std::chrono::milliseconds goal_delay() const override {return 700ms;}
};

class SlowGoalResponseTest : public GrpcActionBridgeTest
{
protected:
  std::chrono::milliseconds goal_delay() const override {return 2800ms;}
};

class SendFailureShutdownTest : public GrpcActionBridgeTest
{
protected:
  std::function<void()> before_send() override
  {
    return [this]() {
             entered_.set_value();
             release_.get_future().wait();
             throw std::runtime_error("injected send failure");
           };
  }

  std::promise<void> entered_;
  std::promise<void> release_;
};

TEST_F(GrpcActionBridgeTest, SubmitReturnsAfterGoalResponseAndDuplicateDoesNotResubmit)
{
  testing::internal::CaptureStderr();
  const auto started = std::chrono::steady_clock::now();
  const auto first = submit();
  EXPECT_LT(std::chrono::steady_clock::now() - started, 500ms);
  EXPECT_TRUE(first.accepted());
  EXPECT_EQ(first.state(), "RUNNING");
  ASSERT_TRUE(orchestrator_->wait_for_goals(1));

  const auto duplicate = submit();
  const auto logs = testing::internal::GetCapturedStderr();
  EXPECT_TRUE(duplicate.accepted());
  EXPECT_EQ(orchestrator_->goal_count(), 1U);
  const std::string dispatch =
    "Dispatching ExecuteWorkflow Goal request_id=request-1 task_id=task-1";
  const auto first_dispatch = logs.find(dispatch);
  EXPECT_NE(first_dispatch, std::string::npos) << logs;
  EXPECT_EQ(logs.find(dispatch, first_dispatch + 1), std::string::npos) << logs;
  orchestrator_->complete("request-1");
}

TEST_F(GrpcActionBridgeTest, CancelUsesTheHandleForItsRequestId)
{
  ASSERT_TRUE(submit("request-cancel", "task-cancel").accepted());
  ASSERT_TRUE(orchestrator_->wait_for_goals(1));
  runtime_gateway::CancelWorkflowRequest request;
  request.set_request_id("request-cancel");
  runtime_gateway::CancelWorkflowReply reply;
  grpc::ClientContext context;
  const auto status = stub_->CancelWorkflow(&context, request, &reply);
  ASSERT_TRUE(status.ok()) << status.error_message();
  EXPECT_TRUE(reply.accepted());
  EXPECT_EQ(reply.state(), "CANCELING");

  runtime_gateway::GetTaskRequest get_request;
  get_request.set_task_id("task-cancel");
  runtime_gateway::TaskRecord task;
  grpc::ClientContext get_context;
  ASSERT_TRUE(stub_->GetTask(&get_context, get_request, &task).ok());
  EXPECT_EQ(task.state(), "CANCELING");
  EXPECT_EQ(task.target_id(), "dock_a");
  EXPECT_EQ(submit("request-cancel", "task-cancel").state(), "CANCELING");
  EXPECT_TRUE(orchestrator_->wait_for_cancel("request-cancel"));
  orchestrator_->finish("request-cancel", ExecuteWorkflow::Result::CANCELED);
}

TEST_F(GrpcActionBridgeTest, CancelAndShutdownDispatchOnlyOneCancelRequest)
{
  ASSERT_TRUE(submit("request-once", "task-once").accepted());
  ASSERT_TRUE(orchestrator_->wait_for_goals(1));
  runtime_gateway::CancelWorkflowRequest request;
  request.set_request_id("request-once");
  runtime_gateway::CancelWorkflowReply reply;
  grpc::ClientContext context;
  ASSERT_TRUE(stub_->CancelWorkflow(&context, request, &reply).ok());
  ASSERT_TRUE(orchestrator_->wait_for_cancel("request-once"));
  orchestrator_->finish("request-once", ExecuteWorkflow::Result::CANCELED);
  server_->shutdown();
  std::this_thread::sleep_for(50ms);
  EXPECT_EQ(orchestrator_->cancel_count(), 1U);
  EXPECT_EQ(service_->cancel_dispatch_count(), 1U);
}

TEST_F(DelayedGrpcActionBridgeTest, TimedOutSubmitCancelsLateAcceptedGoal)
{
  runtime_gateway::SubmitWorkflowReply reply;
  const auto status = submit_status("request-timeout", "task-timeout", &reply);
  EXPECT_EQ(status.error_code(), grpc::StatusCode::DEADLINE_EXCEEDED);
  EXPECT_TRUE(orchestrator_->wait_for_cancel("request-timeout"));
  orchestrator_->finish("request-timeout", ExecuteWorkflow::Result::CANCELED);
}

TEST_F(DelayedGrpcActionBridgeTest, CancelWhileSubmittingCancelsLateAcceptedGoal)
{
  runtime_gateway::SubmitWorkflowReply submit_reply;
  auto pending = std::async(
    std::launch::async, [this, &submit_reply]() {
      return submit_status("request-pending-cancel", "task-pending-cancel", &submit_reply);
    });
  std::this_thread::sleep_for(100ms);

  runtime_gateway::CancelWorkflowRequest request;
  request.set_request_id("request-pending-cancel");
  runtime_gateway::CancelWorkflowReply reply;
  grpc::ClientContext context;
  ASSERT_TRUE(stub_->CancelWorkflow(&context, request, &reply).ok());
  EXPECT_TRUE(reply.accepted());
  EXPECT_EQ(reply.state(), "CANCELING");
  EXPECT_EQ(pending.get().error_code(), grpc::StatusCode::DEADLINE_EXCEEDED);
  runtime_gateway::GetTaskRequest get_request;
  get_request.set_task_id("task-pending-cancel");
  runtime_gateway::TaskRecord task;
  grpc::ClientContext get_context;
  ASSERT_TRUE(stub_->GetTask(&get_context, get_request, &task).ok());
  EXPECT_EQ(task.state(), "CANCELING");
  EXPECT_TRUE(orchestrator_->wait_for_cancel("request-pending-cancel"));
  orchestrator_->finish("request-pending-cancel", ExecuteWorkflow::Result::CANCELED);
}

TEST_F(DelayedGrpcActionBridgeTest, ShutdownOverlappingGoalResponseStillCancelsLateGoal)
{
  runtime_gateway::SubmitWorkflowReply reply;
  auto pending = std::async(
    std::launch::async, [this, &reply]() {
      return submit_status("request-pending-shutdown", "task-pending-shutdown", &reply);
    });
  std::this_thread::sleep_for(100ms);
  auto shutdown = std::async(std::launch::async, [this]() {server_->shutdown();});
  (void)pending.get();
  EXPECT_TRUE(orchestrator_->wait_for_cancel("request-pending-shutdown"));
  EXPECT_EQ(shutdown.wait_for(100ms), std::future_status::timeout);
  orchestrator_->finish("request-pending-shutdown", ExecuteWorkflow::Result::SAFE_STOP);
  shutdown.get();
  EXPECT_EQ(service_->active_request_count(), 0U);
}

TEST_F(SlowGoalResponseTest, ShutdownWaitsForLateGoalBeforeReturning)
{
  runtime_gateway::SubmitWorkflowReply reply;
  EXPECT_EQ(
    submit_status("request-slow-shutdown", "task-slow-shutdown", &reply).error_code(),
    grpc::StatusCode::DEADLINE_EXCEEDED);
  const auto started = std::chrono::steady_clock::now();
  auto shutdown = std::async(std::launch::async, [this]() {server_->shutdown();});
  ASSERT_TRUE(orchestrator_->wait_for_cancel("request-slow-shutdown", 4s));
  EXPECT_EQ(shutdown.wait_for(100ms), std::future_status::timeout);
  orchestrator_->finish("request-slow-shutdown", ExecuteWorkflow::Result::CANCELED);
  shutdown.get();
  EXPECT_GE(std::chrono::steady_clock::now() - started, 2200ms);
  EXPECT_TRUE(orchestrator_->wait_for_cancel("request-slow-shutdown"));
  EXPECT_EQ(service_->active_request_count(), 0U);
}

TEST_F(SlowGoalResponseTest, ShutdownRejectsNewRpcIntakeWhileWaitingForLateGoal)
{
  runtime_gateway::SubmitWorkflowReply submit_reply;
  EXPECT_EQ(
    submit_status("request-intake", "task-intake", &submit_reply).error_code(),
    grpc::StatusCode::DEADLINE_EXCEEDED);
  auto shutdown = std::async(std::launch::async, [this]() {server_->shutdown();});
  std::this_thread::sleep_for(100ms);

  runtime_gateway::CancelWorkflowRequest cancel_request;
  cancel_request.set_request_id("request-intake");
  runtime_gateway::CancelWorkflowReply cancel_reply;
  grpc::ClientContext cancel_context;
  cancel_context.set_deadline(std::chrono::system_clock::now() + 500ms);
  EXPECT_EQ(
    stub_->CancelWorkflow(&cancel_context, cancel_request, &cancel_reply).error_code(),
    grpc::StatusCode::UNAVAILABLE);

  runtime_gateway::GetTaskRequest task_request;
  task_request.set_task_id("task-intake");
  runtime_gateway::TaskRecord task_reply;
  grpc::ClientContext task_context;
  task_context.set_deadline(std::chrono::system_clock::now() + 500ms);
  EXPECT_EQ(
    stub_->GetTask(&task_context, task_request, &task_reply).error_code(),
    grpc::StatusCode::UNAVAILABLE);

  runtime_gateway::GetStatsRequest stats_request;
  runtime_gateway::RuntimeStats stats_reply;
  grpc::ClientContext stats_context;
  stats_context.set_deadline(std::chrono::system_clock::now() + 500ms);
  EXPECT_EQ(
    stub_->GetStats(&stats_context, stats_request, &stats_reply).error_code(),
    grpc::StatusCode::UNAVAILABLE);
  ASSERT_TRUE(orchestrator_->wait_for_cancel("request-intake", 4s));
  orchestrator_->finish("request-intake", ExecuteWorkflow::Result::CANCELED);
  shutdown.get();
}

TEST_F(GrpcActionBridgeTest, ShutdownWaitsForAcceptedWorkflowTerminalResult)
{
  ASSERT_TRUE(submit("request-terminal", "task-terminal").accepted());
  ASSERT_TRUE(orchestrator_->wait_for_goals(1));
  auto shutdown = std::async(std::launch::async, [this]() {server_->shutdown();});
  ASSERT_TRUE(orchestrator_->wait_for_cancel("request-terminal"));
  EXPECT_EQ(shutdown.wait_for(100ms), std::future_status::timeout);
  orchestrator_->finish("request-terminal", ExecuteWorkflow::Result::CANCELED);
  shutdown.get();
  EXPECT_EQ(service_->active_request_count(), 0U);
}

TEST_F(CancelTerminalRaceTest, CancelReturnsLatestTerminalRecordWhenGoalFinishesDuringLookup)
{
  ASSERT_TRUE(submit("request-race", "task-race").accepted());
  ASSERT_TRUE(orchestrator_->wait_for_goals(1));
  runtime_gateway::CancelWorkflowRequest request;
  request.set_request_id("request-race");
  runtime_gateway::CancelWorkflowReply reply;
  grpc::Status status;
  auto cancel = std::async(
    std::launch::async, [this, &request, &reply, &status]() {
      grpc::ClientContext context;
      status = stub_->CancelWorkflow(&context, request, &reply);
    });
  ASSERT_EQ(cancel_entered_.get_future().wait_for(2s), std::future_status::ready);
  orchestrator_->complete("request-race");
  runtime_gateway::GetTaskRequest get_request;
  get_request.set_task_id("task-race");
  runtime_gateway::TaskRecord task;
  for (int attempt = 0; attempt < 100 && task.state() != "COMPLETED"; ++attempt) {
    grpc::ClientContext context;
    ASSERT_TRUE(stub_->GetTask(&context, get_request, &task).ok());
    std::this_thread::sleep_for(5ms);
  }
  ASSERT_EQ(task.state(), "COMPLETED");
  release_cancel_.set_value();
  cancel.get();
  ASSERT_TRUE(status.ok());
  EXPECT_FALSE(reply.accepted());
  EXPECT_EQ(reply.state(), "COMPLETED");
  EXPECT_EQ(reply.message(), "done");
}

TEST_F(PostLookupCancelTerminalRaceTest, CancelReturnsTerminalResultWhenGoalFinishesBeforeDispatch)
{
  ASSERT_TRUE(submit("request-post-lookup", "task-post-lookup").accepted());
  ASSERT_TRUE(orchestrator_->wait_for_goals(1));
  runtime_gateway::CancelWorkflowRequest request;
  request.set_request_id("request-post-lookup");
  runtime_gateway::CancelWorkflowReply reply;
  grpc::Status status;
  auto cancel = std::async(
    std::launch::async, [this, &request, &reply, &status]() {
      grpc::ClientContext context;
      status = stub_->CancelWorkflow(&context, request, &reply);
    });
  ASSERT_EQ(cancel_entered_.get_future().wait_for(2s), std::future_status::ready);
  orchestrator_->complete("request-post-lookup");
  runtime_gateway::GetTaskRequest get_request;
  get_request.set_task_id("task-post-lookup");
  runtime_gateway::TaskRecord task;
  for (int attempt = 0; attempt < 100 && task.state() != "COMPLETED"; ++attempt) {
    grpc::ClientContext context;
    ASSERT_TRUE(stub_->GetTask(&context, get_request, &task).ok());
    std::this_thread::sleep_for(5ms);
  }
  ASSERT_EQ(task.state(), "COMPLETED");
  release_cancel_.set_value();
  cancel.get();
  ASSERT_TRUE(status.ok());
  EXPECT_FALSE(reply.accepted());
  EXPECT_EQ(reply.state(), "COMPLETED");
  EXPECT_EQ(reply.message(), "done");
}

TEST_F(SendFailureShutdownTest, SendExceptionErasesPendingAndWakesShutdown)
{
  runtime_gateway::SubmitWorkflowReply reply;
  auto submit_call = std::async(
    std::launch::async, [this, &reply]() {
      return submit_status("request-send-failure", "task-send-failure", &reply);
    });
  ASSERT_EQ(entered_.get_future().wait_for(2s), std::future_status::ready);
  auto shutdown = std::async(std::launch::async, [this]() {server_->shutdown();});
  std::this_thread::sleep_for(100ms);
  const auto released_at = std::chrono::steady_clock::now();
  release_.set_value();
  EXPECT_EQ(submit_call.get().error_code(), grpc::StatusCode::UNAVAILABLE);
  shutdown.get();
  EXPECT_LT(std::chrono::steady_clock::now() - released_at, 500ms);
  EXPECT_EQ(service_->active_request_count(), 0U);
}

TEST_F(GrpcActionBridgeTest, TerminalMemoryRecordYieldsToSqliteRecord)
{
  ASSERT_TRUE(submit("request-stored", "task-stored").accepted());
  ASSERT_TRUE(orchestrator_->wait_for_goals(1));
  ASSERT_TRUE(store_->insert({"task-stored", "stored_target", 1, 0, 0, 42, "persisted", 99}));
  orchestrator_->complete("request-stored");

  runtime_gateway::TaskRecord reply;
  bool stored_record_visible = false;
  for (int attempt = 0; attempt < 100 && !stored_record_visible; ++attempt) {
    reply.Clear();
    runtime_gateway::GetTaskRequest request;
    request.set_task_id("task-stored");
    grpc::ClientContext context;
    ASSERT_TRUE(stub_->GetTask(&context, request, &reply).ok());
    stored_record_visible =
      reply.state() == "COMPLETED" && reply.target_id() == "stored_target" &&
      reply.duration_ms() == 42U && reply.message() == "persisted";
    if (!stored_record_visible) {
      std::this_thread::sleep_for(5ms);
    }
  }
  ASSERT_TRUE(stored_record_visible);
  EXPECT_EQ(reply.target_id(), "stored_target");
  EXPECT_EQ(reply.duration_ms(), 42U);
  EXPECT_EQ(reply.message(), "persisted");
}

TEST_F(GrpcActionBridgeTest, ShutdownClearsRegistryAfterTerminalCallback)
{
  ASSERT_TRUE(submit("request-shutdown", "task-shutdown").accepted());
  ASSERT_TRUE(orchestrator_->wait_for_goals(1));
  auto shutdown = std::async(std::launch::async, [this]() {server_->shutdown();});
  EXPECT_TRUE(orchestrator_->wait_for_cancel("request-shutdown"));
  EXPECT_EQ(shutdown.wait_for(100ms), std::future_status::timeout);
  orchestrator_->finish("request-shutdown", ExecuteWorkflow::Result::CANCELED);
  shutdown.get();
  EXPECT_EQ(service_->active_request_count(), 0U);
}

TEST_F(GrpcActionBridgeTest, ChangedTimeoutUnderSameRequestIdConflictsWithoutRedispatch)
{
  ASSERT_TRUE(submit("request-timeout", "task-timeout").accepted());
  ASSERT_TRUE(orchestrator_->wait_for_goals(1));
  orchestrator_->complete("request-timeout");
  ASSERT_TRUE(wait_for_durable("request-timeout", "COMPLETED"));

  runtime_gateway::SubmitWorkflowReply reply;
  // Same request_id and payload, different valid timeout -> conflict, no goal.
  const auto status = submit_status("request-timeout", "task-timeout", &reply, 6000);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.error_code(), grpc::StatusCode::INVALID_ARGUMENT);
  EXPECT_EQ(orchestrator_->goal_count(), 1U);
}

TEST_F(GrpcActionBridgeTest, TerminalRetryAfterRestartReplaysStoredResultWithoutRedispatch)
{
  ASSERT_TRUE(submit("request-restart", "task-restart").accepted());
  ASSERT_TRUE(orchestrator_->wait_for_goals(1));
  orchestrator_->complete("request-restart");
  ASSERT_TRUE(wait_for_durable("request-restart", "COMPLETED"));
  const auto completed = orchestrator_->goal_count();

  // Restart the gateway against the same SQLite file: the in-memory registry
  // is gone, but the durable ledger must still suppress the dispatch.
  server_->shutdown();
  service_.reset();
  server_.reset();
  stub_.reset();
  service_ = std::make_unique<runtime_gateway::GatewayService>(
    *gateway_node_, *store_, before_send(), before_cancel_lookup(), before_cancel_dispatch());
  server_ = std::make_unique<runtime_gateway::LoopbackServer>(*service_, 0);
  server_->start();
  stub_ = runtime_gateway::RobotRuntime::NewStub(
    grpc::CreateChannel(server_->address(), grpc::InsecureChannelCredentials()));

  runtime_gateway::SubmitWorkflowReply reply;
  const auto status = submit_status("request-restart", "task-restart", &reply);
  ASSERT_TRUE(status.ok()) << status.error_message();
  EXPECT_FALSE(reply.accepted());
  EXPECT_EQ(reply.state(), "COMPLETED");
  EXPECT_EQ(orchestrator_->goal_count(), completed);
}

}  // namespace
