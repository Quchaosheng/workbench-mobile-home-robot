#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
checker="$script_dir/check_arm64_environment.sh"
temp_root=$(mktemp -d)
trap 'rm -rf "$temp_root"' EXIT

run_expected_failure() {
  local report=$1
  shift
  set +e
  env "$@" "$checker" "$report" >/dev/null 2>&1
  local status=$?
  set -e
  [[ $status -ne 0 ]] || {
    printf 'Expected ARM64 checker failure for: %s\n' "$*" >&2
    exit 1
  }
}

rk_report="$temp_root/rk3568-humble.txt"
run_expected_failure "$rk_report" \
  RUNTIME_PLATFORM_PROFILE=rk3568 ROS_DISTRO=humble
grep -q '^platform_profile=rk3568$' "$rk_report" || {
  printf 'RK3568 profile was not recorded.\n' >&2
  exit 1
}
grep -q '^ros_distro=humble$' "$rk_report" || {
  printf 'Humble distribution was not recorded.\n' >&2
  exit 1
}
if grep -q '^issue=unsupported platform profile:' "$rk_report"; then
  printf 'RK3568 profile was incorrectly rejected.\n' >&2
  exit 1
fi
if grep -q '^issue=unsupported ROS distribution:' "$rk_report"; then
  printf 'Humble distribution was incorrectly rejected.\n' >&2
  exit 1
fi

bad_profile_report="$temp_root/bad-profile.txt"
run_expected_failure "$bad_profile_report" \
  RUNTIME_PLATFORM_PROFILE=unknown ROS_DISTRO=jazzy
grep -q '^issue=unsupported platform profile: unknown$' "$bad_profile_report" || {
  printf 'Unknown profile was not rejected.\n' >&2
  exit 1
}

bad_distro_report="$temp_root/bad-distro.txt"
run_expected_failure "$bad_distro_report" \
  RUNTIME_PLATFORM_PROFILE=generic-arm64 ROS_DISTRO=iron
grep -q '^issue=unsupported ROS distribution: iron$' "$bad_distro_report" || {
  printf 'Unknown ROS distribution was not rejected.\n' >&2
  exit 1
}

printf 'PASS: ARM64 configuration behavior\n'
