// The SocketCAN `hardware_interface::SystemInterface` (issue #541).
//
// What this file adds is the route a trajectory takes to a joint on physical
// hardware. Until it existed the only hardware plugin was the Gazebo one, so a
// command had nowhere to go.
//
// The design is deliberately stupid about failure:
//
//   * configure() opens the socket and reads the arbitration id table. A missing
//     socket, an unreadable table or an unset id is a configure FAILURE, because a
//     hardware interface that starts without a destination is a safety problem
//     waiting for its first command.
//   * read() decodes whatever arrived and records when the last frame was seen. It
//     does not synthesise state: a caller that gets the same positions twice has
//     received two frames, not one frame twice.
//   * write() refuses when the state is stale. It never re-sends the previous
//     command, because a command nobody re-issued must not resurrect a stopped
//     robot.
//
// What this does NOT prove: that a frame reached an MCU, that a joint moved, or that
// a stop was physically achieved. Every evidence table keeps hardware
// NOT_EXECUTED until a bring-up record exists.
//
// Built only where `hardware_interface` is installed; see CMakeLists.txt. The frame
// codec it calls is ROS-free and its tests run anywhere a compiler exists.

#include "workbench_mobile_base_hardware/can_system_interface.hpp"

#include <linux/can.h>
#include <linux/can/raw.h>
#include <net/if.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <fstream>
#include <cmath>
#include <cstring>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include "hardware_interface/types/hardware_interface_return_values.hpp"
#include "hardware_interface/types/hardware_interface_type_values.hpp"
#include "rclcpp/rclcpp.hpp"

namespace workbench_mobile_base_hardware
{

namespace
{

rclcpp::Logger logger()
{
  return rclcpp::get_logger("workbench_mobile_base_hardware");
}

//: One steady clock for the throttle macros. RCLCPP_*_THROTTLE takes a Clock
//: reference; constructing one per call would make every call the first one and
//: throttle nothing.
rclcpp::Clock & throttle_clock()
{
  static rclcpp::Clock clock(RCL_STEADY_TIME);
  return clock;
}

//: How many frames read() drains per cycle. Bounded so a flooding bus cannot starve
//: the controller loop: a read that never returns is a robot that never commands.
constexpr int kMaxFramesPerRead = 32;

}  // namespace

hardware_interface::CallbackReturn CanSystemInterface::on_init(
  const hardware_interface::HardwareInfo & info)
{
  if (hardware_interface::SystemInterface::on_init(info) !=
    hardware_interface::CallbackReturn::SUCCESS)
  {
    return hardware_interface::CallbackReturn::ERROR;
  }

  // Parameters are read here and validated again in on_configure, because on_init
  // may run before the bus exists and a parameter error should be reported before
  // anything is opened.
  const auto & parameters = info_.hardware_parameters;

  const auto can_interface = parameters.find("can_interface");
  if (can_interface == parameters.end() || can_interface->second.empty()) {
    RCLCPP_ERROR(
      logger(),
      "can_interface is not set. The interface cannot guess which bus to open: a guess "
      "is how a command reaches a different device than intended.");
    return hardware_interface::CallbackReturn::ERROR;
  }
  can_interface_ = can_interface->second;

  const auto table_path = parameters.find("arbitration_id_table");
  if (table_path == parameters.end() || table_path->second.empty()) {
    RCLCPP_ERROR(
      logger(),
      "arbitration_id_table is not set. Without it a command frame has no destination, "
      "and a default id is worse than no id.");
    return hardware_interface::CallbackReturn::ERROR;
  }
  arbitration_table_path_ = table_path->second;

  const auto timeout = parameters.find("state_timeout_s");
  if (timeout != parameters.end()) {
    try {
      state_timeout_s_ = std::stod(timeout->second);
    } catch (const std::exception & error) {
      RCLCPP_ERROR(logger(), "state_timeout_s=%s is not a number: %s", timeout->second.c_str(), error.what());
      return hardware_interface::CallbackReturn::ERROR;
    }
    if (!std::isfinite(state_timeout_s_) || state_timeout_s_ <= 0.0) {
      RCLCPP_ERROR(
        logger(),
        "state_timeout_s=%s must be positive; a zero timeout accepts any frame, including "
        "one from a previous run.",
        timeout->second.c_str());
      return hardware_interface::CallbackReturn::ERROR;
    }
  }

  const auto rate = parameters.find("command_rate_hz");
  if (rate != parameters.end()) {
    try {
      command_rate_hz_ = std::stod(rate->second);
    } catch (const std::exception & error) {
      RCLCPP_ERROR(logger(), "command_rate_hz=%s is not a number: %s", rate->second.c_str(), error.what());
      return hardware_interface::CallbackReturn::ERROR;
    }
  }

  // Joint state storage: one command and one position per joint, sized from the
  // description so the controller configuration needs no edit to drive this instead
  // of the Gazebo plugin.
  joint_positions_.assign(info_.joints.size(), 0.0);
  joint_commands_.assign(info_.joints.size(), 0.0);
  joint_limits_.clear();
  joint_limits_.reserve(info_.joints.size());
  for (const auto & joint : info_.joints) {
    double minimum = 0.0;
    double maximum = 0.0;
    for (const auto & command_interface : joint.command_interfaces) {
      for (const auto & limit : command_interface.min) {
        minimum = std::stod(limit);
        break;
      }
      for (const auto & limit : command_interface.max) {
        maximum = std::stod(limit);
        break;
      }
    }
    joint_limits_.push_back({joint.name, minimum, maximum});
  }

  return hardware_interface::CallbackReturn::SUCCESS;
}

hardware_interface::CallbackReturn CanSystemInterface::on_configure(
  const rclcpp_lifecycle::State & /*previous_state*/)
{
  // The arbitration table is read first: a bus with no valid id table is not a bus
  // this interface may write to.
  if (!load_arbitration_table()) {
    return hardware_interface::CallbackReturn::ERROR;
  }

  if (!open_socket()) {
    return hardware_interface::CallbackReturn::FAILURE;
  }

  active_.store(true);
  last_state_at_ = std::chrono::steady_clock::now();
  RCLCPP_INFO(
    logger(), "configured %s on %s with %zu joint(s)", info_.name.c_str(), can_interface_.c_str(),
    info_.joints.size());
  return hardware_interface::CallbackReturn::SUCCESS;
}

bool CanSystemInterface::load_arbitration_table()
{
  // The table is a small YAML-free key=value file so this file needs no YAML
  // dependency; `arbitration_id_table` points at it in the URDF. A duplicate or
  // unparsable id is a refusal, not a last-one-wins.
  std::ifstream stream(arbitration_table_path_);
  if (!stream) {
    RCLCPP_ERROR(
      logger(), "cannot read arbitration_id_table %s (%s)", arbitration_table_path_.c_str(),
      std::strerror(errno));
    return false;
  }

  ArbIdTable table;
  std::string line;
  int line_number = 0;
  while (std::getline(stream, line)) {
    ++line_number;
    const auto comment = line.find('#');
    if (comment != std::string::npos) {
      line.erase(comment);
    }
    std::istringstream parser(line);
    std::string key;
    unsigned long value = 0;
    if (!(parser >> key >> value)) {
      continue;  // blank line or a comment after stripping
    }

    std::uint32_t * target = nullptr;
    if (key == "command") {
      target = &table.command;
    } else if (key == "state") {
      target = &table.state;
    } else {
      RCLCPP_ERROR(logger(), "%s:%d names unknown arbitration key %s", arbitration_table_path_.c_str(), line_number, key.c_str());
      return false;
    }
    if (*target != 0) {
      // A duplicate id means two meanings for one frame, which is a bus-level
      // ambiguity no runtime check can resolve later.
      RCLCPP_ERROR(
        logger(), "%s:%d repeats arbitration key %s; a duplicate id means two meanings for one frame",
        arbitration_table_path_.c_str(), line_number, key.c_str());
      return false;
    }
    if (value == 0 || value > 0x1FFFFFFF) {
      RCLCPP_ERROR(
        logger(), "%s:%d value 0x%lX is not a valid 29-bit arbitration id",
        arbitration_table_path_.c_str(), line_number, value);
      return false;
    }
    *target = static_cast<std::uint32_t>(value);
  }

  if (table.command == 0 || table.state == 0) {
    RCLCPP_ERROR(
      logger(), "%s does not define both command and state ids; a half-set table is not a table",
      arbitration_table_path_.c_str());
    return false;
  }
  table.set = true;
  arbitration_table_ = table;
  return true;
}

bool CanSystemInterface::open_socket()
{
  const int descriptor = ::socket(PF_CAN, SOCK_RAW, CAN_RAW);
  if (descriptor < 0) {
    RCLCPP_ERROR(logger(), "cannot create a CAN socket: %s", std::strerror(errno));
    return false;
  }

  struct ifreq interface_request {};
  std::strncpy(interface_request.ifr_name, can_interface_.c_str(), IFNAMSIZ - 1);
  if (::ioctl(descriptor, SIOCGIFINDEX, &interface_request) < 0) {
    // The interface does not exist. This is the "configure fails when can_interface
    // does not exist" case, and it is a configure-time failure rather than a retry:
    // an interface that silently retries is one an operator cannot see is broken.
    RCLCPP_ERROR(
      logger(), "can_interface %s does not exist (%s)", can_interface_.c_str(), std::strerror(errno));
    ::close(descriptor);
    return false;
  }

  struct sockaddr_can address {};
  address.can_family = AF_CAN;
  address.can_ifindex = interface_request.ifr_ifindex;
  if (::bind(descriptor, reinterpret_cast<struct sockaddr *>(&address), sizeof(address)) < 0) {
    RCLCPP_ERROR(
      logger(), "cannot bind CAN socket to %s (%s)", can_interface_.c_str(), std::strerror(errno));
    ::close(descriptor);
    return false;
  }

  // Read side only ever needs state ids; a filter keeps a busy bus from spending the
  // bounded read budget on frames this interface cannot use.
  struct can_filter filter {};
  filter.can_id = arbitration_table_.state;
  filter.can_mask = CAN_SFF_MASK | CAN_EFF_FLAG;
  ::setsockopt(descriptor, SOL_CAN_RAW, CAN_RAW_FILTER, &filter, sizeof(filter));

  can_descriptor_ = descriptor;
  return true;
}

hardware_interface::CallbackReturn CanSystemInterface::on_activate(
  const rclcpp_lifecycle::State & /*previous_state*/)
{
  active_.store(true);
  // Activation starts a fresh staleness window rather than inheriting the configure
  // timestamp: time spent configuring is not time a state frame is fresh for.
  last_state_at_ = std::chrono::steady_clock::now();
  return hardware_interface::CallbackReturn::SUCCESS;
}

hardware_interface::CallbackReturn CanSystemInterface::on_deactivate(
  const rclcpp_lifecycle::State & /*previous_state*/)
{
  active_.store(false);
  return hardware_interface::CallbackReturn::SUCCESS;
}

hardware_interface::CallbackReturn CanSystemInterface::on_cleanup(
  const rclcpp_lifecycle::State & /*previous_state*/)
{
  active_.store(false);
  if (can_descriptor_ >= 0) {
    ::close(can_descriptor_);
    can_descriptor_ = -1;
  }
  return hardware_interface::CallbackReturn::SUCCESS;
}

std::vector<hardware_interface::StateInterface> CanSystemInterface::export_state_interfaces()
{
  std::vector<hardware_interface::StateInterface> interfaces;
  interfaces.reserve(joint_positions_.size());
  for (std::size_t index = 0; index < joint_positions_.size(); ++index) {
    interfaces.emplace_back(
      info_.joints[index].name, hardware_interface::HW_IF_POSITION, &joint_positions_[index]);
  }
  return interfaces;
}

std::vector<hardware_interface::CommandInterface> CanSystemInterface::export_command_interfaces()
{
  std::vector<hardware_interface::CommandInterface> interfaces;
  interfaces.reserve(joint_commands_.size());
  for (std::size_t index = 0; index < joint_commands_.size(); ++index) {
    interfaces.emplace_back(
      info_.joints[index].name, hardware_interface::HW_IF_POSITION, &joint_commands_[index]);
  }
  return interfaces;
}

hardware_interface::return_type CanSystemInterface::read(
  const rclcpp::Time & /*time*/, const rclcpp::Duration & /*period*/)
{
  if (can_descriptor_ < 0) {
    return hardware_interface::return_type::ERROR;
  }

  for (int attempt = 0; attempt < kMaxFramesPerRead; ++attempt) {
    struct can_frame frame {};
    const auto received = ::recv(can_descriptor_, &frame, sizeof(frame), MSG_DONTWAIT);
    if (received < 0) {
      if (errno == EAGAIN || errno == EWOULDBLOCK) {
        break;  // nothing pending; the bounded loop is why a flood cannot starve it
      }
      RCLCPP_ERROR_THROTTLE(
        logger(), throttle_clock(), 1000, "CAN read failed: %s",
        std::strerror(errno));
      return hardware_interface::return_type::ERROR;
    }

    std::vector<std::uint8_t> payload(frame.data, frame.data + frame.can_dlc);
    std::vector<std::int16_t> positions;
    if (!decode_joint_state(arbitration_table_, payload, positions)) {
      continue;  // a malformed or foreign frame is skipped, not guessed at
    }
    for (std::size_t index = 0; index < positions.size() && index < joint_positions_.size();
      ++index)
    {
      joint_positions_[index] = static_cast<double>(positions[index]) / 1000.0;
    }
    last_state_at_ = std::chrono::steady_clock::now();
    state_seen_ = true;
  }
  return hardware_interface::return_type::OK;
}

hardware_interface::return_type CanSystemInterface::write(
  const rclcpp::Time & /*time*/, const rclcpp::Duration & /*period*/)
{
  if (can_descriptor_ < 0) {
    return hardware_interface::return_type::ERROR;
  }

  const double age = std::chrono::duration<double>(
    std::chrono::steady_clock::now() - last_state_at_)
    .count();
  const auto verdict = judge_state_age(age, state_timeout_s_);
  if (!verdict.accept_commands) {
    // The whole point: report, and do NOT repeat the last command. A command nobody
    // re-issued must not resurrect a stopped robot.
    RCLCPP_ERROR_THROTTLE(
      logger(), throttle_clock(), 1000, "refusing to write: %s",
      verdict.reason.c_str());
    return hardware_interface::return_type::ERROR;
  }

  const auto encoded = encode_joint_command(
    arbitration_table_, joint_limits_, joint_commands_, next_sequence_);
  if (!encoded.ok()) {
    // An invalid command is refused rather than clipped, and the refusal names the
    // joint: a silently clipped command is an arm that moved and nobody said why.
    RCLCPP_ERROR_THROTTLE(
      logger(), throttle_clock(), 1000, "refusing command %u: %s",
      static_cast<unsigned>(next_sequence_), encoded.reason.c_str());
    return hardware_interface::return_type::ERROR;
  }

  struct can_frame frame {};
  frame.can_id = arbitration_table_.command;
  frame.can_dlc = static_cast<__u8>(encoded.bytes.size());
  std::memcpy(frame.data, encoded.bytes.data(), encoded.bytes.size());
  if (::write(can_descriptor_, &frame, sizeof(frame)) != static_cast<ssize_t>(sizeof(frame))) {
    RCLCPP_ERROR_THROTTLE(
      logger(), throttle_clock(), 1000, "CAN write failed: %s",
      std::strerror(errno));
    return hardware_interface::return_type::ERROR;
  }

  next_sequence_ = static_cast<std::uint8_t>(next_sequence_ + 1);
  RCLCPP_INFO_THROTTLE(
    logger(), throttle_clock(), 5000,
    "wrote command %u to %s (state age %.3fs)", static_cast<unsigned>(next_sequence_),
    can_interface_.c_str(), age);
  return hardware_interface::return_type::OK;
}

}  // namespace workbench_mobile_base_hardware

#include "pluginlib/class_list_macros.hpp"
PLUGINLIB_EXPORT_CLASS(
  workbench_mobile_base_hardware::CanSystemInterface, hardware_interface::SystemInterface)
