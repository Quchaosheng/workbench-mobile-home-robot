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


#: Where the unit templates live, and the YAML that declares which services exist.
SERVICES_YAML = "bsp/rootfs/services.yaml"
SYSTEMD_DIR = "bsp/rootfs/systemd"

#: YAML `restart:` value -> the systemd `Restart=` value that implements it. A
#: service that says `on-failure` in the YAML and `always` in the unit is two
#: different policies wearing one name (issue #577).
RESTART_MAP = {
    "on-failure": "on-failure",
    "no": "no",
    "always": "always",
}

#: systemd units that are not workbench services and may appear in `After=`
#: without a services.yaml row.
SYSTEMD_ORDERING_TARGETS = frozenset(
    {"network.target", "network-online.target", "local-fs.target", "multi-user.target", "basic.target"}
)


def _unit_fields(unit: str) -> dict[str, list[str]]:
    """Parse the directive lines of a systemd unit into a name -> values map."""
    fields: dict[str, list[str]] = {}
    for line in unit.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        fields.setdefault(name.strip(), []).append(value.strip())
    return fields


def validate_service_topology() -> list[str]:
    """Every YAML service has a unit, and the unit does not contradict the YAML.

    Issue #577: the rootfs declared six services and only the camera had a unit
    template, so five services existed as a name in a YAML file and nothing else.
    This check is the reason a service cannot be added without its unit.
    """
    errors: list[str] = []
    document = yaml.safe_load((ROOT / SERVICES_YAML).read_text(encoding="utf-8"))
    services = document.get("services") or {}
    if not services:
        return ["services.yaml declares no services"]

    for name, spec in sorted(services.items()):
        unit_path = ROOT / SYSTEMD_DIR / f"{name}.service.in"
        if not unit_path.is_file():
            errors.append(f"service {name!r} is declared in services.yaml with no unit template")
            continue
        fields = _unit_fields(unit_path.read_text(encoding="utf-8"))

        declared_restart = str(spec.get("restart", "")).strip()
        expected = RESTART_MAP.get(declared_restart)
        if expected is None:
            errors.append(f"service {name!r} declares restart {declared_restart!r}, which has no systemd mapping")
        else:
            actual = fields.get("Restart", [])
            if actual != [expected]:
                errors.append(
                    f"service {name!r} declares Restart={declared_restart!r} "
                    f"but its unit has Restart={actual or 'unset'}"
                )
            if expected != "no" and ("StartLimitBurst" not in fields or "StartLimitIntervalSec" not in fields):
                errors.append(
                    f"service {name!r} restarts but its unit declares no StartLimitBurst/StartLimitIntervalSec"
                )

        declared_after = {str(item).strip() for item in spec.get("after") or []}
        unit_after: set[str] = set()
        for value in fields.get("After", []):
            unit_after.update(part.strip().removesuffix(".service") for part in value.split())
        undeclared = {item for item in unit_after - declared_after if item not in SYSTEMD_ORDERING_TARGETS}
        if undeclared:
            errors.append(f"service {name!r} orders after {sorted(undeclared)}, which services.yaml does not declare")
        missing = {item for item in declared_after - unit_after if item not in SYSTEMD_ORDERING_TARGETS}
        if missing:
            errors.append(f"service {name!r} declares after {sorted(missing)} but its unit does not order after it")

        if spec.get("safety_authority") is True:
            errors.append(f"service {name!r} claims safety_authority; no rootfs service may")

    for unit_path in sorted((ROOT / SYSTEMD_DIR).glob("*.service.in")):
        name = unit_path.name.removesuffix(".service.in")
        if name not in services:
            errors.append(f"unit {unit_path.name} has no row in services.yaml")

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
    errors.extend(validate_service_topology())
    errors.extend(validate_image_inputs())
    return errors


if __name__ == "__main__":
    failures = validate()
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        raise SystemExit(1)
    print("BSP manifest validation passed")
