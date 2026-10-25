#include "runtime_gateway/gateway_service.hpp"
#include "runtime_gateway/loopback_server.hpp"

#include "runtime_history/defaults.hpp"
#include "runtime_history/store.hpp"

#include <chrono>
#include <exception>
#include <csignal>
#include <memory>
#include <string>
#include <thread>

#include <rclcpp/rclcpp.hpp>

namespace
{

volatile std::sig_atomic_t stop_requested{};

void request_stop(int) {stop_requested = 1;}

}  // namespace

int main(int argc, char ** argv)
{
  rclcpp::init(
    argc, argv, rclcpp::InitOptions{}, rclcpp::SignalHandlerOptions::None);
  std::signal(SIGINT, request_stop);
  std::signal(SIGTERM, request_stop);
  try {
    auto node = std::make_shared<rclcpp::Node>("runtime_gateway");
    const auto port = node->declare_parameter<int>("port", 50051);
    const auto database_path = node->declare_parameter<std::string>(
      "database_path", runtime_history::kDefaultDatabasePath);
    if (port < 1 || port > 65535 || database_path.empty()) {
      throw std::invalid_argument("port and database_path must be valid");
    }

    runtime_history::Store store(database_path);
    runtime_gateway::GatewayService service(*node, store);
    runtime_gateway::LoopbackServer server(service, port);
    server.start();
    RCLCPP_INFO(node->get_logger(), "gRPC listening on %s", server.address().c_str());

    rclcpp::executors::MultiThreadedExecutor executor;
    executor.add_node(node);
    std::thread spin_thread([&executor]() {executor.spin();});
    while (!stop_requested) {
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    bool shutdown_failed = false;
    try {
      server.shutdown();
    } catch (const std::exception & error) {
      shutdown_failed = true;
      RCLCPP_ERROR(node->get_logger(), "%s", error.what());
    }
    executor.cancel();
    spin_thread.join();
    executor.remove_node(node);
    if (shutdown_failed) {
      rclcpp::shutdown();
      return 1;
    }
  } catch (const std::exception & error) {
    RCLCPP_FATAL(rclcpp::get_logger("runtime_gateway"), "%s", error.what());
    rclcpp::shutdown();
    return 1;
  }
  rclcpp::shutdown();
  return 0;
}
