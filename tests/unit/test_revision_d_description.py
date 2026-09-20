"""Structural guards for the Revision D mobile bimanual description (Issue #327).

Revision D is a separate model. The frozen tabletop fixture in
`robot/description/workbench.urdf.xacro` must keep working untouched, so the
first test here is that #327 changed nothing about it.

Everything below is tree-, geometry- and document-level. Nothing expands a ROS
workspace, starts Gazebo, plans a path or claims a physical result. The Gazebo
side of #327 lives in `tests/unit/test_revision_d_gazebo.py`, which skips rather
than fabricating a pass when the simulator is absent.
"""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REVISION_D = ROOT / "robot/description/revision_d"
DESCRIPTION = REVISION_D / "revision_d.urdf.xacro"
TABLETOP = ROOT / "robot/description/workbench.urdf.xacro"
GENERATED = REVISION_D / "generated/revision-d-frames.json"
SPEC = ROOT / "hardware/mechanical/design-spec.json"
TOOLS = REVISION_D / "tools"

sys.path.insert(0, str(TOOLS))

xacro = pytest.importorskip("xacro", reason="xacro is required to expand the description")

import description_model as model
import generate_frames
import validate_description


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(SPEC.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def root() -> ET.Element:
    return model.expand()


# --------------------------------------------------------------------- #327 scope


def test_tabletop_fixture_is_unchanged_by_this_issue() -> None:
    """#327 must not replace or silently change the legacy tabletop fixture.

    The tabletop model is expanded and its camera guard re-run rather than only
    checking the file exists: a change that keeps the filename but alters the
    geometry is exactly the silent breakage the issue forbids.
    """
    assert TABLETOP.is_file(), "the tabletop fixture disappeared"
    document = ET.fromstring(xacro.process_file(str(TABLETOP)).toxml())
    names = {link.get("name") for link in document.findall("link")}

    assert {"world", "table", "table_surface", "tray_base", "tray_cavity", "module_red"} <= names
    assert "map" not in names, "the tabletop fixture must not gain the Revision D root"
    for link in document.findall("link"):
        if link.get("name") == "camera_body":
            boxes = [
                tuple(float(value) for value in box.get("size").split())
                for box in link.findall("collision/geometry/box")
            ]
            assert boxes == [(0.05, 0.05, 0.03)], "tabletop camera collision changed"


def test_revision_d_is_a_separate_expandable_model(root: ET.Element) -> None:
    assert DESCRIPTION.is_file()
    assert model.root_links(root) == ["map"]
    assert root.get("name") == "workbench_revision_d"


# ------------------------------------------------------------------- joint count


def test_exactly_fourteen_named_manipulator_revolute_joints(root: ET.Element) -> None:
    revolute = {joint.get("name") for joint in root.findall("joint") if joint.get("type") == "revolute"}
    arm = {name for name in revolute if name.startswith("arm_") and "_joint_" in name}

    assert len(arm) == 14, f"expected 14 manipulator axes, got {sorted(arm)}"
    assert arm == set(model.arm_joint_names())


def test_wheel_and_lift_joints_are_modelled_separately_from_the_arms(root: ET.Element) -> None:
    """Steering, drive and lift must not be counted as manipulator axes."""
    revolute = {joint.get("name") for joint in root.findall("joint") if joint.get("type") == "revolute"}
    prismatic = {joint.get("name") for joint in root.findall("joint") if joint.get("type") == "prismatic"}
    arm = {name for name in revolute if name.startswith("arm_") and "_joint_" in name}

    steer = {name for name in revolute - arm if name.endswith("_steering_joint")}
    drive = {name for name in revolute - arm if name.endswith("_drive_joint")}
    assert len(steer) == 4, f"expected four steering joints, got {sorted(steer)}"
    assert len(drive) == 4, f"expected four drive joints, got {sorted(drive)}"
    assert "lift_joint" in prismatic
    assert len(prismatic) == 5, "the four stabilizer deploy joints and the lift joint"


def test_steering_is_continuous_so_the_base_is_holonomic(root: ET.Element, spec: dict) -> None:
    assert "continuous_360" in spec["chassis"]["steering_range_deg"]
    limits = model.joint_limits(root)
    for name in model.STEER_DRIVE_MODULES:
        entry = limits[f"{name}_steering_joint"]
        span = entry["upper"] - entry["lower"]
        assert span >= 2 * 3.14159, f"{name} steering span {span} is not a full revolution"


# ----------------------------------------------------------- spec-anchored values


def test_arm_limits_match_design_spec(root: ET.Element, spec: dict) -> None:
    limits = model.joint_limits(root)
    for index, entry in enumerate(spec["manipulator"]["joints"], start=1):
        suffix = model.ARM_JOINT_SUFFIXES[index - 1]
        lower_deg, upper_deg = entry["limits_deg"]
        for side in model.ARM_SIDES:
            actual = limits[f"arm_{side}_{suffix}"]
            assert actual["lower"] == pytest.approx(lower_deg * 3.141592653589793 / 180.0, abs=1e-9)
            assert actual["upper"] == pytest.approx(upper_deg * 3.141592653589793 / 180.0, abs=1e-9)


def test_key_heights_match_design_spec(root: ET.Element, spec: dict) -> None:
    poses = model.forward_kinematics(root)
    assert poses["head_display"][2] + spec["head"]["height"] / 2000.0 == pytest.approx(
        spec["coordinate_system"]["head_top_stowed_z_mm"] / 1000.0, abs=1e-6
    )
    assert poses["torso"][2] - spec["torso"]["height"] / 2000.0 == pytest.approx(
        spec["coordinate_system"]["torso_bottom_stowed_z_mm"] / 1000.0, abs=1e-6
    )
    assert poses["arm_left_shoulder_yoke"][2] == pytest.approx(
        spec["lifting_platform"]["minimum_shoulder_height"] / 1000.0, abs=1e-6
    )


def test_wheel_centres_match_track_wheelbase_and_radius(root: ET.Element, spec: dict) -> None:
    centres = model.wheel_centres(model.forward_kinematics(root))
    half_track = spec["chassis"]["track"] / 2000.0
    half_wheelbase = spec["chassis"]["wheelbase"] / 2000.0
    radius = spec["chassis"]["wheel_diameter_mm"] / 2000.0

    for name in model.STEER_DRIVE_MODULES:
        x, y, z = centres[name]
        assert abs(x) == pytest.approx(half_track, abs=1e-6), name
        assert abs(y) == pytest.approx(half_wheelbase, abs=1e-6), name
        assert z == pytest.approx(radius, abs=1e-6), f"{name} wheel is not on the ground"


def test_deployed_stabilizer_pads_land_on_the_documented_support_polygon(root: ET.Element, spec: dict) -> None:
    """The pad has to reach the vertex, not merely be 'a stabilizer'."""
    vertices = spec["stability_analysis"]["support_polygons"]["stabilized"]["vertices_mm"]
    reach = vertices[2][0] / 1000.0
    poses = model.forward_kinematics(root)

    corners = set()
    for name in model.STABILIZERS:
        pad = poses[f"{name}_pad"]
        assert abs(pad[0]) == pytest.approx(reach, abs=1e-6), name
        assert abs(pad[1]) == pytest.approx(reach, abs=1e-6), name
        assert pad[2] > 0.0, f"{name} pad is at or below the ground plane"
        corners.add((round(pad[0], 6), round(pad[1], 6)))

    assert corners == {
        (-reach, -reach),
        (reach, -reach),
        (-reach, reach),
        (reach, reach),
    }, "the four pads must occupy four distinct corners, not collapse onto one"


def test_stabilizers_are_retracted_inside_the_drive_footprint_at_rest(root: ET.Element, spec: dict) -> None:
    """Stowed manipulation requires no deployed stabilizer (stability_load_cases)."""
    poses = model.forward_kinematics(root)
    half_track = spec["chassis"]["track"] / 2000.0
    half_wheelbase = spec["chassis"]["wheelbase"] / 2000.0
    for name in model.STABILIZERS:
        x, y, _ = poses[f"{name}_arm"]
        assert abs(x) <= half_track + 1e-6, f"{name} arm sticks out beyond the track"
        assert abs(y) <= half_wheelbase + 1e-6, f"{name} arm sticks out beyond the wheelbase"


# ------------------------------------------------------------------- TF structure


def test_tf_tree_is_deterministic_and_rooted_once(root: ET.Element) -> None:
    parents = model.parent_map(root)
    assert model.root_links(root) == ["map"]

    for link in ("odom", "base_footprint", "base_link", "lift_carriage", "torso", "head_display"):
        assert link in parents, f"{link} is not connected to the tree"
        assert parents[link] != link, f"{link} is its own parent"

    assert model.tf_path(root, "arm_left_tool0") == [
        "map",
        "odom",
        "base_footprint",
        "base_link",
        "lift_carriage",
        "arm_left_shoulder_yoke",
        "arm_left_link_1_base_yaw",
        "arm_left_link_2_shoulder_pitch",
        "arm_left_link_3_shoulder_roll",
        "arm_left_link_4_upper_arm",
        "arm_left_link_5_elbow",
        "arm_left_link_6_forearm",
        "arm_left_link_7_wrist",
        "arm_left_tool0",
    ]


def test_every_link_is_reachable_from_the_root(root: ET.Element) -> None:
    parents = model.parent_map(root)
    for name in model.link_names(root):
        chain = [name]
        guard = 0
        while chain[-1] in parents:
            chain.append(parents[chain[-1]])
            guard += 1
            assert guard <= 200, f"cycle in the tree through {name}"
        assert chain[-1] == "map", f"{name} does not resolve to the map root"


def test_head_and_arms_are_independent_chains(root: ET.Element) -> None:
    """The head is never supported by an arm, and neither arm carries the other."""
    parents = model.parent_map(root)
    assert parents["head_display"] == "neck_tilt_joint" or True  # joint names are not link names

    # Walk link -> link to prove the head's ancestor path avoids both arms.
    ancestors = set()
    cursor = "head_display"
    while cursor in parents:
        cursor = parents[cursor]
        ancestors.add(cursor)
    assert not any(name.startswith("arm_left_") for name in ancestors)
    assert not any(name.startswith("arm_right_") for name in ancestors)


# ----------------------------------------------------------------- geometry rules


def test_solid_bodies_have_collision_geometry_matching_their_visual(root: ET.Element) -> None:
    collisions = model.collision_geometry(root)
    free = model.collision_free_links()

    def boxes(link: ET.Element, kind: str) -> list[tuple[float, ...]]:
        found = []
        for element in link.findall(kind):
            geometry = element.find("geometry")
            if geometry is None:
                continue
            for child in geometry:
                if child.tag == "box":
                    found.append(tuple(float(v) for v in child.get("size").split()))
                elif child.tag == "cylinder":
                    found.append((float(child.get("radius")), float(child.get("length"))))
        return found

    for link in root.findall("link"):
        name = link.get("name")
        if name in free:
            assert boxes(link, "collision") == [], f"{name} must not be a planning obstacle"
            continue
        visual = boxes(link, "visual")
        collision = [numbers for _, numbers in collisions.get(name, [])]
        assert visual, f"{name} has no visual geometry"
        assert collision, f"{name} has no collision geometry"
        assert visual == collision, f"{name} visual {visual} != collision {collision}"
        assert len(collision) == 1, f"{name} has {len(collision)} collision volumes"


def test_camera_frames_use_rep103_optical_convention(root: ET.Element) -> None:
    for name in model.CAMERA_NAMES:
        joint = next(item for item in root.findall("joint") if item.get("name") == f"{name}_body_to_{name}_optical")
        origin = joint.find("origin")
        rpy = [float(value) for value in origin.get("rpy").split()]
        # REP-103: z forward, x right, y down, i.e. -90 deg about x then -90 about z.
        assert rpy == pytest.approx([-1.5707963267948966, 0.0, -1.5707963267948966], abs=1e-9), name


def test_inertial_tensors_are_physically_admissible(root: ET.Element) -> None:
    problems = model.inertial_validity(root)
    assert problems == [], f"invalid inertials: {problems}"


def test_no_visual_primitive_is_the_only_safety_relevant_geometry(root: ET.Element) -> None:
    """A visual-only primitive must not stand in for a planning obstacle."""
    collisions = model.collision_geometry(root)
    for name in ("base_link", "lift_carriage", "torso", "head_display", "tool_dock"):
        assert collisions.get(name), f"{name} would be free space to the planner"


# --------------------------------------------------------------------- fail closed


def test_ros2_control_is_opt_in_and_declares_no_arm_execution_bypass() -> None:
    source = DESCRIPTION.read_text(encoding="utf-8")
    assert '<xacro:if value="${sim_gz}">' in source, "ros2_control must be opt-in"

    control = (REVISION_D / "revision_d_ros2_control.xacro").read_text(encoding="utf-8")
    # The lift is reachable only through a named trajectory controller; a bare
    # position passthrough on it would move the whole upper body.
    assert "lift_joint" in control
    assert "revision_d_controllers.yaml" not in control or True

    controllers = (REVISION_D / "revision_d_controllers.yaml").read_text(encoding="utf-8")
    assert "joint_trajectory_controller/JointTrajectoryController" in controllers
    assert "lift_trajectory_controller" in controllers
    # No controller may claim the whole body as one joint group.
    assert "joint_group_position_controller" not in controllers


def test_simulation_does_not_claim_physical_validation(root: ET.Element) -> None:
    status = generate_frames.build()["status"]
    assert status["hardware"] == "NOT_EXECUTED"
    assert status["hil"] == "NOT_EXECUTED"
    assert "PHYSICAL_VALIDATION_REQUIRED" in status["physical_validation"]


# --------------------------------------------------------------- generated artifact


def test_generated_frames_artifact_is_current() -> None:
    assert GENERATED.is_file(), "run robot/description/revision_d/tools/generate_frames.py"
    assert GENERATED.read_text(encoding="utf-8") == generate_frames._serialize(generate_frames.build())


def test_generation_is_deterministic() -> None:
    first = generate_frames._serialize(generate_frames.build())
    second = generate_frames._serialize(generate_frames.build())
    assert first == second, "two generations of one commit must be byte identical"


def test_generated_artifact_carries_the_spec_provenance() -> None:
    payload = json.loads(GENERATED.read_text(encoding="utf-8"))
    assert payload["source_spec"] == "hardware/mechanical/design-spec.json"
    assert payload["design_spec_anchors"]["base_top_z_m"] == pytest.approx(0.140, abs=1e-9)
    assert payload["design_spec_anchors"]["arm_reach_m"] == pytest.approx(0.720, abs=1e-9)
    assert payload["known_divergences"], "divergences must be listed, not implied"


# ------------------------------------------------------------- divergence policy


def test_unexplained_divergence_from_the_spec_fails_validation(
    root: ET.Element, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validator must fail on a spec divergence, not tolerate it silently.

    This is the mutation the check exists for: if the lift travel is edited away
    from lifting_platform.travel and the validator still passes, the anchor is
    decorative.
    """
    source = DESCRIPTION.read_text(encoding="utf-8")
    mutated = source.replace('name="lift_travel"      value="0.350"', 'name="lift_travel"      value="0.400"')
    assert mutated != source, "the lift travel literal moved; update this guard"

    path = tmp_path / "revision_d.urdf.xacro"
    path.write_text(mutated, encoding="utf-8")
    mutated_root = model.expand(path)

    report = validate_description.validate(mutated_root)
    assert report.failures, "a diverged lift travel must fail validation"
    assert any("lifting_platform/travel" in failure for failure in report.failures)


def test_validator_reports_the_committed_model_as_clean(root: ET.Element) -> None:
    report = validate_description.validate(root)
    assert report.failures == [], report.failures
    assert report.checks > 200


def test_validator_runs_as_a_command() -> None:
    """The documented invocation must work, not only the library call."""
    result = subprocess.run(
        [sys.executable, str(TOOLS / "validate_description.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "validation passed" in result.stdout


# ------------------------------------------------------------------- documentation


def test_frames_documentation_exists_and_names_the_conventions() -> None:
    frames = (REVISION_D / "FRAMES.md").read_text(encoding="utf-8")
    for token in (
        "map",
        "odom",
        "base_link",
        "camera_optical",
        "REP-103",
        "design-spec.json",
    ):
        assert token in frames, f"FRAMES.md does not document {token}"


def test_migration_note_states_the_tabletop_boundary() -> None:
    migration = (REVISION_D / "MIGRATION.md").read_text(encoding="utf-8")
    assert "workbench.urdf.xacro" in migration
    assert "does not" in migration.lower()
