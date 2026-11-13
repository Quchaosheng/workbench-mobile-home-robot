"""Every scenario's `world_version` resolves to a committed world (#542).

`sim/worlds/` held only a README, so `world_version` was a string nothing
resolved. A manifest could name a world that was never committed, and the failure
surfaced at run time as a missing model URI rather than at load. That is the gap
these tests close: the version is now checked when the manifests are validated,
and the two worlds it resolves to are asserted to contain the entities the
scenarios address by name.

What these tests do not do: they do not run Gazebo. `gz sdf -k` needs the
simulator, which this environment does not have, so the SDF is parsed as XML and
its structure is asserted. A structural pass is not a physics pass, and the pull
request says so.
"""

from __future__ import annotations

import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORLDS = ROOT / "sim" / "worlds"
MAPS = ROOT / "robot" / "mobile_base" / "maps"

sys.path.insert(0, str(ROOT / "tools" / "scripts"))


def world_file(version: str) -> Path:
    from validate_scenarios import world_path

    return world_path(version)


class TestEveryManifestResolves:
    def test_every_committed_manifest_names_a_committed_world(self) -> None:
        """The check that was missing: a version with no file is a load failure."""
        manifests = sorted((ROOT / "sim" / "scenarios").rglob("*.json"))
        assert manifests, "no scenario manifests found"
        versions = set()
        failures: list[str] = []
        for path in manifests:
            payload = json.loads(path.read_text(encoding="utf-8"))
            try:
                world_file(payload["world_version"])
            except RuntimeError as exc:
                failures.append(f"{path.name}: {exc}")
            versions.add(payload["world_version"])
        assert failures == [], failures
        assert versions == {"WorkbenchSim-v0", "WorkbenchSim-v0.2"}

    def test_an_unknown_version_is_refused(self) -> None:
        from validate_scenarios import world_path

        with pytest.raises(RuntimeError, match="has no committed world file"):
            world_path("WorkbenchSim-v99")

    def test_a_registered_version_with_no_file_is_refused(self, monkeypatch) -> None:
        """A map entry alone is not enough; the file has to exist."""
        from validate_scenarios import WORLD_FILES, world_path

        monkeypatch.setitem(WORLD_FILES, "WorkbenchSim-ghost", "does_not_exist.sdf")
        with pytest.raises(RuntimeError, match="is not committed"):
            world_path("WorkbenchSim-ghost")


class TestTheHouseholdWorld:
    @pytest.fixture(scope="class")
    @staticmethod
    def world() -> ET.Element:
        return ET.parse(world_file("WorkbenchSim-v0.2")).getroot().find("world")

    def test_the_entities_a_scenario_addresses_are_named(self, world: ET.Element) -> None:
        """The shelf, the parcel bay and the inspection table are separate named entities."""
        names = {model.get("name") for model in world.findall("model")}
        for required in (
            "shelf_frame",
            "shelf_bay_low",
            "shelf_bay_mid",
            "shelf_bay_high",
            "parcel_bay",
            "inspection_table",
            "tray",
            "removable_obstacle",
        ):
            assert required in names, f"{required} is not a named model"

    def test_the_removable_obstacle_is_separately_addressable(self, world: ET.Element) -> None:
        """`path-blocked-*` removes it by name rather than editing the world."""
        obstacle = [m for m in world.findall("model") if m.get("name") == "removable_obstacle"]
        assert len(obstacle) == 1
        assert obstacle[0].find("static").text == "true"

    def test_the_two_lights_a_low_light_variant_dims_are_separate(self, world: ET.Element) -> None:
        """A low-light variant dims the lamp and leaves the sun alone."""
        lights = {light.get("name"): light.get("type") for light in world.findall("light")}
        assert lights["room_sun"] == "directional"
        assert lights["overhead_lamp"] == "point"

    def test_the_room_clears_the_chassis_with_space_to_rotate(self, world: ET.Element) -> None:
        """The chassis is 540 x 520 mm; the room leaves metres of clearance."""
        spec = json.loads((ROOT / "hardware" / "mechanical" / "design-spec.json").read_text(encoding="utf-8"))
        chassis_m = spec["chassis"]["width"] / 1000.0
        floor = next(m for m in world.findall("model") if m.get("name") == "floor")
        plane = floor.find(".//plane")
        size = [float(v) for v in plane.find("size").text.split()]
        assert size[0] > 4 * chassis_m, "the room must leave room to rotate in place"
        assert size[1] > 4 * chassis_m

    def test_no_model_uses_an_external_uri(self, world: ET.Element) -> None:
        """A mesh URI would be an asset a licence review has to clear."""
        for uri in world.iter("uri"):
            assert not (uri.text or "").strip(), f"unexpected external asset: {uri.text}"

    def test_every_model_has_a_pose_and_a_link(self, world: ET.Element) -> None:
        for model in world.findall("model"):
            name = model.get("name") or "?"
            assert model.find("pose") is not None, f"{name} has no pose"
            assert model.find("link") is not None, f"{name} has no link"


class TestTheTabletopWorld:
    @pytest.fixture(scope="class")
    @staticmethod
    def world() -> ET.Element:
        return ET.parse(world_file("WorkbenchSim-v0")).getroot().find("world")

    def test_the_objects_the_frozen_scenarios_name_exist(self, world: ET.Element) -> None:
        names = {model.get("name") for model in world.findall("model")}
        for required in ("tray", "red_block", "removable_obstacle", "moving_target", "workbench_table"):
            assert required in names, f"{required} is not a named model"

    def test_the_block_is_not_static(self, world: ET.Element) -> None:
        """A static block would make a grasp unobservable."""
        block = next(m for m in world.findall("model") if m.get("name") == "red_block")
        static = block.find("static")
        assert static is None or static.text != "true"
        assert block.find(".//inertial") is not None, "a movable body needs inertia"

    def test_the_two_worlds_are_not_the_same_file(self) -> None:
        """The frozen tabletop scenarios and the household ones were frozen apart."""
        assert world_file("WorkbenchSim-v0") != world_file("WorkbenchSim-v0.2")


class TestTheCommittedMap:
    def test_the_map_and_its_sidecar_are_committed(self) -> None:
        pgm = MAPS / "workbench_household_v0.2.pgm"
        sidecar = MAPS / "workbench_household_v0.2.yaml"
        assert pgm.is_file()
        assert sidecar.is_file()

    def test_the_sidecar_digest_matches_the_image(self) -> None:
        """A rewritten map fails the digest, so a run cannot cite a stale one."""
        pgm = MAPS / "workbench_household_v0.2.pgm"
        sidecar = MAPS / "workbench_household_v0.2.yaml"
        digest = hashlib.sha256(pgm.read_bytes()).hexdigest()
        text = sidecar.read_text(encoding="utf-8")
        assert f"sha256: {digest}" in text

    def test_the_sidecar_records_a_revision(self) -> None:
        text = (MAPS / "workbench_household_v0.2.yaml").read_text(encoding="utf-8")
        assert "revision: workbench-household-v0.2-map-1" in text

    def test_the_image_is_a_valid_pgm(self) -> None:
        pgm = (MAPS / "workbench_household_v0.2.pgm").read_bytes()
        assert pgm.startswith(b"P5\n")
        header = pgm.split(b"\n", 3)
        width, height = (int(v) for v in header[1].split())
        assert width > 0 and height > 0
        # P5 with maxval 255 is one byte per pixel.
        assert len(header[3]) == width * height

    def test_the_sidecar_states_what_the_map_does_not_claim(self) -> None:
        """A free cell is not a safety promise about a dynamic obstacle."""
        text = (MAPS / "workbench_household_v0.2.yaml").read_text(encoding="utf-8")
        assert "dynamic obstacle" in text
