"""Load, expand and check the Revision D robot description (Issue #327).

The model's authority is hardware/mechanical/design-spec.json. Everything here
exists so that authority is machine-checkable rather than asserted in prose:

* :func:`load_spec` reads the mechanical spec.
* :func:`expand` turns the Xacro tree into a parsed URDF.
* :func:`forward_kinematics` returns world poses, because several acceptance
  values are *derived* through the geometry (the deployed stabilizer pad must
  land on the documented support polygon, the head top must land on the
  documented stowed height) and a literal comparison cannot see those.
* :func:`known_divergences` is the single place a documented value is allowed to
  differ, each with a reason. A divergence that is not listed here fails.

Nothing in this module starts Gazebo, plans a path or claims a physical result.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

REVISION_D_DIR = Path(__file__).resolve().parents[1]
ROOT = REVISION_D_DIR.parents[2]
DESCRIPTION = REVISION_D_DIR / "revision_d.urdf.xacro"
SPEC = ROOT / "hardware/mechanical/design-spec.json"
GENERATED = REVISION_D_DIR / "generated/revision-d-frames.json"

ARM_SIDES = ("left", "right")
ARM_JOINT_SUFFIXES = (
    "joint_1_base_yaw",
    "joint_2_shoulder_pitch",
    "joint_3_shoulder_roll",
    "joint_4_elbow_pitch",
    "joint_5_forearm_roll",
    "joint_6_wrist_pitch",
    "joint_7_tool_roll",
)
STEER_DRIVE_MODULES = (
    "drive_module_front_left",
    "drive_module_front_right",
    "drive_module_rear_left",
    "drive_module_rear_right",
)
STABILIZERS = (
    "stabilizer_front_left",
    "stabilizer_front_right",
    "stabilizer_rear_left",
    "stabilizer_rear_right",
)
CAMERA_NAMES = (
    "base_camera",
    "head_camera",
    "arm_left_wrist_camera",
    "arm_right_wrist_camera",
)
LIFT_JOINT_MIN_Z = 0.0
LIFT_JOINT_MAX_Z = 0.35


@dataclass(frozen=True)
class Divergence:
    """One design-spec value the model deliberately does not copy."""

    pointer: str
    spec_value: object
    model_value: object
    reason: str


def known_divergences() -> tuple[Divergence, ...]:
    """Documented values that intentionally differ, each with a reason.

    The head is carried by a neck chain, not by the 72 mm pedestal in
    ``head.neck_mount``. Fitting that pedestal between the documented torso top
    (500 + 430 mm) and the documented stowed head top (1100 mm) is impossible
    without shrinking the head below its own 128 mm height, so the standoff is
    the value that has to give.
    """
    return (
        Divergence(
            pointer="/head/neck_mount/pedestal_height_mm",
            spec_value=72,
            model_value=208,
            reason=(
                "the neck standoff is the residual of the documented stowed height chain "
                "(head_top_stowed_z - head height - torso top = 1100 - 128 - 930 = 42 mm gap is "
                "not enough for any pedestal), so the pedestal absorbs the difference and the "
                "120 mm register/support stack in neck_mount is not modelled as separate links"
            ),
        ),
        Divergence(
            pointer="/chassis/ground_clearance",
            spec_value=28,
            model_value=28,
            reason=(
                "equal on purpose; listed here so the generator prints it in the frame "
                "artifact and a future edit to either side is visible rather than silent"
            ),
        ),
    )


def load_spec() -> dict:
    import json

    return json.loads(SPEC.read_text(encoding="utf-8"))


def expand(path: Path | None = None) -> ET.Element:
    """Expand the Xacro source and parse the result."""
    import xacro

    document = xacro.process_file(str(path or DESCRIPTION))
    return ET.fromstring(document.toxml())


def _floats(value: str | None) -> list[float]:
    return [float(item) for item in (value or "0 0 0").split()]


def _rotation(rpy: list[float]) -> tuple[tuple[float, ...], ...]:
    roll, pitch, yaw = rpy
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _compose(parent, child):
    (pr, pxyz), (cr, cxyz) = parent, child
    rotated = tuple(sum(pr[row][col] * cxyz[col] for col in range(3)) + pxyz[row] for row in range(3))
    combined = tuple(tuple(sum(pr[row][k] * cr[k][col] for k in range(3)) for col in range(3)) for row in range(3))
    return combined, rotated


def joint_origins(root: ET.Element) -> dict[str, tuple]:
    """Map each child link to its parent link, origin and rotation."""
    table: dict[str, tuple] = {}
    for joint in root.findall("joint"):
        origin = joint.find("origin")
        xyz = _floats(origin.get("xyz") if origin is not None else None)
        rpy = _floats(origin.get("rpy") if origin is not None else None)
        table[joint.find("child").get("link")] = (
            joint.find("parent").get("link"),
            tuple(xyz),
            _rotation(rpy),
        )
    return table


def forward_kinematics(root: ET.Element) -> dict[str, tuple[float, float, float]]:
    """World position of every link at the zero configuration of every joint."""
    table = joint_origins(root)
    poses: dict[str, tuple[float, float, float]] = {}

    def resolve(link: str) -> tuple:
        if link in poses:
            rotation, position = poses[link]
            return rotation, position
        if link not in table:
            identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
            poses[link] = (identity, (0.0, 0.0, 0.0))
            return poses[link]
        parent, xyz, rotation = table[link]
        parent_rotation, parent_position = resolve(parent)
        poses[link] = _compose((parent_rotation, parent_position), (rotation, xyz))
        return poses[link]

    for link in table:
        resolve(link)
    for link in root.findall("link"):
        resolve(link.get("name"))
    return {name: pose[1] for name, pose in poses.items()}


def link_names(root: ET.Element) -> list[str]:
    return [link.get("name") for link in root.findall("link")]


def joint_names(root: ET.Element) -> list[str]:
    return [joint.get("name") for joint in root.findall("joint")]


def arm_joint_names() -> list[str]:
    return [f"arm_{side}_{suffix}" for side in ARM_SIDES for suffix in ARM_JOINT_SUFFIXES]


def joint_limits(root: ET.Element) -> dict[str, dict]:
    limits: dict[str, dict] = {}
    for joint in root.findall("joint"):
        limit = joint.find("limit")
        if limit is None:
            continue
        limits[joint.get("name")] = {
            "type": joint.get("type"),
            "lower": float(limit.get("lower")),
            "upper": float(limit.get("upper")),
            "velocity": float(limit.get("velocity")),
            "effort": float(limit.get("effort")),
        }
    return limits


def collision_geometry(root: ET.Element) -> dict[str, list[tuple[str, tuple[float, ...]]]]:
    """Collision primitives per link, as (tag, numbers) pairs."""
    result: dict[str, list[tuple[str, tuple[float, ...]]]] = {}
    for link in root.findall("link"):
        found: list[tuple[str, tuple[float, ...]]] = []
        for collision in link.findall("collision"):
            geometry = collision.find("geometry")
            if geometry is None:
                continue
            for child in geometry:
                if child.tag == "box":
                    found.append(("box", tuple(_floats(child.get("size")))))
                elif child.tag == "cylinder":
                    found.append(("cylinder", (float(child.get("radius")), float(child.get("length")))))
        result[link.get("name")] = found
    return result


def inertial_validity(root: ET.Element) -> list[str]:
    """Return one message per inertial tensor that is not physically admissible."""
    problems: list[str] = []
    for link in root.findall("link"):
        name = link.get("name")
        inertia = link.find("inertial")
        if inertia is None:
            if name in PURE_FRAME_LINKS:
                continue
            problems.append(f"{name}: link has no inertial element")
            continue
        mass = float(inertia.find("mass").get("value"))
        tensor = inertia.find("inertia")
        ixx = float(tensor.get("ixx"))
        iyy = float(tensor.get("iyy"))
        izz = float(tensor.get("izz"))
        ixy = float(tensor.get("ixy", 0.0))
        ixz = float(tensor.get("ixz", 0.0))
        iyz = float(tensor.get("iyz", 0.0))
        if mass <= 0.0:
            problems.append(f"{name}: mass {mass} is not positive")
        if min(ixx, iyy, izz) <= 0.0:
            problems.append(f"{name}: principal inertia {ixx, iyy, izz} is not positive")
        # A principal-axis violation means the tensor is not that of a real body.
        if ixx + iyy < izz - 1e-12 or iyy + izz < ixx - 1e-12 or izz + ixx < iyy - 1e-12:
            problems.append(f"{name}: inertia {ixx, iyy, izz} violates the triangle inequality")
        if max(abs(ixy), abs(ixz), abs(iyz)) > 1e-12:
            problems.append(f"{name}: off-diagonal inertia is not supported by this model")
    return problems


PURE_FRAME_LINKS = frozenset(
    {
        "map",
        "odom",
        "base_footprint",
        "parcel_bay",
        "tool_dock_slot_1",
        "tool_dock_slot_2",
        "tool_dock_slot_3",
        "arm_left_tool0",
        "arm_right_tool0",
        "base_camera_optical",
        "head_camera_optical",
        "arm_left_wrist_camera_optical",
        "arm_right_wrist_camera_optical",
        "head_window",
    }
)


def collision_free_links() -> frozenset[str]:
    """Links that must carry no collision geometry, because they are conventions.

    A zero-size frame treated as a solid body is a phantom planning obstacle, so
    the optical frames and the pure transform frames carry none. ``head_window``
    is the smoked-glass expression window: the head box behind it already owns
    the collision volume for that part of the shell.
    """
    return frozenset(
        {
            "map",
            "odom",
            "base_footprint",
            "parcel_bay",
            "tool_dock_slot_1",
            "tool_dock_slot_2",
            "tool_dock_slot_3",
            "arm_left_tool0",
            "arm_right_tool0",
            "base_camera_optical",
            "head_camera_optical",
            "arm_left_wrist_camera_optical",
            "arm_right_wrist_camera_optical",
            "head_window",
        }
    )


def wheel_centres(poses: dict[str, tuple[float, float, float]]) -> dict[str, tuple[float, float, float]]:
    return {name: poses[f"{name}_wheel"] for name in STEER_DRIVE_MODULES}


def root_links(root: ET.Element) -> list[str]:
    children = {joint.find("child").get("link") for joint in root.findall("joint")}
    return [name for name in link_names(root) if name not in children]


def parent_map(root: ET.Element) -> dict[str, str]:
    return {joint.find("child").get("link"): joint.find("parent").get("link") for joint in root.findall("joint")}


def tf_path(root: ET.Element, link: str) -> list[str]:
    parents = parent_map(root)
    chain = [link]
    while chain[-1] in parents:
        chain.append(parents[chain[-1]])
    return list(reversed(chain))
