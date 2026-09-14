"""Validate the repository-side robot BSP manifests."""

from __future__ import annotations

import csv
from pathlib import Path

import yaml

try:
    from bsp.validation.validate_image_inputs import validate as validate_image_inputs
except ModuleNotFoundError:  # Direct script execution from bsp/validation.
    from validate_image_inputs import validate as validate_image_inputs

ROOT = Path(__file__).resolve().parents[2]


def validate_camera_manifest(camera: dict) -> list[str]:
    errors: list[str] = []
    if camera.get("quantity_per_robot") != 1 or camera.get("selection", {}).get("model") != "D435":
        errors.append("prototype BSP requires exactly one D435 head camera")
    if camera.get("selection", {}).get("interface") != "USB_3":
        errors.append("head RGB-D camera must use the selected USB 3 boundary")
    identity = camera.get("device_identity", {})
    if identity.get("method") != "librealsense_serial":
        errors.append("D435 identity must use the librealsense serial selector")
    if identity.get("single_video_device_symlink") != "prohibited_multi_interface_device":
        errors.append("D435 must not use one shared video-device symlink")
    if camera.get("boundaries", {}).get("physical_evidence_ready") is not False:
        errors.append("camera physical evidence must remain false before calibration and bring-up")
    return errors


def validate_camera_deployment(deployment: dict, unit: str, launcher: str, environment_example: str) -> list[str]:
    errors: list[str] = []
    unresolved = "TBD_" in yaml.safe_dump(deployment)
    if unresolved and deployment.get("installable") is not False:
        errors.append("camera deployment with unresolved selections must not be installable")
    if deployment.get("identity", {}).get("selector") != "librealsense_serial":
        errors.append("camera deployment must bind the D435 by librealsense serial")
    if deployment.get("identity", {}).get("single_video_device_symlink") != "prohibited":
        errors.append("camera deployment must prohibit a shared video-device symlink")
    if deployment.get("service", {}).get("safety_authority") is not False:
        errors.append("camera service must not own safety authority")
    if "ExecStart=/usr/libexec/workbench/camera-head-launch" not in unit:
        errors.append("camera systemd unit must use the guarded launcher")
    if 'ExecStartPre=/usr/bin/test "${D435_SERIAL}" != "TBD_FROM_PURCHASED_UNIT"' not in unit:
        errors.append("camera systemd unit must reject the placeholder serial")
    if "serial_no:=${D435_SERIAL}" not in launcher:
        errors.append("camera launcher must pass the frozen serial to the ROS driver")
    if "/opt/ros/${ROS_DISTRO}/setup.sh" not in launcher:
        errors.append("camera launcher must load the selected ROS environment")
    required_placeholders = {
        "D435_SERIAL=TBD_FROM_PURCHASED_UNIT",
        "ROS_DISTRO=TBD_COMPATIBILITY_SELECTION",
    }
    if not required_placeholders.issubset(environment_example.splitlines()):
        errors.append("camera environment example must remain explicitly unresolved")
    return errors


def validate_manifests(manifest: dict, compatibility: dict, bringup: dict) -> list[str]:
    errors: list[str] = []

    if manifest["linux_board"].get("model") != "NVIDIA_JETSON_ORIN_NANO_SUPER_DEVELOPER_KIT_8GB":
        errors.append("unexpected Linux board selection")
    controllers = manifest.get("controllers", [])
    if len(controllers) != 6:
        errors.append("exactly six controller domains are required")
    ids = [controller.get("id") for controller in controllers]
    node_ids = [controller.get("node_id") for controller in controllers]
    if len(set(ids)) != len(ids):
        errors.append("controller domain IDs must be unique")
    if len(set(node_ids)) != len(node_ids):
        errors.append("controller node IDs must be unique")
    safety = next((controller for controller in controllers if controller.get("id") == "MCU-SAFETY"), None)
    if not safety or safety.get("independent_reset_domain") is not True:
        errors.append("MCU-SAFETY must have an independent reset domain")
    if set(compatibility.get("controllers", {})) != set(ids):
        errors.append("firmware compatibility domains do not match board domains")
    if bringup.get("status") != "plan_not_executed":
        errors.append("bring-up status must remain plan_not_executed")
    if any(test.get("result") != "NOT_EXECUTED" for test in bringup.get("tests", [])):
        errors.append("physical bring-up results must remain NOT_EXECUTED")
    return errors


def validate() -> list[str]:
    manifest = yaml.safe_load((ROOT / "bsp/board-manifest.yaml").read_text(encoding="utf-8"))
    compatibility = yaml.safe_load((ROOT / "bsp/firmware/compatibility.yaml").read_text(encoding="utf-8"))
    bringup = yaml.safe_load((ROOT / "bsp/validation/bringup-plan.yaml").read_text(encoding="utf-8"))
    errors = validate_manifests(manifest, compatibility, bringup)
    kernel_config = (ROOT / "bsp/linux/robot_bsp.config").read_text(encoding="utf-8")
    required_symbols = {
        "CONFIG_CAN=y",
        "CONFIG_CAN_RAW=y",
        "CONFIG_CAN_DEV=y",
        "CONFIG_CAN_FD_MODE=y",
        "CONFIG_CAN_PEAK_USB=m",
        "CONFIG_WATCHDOG=y",
        "CONFIG_PSTORE=y",
        "CONFIG_VIDEO_DEV=y",
        "CONFIG_VIDEO_V4L2=y",
        "CONFIG_USB_VIDEO_CLASS=y",
        "CONFIG_BLK_DEV_NVME=y",
    }
    missing_symbols = sorted(symbol for symbol in required_symbols if symbol not in kernel_config.splitlines())
    if missing_symbols:
        errors.append(f"kernel config missing required symbols: {', '.join(missing_symbols)}")
    services = yaml.safe_load((ROOT / "bsp/rootfs/services.yaml").read_text(encoding="utf-8"))
    if services.get("default_motion_state") != "inhibited":
        errors.append("rootfs must boot with motion inhibited")
    if any(service.get("safety_authority") is not False for service in services.get("services", {}).values()):
        errors.append("no Linux service may own safety authority")
    with (ROOT / "bsp/procurement/selection-register.csv").open(newline="", encoding="utf-8") as handle:
        selections = list(csv.DictReader(handle))
    if len(selections) != 8 or len({selection["item_id"] for selection in selections}) != 8:
        errors.append("procurement register must contain eight unique BSP selections")
    if any(not selection["required_closure"].strip() for selection in selections):
        errors.append("every BSP selection requires an explicit closure action")
    camera = yaml.safe_load((ROOT / "bsp/sensors/camera-head.yaml").read_text(encoding="utf-8"))
    errors.extend(validate_camera_manifest(camera))
    deployment = yaml.safe_load((ROOT / "bsp/rootfs/camera-head-deployment.yaml").read_text(encoding="utf-8"))
    unit = (ROOT / "bsp/rootfs/systemd/robot-camera-head.service.in").read_text(encoding="utf-8")
    launcher = (ROOT / "bsp/rootfs/libexec/camera-head-launch").read_text(encoding="utf-8")
    environment_example = (ROOT / "bsp/rootfs/camera-head.env.example").read_text(encoding="utf-8")
    errors.extend(validate_camera_deployment(deployment, unit, launcher, environment_example))
    errors.extend(validate_image_inputs())
    return errors


if __name__ == "__main__":
    failures = validate()
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        raise SystemExit(1)
    print("BSP manifest validation passed")
