#pragma once

#include "runtime_gateway/request_registry.hpp"

#include "robot_runtime.grpc.pb.h"

#include <functional>
#include <memory>

namespace rclcpp
{
class Node;
}

namespace runtime_history
{
class Store;
}

namespace runtime_gateway
{

class GatewayService final : public RobotRuntime::Service
{
public:
  GatewayService(
    rclcpp::Node & node, runtime_history::Store & store,
    std::function<void()> before_send = {},
    std::function<void()> before_cancel_lookup = {},
    std::function<void()> before_cancel_dispatch = {});
  ~GatewayService() override;

  grpc::Status SubmitWorkflow(
    grpc::ServerContext *, const SubmitWorkflowRequest *, SubmitWorkflowReply *) override;
  grpc::Status CancelWorkflow(
    grpc::ServerContext *, const CancelWorkflowRequest *, CancelWorkflowReply *) override;
  grpc::Status GetTask(
    grpc::ServerContext *, const GetTaskRequest *, TaskRecord *) override;
  grpc::Status GetStats(
    grpc::ServerContext *, const GetStatsRequest *, RuntimeStats *) override;

  void stop_accepting();
  void cancel_active();
  void clear();
  std::size_t active_request_count() const;
  std::size_t cancel_dispatch_count() const;

private:
  struct State;
  std::shared_ptr<State> state_;
};

}  // namespace runtime_gateway
