#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
repository=$(cd "$script_dir/.." && pwd -P)
workspace="$repository/ros2_ws"
ros_distro=${ROS_DISTRO:-jazzy}
report="$repository/arm64-${ros_distro}-environment-report.txt"
profile=${RUNTIME_PLATFORM_PROFILE:-generic-arm64}
export RUNTIME_PLATFORM_PROFILE="$profile"
export ROS_DISTRO="$ros_distro"
packages=(
  robot_task_interfaces runtime_can virtual_can_device device_bridge
  task_executor runtime_monitor runtime_history task_orchestrator
  runtime_gateway ai_task_adapter perception_task_adapter
)

[[ -d "$workspace/src" ]] || {
  printf 'Invalid bundle layout; missing workspace source: %s/src\n' "$workspace" >&2
  exit 2
}

"$script_dir/check_arm64_environment.sh" "$report" || {
  printf 'ARM64 environment is not compatible; review this report: %s\n' "$report" >&2
  exit 2
}

set +u
source "/opt/ros/$ros_distro/setup.bash"
set -u
cd "$workspace"

if ! rosdep check --from-paths src --ignore-src --rosdistro "$ros_distro"; then
  printf 'rosdep check failed; review missing dependencies without changing code first.\n' >&2
  exit 3
fi

set -o pipefail
colcon build \
  --packages-select "${packages[@]}" \
  --cmake-force-configure \
  --cmake-args -DBUILD_TESTING=ON \
  2>&1 | tee "$repository/arm64-build.log"

printf 'PASS: ARM64 build completed for profile %s. Next: %s/run_arm64_smoke.sh\n' \
  "$profile" "$script_dir"
