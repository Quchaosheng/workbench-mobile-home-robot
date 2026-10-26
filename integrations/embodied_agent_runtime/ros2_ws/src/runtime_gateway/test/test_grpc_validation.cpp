#include "runtime_gateway/gateway_service.hpp"

#include "runtime_history/store.hpp"

#include <filesystem>
#include <fstream>
#include <array>
#include <cstdint>
#include <memory>
#include <string>

#include <grpcpp/grpcpp.h>
#include <gtest/gtest.h>
#include <rclcpp/rclcpp.hpp>

namespace
{

class GrpcValidationTest : public testing::Test
{
protected:
  static void SetUpTestSuite()
  {
    int argc = 1;
    char name[] = "test_grpc_validation";
    char * argv[] = {name, nullptr};
    rclcpp::init(argc, argv);
  }

  static void TearDownTestSuite() {rclcpp::shutdown();}

  void SetUp() override
  {
    path_ = (std::filesystem::temp_directory_path() /
      ("runtime_gateway_" + std::to_string(counter_++) + ".sqlite3")).string();
    std::filesystem::remove(path_);
    store_ = std::make_unique<runtime_history::Store>(path_);
    node_ = std::make_shared<rclcpp::Node>("grpc_validation_test");
    service_ = std::make_unique<runtime_gateway::GatewayService>(*node_, *store_);

    grpc::ServerBuilder builder;
    builder.RegisterService(service_.get());
    int port{};
    builder.AddListeningPort("127.0.0.1:0", grpc::InsecureServerCredentials(), &port);
    server_ = builder.BuildAndStart();
    ASSERT_NE(server_, nullptr);
    ASSERT_GT(port, 0);
    address_ = "127.0.0.1:" + std::to_string(port);
    stub_ = runtime_gateway::RobotRuntime::NewStub(
      grpc::CreateChannel(
        address_,
        grpc::InsecureChannelCredentials()));
  }

  void TearDown() override
  {
    service_->stop_accepting();
    service_->cancel_active();
    server_->Shutdown();
    server_->Wait();
    service_->clear();
    stub_.reset();
    service_.reset();
    node_.reset();
    store_.reset();
    std::filesystem::remove(path_);
    std::filesystem::remove(path_ + "-wal");
    std::filesystem::remove(path_ + "-shm");
  }

  runtime_gateway::SubmitWorkflowRequest valid_request() const
  {
    runtime_gateway::SubmitWorkflowRequest request;
    request.set_request_id("request-1");
    request.set_workflow_id("single_task");
    request.set_task_id("task-1");
    request.set_target_id("dock_a");
    request.set_timeout_ms(3000);
    return request;
  }

  static inline unsigned counter_{};
  std::string path_;
  std::string address_;
  std::shared_ptr<rclcpp::Node> node_;
  std::unique_ptr<runtime_history::Store> store_;
  std::unique_ptr<runtime_gateway::GatewayService> service_;
  std::unique_ptr<grpc::Server> server_;
  std::unique_ptr<runtime_gateway::RobotRuntime::Stub> stub_;
};

TEST_F(GrpcValidationTest, RejectsMissingRequiredFieldAsInvalidArgument)
{
  auto request = valid_request();
  request.clear_target_id();
  runtime_gateway::SubmitWorkflowReply reply;
  grpc::ClientContext context;
  const auto status = stub_->SubmitWorkflow(&context, request, &reply);
  EXPECT_EQ(status.error_code(), grpc::StatusCode::INVALID_ARGUMENT);
  EXPECT_EQ(service_->active_request_count(), 0U);
}

TEST_F(GrpcValidationTest, RejectsUnknownWorkflowWithoutActionSubmission)
{
  auto request = valid_request();
  request.set_workflow_id("uploaded_xml");
  runtime_gateway::SubmitWorkflowReply reply;
  grpc::ClientContext context;
  const auto status = stub_->SubmitWorkflow(&context, request, &reply);
  EXPECT_TRUE(status.ok());
  EXPECT_FALSE(reply.accepted());
  EXPECT_EQ(reply.state(), "REJECTED");

  runtime_gateway::GetTaskRequest get_request;
  get_request.set_task_id("task-1");
  runtime_gateway::TaskRecord task;
  grpc::ClientContext get_context;
  ASSERT_TRUE(stub_->GetTask(&get_context, get_request, &task).ok());
  EXPECT_TRUE(task.found());
  EXPECT_EQ(task.state(), "REJECTED");
}

TEST_F(GrpcValidationTest, ReportsUnavailableWhenOrchestratorIsMissing)
{
  const auto request = valid_request();
  runtime_gateway::SubmitWorkflowReply reply;
  grpc::ClientContext context;
  const auto status = stub_->SubmitWorkflow(&context, request, &reply);
  EXPECT_EQ(status.error_code(), grpc::StatusCode::UNAVAILABLE);
}

TEST_F(GrpcValidationTest, ReportsNoDataForEmptySqliteStats)
{
  runtime_gateway::GetStatsRequest request;
  runtime_gateway::RuntimeStats reply;
  grpc::ClientContext context;
  const auto status = stub_->GetStats(&context, request, &reply);
  ASSERT_TRUE(status.ok()) << status.error_message();
  EXPECT_FALSE(reply.has_data());
  EXPECT_EQ(reply.sample_count(), 0U);
  ASSERT_EQ(reply.outcome_counts_size(), 4);
}

TEST_F(GrpcValidationTest, MapsNonemptySqliteStatistics)
{
  const std::array<std::uint64_t, 5> durations{10, 20, 30, 40, 100};
  for (std::size_t index = 0; index < durations.size(); ++index) {
    ASSERT_TRUE(
      store_->insert(
      {
        "stats-" + std::to_string(index), "dock_a", 1,
        static_cast<std::uint8_t>(index % 4), 0, durations[index], "done",
        static_cast<std::int64_t>(index)}));
  }
  runtime_gateway::GetStatsRequest request;
  runtime_gateway::RuntimeStats reply;
  grpc::ClientContext context;
  ASSERT_TRUE(stub_->GetStats(&context, request, &reply).ok());
  EXPECT_TRUE(reply.has_data());
  EXPECT_EQ(reply.sample_count(), 5U);
  ASSERT_EQ(reply.outcome_counts_size(), 4);
  EXPECT_EQ(reply.outcome_counts(0), 2U);
  EXPECT_EQ(reply.outcome_counts(1), 1U);
  EXPECT_EQ(reply.outcome_counts(2), 1U);
  EXPECT_EQ(reply.outcome_counts(3), 1U);
  EXPECT_EQ(reply.p50_ms(), 30U);
  EXPECT_EQ(reply.p95_ms(), 100U);
  EXPECT_EQ(reply.p99_ms(), 100U);
  EXPECT_EQ(reply.max_ms(), 100U);
}

TEST_F(GrpcValidationTest, InstalledStyleClientSuccessIsOneJsonLine)
{
  const auto output = path_ + ".json";
  const std::string command = "RUNTIME_GATEWAY_ADDRESS=" + address_ + " \"" +
    RUNTIME_GATEWAY_CLIENT_PATH + "\" get-stats > \"" + output + "\"";
  ASSERT_EQ(std::system(command.c_str()), 0);
  std::ifstream stream(output);
  std::string first;
  std::string second;
  ASSERT_TRUE(std::getline(stream, first));
  EXPECT_FALSE(first.empty());
  EXPECT_EQ(first.front(), '{');
  EXPECT_EQ(first.back(), '}');
  EXPECT_FALSE(std::getline(stream, second));
  std::filesystem::remove(output);
}

}  // namespace
