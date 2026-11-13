"""The rootfs topology declares six services and now has six units (#577).

`bsp/rootfs/services.yaml` declared six services, and only `robot-camera-head` had
a systemd unit template. The other five existed as a name in a YAML file and
nothing else: no `After=`, no `Restart=`, no `StartLimitBurst=`, no unit for
`systemd-analyze` to check. A topology document that describes services nothing
implements is a plan, not a topology.

These tests assert that the YAML and the unit set agree, that the hardening
matches the camera unit's, and that no unit can be ordered before the safety
monitor. `systemd-analyze verify` runs where the tool exists and **skips with a
reason** where it does not — an unrun case is not a pass.

What this does not claim: a boot. Nothing here runs the ordered set on a Jetson or
on any device. The evidence is container and static, and the pull request says so.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "bsp" / "rootfs" / "services.yaml"
SYSTEMD = ROOT / "bsp" / "rootfs" / "systemd"

sys.path.insert(0, str(ROOT / "bsp" / "validation"))

#: The hardening every unit must carry, matching the camera unit that already had it.
REQUIRED_HARDENING = ("NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true")


def services() -> dict:
    return yaml.safe_load(SERVICES.read_text(encoding="utf-8"))


def unit_text(name: str) -> str:
    return (SYSTEMD / f"{name}.service.in").read_text(encoding="utf-8")


def unit_names() -> list[str]:
    return sorted(path.name.removesuffix(".service.in") for path in SYSTEMD.glob("*.service.in"))


class TestEveryDeclaredServiceHasAUnit:
    def test_the_yaml_and_the_unit_set_are_the_same_set(self) -> None:
        declared = set(services()["services"])
        implemented = set(unit_names())
        assert declared == implemented, {
            "declared_without_unit": sorted(declared - implemented),
            "unit_without_declaration": sorted(implemented - declared),
        }

    def test_the_validator_agrees(self) -> None:
        """The check that makes the two sets stay equal lives in the validator."""
        import validate_manifests

        assert validate_manifests.validate_service_topology() == []

    @pytest.mark.parametrize(
        "name",
        [
            "robot-can-gateway",
            "robot-safety-monitor",
            "robot-device-manager",
            "robot-evidence-logger",
            "robot-update-agent",
        ],
    )
    def test_each_previously_missing_unit_exists(self, name: str) -> None:
        assert (SYSTEMD / f"{name}.service.in").is_file()


class TestTheUnitMatchesTheYaml:
    @pytest.mark.parametrize("name", sorted(yaml.safe_load(SERVICES.read_text(encoding="utf-8"))["services"]))
    def test_restart_policy_matches(self, name: str) -> None:
        declared = services()["services"][name]["restart"]
        # YAML 1.1 reads a bare `no` as False, which is why the YAML quotes it.
        assert isinstance(declared, str), f"{name} restart must be a string, got {declared!r}"
        assert f"Restart={declared}" in unit_text(name)

    @pytest.mark.parametrize("name", sorted(yaml.safe_load(SERVICES.read_text(encoding="utf-8"))["services"]))
    def test_ordering_matches(self, name: str) -> None:
        spec = services()["services"][name]
        text = unit_text(name)
        for item in spec.get("after") or []:
            if item.endswith(".target"):
                continue
            assert f"{item}.service" in text, f"{name} does not order after {item}"

    def test_a_restarting_service_bounds_its_restarts(self) -> None:
        """An unbounded restart loop is how a crash hides a fault."""
        for name, spec in services()["services"].items():
            if spec["restart"] == "no":
                continue
            text = unit_text(name)
            assert "StartLimitBurst=" in text, f"{name} restarts without a burst limit"
            assert "StartLimitIntervalSec=" in text, f"{name} restarts without a limit interval"


class TestTheHardening:
    @pytest.mark.parametrize("name", sorted(yaml.safe_load(SERVICES.read_text(encoding="utf-8"))["services"]))
    def test_every_unit_keeps_the_camera_hardening(self, name: str) -> None:
        text = unit_text(name)
        for directive in REQUIRED_HARDENING:
            assert directive in text, f"{name} is missing {directive}"

    def test_no_service_claims_safety_authority(self) -> None:
        for name, spec in services()["services"].items():
            assert spec.get("safety_authority") is False, name


class TestTheSafetyMonitorGate:
    def test_no_unit_is_ordered_before_the_safety_monitor(self) -> None:
        """`actuator_enable_during_boot: false` has to hold by construction.

        The safety monitor is the service that must observe the MCU before an
        actuator can be enabled. Two kinds of unit are legitimately earlier and
        neither can enable an actuator:

        * `robot-can-gateway` is transport only and `safety_authority: false`;
        * `robot-evidence-logger` writes the log store and never opens the CAN bus.

        Everything else must order after the monitor, so a new unit cannot be
        added ahead of it by accident.
        """
        monitor = "robot-safety-monitor"
        # Three units are legitimately not downstream of the monitor, and none of
        # them can enable an actuator, which is what the gate is about:
        #
        #   * `robot-can-gateway` is transport only, `safety_authority: false`;
        #   * `robot-evidence-logger` writes the log store, never the CAN bus;
        #   * `robot-update-agent` writes the inactive update slot and never the
        #     running root, so it cannot start anything that moves.
        #
        # The exemption is asserted below rather than assumed.
        allowed_earlier = {"robot-can-gateway", "robot-evidence-logger", "robot-update-agent"}

        def ordered_after(name: str) -> set[str]:
            return {
                part.strip().removesuffix(".service")
                for line in unit_text(name).splitlines()
                if line.strip().startswith("After=")
                for part in line.partition("=")[2].split()
            }

        def reaches_monitor(name: str, seen: set[str] | None = None) -> bool:
            """Ordering is transitive: device-manager after the monitor makes camera-head too."""
            visited = seen or set()
            if name in visited:
                return False
            visited.add(name)
            direct = ordered_after(name)
            if monitor in direct:
                return True
            return any(reaches_monitor(parent, visited) for parent in direct if parent in set(unit_names()))

        offenders: list[str] = []
        for name in unit_names():
            if name == monitor or name in allowed_earlier:
                continue
            if not reaches_monitor(name):
                offenders.append(f"{name} does not reach {monitor}: {sorted(ordered_after(name))}")
        assert offenders == [], offenders

    def test_the_earlier_units_cannot_enable_an_actuator(self) -> None:
        """The exemption above is only sound if none of them can move the robot."""
        for name in ("robot-can-gateway", "robot-evidence-logger", "robot-update-agent"):
            spec = services()["services"][name]
            assert spec.get("safety_authority") is False, name
            assert spec.get("mode") != "control", name

    def test_the_update_agent_writes_only_the_inactive_slot(self) -> None:
        """The reason it may run before the monitor is that it touches no running state."""
        text = unit_text("robot-update-agent")
        assert "ReadWritePaths=/var/lib/workbench/update" in text
        assert "Restart=no" in text

    def test_the_gate_is_declared_in_the_yaml(self) -> None:
        gates = services()["gates"]
        assert gates["actuator_enable_during_boot"] is False
        assert gates["missing_safety_monitor_inhibits_motion"] is True
        assert gates["service_restart_never_clears_safety_latch"] is True


class TestTheUnitsParse:
    @pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze is not installed here")
    @pytest.mark.parametrize("name", sorted(yaml.safe_load(SERVICES.read_text(encoding="utf-8"))["services"]))
    def test_systemd_analyze_accepts_the_unit(self, name: str, tmp_path: Path) -> None:
        """`systemd-analyze verify` parses the unit; two expected complaints are filtered.

        Every unit is copied into one directory so `Requires=` and `After=` resolve
        against each other rather than against the host's systemd. Two complaints
        are then expected and filtered: the `ExecStart` binary lives only on a
        deployed image, and `sys-subsystem-net-devices-can0.device` is a kernel
        device node this container does not have. A syntax error, a bad directive
        or a malformed section is not filtered and fails here.
        """
        for unit_name in unit_names():
            (tmp_path / f"{unit_name}.service").write_text(unit_text(unit_name), encoding="utf-8")
        result = subprocess.run(
            ["systemd-analyze", "verify", str(tmp_path / f"{name}.service")],
            capture_output=True,
            text=True,
        )
        ignored = (
            "is not executable",
            "No such file or directory",
            "marked executable",
            "Unit sys-subsystem-net-devices",
            "not found",
        )
        unexpected = [
            line
            for line in (result.stdout + result.stderr).splitlines()
            if line.strip() and not any(fragment in line for fragment in ignored)
        ]
        assert unexpected == [], unexpected

    def test_every_unit_has_the_three_sections(self) -> None:
        for name in unit_names():
            text = unit_text(name)
            for section in ("[Unit]", "[Service]", "[Install]"):
                assert section in text, f"{name} is missing {section}"
