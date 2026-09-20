#!/usr/bin/env python3
"""Validate an expanded Revision D URDF against hardware/mechanical/design-spec.json.

Usage:
    xacro robot/description/revision_d/revision_d.urdf.xacro > /tmp/revision_d.urdf
    python3 robot/description/revision_d/tools/validate_description.py /tmp/revision_d.urdf

With no argument the model is expanded in-process, so the checker is also useful
as a single command in CI. Exit code is the number of failed checks, capped at
120, and every failure prints the design-spec pointer it came from.

The checks are structural and geometric. Passing means the model is internally
consistent and consistent with the mechanical spec. It does not mean the robot
has been built, that a controller is tuned, or that any motion is safe: hardware
and HIL status stay NOT_EXECUTED.
"""

from __future__ import annotations

import argparse
import math
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import description_model as model

TOLERANCE_M = 1e-6
TOLERANCE_RAD = 1e-9


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    checks: int = 0

    def check(self, condition: bool, message: str) -> bool:
        self.checks += 1
        if not condition:
            self.failures.append(message)
        return condition


def _spec_pointer(pointer: str) -> str:
    return f"design-spec.json#{pointer}"


def validate(root: ET.Element) -> Report:
    report = Report()
    spec = model.load_spec()
    poses = model.forward_kinematics(root)
    limits = model.joint_limits(root)
    collisions = model.collision_geometry(root)

    # ---------------------------------------------------------------- structure
    report.check(
        model.root_links(root) == ["map"],
        f"the tree must have exactly one root and it must be map, got {model.root_links(root)}",
    )
    names = model.link_names(root)
    report.check(len(names) == len(set(names)), "duplicate link names in the description")
    joints = model.joint_names(root)
    report.check(len(joints) == len(set(joints)), "duplicate joint names in the description")

    # ------------------------------------------------------- manipulator axes
    arm_joints = [name for name in joints if name.startswith("arm_") and "_joint_" in name]
    report.check(
        len(arm_joints) == 14,
        f"{_spec_pointer('/manipulator/total_revolute_axes')} requires exactly 14 manipulator "
        f"revolute joints, got {len(arm_joints)}",
    )
    for name in model.arm_joint_names():
        report.check(name in limits, f"missing manipulator joint {name}")
        if name not in limits:
            continue
        entry = limits[name]
        report.check(
            entry["type"] == "revolute",
            f"{name} must be revolute, got {entry['type']}",
        )
        report.check(
            entry["lower"] < entry["upper"],
            f"{name} has reversed limits {entry['lower']}..{entry['upper']}",
        )
    for side in model.ARM_SIDES:
        report.check(
            f"arm_{side}_tool0" in names,
            f"arm_{side} has no tool0 flange frame",
        )

    manipulator = spec["manipulator"]["joints"]
    for index, entry in enumerate(manipulator, start=1):
        suffix = model.ARM_JOINT_SUFFIXES[index - 1]
        lower_deg, upper_deg = entry["limits_deg"]
        for side in model.ARM_SIDES:
            name = f"arm_{side}_{suffix}"
            if name not in limits:
                continue
            actual = limits[name]
            report.check(
                math.isclose(actual["lower"], math.radians(lower_deg), abs_tol=TOLERANCE_RAD),
                f"{name} lower limit {actual['lower']:.9f} rad is not the documented "
                f"{lower_deg} deg from {_spec_pointer(f'/manipulator/joints/{index}/limits_deg')}",
            )
            report.check(
                math.isclose(actual["upper"], math.radians(upper_deg), abs_tol=TOLERANCE_RAD),
                f"{name} upper limit {actual['upper']:.9f} rad is not the documented "
                f"{upper_deg} deg from {_spec_pointer(f'/manipulator/joints/{index}/limits_deg')}",
            )
            _continuous, peak = entry["continuous_peak_torque_nm"]
            report.check(
                math.isclose(actual["effort"], float(peak), abs_tol=1e-9),
                f"{name} effort {actual['effort']} Nm is not the documented peak "
                f"{peak} Nm from {_spec_pointer(f'/manipulator/joints/{index}/continuous_peak_torque_nm')}",
            )

    # ------------------------------------------------------- base and drivetrain
    chassis = spec["chassis"]
    report.check(
        math.isclose(
            float(limits["lift_joint"]["upper"]),
            spec["lifting_platform"]["travel"] / 1000.0,
            abs_tol=TOLERANCE_M,
        ),
        f"lift_joint travel {limits['lift_joint']['upper']} m is not the documented "
        f"{spec['lifting_platform']['travel']} mm from {_spec_pointer('/lifting_platform/travel')}",
    )
    report.check(
        math.isclose(
            float(limits["lift_joint"]["lower"]),
            model.LIFT_JOINT_MIN_Z,
            abs_tol=TOLERANCE_M,
        ),
        f"lift_joint lower must be {model.LIFT_JOINT_MIN_Z} m, got {limits['lift_joint']['lower']}",
    )

    for name in model.STEER_DRIVE_MODULES:
        report.check(
            f"{name}_steering_joint" in limits,
            f"drive module {name} has no steering joint",
        )
        report.check(
            f"{name}_drive_joint" in limits,
            f"drive module {name} has no drive joint",
        )
    report.check(
        len(model.STEER_DRIVE_MODULES) == chassis["drive_module_count"],
        f"{_spec_pointer('/chassis/drive_module_count')} says "
        f"{chassis['drive_module_count']} modules, the model has "
        f"{len(model.STEER_DRIVE_MODULES)}",
    )

    centres = model.wheel_centres(poses)
    half_track = chassis["track"] / 2000.0
    half_wheelbase = chassis["wheelbase"] / 2000.0
    wheel_radius = chassis["wheel_diameter_mm"] / 2000.0
    expected_corners = {
        "drive_module_front_left": (-half_track, -half_wheelbase),
        "drive_module_front_right": (half_track, -half_wheelbase),
        "drive_module_rear_left": (-half_track, half_wheelbase),
        "drive_module_rear_right": (half_track, half_wheelbase),
    }
    for name, (ex, ey) in expected_corners.items():
        cx, cy, cz = centres[name]
        report.check(
            math.isclose(cx, ex, abs_tol=TOLERANCE_M) and math.isclose(cy, ey, abs_tol=TOLERANCE_M),
            f"{name} wheel centre ({cx:.6f}, {cy:.6f}) is not the documented track/wheelbase "
            f"corner ({ex:.6f}, {ey:.6f}) from "
            f"{_spec_pointer('/chassis/track')} and {_spec_pointer('/chassis/wheelbase')}",
        )
        report.check(
            math.isclose(cz, wheel_radius, abs_tol=TOLERANCE_M),
            f"{name} wheel centre is {cz:.6f} m above the ground, not the documented radius "
            f"{wheel_radius:.6f} m from {_spec_pointer('/chassis/wheel_diameter_mm')}",
        )

    # ------------------------------------------------------------ key z heights
    # Derive the enclosure top from the model's own base housing box instead of
    # restating it, so the assertion tracks the geometry that actually exists.
    base_housing = next((numbers[2] for _, numbers in collisions["base_link"]), None)
    report.check(base_housing is not None, "base_link has no collision box to size the enclosure")
    if base_housing is not None:
        # base_link's origin is the housing *bottom*, so the top is the full
        # height above it, not half of it.
        base_top = poses["base_link"][2] + base_housing
        report.check(
            math.isclose(
                base_top,
                spec["coordinate_system"]["base_top_z_mm"] / 1000.0,
                abs_tol=TOLERANCE_M,
            ),
            f"base enclosure top is {base_top:.6f} m, not the documented "
            f"{spec['coordinate_system']['base_top_z_mm'] / 1000.0:.6f} m from "
            f"{_spec_pointer('/coordinate_system/base_top_z_mm')}",
        )
    head_top = poses["head_display"][2] + spec["head"]["height"] / 2000.0
    report.check(
        math.isclose(
            head_top,
            spec["coordinate_system"]["head_top_stowed_z_mm"] / 1000.0,
            abs_tol=TOLERANCE_M,
        ),
        f"stowed head top is {head_top:.6f} m, not the documented "
        f"{spec['coordinate_system']['head_top_stowed_z_mm'] / 1000.0:.6f} m from "
        f"{_spec_pointer('/coordinate_system/head_top_stowed_z_mm')}",
    )
    torso_bottom = poses["torso"][2] - spec["torso"]["height"] / 2000.0
    report.check(
        math.isclose(
            torso_bottom,
            spec["coordinate_system"]["torso_bottom_stowed_z_mm"] / 1000.0,
            abs_tol=TOLERANCE_M,
        ),
        f"stowed torso bottom is {torso_bottom:.6f} m, not the documented "
        f"{spec['coordinate_system']['torso_bottom_stowed_z_mm'] / 1000.0:.6f} m from "
        f"{_spec_pointer('/coordinate_system/torso_bottom_stowed_z_mm')}",
    )
    shoulder_min = poses["arm_left_shoulder_yoke"][2]
    report.check(
        math.isclose(
            shoulder_min,
            spec["lifting_platform"]["minimum_shoulder_height"] / 1000.0,
            abs_tol=TOLERANCE_M,
        ),
        f"stowed shoulder height is {shoulder_min:.6f} m, not the documented "
        f"{spec['lifting_platform']['minimum_shoulder_height'] / 1000.0:.6f} m from "
        f"{_spec_pointer('/lifting_platform/minimum_shoulder_height')}",
    )
    shoulder_max = shoulder_min + float(limits["lift_joint"]["upper"])
    report.check(
        math.isclose(
            shoulder_max,
            spec["lifting_platform"]["maximum_shoulder_height"] / 1000.0,
            abs_tol=TOLERANCE_M,
        ),
        f"raised shoulder height is {shoulder_max:.6f} m, not the documented "
        f"{spec['lifting_platform']['maximum_shoulder_height'] / 1000.0:.6f} m from "
        f"{_spec_pointer('/lifting_platform/maximum_shoulder_height')}",
    )
    spacing = abs(poses["arm_left_shoulder_yoke"][0] - poses["arm_right_shoulder_yoke"][0])
    report.check(
        math.isclose(spacing, spec["shoulder_mounts"]["center_spacing_mm"] / 1000.0, abs_tol=TOLERANCE_M),
        f"shoulder centre spacing is {spacing:.6f} m, not the documented "
        f"{spec['shoulder_mounts']['center_spacing_mm'] / 1000.0:.6f} m from "
        f"{_spec_pointer('/shoulder_mounts/center_spacing_mm')}",
    )

    # ------------------------------------------------------------- stabilizers
    vertices = spec["stability_analysis"]["support_polygons"]["stabilized"]["vertices_mm"]
    reach = vertices[2][0] / 1000.0
    for name in model.STABILIZERS:
        pad = poses[f"{name}_pad"]
        report.check(
            math.isclose(abs(pad[0]), reach, abs_tol=TOLERANCE_M)
            and math.isclose(abs(pad[1]), reach, abs_tol=TOLERANCE_M),
            f"{name} deployed pad is ({pad[0]:.6f}, {pad[1]:.6f}), not a corner of the "
            f"documented stabilized support polygon at +/-{reach:.3f} m from "
            f"{_spec_pointer('/stability_analysis/support_polygons/stabilized/vertices_mm')}",
        )
        report.check(
            pad[2] > 0.0,
            f"{name} deployed pad is at z={pad[2]:.6f}, at or below the ground plane",
        )

    # -------------------------------------------------------------- camera frames
    for name in model.CAMERA_NAMES:
        report.check(f"{name}_body" in names, f"{name} has no solid body link")
        report.check(f"{name}_optical" in names, f"{name} has no optical frame")
        junction = f"{name}_body_to_{name}_optical"
        report.check(junction in joints, f"{name} optical frame is not attached: {junction}")
        if junction in joints:
            joint = next(item for item in root.findall("joint") if item.get("name") == junction)
            origin = joint.find("origin")
            report.check(
                joint.get("type") == "fixed",
                f"{junction} must be fixed, got {joint.get('type')}",
            )
            report.check(
                origin is not None and origin.get("xyz", "0 0 0").split() == ["0", "0", "0"],
                f"{junction} must be coincident with its body frame",
            )

    # ------------------------------------------------------------- collision set
    for link, geometry in collisions.items():
        if link in model.collision_free_links():
            report.check(
                not geometry,
                f"{link} is a convention frame and must carry no collision geometry, got {geometry}",
            )
    for link in names:
        if link in model.collision_free_links():
            continue
        report.check(
            bool(collisions.get(link)),
            f"{link} is a solid body with no collision geometry: planning would treat it as free space",
        )
        geometry = collisions.get(link, [])
        report.check(
            len(geometry) <= 1,
            f"{link} carries {len(geometry)} collision volumes; one is the model's single source for that body",
        )

    # Every visual must be matched by a collision of the same size, so a visual
    # primitive can never be the only thing a planner can see.
    for link in root.findall("link"):
        name = link.get("name")
        if name in model.collision_free_links():
            continue
        visual_sizes = []
        for visual in link.findall("visual"):
            geometry = visual.find("geometry")
            if geometry is None:
                continue
            for child in geometry:
                if child.tag == "box":
                    visual_sizes.append(tuple(model._floats(child.get("size"))))
                elif child.tag == "cylinder":
                    visual_sizes.append((float(child.get("radius")), float(child.get("length"))))
        collision_sizes = [numbers for _, numbers in collisions.get(name, [])]
        report.check(
            visual_sizes == collision_sizes,
            f"{name} visual {visual_sizes} and collision {collision_sizes} geometry differ; "
            "a visual-only primitive is not a planning obstacle",
        )

    # ------------------------------------------------------------ inertial check
    for problem in model.inertial_validity(root):
        report.check(False, problem)

    # --------------------------------------------------------- frames documents
    for required in (
        "map",
        "odom",
        "base_footprint",
        "base_link",
        "lift_carriage",
        "torso",
        "parcel_bay",
        "tool_dock",
        "neck_pedestal",
        "neck_yaw",
        "head_display",
        "head_window",
    ):
        report.check(required in names, f"missing required frame {required}")
    for index in range(1, spec["torso"]["tool_dock_slots"] + 1):
        report.check(
            f"tool_dock_slot_{index}" in names,
            f"missing tool dock slot {index} (design-spec.json#/torso/tool_dock_slots)",
        )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "urdf",
        nargs="?",
        type=Path,
        help="expanded URDF; when omitted the model is expanded in-process",
    )
    args = parser.parse_args(argv)

    if args.urdf is None:
        root = model.expand()
        source = str(model.DESCRIPTION)
    else:
        root = ET.fromstring(args.urdf.read_text(encoding="utf-8"))
        source = str(args.urdf)

    report = validate(root)
    for failure in report.failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    print(f"checked {report.checks} assertions against {source}")
    if report.failures:
        print(f"{len(report.failures)} failed", file=sys.stderr)
        return min(len(report.failures), 120)
    print("Revision D description validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
