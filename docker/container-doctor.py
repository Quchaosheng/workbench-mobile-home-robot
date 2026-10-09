#!/usr/bin/env python3
"""Fail-closed, read-only checks for container capability profiles."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any

MATRIX_PATH = Path("/usr/share/workbench/container/gpu-arch-matrix.json")
DEVELOPMENT_MATRIX_PATH = Path(__file__).with_name("gpu-arch-matrix.json")
GPU_PROFILES = {"ros-sim", "gz-gui-x11", "gz-gui-wayland", "mujoco-gpu", "gpu-validation"}
VALID_PROFILES = {"dashboard", *GPU_PROFILES, "hardware-shell"}


def _matrix() -> dict[str, Any]:
    path = MATRIX_PATH if MATRIX_PATH.is_file() else DEVELOPMENT_MATRIX_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def _version(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _nvidia_query() -> tuple[list[dict[str, str]], str | None]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return [], "nvidia-smi is unavailable"
    result = subprocess.run(
        [executable, "--query-gpu=index,uuid,name,driver_version,compute_cap", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return [], result.stderr.strip() or "nvidia-smi failed"
    devices = []
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) == 5:
            devices.append(
                {
                    "index": fields[0],
                    "uuid": fields[1],
                    "name": fields[2],
                    "driver": fields[3],
                    "compute_capability": fields[4],
                }
            )
    return devices, None


def detect_gpu_tier(name: str, compute_capability: str) -> str | None:
    """Require both the physical product name and architecture capability."""
    for tier, generation in _matrix()["generations"].items():
        name_matches = any(pattern.casefold() in name.casefold() for pattern in generation["name_patterns"])
        if name_matches and compute_capability in generation["compute_capabilities"]:
            return tier
    return None


def _gpu_checks(checks: dict[str, Any], failures: list[str]) -> None:
    matrix = _matrix()
    devices, error = _nvidia_query()
    checks["gpu_devices"] = devices
    if error:
        failures.append(error)
        return
    minimum_driver = _version(matrix["minimum_driver_linux"])
    for device in devices:
        if _version(device["driver"]) < minimum_driver:
            failures.append(
                f"GPU {device['index']} driver {device['driver']} is below {matrix['minimum_driver_linux']}"
            )
        device["detected_tier"] = detect_gpu_tier(device["name"], device["compute_capability"]) or "unsupported"
    requested = os.environ.get("WORKBENCH_GPU_TIER", "auto")
    valid_tiers = set(matrix["generations"])
    if requested not in {"auto", *valid_tiers}:
        failures.append(f"WORKBENCH_GPU_TIER must be auto or one of {sorted(valid_tiers)}")
    detected = sorted({device["detected_tier"] for device in devices if device["detected_tier"] != "unsupported"})
    checks["detected_gpu_tiers"] = detected
    if not detected:
        failures.append("no physical GeForce RTX 30/40/50 GPU matches both name and compute capability")
    if requested != "auto" and requested not in detected:
        failures.append(f"requested physical GPU tier {requested} is not present")

    vendor_dir = Path("/usr/share/glvnd/egl_vendor.d")
    vendor_files = sorted(str(path) for path in vendor_dir.glob("*.json") if path.is_file())
    checks["egl_vendor_files"] = vendor_files
    nvidia_vendor = []
    for filename in vendor_files:
        try:
            text = Path(filename).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "nvidia" in text.casefold() or "libEGL_nvidia" in text:
            nvidia_vendor.append(filename)
    checks["nvidia_egl_vendor_files"] = nvidia_vendor
    if not nvidia_vendor:
        failures.append("NVIDIA EGL vendor JSON is unavailable; GPU rendering cannot be validated")


def _hardware_checks(checks: dict[str, Any], failures: list[str]) -> None:
    serial_spec = os.environ.get("WORKBENCH_SERIAL_DEVICE", "")
    serial_path = Path(os.environ.get("WORKBENCH_SERIAL_CONTAINER_PATH", "/dev/workbench-serial"))
    if not serial_spec.startswith("/dev/serial/by-id/") or any(character in serial_spec for character in "*?[]"):
        failures.append("WORKBENCH_SERIAL_DEVICE must be one explicit /dev/serial/by-id path")
    try:
        is_char_device = stat.S_ISCHR(serial_path.stat().st_mode)
    except OSError:
        is_char_device = False
    checks["serial_container_path"] = str(serial_path)
    checks["serial_is_char_device"] = is_char_device
    if not is_char_device:
        failures.append(f"mapped serial path {serial_path} is not a character device")

    for variable in ("WORKBENCH_CAN_INTERFACE", "WORKBENCH_DDS_INTERFACE"):
        interface = os.environ.get(variable, "")
        try:
            socket.if_nametoindex(interface)
        except OSError:
            failures.append(f"{variable} does not name an existing host interface")
    # rcl enables security only when the value matches the literal "true"
    # (rcl_security_enabled compares with strcmp).  Any other spelling such as
    # "1" is compared as false and silently disables security, so the doctor
    # must reject it instead of reporting a healthy Enforce profile.
    security_enable = os.environ.get("ROS_SECURITY_ENABLE", "")
    checks["ros_security_enable"] = security_enable
    if security_enable != "true" or os.environ.get("ROS_SECURITY_STRATEGY") != "Enforce":
        failures.append('SROS2 Enforce requires ROS_SECURITY_ENABLE="true" and ROS_SECURITY_STRATEGY="Enforce"')
    # rcl treats a leading "1" as the deprecated localhost-only flag, and that
    # flag makes it ignore ROS_AUTOMATIC_DISCOVERY_RANGE entirely.  A LAN profile
    # that inherits it would silently stop discovering peers off-host.
    if os.environ.get("ROS_LOCALHOST_ONLY", "").startswith("1"):
        failures.append(
            "ROS_LOCALHOST_ONLY=1 overrides ROS_AUTOMATIC_DISCOVERY_RANGE; "
            "unset it and use the discovery range variable"
        )
    keystore = Path(os.environ.get("WORKBENCH_SROS2_KEYSTORE", ""))
    checks["sros2_keystore"] = str(keystore)
    if not keystore.is_dir() or not os.access(keystore, os.R_OK):
        failures.append("a readable external SROS2 keystore directory is required")


def check_profile(profile: str) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "schema_version": "workbench-container-doctor-v1",
        "profile": profile,
        "architecture": os.uname().machine if hasattr(os, "uname") else "unknown",
        "ros_distro": os.environ.get("ROS_DISTRO"),
        "cuda_baseline": "12.8",
        "image_digest": os.environ.get("WORKBENCH_IMAGE_DIGEST", "local-unpinned"),
        "commit": os.environ.get("WORKBENCH_COMMIT", "unknown"),
        "ros2": shutil.which("ros2"),
        "gz": shutil.which("gz"),
        "status": "PASS",
        "reason": None,
    }
    failures: list[str] = []
    if profile not in VALID_PROFILES:
        failures.append(f"unknown container profile: {profile}")
    if checks["architecture"] != "x86_64":
        failures.append("linux/amd64 is required")
    if profile in {"ros-sim", "gz-gui-x11", "gz-gui-wayland"}:
        if checks["ros_distro"] != "jazzy" or not checks["ros2"]:
            failures.append("ROS 2 Jazzy is required")
        if not checks["gz"]:
            failures.append("Gazebo Harmonic is required")
    if profile in GPU_PROFILES:
        _gpu_checks(checks, failures)
    if profile == "mujoco-gpu" and os.environ.get("MUJOCO_GL") != "egl":
        failures.append("MUJOCO_GL=egl is required")
    if profile == "gz-gui-x11":
        authority = Path(os.environ.get("XAUTHORITY", ""))
        if not os.environ.get("DISPLAY") or not authority.is_file():
            failures.append("DISPLAY and a regular read-only Xauthority cookie are required")
    if profile == "gz-gui-wayland":
        display = os.environ.get("WAYLAND_DISPLAY", "")
        socket_path = Path(os.environ.get("XDG_RUNTIME_DIR", "")) / display
        try:
            is_socket = stat.S_ISSOCK(socket_path.stat().st_mode)
        except OSError:
            is_socket = False
        if not display or not is_socket:
            failures.append("the mapped Wayland display must be a socket")
    if profile == "hardware-shell":
        _hardware_checks(checks, failures)
    if failures:
        checks["status"] = "NOT_EXECUTED"
        checks["reason"] = "; ".join(failures)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="dashboard")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = check_profile(args.profile)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
