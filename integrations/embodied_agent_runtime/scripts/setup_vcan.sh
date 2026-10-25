#!/usr/bin/env bash
set -euo pipefail

interface_name="${1:-vcan0}"

if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'vcan requires Linux. Use Ubuntu, supported WSL2, or a suitable Docker host.\n'
  exit 1
fi

if ip link show "$interface_name" >/dev/null 2>&1; then
  sudo ip link set "$interface_name" up
  printf '%s already exists and is up.\n' "$interface_name"
  exit 0
fi

sudo modprobe vcan
sudo ip link add dev "$interface_name" type vcan
sudo ip link set "$interface_name" up
ip -details link show "$interface_name"

printf 'Use "candump %s" in one terminal and "cansend %s 100#01020304" in another.\n' \
  "$interface_name" "$interface_name"
