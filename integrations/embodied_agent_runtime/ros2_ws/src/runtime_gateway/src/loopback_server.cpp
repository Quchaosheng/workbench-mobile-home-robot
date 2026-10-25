#include "runtime_gateway/loopback_server.hpp"

#include "runtime_gateway/gateway_service.hpp"

#include <chrono>
#include <exception>
#include <stdexcept>

#include <grpcpp/grpcpp.h>

namespace runtime_gateway
{

LoopbackServer::LoopbackServer(GatewayService & service, const int port)
: service_(service), port_(port)
{
  if (port < 0 || port > 65535) {
    throw std::invalid_argument("port must be between 0 and 65535");
  }
}

LoopbackServer::~LoopbackServer()
{
  try {
    shutdown();
  } catch (const std::exception &) {
  }
}

void LoopbackServer::start()
{
  if (thread_.joinable()) {
    return;
  }
  thread_ = std::thread(
    [this]() {
      grpc::ServerBuilder builder;
      builder.RegisterService(&service_);
      int selected_port{};
      builder.AddListeningPort(
        "127.0.0.1:" + std::to_string(port_), grpc::InsecureServerCredentials(), &selected_port);
      auto server = builder.BuildAndStart();
      {
        const std::lock_guard<std::mutex> lock(mutex_);
        if (!server) {
          error_ = "failed to start loopback gRPC server";
        } else {
          address_ = "127.0.0.1:" + std::to_string(selected_port);
          server_ = std::move(server);
        }
        ready_ = true;
      }
      ready_condition_.notify_all();
      if (server_) {
        server_->Wait();
      }
    });

  std::unique_lock<std::mutex> lock(mutex_);
  ready_condition_.wait(lock, [this]() {return ready_;});
  if (!error_.empty()) {
    lock.unlock();
    thread_.join();
    throw std::runtime_error(error_);
  }
}

void LoopbackServer::shutdown()
{
  if (!thread_.joinable()) {
    return;
  }
  service_.stop_accepting();
  {
    const std::lock_guard<std::mutex> lock(mutex_);
    if (server_) {
      server_->Shutdown(std::chrono::system_clock::now() + std::chrono::milliseconds(100));
    }
  }
  std::exception_ptr cancel_error;
  try {
    service_.cancel_active();
  } catch (...) {
    cancel_error = std::current_exception();
  }
  thread_.join();
  service_.clear();
  {
    const std::lock_guard<std::mutex> lock(mutex_);
    server_.reset();
  }
  if (cancel_error) {
    std::rethrow_exception(cancel_error);
  }
}

const std::string & LoopbackServer::address() const {return address_;}

}  // namespace runtime_gateway
