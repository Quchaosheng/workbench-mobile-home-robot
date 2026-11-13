"""Gazebo acceptance cases for the Revision D model (Issue #327).

The issue asks for Gazebo tests covering spawn, joint-limit rejection,
self/forbidden-volume collision checks, lift states, stabilizer states and
clean shutdown/restart.

Those cases need a simulator. When one is absent these tests **skip** and say
so; they never convert an unrun case into a pass. That distinction is the whole
point of the repository's NOT_EXECUTED convention, and the sibling module
`tests/unit/test_revision_d_description.py` is what actually runs in CI.

What is asserted here without a simulator is the part that can be: that the test
bodies exist, that they are wired to a real launch/URDF command, and that the
harness refuses to report success when the runtime is missing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REVISION_D = ROOT / "robot/description/revision_d"
LAUNCH = REVISION_D / "launch/revision_d.launch.py"
DESCRIPTION = REVISION_D / "revision_d.urdf.xacro"
HARNESS = REVISION_D / "tools/gazebo_acceptance.py"

sys.path.insert(0, str(REVISION_D / "tools"))

pytest.importorskip("xacro", reason="xacro is required to expand the description")

GAZEBO_CASES = (
    "spawn",
    "joint_limit_rejection",
    "self_collision",
    "forbidden_volume",
    "lift_states",
    "stabilizer_states",
    "shutdown_restart",
)


def test_the_harness_declares_every_case_the_issue_asks_for() -> None:
    import gazebo_acceptance

    assert tuple(gazebo_acceptance.CASES) == GAZEBO_CASES


def test_the_harness_reports_not_executed_without_a_simulator() -> None:
    """The fail-closed path: no simulator must be NOT_EXECUTED, never a pass."""
    import gazebo_acceptance

    result = gazebo_acceptance.run(simulator_available=False)
    assert result["status"] == "NOT_EXECUTED"
    assert set(result["cases"].values()) == {"NOT_EXECUTED"}
    assert result["exit_code"] == 2


def test_the_harness_can_be_invoked_as_a_command() -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--simulator-available", "false"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "NOT_EXECUTED" in result.stdout


def test_a_present_simulator_still_cannot_produce_a_fabricated_pass() -> None:
    """With a simulator present the harness must still refuse a pass until implemented.

    This runs everywhere, including the container image, which does ship Gazebo.
    The point is not that the cases pass: the bodies for spawn, joint-limit
    rejection, collision, lift, stabilizer and shutdown are not written yet, so
    the harness must fail loudly rather than emit success. Asserting the failure
    keeps the earlier NOT_EXECUTED test honest -- if this branch silently
    returned 0 after a skip was turned into an "available" run, the suite would
    report seven unrun cases as green.
    """
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--simulator-available", "true"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode != 0, "the harness reported success for cases it has not implemented: " + result.stdout
    combined = result.stdout + result.stderr
    assert "not implemented" in combined, combined
    assert "refusing to report" in combined, combined
    for case in GAZEBO_CASES:
        assert f"{case}: EXECUTED" not in combined
        assert f"{case}: PASS" not in combined


@pytest.mark.skipif(
    shutil.which("gz") is None and shutil.which("ign") is None,
    reason="no Gazebo on PATH; the #327 Gazebo cases stay NOT_EXECUTED",
)
def test_gazebo_acceptance_reports_not_executed_only_without_a_simulator() -> None:
    """Detection, not the override, is what decides NOT_EXECUTED."""
    result = subprocess.run(
        [sys.executable, str(HARNESS)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    # A simulator is on PATH here, so the harness must not claim a pass either.
    assert result.returncode != 0, result.stdout + result.stderr


def test_launch_exposes_no_unguarded_trajectory_path() -> None:
    source = LAUNCH.read_text(encoding="utf-8")
    # No joint-group passthrough and no topic relay to the lift.
    assert "joint_group_position_controller" not in source
    assert "topic_tools" not in source
    assert "lift_trajectory_controller" in source
    # A missing simulator must abort the launch rather than half-start.
    assert "Absent simulator is not a pass" in source


def test_description_marks_hardware_status_not_executed() -> None:
    source = DESCRIPTION.read_text(encoding="utf-8")
    assert "NOT_EXECUTED" in source, "the model must not imply physical evidence"


class TestSensorPlugins:
    """Issue #580: the model must be able to produce sensor data at all.

    The Revision D xacro expands to three cameras, an IMU and a bumper in the
    chassis sensor list, and until this change it contained **zero** ``<sensor>``
    elements: a Gazebo run produced no image, no depth and no IMU, so the
    perception path had nothing to consume. These assertions need no simulator --
    they read the expanded model -- and they are the difference between "the frame
    exists" and "the sensor exists".
    """

    @staticmethod
    def expand(**mappings: str):
        import xml.etree.ElementTree as ET

        import xacro

        document = xacro.process_file(str(DESCRIPTION), mappings=mappings)
        return ET.fromstring(document.toxml())

    def test_no_camera_sensor_is_declared_by_default(self) -> None:
        """A bare expansion must not gain topics just by existing."""
        root = self.expand()
        names = {sensor.get("name") for sensor in root.iter("sensor")}
        assert names == {"base_imu", "bumper_front_contact"}

    def test_the_imu_and_bumper_are_declared_even_without_the_flag(self) -> None:
        """These are hardware the model claims; they are not a render stream."""
        root = self.expand()
        names = {sensor.get("name") for sensor in root.iter("sensor")}
        assert "base_imu" in names
        assert "bumper_front_contact" in names

    def test_enabling_sensors_adds_the_camera_streams(self) -> None:
        root = self.expand(sensors="true")
        sensors = {sensor.get("name"): sensor.get("type") for sensor in root.iter("sensor")}

        assert sensors["base_camera_image"] == "camera"
        assert sensors["head_camera_depth"] == "depth_camera"
        assert sensors["arm_left_wrist_camera_image"] == "camera"
        assert sensors["arm_right_wrist_camera_image"] == "camera"
        assert sensors["base_imu"] == "imu"
        assert sensors["bumper_front_contact"] == "contact"

    @pytest.mark.parametrize(
        "name",
        [
            "base_imu",
            "bumper_front_contact",
            "base_camera_image",
            "head_camera_depth",
            "arm_left_wrist_camera_image",
            "arm_right_wrist_camera_image",
        ],
    )
    def test_every_sensor_has_a_plugin(self, name: str) -> None:
        """A `<sensor>` without a system plugin is a declaration that publishes nothing."""
        root = self.expand(sensors="true")
        found = [sensor for sensor in root.iter("sensor") if sensor.get("name") == name]
        assert found, f"{name} is missing from the expansion"
        assert found[0].find("plugin") is not None, f"{name} has no system plugin"

    def test_every_gazebo_reference_names_a_real_link(self) -> None:
        """Gazebo silently ignores a reference that points at nothing.

        That silence is how the model came to have camera *frames* while having no
        camera *sensors*: a wrong reference produces no error, only absence.
        """
        root = self.expand(sensors="true")
        links = {link.get("name") for link in root.iter("link")}
        missing = [
            block.get("reference")
            for block in root.iter("gazebo")
            if block.get("reference") is not None and block.get("reference") not in links
        ]
        assert missing == [], f"<gazebo reference> names no link: {missing}"

    def test_the_camera_values_are_marked_as_fixture_values(self) -> None:
        """A lens angle the design spec does not pin must not read as one it does."""
        source = DESCRIPTION.read_text(encoding="utf-8")
        assert "fixture choice" in source
        assert "not a datasheet" in source

    def test_the_launch_file_enables_sensors(self) -> None:
        """The launch file is where a Gazebo spawn asks for the streams."""
        source = LAUNCH.read_text(encoding="utf-8")
        assert '"sensors": "true" if sensors else "false"' in source
