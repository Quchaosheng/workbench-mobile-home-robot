"""Structural guard for the workbench camera body in the frozen description.

Issue #261: `camera_body` carried visual geometry but no collision geometry, so
MoveIt saw the camera housing as free space and could accept an approach that
clips it. The description is a static fixture shared by reachability checks, so
the regression has to be caught structurally rather than by a motion run.

The checks here are deliberately string- and tree-level only. They do not
compose the arm, start Gazebo, plan a path, or claim any physical result.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DESCRIPTION = ROOT / "robot/description/workbench.urdf.xacro"
XACRO_NAMESPACE = "{http://www.ros.org/wiki/xacro}"

CAMERA_BOX = (0.05, 0.05, 0.03)
CAMERA_FRAMES = {
    "links": ("camera_post", "camera_body", "camera_optical"),
    "joints": ("surface_to_post", "post_to_camera", "camera_body_to_optical"),
}

xacro = pytest.importorskip("xacro", reason="xacro is required to expand the robot description")


def expand() -> ET.Element:
    document = xacro.process_file(str(DESCRIPTION))
    return ET.fromstring(document.toxml())


def boxes(link: ET.Element, kind: str) -> list[tuple[float, float, float]]:
    found: list[tuple[float, float, float]] = []
    for element in link.findall(kind):
        geometry = element.find("geometry")
        box = geometry.find("box") if geometry is not None else None
        if box is None:
            continue
        found.append(tuple(float(value) for value in box.get("size", "").split()))
    return found


def link_named(root: ET.Element, name: str) -> ET.Element:
    for link in root.findall("link"):
        if link.get("name") == name:
            return link
    raise AssertionError(f"description has no link named {name!r}")


def test_camera_body_carries_collision_geometry_matching_its_visual() -> None:
    link = link_named(expand(), "camera_body")

    visual = boxes(link, "visual")
    collision = boxes(link, "collision")

    assert visual == [CAMERA_BOX], f"camera_body visual box changed: {visual}"
    assert collision == [CAMERA_BOX], f"camera_body collision box is {collision}, not {[CAMERA_BOX]}"
    assert collision == visual


def test_visual_and_collision_origins_match_by_default() -> None:
    link = link_named(expand(), "camera_body")

    def origins(kind: str) -> list[tuple[float, ...] | None]:
        result: list[tuple[float, ...] | None] = []
        for element in link.findall(kind):
            origin = element.find("origin")
            if origin is None:
                result.append(None)
                continue
            xyz = [float(value) for value in origin.get("xyz", "0 0 0").split()]
            rpy = [float(value) for value in origin.get("rpy", "0 0 0").split()]
            result.append(tuple(xyz + rpy))
        return result

    # An origin on only one of the two would offset the collision volume from the
    # visual box, which is exactly the silent mismatch this guard exists for.
    assert origins("collision") == origins("visual")


def test_camera_frames_and_transforms_are_unchanged() -> None:
    root = expand()
    link_names = {link.get("name") for link in root.findall("link")}
    joint_names = {joint.get("name") for joint in root.findall("joint")}

    for name in CAMERA_FRAMES["links"]:
        assert name in link_names, f"camera link {name!r} disappeared"
    for name in CAMERA_FRAMES["joints"]:
        assert name in joint_names, f"camera joint {name!r} disappeared"

    optical = next(joint for joint in root.findall("joint") if joint.get("name") == "camera_body_to_optical")
    assert optical.get("type") == "fixed"
    assert optical.find("parent").get("link") == "camera_body"
    assert optical.find("child").get("link") == "camera_optical"


def test_camera_body_is_the_single_collision_source_for_the_housing() -> None:
    root = expand()
    link = link_named(root, "camera_body")

    # Exactly one collision volume: a second one would make the housing obstacle
    # geometry depend on which element a consumer happened to read.
    assert len(link.findall("collision")) == 1

    # The optical frame is a pure convention frame and must stay collision-free,
    # otherwise planning would treat a zero-size frame as a solid body.
    assert boxes(link_named(root, "camera_optical"), "collision") == []

    # A housing-sized box must not reappear on any other link.
    housing = [
        name
        for name in (link.get("name") for link in root.findall("link"))
        if name != "camera_body" and CAMERA_BOX in boxes(link_named(root, name), "collision")
    ]
    assert housing == []


def test_description_expands_to_one_tree_rooted_at_world() -> None:
    root = expand()

    links = [link.get("name") for link in root.findall("link")]
    assert len(links) == len(set(links)), "duplicate link names in the description"
    assert links.count("world") == 1

    children: set[str] = set()
    for joint in root.findall("joint"):
        children.add(joint.find("child").get("link"))
    # A single root is one that is never a child, and it must be `world`.
    assert "world" not in children
    assert [name for name in links if name not in children] == ["world"]


def _without_camera_collision() -> str:
    """Return the description text with the camera collision element commented out."""
    source = DESCRIPTION.read_text(encoding="utf-8")
    start = source.index('<link name="camera_body">')
    end = source.index("</link>", start)
    block = source[start:end]

    assert "<collision>" in block and "</collision>" in block
    stripped_block = block.replace("<collision>", "<!--<collision>", 1).replace("</collision>", "</collision>-->", 1)
    return source.replace(block, stripped_block, 1)


def test_missing_collision_geometry_fails_this_guard(tmp_path: Path) -> None:
    """A visual-only camera body must not pass the structural check.

    This is the mutation the guard exists for: if it stops detecting a removed
    collision element, the test above is no longer load-bearing.
    """
    mutated = tmp_path / "workbench.urdf.xacro"
    mutated.write_text(_without_camera_collision(), encoding="utf-8")

    root = ET.fromstring(xacro.process_file(str(mutated)).toxml())

    assert boxes(link_named(root, "camera_body"), "collision") == []
    assert boxes(link_named(root, "camera_body"), "visual") == [CAMERA_BOX]
