// The SocketCAN `hardware_interface::SystemInterface` declaration (issue #541).
//
// Built only where `hardware_interface` is installed, which is the full-stack image.
// The frame codec this class calls is ROS-free and is tested on any host with a
// compiler; see `joint_frame_codec.hpp`.

#ifndef WORKBENCH_MOBILE_BASE_HARDWARE__CAN_SYSTEM_INTERFACE_HPP_
#define WORKBENCH_MOBILE_BASE_HARDWARE__CAN_SYSTEM_INTERFACE_HPP_

#include <atomic>
#include <chrono>
#include <cstdint>
#include <string>
#include <vector>

#include "hardware_interface/system_interface.hpp"
#include "hardware_interface/handle.hpp"
#include "hardware_interface/hardware_info.hpp"
#include "hardware_interface/types/hardware_interface_return_values.hpp"
#include "rclcpp/macros.hpp"
#include "rclcpp_lifecycle/state.hpp"
#include "workbench_mobile_base_hardware/joint_frame_codec.hpp"

namespace workbench_mobile_base_hardware
{

/// Drives the Revision D joints over SocketCAN.
///
/// The parameter set is the one the issue names: `can_interface`,
/// `arbitration_id_table`, `state_timeout_s`, `command_rate_hz`. A missing
/// `can_interface` or table is an `on_init` error, not a default, because a default
/// destination is how a command reaches a device nobody meant to address.
class CanSystemInterface : public hardware_interface::SystemInterface
{
public:
  RCLCPP_SHARED_PTR_DEFINITIONS(CanSystemInterface)

  hardware_interface::CallbackReturn on_init(
    const hardware_interface::HardwareInfo & info) override;

  hardware_interface::CallbackReturn on_configure(
    const rclcpp_lifecycle::State & previous_state) override;

  hardware_interface::CallbackReturn on_activate(
    const rclcpp_lifecycle::State & previous_state) override;

  hardware_interface::CallbackReturn on_deactivate(
    const rclcpp_lifecycle::State & previous_state) override;

  hardware_interface::CallbackReturn on_cleanup(
    const rclcpp_lifecycle::State & previous_state) override;

  std::vector<hardware_interface::StateInterface> export_state_interfaces() override;

  std::vector<hardware_interface::CommandInterface> export_command_interfaces() override;

  hardware_interface::return_type read(
    const rclcpp::Time & time, const rclcpp::Duration & period) override;

  hardware_interface::return_type write(
    const rclcpp::Time & time, const rclcpp::Duration & period) override;

private:
  bool load_arbitration_table();
  bool open_socket();

  std::string can_interface_;
  std::string arbitration_table_path_;
  double state_timeout_s_{0.2};
  double command_rate_hz_{100.0};

  ArbitrationTable arbitration_table_{};

  std::vector<double> joint_positions_;
  std::vector<double> joint_commands_;
  std::vector<JointLimit> joint_limits_;

  std::chrono::steady_clock::time_point last_state_at_{};
  std::uint8_t next_sequence_{0};
  int can_descriptor_{-1};
  std::atomic<bool> active_{false};
  bool state_seen_{false};
};

}  // namespace workbench_mobile_base_hardware

#endif  // WORKBENCH_MOBILE_BASE_HARDWARE__CAN_SYSTEM_INTERFACE_HPP_
