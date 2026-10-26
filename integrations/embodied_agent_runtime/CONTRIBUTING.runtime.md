# Contributing

## Development Environment

The primary development host may be Windows. Use Windows for editing, Git, and
GitHub operations, and use WSL2 Ubuntu 24.04 with ROS 2 Jazzy for Linux runtime
builds and tests. Native Ubuntu 24.04 is equivalent. The runtime and its Bash
scripts are not validated as native Windows executables.

Do not share `build`, `install`, or `log` trees between Windows, WSL2, x86_64
Linux, and ARM64. Remove or isolate those trees when changing environments.

Check the Windows/WSL2 environment from the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\windows_wsl.ps1 -Mode Check
```

Run the complete eleven-package WSL2 build and test in isolated directories:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\windows_wsl.ps1 -Mode BuildTest
```

The tool reports missing prerequisites but does not install them. Build output
is copied into the WSL-native `$HOME/.cache/embodied-agent-runtime-wsl` tree,
separate from the Windows source and ARM64 workspace trees.

## Build and Test

From an Ubuntu 24.04 or WSL2 Ubuntu 24.04 shell:

```bash
set +u
source /opt/ros/jazzy/setup.bash
set -u
cd ros2_ws
rosdep install --from-paths src --ignore-src --rosdistro jazzy -y
colcon build --cmake-args -DBUILD_TESTING=ON
colcon test --return-code-on-test-failure
colcon test-result --test-result-base build --verbose
```

Run shell syntax checks when changing scripts:

```bash
find scripts -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n
```

Run the PowerShell behavior test when changing the Windows entry point:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\test_windows_wsl.ps1
```

On an ARM64 target, run `scripts/check_arm64_environment.sh`, then
`scripts/build_on_arm64.sh` and `scripts/run_arm64_smoke.sh`. The default
profile is `generic-arm64`; `rk3568` is a CPU-only generic alias. Set
`RUNTIME_PLATFORM_PROFILE=x5` only for the optional X5 intent profile. These
selections do not install vendor runtimes or prove board compatibility.

Use the default Jazzy/Ubuntu 24.04 pair unless the target image requires
Humble/Ubuntu 22.04:

```bash
RUNTIME_PLATFORM_PROFILE=rk3568 ROS_DISTRO=humble \
  bash scripts/check_arm64_environment.sh
RUNTIME_PLATFORM_PROFILE=rk3568 ROS_DISTRO=humble \
  bash scripts/build_on_arm64.sh
RUNTIME_PLATFORM_PROFILE=rk3568 ROS_DISTRO=humble \
  bash scripts/run_arm64_smoke.sh
```

## Change Rules

- Preserve `ExecuteWorkflow` as the external workflow boundary.
- Keep device commands behind the Task Executor and Device Bridge boundaries.
- Do not bypass validation, bounded deadlines, cancellation, or diagnostics.
- Keep perception adapters independent of Device Bridge, SocketCAN, and direct
  task execution.
- Add dependencies through package manifests and `rosdep`, and update
  `THIRD_PARTY_NOTICES.md` when a direct dependency changes.
- Never commit credentials, private paths, target logs, or generated build
  trees.

## Pull Requests

Describe the behavior changed, the failure or safety case considered, and the
commands used for verification. Keep claims within the evidence actually
collected. Software `vcan0`, generated images, and fake Action servers do not
prove physical CAN, cameras, actuators, stopping, ARM64/X5, BPU/NPU, or GPIO.

GitHub Actions must pass before merge. Hardware-facing changes also need
separate target evidence with secrets and site-specific information removed.
