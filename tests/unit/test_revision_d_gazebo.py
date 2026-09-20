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


@pytest.mark.skipif(
    shutil.which("gz") is None and shutil.which("ign") is None,
    reason="no Gazebo on PATH; the #327 Gazebo cases stay NOT_EXECUTED",
)
def test_gazebo_cases_run_when_a_simulator_is_present() -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--simulator-available", "true"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
