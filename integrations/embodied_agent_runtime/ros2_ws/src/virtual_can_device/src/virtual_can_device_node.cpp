#include "robot_task_interfaces/msg/device_state.hpp"
#include "runtime_can/protocol.hpp"
#include "runtime_can/socket_can.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>

#include "rclcpp/rclcpp.hpp"

class VirtualCanDeviceNode : public rclcpp::Node
{
public:
  using DeviceState = robot_task_interfaces::msg::DeviceState;

  VirtualCanDeviceNode()
  : Node("virtual_can_device")
  {
    interface_name_ = declare_parameter<std::string>("interface_name", "vcan0");
    device_id_ = declare_parameter<std::string>("device_id", "virtual_ecu_1");
    mode_ = declare_parameter<std::string>("mode", "normal");
    const auto configured_delay_ms = declare_parameter<std::int64_t>("delay_ms", 500);
    const auto configured_cache_capacity = declare_parameter<std::int64_t>(
      "replay_cache_capacity", 4096);
    delay_ms_ = static_cast<int>(
      std::clamp<std::int64_t>(configured_delay_ms, 0, 60000));
    if (configured_cache_capacity < 1 || configured_cache_capacity > 65536) {
      throw std::invalid_argument("replay_cache_capacity must be in [1, 65536]");
    }
    replay_cache_capacity_ = static_cast<std::size_t>(configured_cache_capacity);
    if (!is_supported_mode(mode_)) {
      throw std::invalid_argument("unsupported virtual device mode: " + mode_);
    }

    state_publisher_ = create_publisher<DeviceState>("device_state", 10);

    std::string error;
    if (!socket_.open(interface_name_, error)) {
      throw std::runtime_error("failed to open " + interface_name_ + ": " + error);
    }
    if (!socket_.set_filter(runtime_can::kCommandFrameId, error)) {
      throw std::runtime_error("failed to set command filter: " + error);
    }

    running_ = true;
    io_thread_ = std::thread([this]() {receive_loop();});
    RCLCPP_INFO(
      get_logger(), "Ready on %s mode=%s delay_ms=%d replay_cache_capacity=%zu",
      interface_name_.c_str(), mode_.c_str(), delay_ms_, replay_cache_capacity_);
  }

  ~VirtualCanDeviceNode() override
  {
    running_ = false;
    if (io_thread_.joinable()) {
      io_thread_.join();
    }
  }

private:
  struct CacheEntry
  {
    runtime_can::Command command;
    std::optional<runtime_can::Response> response;
  };

  static bool is_supported_mode(const std::string & mode)
  {
    return mode == "normal" || mode == "delay_ack" || mode == "drop_ack" ||
           mode == "drop_stop_ack" || mode == "reject" || mode == "fault";
  }

  void receive_loop()
  {
    using namespace std::chrono_literals;
    while (running_ && rclcpp::ok()) {
      runtime_can::RawFrame frame;
      std::string error;
      const auto status = socket_.receive(frame, 100ms, error);
      if (status == runtime_can::ReceiveStatus::kTimeout) {
        continue;
      }
      if (status == runtime_can::ReceiveStatus::kError) {
        RCLCPP_ERROR(get_logger(), "CAN receive failed: %s", error.c_str());
        continue;
      }

      runtime_can::DecodeError decode_error;
      const auto command = runtime_can::decode_command(frame, decode_error);
      if (!command) {
        RCLCPP_WARN(
          get_logger(), "Ignoring invalid command frame: %s",
          runtime_can::to_string(decode_error).data());
        continue;
      }
      process_command(*command);
    }
  }

  void process_command(const runtime_can::Command & command)
  {
    const auto cached = cache_.find(command.command_id);
    if (cached != cache_.end()) {
      if (!(cached->second.command == command)) {
        runtime_can::Response conflict{
          command.command_id, 3, DeviceState::FAULT, {0, 0, 0}};
        send_response(conflict);
        publish_state(command.command_id, DeviceState::FAULT, 3);
        return;
      }
      if (cached->second.response) {
        send_response(*cached->second.response);
      }
      return;
    }

    if (command.opcode == runtime_can::kStopOpcode) {
      if (mode_ == "drop_stop_ack") {
        cache_command(command, std::nullopt);
        publish_state(command.command_id, DeviceState::BUSY, 0);
        RCLCPP_WARN(get_logger(), "Dropping STOP ACK command_id=%u", command.command_id);
        return;
      }

      runtime_can::Response stop_response;
      stop_response.command_id = command.command_id;
      stop_response.result_code = 0;
      stop_response.device_mode = DeviceState::STOPPED;
      cache_command(command, stop_response);
      send_response(stop_response);
      publish_state(command.command_id, DeviceState::STOPPED, 0);
      RCLCPP_INFO(
        get_logger(), "STOP acknowledged command_id=%u original_command_id=%d",
        command.command_id, command.argument);
      return;
    }

    if (mode_ == "drop_ack" || mode_ == "drop_stop_ack") {
      cache_command(command, std::nullopt);
      publish_state(command.command_id, DeviceState::BUSY, 0);
      RCLCPP_WARN(get_logger(), "Dropping ACK command_id=%u", command.command_id);
      return;
    }

    runtime_can::Response response;
    response.command_id = command.command_id;
    if (mode_ == "reject") {
      response.result_code = 1;
      response.device_mode = DeviceState::NORMAL;
    } else if (mode_ == "fault") {
      response.result_code = 2;
      response.device_mode = DeviceState::FAULT;
    } else {
      response.result_code = 0;
      response.device_mode = DeviceState::NORMAL;
    }

    cache_command(command, response);
    if (mode_ == "delay_ack") {
      std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms_));
    }
    send_response(response);
    publish_state(
      command.command_id, response.device_mode,
      response.result_code == 2 ? 1 : 0);
  }

  void cache_command(
    const runtime_can::Command & command,
    const std::optional<runtime_can::Response> & response)
  {
    if (cache_.size() >= replay_cache_capacity_) {
      cache_.erase(cache_order_.front());
      cache_order_.pop_front();
    }
    cache_.emplace(command.command_id, CacheEntry{command, response});
    cache_order_.push_back(command.command_id);
  }

  void send_response(const runtime_can::Response & response)
  {
    std::string error;
    if (!socket_.send(runtime_can::encode_response(response), error)) {
      RCLCPP_ERROR(get_logger(), "CAN response send failed: %s", error.c_str());
      return;
    }
    ++response_count_;
    RCLCPP_INFO(
      get_logger(), "Response command_id=%u result=%u count=%u",
      response.command_id, response.result_code, response_count_);
  }

  void publish_state(std::uint16_t command_id, std::uint8_t mode, std::uint16_t fault_code)
  {
    DeviceState state;
    state.stamp = get_clock()->now();
    state.device_id = device_id_;
    state.mode = mode;
    state.fault_code = fault_code;
    state.last_command_id = command_id;
    state_publisher_->publish(state);
  }

  std::string interface_name_;
  std::string device_id_;
  std::string mode_;
  int delay_ms_{0};
  std::size_t replay_cache_capacity_{4096};
  std::atomic_bool running_{false};
  std::uint32_t response_count_{0};
  runtime_can::SocketCan socket_;
  rclcpp::Publisher<DeviceState>::SharedPtr state_publisher_;
  std::thread io_thread_;
  std::deque<std::uint16_t> cache_order_;
  std::unordered_map<std::uint16_t, CacheEntry> cache_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  try {
    rclcpp::spin(std::make_shared<VirtualCanDeviceNode>());
  } catch (const std::exception & error) {
    RCLCPP_FATAL(rclcpp::get_logger("virtual_can_device"), "%s", error.what());
    rclcpp::shutdown();
    return 1;
  }
  rclcpp::shutdown();
  return 0;
}
