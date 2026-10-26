#!/usr/bin/env bash
set -Eeuo pipefail

report=${1:-arm64-environment-report.txt}
profile=${RUNTIME_PLATFORM_PROFILE:-generic-arm64}
ros_distro=${ROS_DISTRO:-jazzy}
mkdir -p "$(dirname "$report")"
: > "$report"
issues=()

record() {
  printf '%s\n' "$*" | tee -a "$report"
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

arch=$(uname -m)
record "checked_at=$(date --iso-8601=seconds)"
record "platform_profile=$profile"
record "ros_distro=$ros_distro"
record "architecture=$arch"
record "kernel=$(uname -sr)"
record "memory=$(free -h | awk '/^Mem:/ {print $2}')"
record "root_available=$(df -h / | awk 'NR==2 {print $4}')"

os_id=unknown
os_version=unknown
if [[ -r /etc/os-release ]]; then
  os_id=$(bash -c '. /etc/os-release; printf "%s" "$ID"')
  os_version=$(bash -c '. /etc/os-release; printf "%s" "$VERSION_ID"')
fi
record "os=$os_id"
record "os_version=$os_version"

case "$profile" in
  generic-arm64|rk3568|x5) ;;
  *) issues+=("unsupported platform profile: $profile") ;;
esac

required_ubuntu=unknown
case "$ros_distro" in
  jazzy) required_ubuntu=24.04 ;;
  humble) required_ubuntu=22.04 ;;
  *) issues+=("unsupported ROS distribution: $ros_distro") ;;
esac
record "required_ubuntu=$required_ubuntu"

[[ "$arch" == aarch64 || "$arch" == arm64 ]] || issues+=("expected ARM64, got $arch")
if [[ "$required_ubuntu" != unknown ]]; then
  [[ "$os_id" == ubuntu && "$os_version" == "$required_ubuntu" ]] ||
    issues+=("ROS $ros_distro requires Ubuntu $required_ubuntu")
fi

if [[ -f "/opt/ros/$ros_distro/setup.bash" ]]; then
  record "ros=$ros_distro"
else
  installed_ros=$(find /opt/ros -mindepth 1 -maxdepth 1 -type d -printf '%f ' 2>/dev/null || true)
  record 'ros=MISSING'
  record "installed_ros=${installed_ros:-MISSING}"
  issues+=("ROS 2 $ros_distro not found")
fi

for command_name in colcon cmake c++ python3 rosdep pkg-config ip candump; do
  require_command "$command_name"
done

for package in \
  "ros-$ros_distro-behaviortree-cpp" \
  libgrpc++-dev \
  libprotobuf-dev \
  libprotoc-dev \
  protobuf-compiler \
  protobuf-compiler-grpc \
  libsqlite3-dev \
  libopencv-dev
do
  version=$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null || true)
  if [[ -n "$version" ]]; then
    record "package.$package=$version"
  else
    record "package.$package=MISSING"
    issues+=("missing package: $package")
  fi
done
opencv_version=$(pkg-config --modversion opencv4 2>/dev/null || true)
if [[ -n "$opencv_version" ]]; then
  record "opencv=$opencv_version"
else
  record "opencv=MISSING"
  issues+=("OpenCV 4 pkg-config metadata not found")
fi


record "can_interfaces_begin"
ip -details link show type can 2>/dev/null | tee -a "$report" || true
record "can_interfaces_end"

if ((${#issues[@]})); then
  record "compatible=false"
  for issue in "${issues[@]}"; do
    record "issue=$issue"
  done
  record "NEXT: review this ARM64 environment report before changing the system"
  exit 2
fi

record "compatible=true"
record "NEXT: run repository/scripts/build_on_arm64.sh"
