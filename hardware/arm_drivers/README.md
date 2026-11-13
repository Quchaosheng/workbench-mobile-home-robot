# arm_drivers

Vendor SDK wrappers → `ros2_control` hardware interface.

One subdirectory per arm model, e.g. `franka/`, `ur/`, `custom/`.

**What to implement**: a `hardware_interface::SystemInterface` subclass that
talks to the real arm SDK and emits the same `action_result` as the Gazebo
simulation. joint limits, safety limits and bring-up sequence go here too.

Cross-compilation notes for ARM boards are not written yet. The BSP page
`docs/architecture/robot-bsp-v0.1.md` lists reproducible cross-compile as an open
deliverable and does not yet carry the instructions, so there is nothing to link
to. Until it does, this directory makes no cross-compilation claim: the vendor SDK
wrapper above is built on the target, not cross-built from a workstation.
