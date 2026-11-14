"""A clock nobody verified must refuse its timestamp, not stamp a record (#594).

`bsp/rootfs/services.yaml` declared six services and none synchronised the clock, so
a Jetson without an RTC battery boots at a plausible wrong wall time and every
wall-stamped evidence record is wrong by a constant offset. The monotonic path is
correct and the tests inject their clocks, so nothing in the repository noticed.

These tests drive `bsp/rootfs/clock_check.py`, which answers two questions: is the
wall clock believable for this boot, and is it within tolerance of a reference. Both
are reported rather than assumed, and a refusal names the condition that failed.

What this does not do: it does not boot a device, and it does not reinterpret
`observed_at`. The clock id vocabulary is unchanged; this decides whether a wall
timestamp may be stamped, not what a wall-stamped record means.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "bsp" / "rootfs" / "services.yaml"
SYSTEMD = ROOT / "bsp" / "rootfs" / "systemd"

sys.path.insert(0, str(ROOT / "bsp" / "rootfs"))
sys.path.insert(0, str(ROOT / "bsp" / "validation"))

import clock_check as cc

BUILD = "2026-01-01T00:00:00Z"


class TestASkewedBootIsRefused:
    def test_a_wall_clock_that_predates_the_build_is_refused(self) -> None:
        """A clock earlier than the software running on it was never set."""
        result = cc.check_boot_clock(
            wall_now="2020-01-01T00:00:00Z",
            monotonic_uptime_seconds=60,
            build_date=BUILD,
        )
        assert result.trusted is False
        assert "predates the build date" in result.reason
        assert result.offsets["clock_lag_seconds"] > 0

    def test_a_clock_stepped_forward_after_boot_is_refused(self) -> None:
        """A wrong RTC plus a later sync gives a wall time the uptime cannot explain.

        The wall time is *after* the build date, so the first check passes; what
        fails is the implication that the device booted before its own software
        existed. That is the case a single "is the clock sane" check misses.
        """
        # The build is a day old and the board claims only 60 s of uptime, so it
        # implies it booted 23 hours before its own software was built.
        result = cc.check_boot_clock(
            wall_now="2026-01-01T00:01:00Z",
            monotonic_uptime_seconds=3600,
            build_date=BUILD,
        )
        assert result.trusted is False
        # It is the second check that fired, not the first.
        assert "implied boot wall time" in result.reason
        assert result.offsets["clock_lead_seconds"] > 0

    def test_a_healthy_boot_is_trusted(self) -> None:
        result = cc.check_boot_clock(
            wall_now="2026-11-14T02:00:00Z",
            monotonic_uptime_seconds=3600,
            build_date=BUILD,
        )
        assert result.trusted is True
        assert result.offsets["boot_wall_utc"]

    def test_a_refused_clock_blocks_the_timestamp_but_not_the_record(self) -> None:
        """The record may still be evidence; it is the timestamp that is unusable."""
        result = cc.check_boot_clock(
            wall_now="2020-01-01T00:00:00Z",
            monotonic_uptime_seconds=60,
            build_date=BUILD,
        )
        assert cc.evidence_timestamp_allowed(result) is False
        # The record fields carry the reason, so a reader can see what was refused.
        fields = result.as_record_fields()
        assert fields["wall_clock_trusted"] is False
        assert "predates" in fields["wall_clock_reason"]

    def test_a_trusted_clock_allows_the_timestamp(self) -> None:
        result = cc.check_boot_clock(
            wall_now="2026-11-14T02:00:00Z",
            monotonic_uptime_seconds=3600,
            build_date=BUILD,
        )
        assert cc.evidence_timestamp_allowed(result) is True


class TestTheReferenceOffset:
    def test_an_offset_inside_the_tolerance_is_trusted(self) -> None:
        result = cc.check_reference_offset(
            board_time="2026-11-14T02:00:00.200Z",
            reference_time="2026-11-14T02:00:00Z",
            tolerance=timedelta(seconds=1),
        )
        assert result.trusted is True
        assert result.offsets["clock_offset_seconds"] == pytest.approx(0.2)

    def test_an_offset_outside_the_tolerance_is_refused_and_recorded(self) -> None:
        result = cc.check_reference_offset(
            board_time="2026-11-14T05:00:00Z",
            reference_time="2026-11-14T02:00:00Z",
            tolerance=timedelta(seconds=1),
        )
        assert result.trusted is False
        # The number is recorded, not an absence.
        assert result.offsets["clock_offset_seconds"] == pytest.approx(10800.0)
        assert "outside" in result.reason

    def test_the_record_carries_the_fields_bringup_requires(self) -> None:
        """`bringup-plan.yaml` requires timestamp_utc and raw_capture_hash."""
        plan = yaml.safe_load((ROOT / "bsp/validation/bringup-plan.yaml").read_text(encoding="utf-8"))
        required = set(plan["evidence"]["required_fields"])
        assert {"timestamp_utc", "raw_capture_hash"} <= required
        fields = cc.check_reference_offset(
            board_time="2026-11-14T02:00:00Z",
            reference_time="2026-11-14T02:00:00Z",
        ).as_record_fields()
        assert "timestamp_utc" in fields


class TestTheClockReportItself:
    @pytest.mark.parametrize("value", [None, "", 7, object()])
    def test_a_malformed_timestamp_is_refused(self, value: object) -> None:
        with pytest.raises(cc.ClockError, match="must be an RFC 3339 timestamp"):
            cc.check_boot_clock(wall_now=value, monotonic_uptime_seconds=1, build_date=BUILD)

    @pytest.mark.parametrize("value", [-1, "60", None])
    def test_a_malformed_uptime_is_refused(self, value: object) -> None:
        with pytest.raises(cc.ClockError, match="monotonic_uptime_seconds"):
            cc.check_boot_clock(wall_now="2026-11-14T02:00:00Z", monotonic_uptime_seconds=value, build_date=BUILD)

    def test_a_zero_tolerance_is_refused(self) -> None:
        with pytest.raises(cc.ClockError, match="tolerance must be positive"):
            cc.check_reference_offset(
                board_time="2026-11-14T02:00:00Z",
                reference_time="2026-11-14T02:00:00Z",
                tolerance=timedelta(0),
            )

    def test_a_z_suffixed_timestamp_is_accepted(self) -> None:
        """`Z` is valid RFC 3339; refusing it would reject honest records."""
        result = cc.check_reference_offset(
            board_time="2026-11-14T02:00:00Z",
            reference_time="2026-11-14T02:00:00Z",
        )
        assert result.trusted is True

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        result = cc.check_reference_offset(
            board_time=datetime(2026, 11, 14, 2, 0, 0),
            reference_time="2026-11-14T02:00:00Z",
        )
        assert result.trusted is True


class TestTheServiceIsInTheTopology:
    def test_the_service_is_declared(self) -> None:
        services = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))["services"]
        assert "robot-time-sync" in services
        spec = services["robot-time-sync"]
        assert spec["restart"] == "on-failure"
        assert spec["safety_authority"] is False

    def test_the_unit_exists_and_matches(self) -> None:
        unit = (SYSTEMD / "robot-time-sync.service.in").read_text(encoding="utf-8")
        assert "After=network-online.target" in unit
        assert "Restart=on-failure" in unit
        assert "StartLimitBurst=" in unit
        assert "NoNewPrivileges=true" in unit
        assert "ProtectSystem=strict" in unit

    def test_the_evidence_logger_starts_after_the_clock(self) -> None:
        """A logger that starts before the clock stamps a wrong wall time on every record."""
        unit = (SYSTEMD / "robot-evidence-logger.service.in").read_text(encoding="utf-8")
        assert "robot-time-sync.service" in unit

    def test_the_validator_agrees(self) -> None:
        import validate_manifests

        assert validate_manifests.validate_service_topology() == []

    def test_a_unit_ordered_before_the_clock_fails_validation(self) -> None:
        """The ordering test proves the gate rather than describing it."""
        import validate_manifests

        original = (SYSTEMD / "robot-evidence-logger.service.in").read_text(encoding="utf-8")
        try:
            (SYSTEMD / "robot-evidence-logger.service.in").write_text(
                original.replace("After=local-fs.target robot-time-sync.service", "After=local-fs.target"),
                encoding="utf-8",
            )
            failures = validate_manifests.validate_service_topology()
        finally:
            (SYSTEMD / "robot-evidence-logger.service.in").write_text(original, encoding="utf-8")

        assert any("robot-time-sync" in failure for failure in failures), failures

    def test_the_wall_clock_gate_is_declared(self) -> None:
        gates = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))["gates"]
        assert gates["wall_clock_unverified_refuses_evidence"] is True


class TestTheContainerBoundaryIsDocumented:
    def test_the_page_says_a_container_cannot_repair_the_host_clock(self) -> None:
        page = (ROOT / "docs" / "hardware" / "time-synchronisation.md").read_text(encoding="utf-8")
        assert "inherit the host clock" in page
        assert "cannot repair a wrong host" in page

    def test_the_page_states_why_there_is_no_container_side_fix(self) -> None:
        page = (ROOT / "docs" / "hardware" / "time-synchronisation.md").read_text(encoding="utf-8")
        assert "no** container-side time fix" in page or "no container-side time fix" in page

    def test_the_page_claims_no_physical_boot(self) -> None:
        page = (ROOT / "docs" / "hardware" / "time-synchronisation.md").read_text(encoding="utf-8")
        assert "No physical boot was performed" in page
