#!/usr/bin/env bash
set -Eeuo pipefail

mode=${1:-Check}
repository=$(pwd -P)
issues=()

record() {
  printf '%s\n' "$*"
}

require_command() {
  local command_name=$1
  if command -v "$command_name" >/dev/null 2>&1; then
    record "tool.$command_name=$(command -v "$command_name")"
  else
    record "tool.$command_name=MISSING"
    issues+=("missing command: $command_name")
  fi
}

case "$mode" in
  Check|BuildTest) ;;
  *)
    printf 'Unsupported mode: %s\n' "$mode" >&2
    exit 2
    ;;
esac

os_id=unknown
os_version=unknown
if [[ -r /etc/os-release ]]; then
  os_id=$(bash -c '. /etc/os-release; printf "%s" "${ID:-unknown}"')
  os_version=$(bash -c '. /etc/os-release; printf "%s" "${VERSION_ID:-unknown}"')
fi
architecture=$(uname -m)

record "distribution=${WSL_DISTRO_NAME:-unknown}"
record "os=$os_id"
record "os_version=$os_version"
record "architecture=$architecture"
record "repository=$repository"

[[ "$os_id" == ubuntu && "$os_version" == 24.04 ]] ||
  issues+=("WSL baseline requires Ubuntu 24.04")
case "$architecture" in
  x86_64|aarch64|arm64) ;;
  *) issues+=("unsupported architecture: $architecture") ;;
esac
[[ -f /opt/ros/jazzy/setup.bash ]] || issues+=("ROS 2 Jazzy not found")
[[ -d "$repository/ros2_ws/src" ]] || issues+=("repository layout is missing ros2_ws/src")

for command_name in bash cmake c++ colcon dpkg-query python3 rosdep pkg-config; do
  require_command "$command_name"
done

packages=(
  build-essential can-utils cmake libgrpc++-dev libopencv-dev
  libprotobuf-dev libprotoc-dev libsqlite3-dev pkg-config
  protobuf-compiler protobuf-compiler-grpc python3-colcon-common-extensions
  python3-rosdep ros-jazzy-behaviortree-cpp
)
for package in "${packages[@]}"; do
  version=$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null || true)
  if [[ -n "$version" ]]; then
    record "package.$package=$version"
  else
    record "package.$package=MISSING"
    issues+=("missing package: $package")
  fi
done

if ((${#issues[@]})); then
  record 'compatible=false'
  for issue in "${issues[@]}"; do
    record "issue=$issue"
  done
  record 'NEXT: install the reported WSL dependencies, then rerun Check mode'
  exit 2
fi

record 'compatible=true'
[[ "$mode" == BuildTest ]] || exit 0

set +u
source /opt/ros/jazzy/setup.bash
set -u

native_root="$HOME/.cache/embodied-agent-runtime-wsl"
workspace="$native_root/ros2_ws"
isolation="$native_root/.colcon/windows-wsl"
build="$isolation/build"
install="$isolation/install"
log="$isolation/log"
runtime_packages=(
  robot_task_interfaces runtime_can virtual_can_device device_bridge
  task_executor runtime_monitor runtime_history task_orchestrator
  runtime_gateway ai_task_adapter perception_task_adapter
)

rm -rf "$native_root"
mkdir -p "$workspace"
cp -a "$repository/ros2_ws/src" "$workspace/src"
mkdir -p "$build" "$install" "$log"
cd "$workspace"
record "workspace=$workspace"
if ! rosdep check --from-paths src --ignore-src --rosdistro jazzy; then
  printf '%s\n' 'NEXT: install the reported dependencies before rerunning BuildTest mode' >&2
  exit 3
fi

colcon --log-base "$log" build \
  --build-base "$build" \
  --install-base "$install" \
  --packages-select "${runtime_packages[@]}" \
  --cmake-args -DBUILD_TESTING=ON

set +u
source "$install/setup.bash"
set -u
export PYTHONPATH="/opt/ros/jazzy/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}"
colcon --log-base "$log" test \
  --executor sequential \
  --build-base "$build" \
  --install-base "$install" \
  --packages-select "${runtime_packages[@]}" \
  --return-code-on-test-failure
colcon test-result --test-result-base "$build" --verbose
printf '%s\n' 'PASS: Windows WSL2 Jazzy build and test completed'
