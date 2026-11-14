# mobile_base

Mobile base integration. The verifier contract generalises to navigation goals,
so adding a mobile base doesn't require changing the verification layer.

Planned for v0.4. The verifier will check "robot reached goal pose within
tolerance, with confirmed localisation confidence."

Subdirectories:
- `maps/`           the committed occupancy map and its revision
- `hardware/`       the SocketCAN `ros2_control` interface (see below)
- `description/`    base URDF / TF (not present yet)
- `control/`        Nav2 integration (not present yet)
- `bringup/`        launch files (not present yet)

## hardware

`hardware/workbench_mobile_base_hardware/` is one
`hardware_interface::SystemInterface` that binds the same joint names the Gazebo
plugin binds, encodes them into frames
`interfaces/json_schema/mcu_protocol.schema.json` defines, and fails closed when
the bus is down, the arbitration id table is unset, or a state frame ages out.

The package has two layers on purpose:

- the **frame codec** (`joint_frame_codec`) is ROS-free, so its tests run with a
  C++ compiler alone. That is where the refusals live: an unset arbitration table,
  an out-of-range command, too many joints for a Classic CAN frame, a stale state.
- the **plugin** (`can_system_interface`) needs `hardware_interface`, which only the
  full-stack image carries, so it is built and tested there.

**Hardware status is `NOT_EXECUTED`.** Nothing in this package proves a frame
reached an MCU, that a joint moved, or that a stop was physically achieved. It
proves a command was valid and was handed to a bus. Every evidence table keeps the
hardware and HIL columns `NOT_EXECUTED` until a bring-up record exists.

The plugin parameter set is `can_interface`, `arbitration_id_table`,
`state_timeout_s` and `command_rate_hz`. A missing `can_interface` or table is a
configure error, never a default: a default destination is how a command reaches a
device nobody meant to address.
