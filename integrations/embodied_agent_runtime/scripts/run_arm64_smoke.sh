#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
repository=$(cd "$script_dir/.." && pwd -P)
workspace="$repository/ros2_ws"
ros_distro=${ROS_DISTRO:-jazzy}
report="$repository/arm64-${ros_distro}-smoke-report.txt"
profile=${RUNTIME_PLATFORM_PROFILE:-generic-arm64}
export RUNTIME_PLATFORM_PROFILE="$profile"
export ROS_DISTRO="$ros_distro"
packages=(
  robot_task_interfaces runtime_can virtual_can_device device_bridge
  task_executor runtime_monitor runtime_history task_orchestrator
  runtime_gateway ai_task_adapter perception_task_adapter
)

case "$profile" in
  generic-arm64|rk3568|x5) ;;
  *)
    printf 'Unsupported ARM64 platform profile: %s\n' "$profile" >&2
    exit 2
    ;;
esac

[[ -f "$workspace/install/setup.bash" ]] || {
  printf 'Missing ARM64 install tree; run build_on_arm64.sh first.\n' >&2
  exit 2
}

set +u
source "/opt/ros/$ros_distro/setup.bash"
source "$workspace/install/setup.bash"
set -u
cd "$workspace"

: > "$report"
printf 'platform_profile=%s\n' "$profile" | tee -a "$report"
set -o pipefail
colcon test-result --test-result-base build --delete-yes >/dev/null 2>&1 || true
test_args=(
  --executor sequential
  --packages-select "${packages[@]}"
  --return-code-on-test-failure
)
if [[ "$ros_distro" == "humble" ]]; then
  printf '%s\n' \
    'Humble smoke excludes uncrustify; canonical formatting is enforced by Jazzy CI.' \
    | tee -a "$report"
  test_args+=(--ctest-args -E uncrustify)
fi
colcon test "${test_args[@]}" 2>&1 | tee -a "$report"
colcon test-result --test-result-base build 2>&1 | tee -a "$report"

printf '\nCAN interfaces:\n' | tee -a "$report"
ip -details link show type can 2>&1 | tee -a "$report" || true

e2e="$repository/scripts/run_industrial_e2e.sh"
if ip link show vcan0 >/dev/null 2>&1 && [[ -x "$e2e" ]]; then
  printf '\nRunning software-only vcan0 E2E.\n' | tee -a "$report"
  WORKSPACE="$workspace" SETUP_VCAN=0 "$e2e" 2>&1 | tee -a "$report"
else
  printf '\nSKIP: vcan0 E2E; vcan0 or executable E2E script is unavailable.\n' | tee -a "$report"
fi

printf 'PASS: ARM64 smoke completed; physical CAN and physical stop are not claimed.\n' |
  tee -a "$report"
