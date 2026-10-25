#pragma once

#include <memory>
#include <condition_variable>
#include <mutex>
#include <string>
#include <thread>

#include <grpcpp/server.h>

namespace runtime_gateway
{
class GatewayService;

class LoopbackServer
{
public:
  LoopbackServer(GatewayService & service, int port);
  ~LoopbackServer();

  void start();
  void shutdown();
  const std::string & address() const;

private:
  GatewayService & service_;
  int port_;
  std::string address_;
  std::unique_ptr<grpc::Server> server_;
  std::thread thread_;
  mutable std::mutex mutex_;
  std::condition_variable ready_condition_;
  bool ready_{false};
  std::string error_;
};

}  // namespace runtime_gateway
