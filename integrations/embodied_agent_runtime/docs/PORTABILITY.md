# Portability

The runtime is split into portable policy code and ROS 2 transport adapters.
Keep validation, task-event construction, and state decisions independent of
the device bridge so they can be exercised on a host without robot hardware.

- Linux ROS 2: primary supported runtime and integration-test environment.
- WSL2: supported for Docker and host-side runtime tests; hardware access must
  be provided explicitly by the WSL environment.
- Windows and macOS: suitable for Python tooling, configuration validation,
  and documentation checks, but not native ROS 2 device execution.

Porting work should add a host-side test first, then an adapter-specific test;
never silently replace a missing device bridge with a fake success response.
