"""The collision supervisor stops a 77.5 kg base and says what it could not see (#550).

The robot is 77.5 kg at 1.2 m/s (`REV-D-MASS-001`) with a head RGB-D camera and no
LiDAR. A planner-side costmap is a planning obstacle, and
`robot/description/revision_d/README.md` says collision geometry "is not continuous
collision safety, and it authorizes no execution."

`hardware/safety/collision_supervisor.py` is the independent layer: it consumes range
directly, slows inside a warning zone, stops inside a protective zone, and works with
the planner disabled.

These tests are pure logic and run anywhere. What they do not prove: that a real
sensor measured a real distance, that the base honoured the limit, or that a stop was
physically achieved. Those stay `NOT_EXECUTED`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "hardware" / "safety"))

import collision_supervisor as cs


class TestTheConfigurationIsValidated:
    def test_the_defaults_are_sound(self) -> None:
        supervisor = cs.CollisionSupervisor()
        assert supervisor.zones.protective_m < supervisor.zones.warning_m

    def test_equal_zones_are_refused(self) -> None:
        """Equal thresholds make one zone unreachable, so the design is not the design."""
        with pytest.raises(cs.SupervisorConfigError, match="strictly less"):
            cs.CollisionSupervisor(zones=cs.Zones(warning_m=1.0, protective_m=1.0))

    def test_a_protective_zone_beyond_the_warning_zone_is_refused(self) -> None:
        with pytest.raises(cs.SupervisorConfigError, match="strictly less"):
            cs.CollisionSupervisor(zones=cs.Zones(warning_m=0.5, protective_m=1.5))

    @pytest.mark.parametrize("distance", [0.0, -1.0])
    def test_a_nonpositive_zone_is_refused(self, distance: float) -> None:
        with pytest.raises(cs.SupervisorConfigError, match="positive"):
            cs.CollisionSupervisor(zones=cs.Zones(warning_m=distance, protective_m=distance))

    def test_an_impossible_field_of_view_is_refused(self) -> None:
        with pytest.raises(cs.SupervisorConfigError, match="horizontal_fov_deg"):
            cs.CollisionSupervisor(geometry=cs.Geometry(horizontal_fov_deg=0.0, min_range_m=0.2, max_range_m=3.0))

    def test_a_nonsensical_range_band_is_refused(self) -> None:
        with pytest.raises(cs.SupervisorConfigError, match="min_range_m"):
            cs.CollisionSupervisor(geometry=cs.Geometry(horizontal_fov_deg=87.0, min_range_m=5.0, max_range_m=3.0))


class TestTheStoppingDistance:
    def test_the_distance_includes_the_reaction_term(self) -> None:
        """Braking alone understates it: nothing has decided during the reaction time."""
        zones = cs.Zones(warning_m=2.4, protective_m=1.0)
        # 1.2 * 0.15 + 1.2^2 / (2 * 1.0) = 0.18 + 0.72
        assert zones.stopped_distance_m(0.15) == pytest.approx(0.90)

    def test_the_protective_zone_is_at_least_the_stopping_distance(self) -> None:
        """A protective zone smaller than the stop distance would request a stop too late."""
        zones = cs.default_zones()
        assert zones.protective_m >= zones.stopped_distance_m(cs.DEFAULT_REACTION_S)

    def test_a_negative_reaction_is_refused(self) -> None:
        with pytest.raises(cs.SupervisorConfigError, match="reaction_s"):
            cs.Zones(warning_m=2.4, protective_m=1.0).stopped_distance_m(-0.1)


class TestTheThreeZones:
    def test_far_away_is_clear_and_nominal(self) -> None:
        decision = cs.CollisionSupervisor().evaluate(3.0)
        assert decision.zone is cs.Zone.CLEAR
        assert decision.outcome is cs.Outcome.NOMINAL
        assert decision.limit_fraction == 1.0
        assert decision.stop_requested is False

    def test_the_warning_zone_reduces_the_limit(self) -> None:
        decision = cs.CollisionSupervisor().evaluate(2.0)
        assert decision.zone is cs.Zone.WARNING
        assert decision.outcome is cs.Outcome.SLOWED
        assert 0.0 < decision.limit_fraction < 1.0
        # The reduction is visible in the reason, not applied silently.
        assert "limit reduced" in decision.reason

    def test_the_reduction_is_monotonic_across_the_warning_zone(self) -> None:
        """A step function would brake hard at one range and not at all slightly further out."""
        supervisor = cs.CollisionSupervisor()
        limits = [supervisor.evaluate(r).limit_fraction for r in (2.4, 2.0, 1.6, 1.2, 1.05)]
        assert limits == sorted(limits, reverse=True)
        assert len(set(limits)) == len(limits), "the interpolation should be continuous, not stepped"

    def test_the_protective_zone_requests_a_stop(self) -> None:
        decision = cs.CollisionSupervisor().evaluate(1.0)
        assert decision.zone is cs.Zone.PROTECTIVE
        assert decision.stop_requested is True
        assert decision.limit_fraction == 0.0


class TestAStaleSourceIsTheProtectiveZone:
    def test_a_missing_reading_is_protective_not_clear(self) -> None:
        """The single most important line: unknown is not clear space."""
        decision = cs.CollisionSupervisor().evaluate(None)
        assert decision.zone is cs.Zone.PROTECTIVE
        assert decision.stop_requested is True
        assert "not clear space" in decision.reason

    def test_a_reading_beyond_the_sensor_limit_is_unknown_not_clear(self) -> None:
        """Past its own range the sensor has no information, which is not the same as no obstacle."""
        supervisor = cs.CollisionSupervisor(
            geometry=cs.Geometry(horizontal_fov_deg=87.0, min_range_m=0.2, max_range_m=3.0)
        )
        decision = supervisor.evaluate(9.9)
        assert decision.zone is cs.Zone.PROTECTIVE
        assert decision.range_m is None

    def test_a_negative_reading_is_refused(self) -> None:
        with pytest.raises(cs.SupervisorConfigError, match="negative range"):
            cs.CollisionSupervisor().evaluate(-0.5)


class TestTheTwoFailureDirectionsAreDistinct:
    def test_slowing_for_nothing_is_a_false_positive(self) -> None:
        decision = cs.CollisionSupervisor().evaluate_with_expected(2.0, obstacle_present=False)
        assert decision.outcome is cs.Outcome.FALSE_POSITIVE
        assert decision.outcome is not cs.Outcome.SLOWED

    def test_stopping_for_nothing_is_a_false_positive(self) -> None:
        decision = cs.CollisionSupervisor().evaluate_with_expected(0.5, obstacle_present=False)
        assert decision.outcome is cs.Outcome.FALSE_POSITIVE

    def test_not_reacting_to_an_obstacle_is_a_missed_detection(self) -> None:
        """The dangerous direction, reported as its own outcome rather than nominal.

        The reading is comfortably inside the sensor's range and outside the warning
        zone, so the supervisor stays nominal while an obstacle is present. That is
        exactly the case a "did the supervisor stop?" check cannot see.
        """
        decision = cs.CollisionSupervisor().evaluate_with_expected(2.9, obstacle_present=True)
        assert decision.outcome is cs.Outcome.MISSED_DETECTION
        assert decision.late_by_m is not None

    def test_an_obstacle_inside_the_stopping_distance_is_late_not_nominal(self) -> None:
        """Stopping is still requested, and the record says the detection came too late."""
        decision = cs.CollisionSupervisor().evaluate(0.5)
        assert decision.stop_requested is True
        assert decision.outcome is cs.Outcome.MISSED_DETECTION
        assert decision.late_by_m == pytest.approx(0.4, abs=0.01)

    def test_a_spurious_close_reading_with_no_obstacle_is_a_false_positive(self) -> None:
        """A reading closer than the stop distance is not a late detection if nothing is there.

        This is the mirror of the case above and the reason ground truth has to be
        consulted before the label `evaluate` produces: a missed detection needs
        something to have been missed.
        """
        decision = cs.CollisionSupervisor().evaluate_with_expected(0.5, obstacle_present=False)
        assert decision.outcome is cs.Outcome.FALSE_POSITIVE
        assert decision.stop_requested is True

    def test_a_correct_stop_is_not_labelled_a_failure(self) -> None:
        decision = cs.CollisionSupervisor().evaluate_with_expected(1.0, obstacle_present=True)
        assert decision.outcome is cs.Outcome.STOP_REQUESTED


class TestCoverageIsPublished:
    def test_every_decision_carries_the_blind_regions(self) -> None:
        """A supervisor that reports clear over a blind region is lying by omission."""
        decision = cs.CollisionSupervisor().evaluate(5.0)
        assert decision.blind_regions
        joined = " ".join(decision.blind_regions)
        assert "degrees uncovered" in joined
        assert "closer than" in joined
        assert "beyond" in joined

    def test_the_blind_regions_are_in_the_record_fields(self) -> None:
        fields = cs.CollisionSupervisor().evaluate(2.0).as_record_fields()
        assert fields["blind_regions"]
        assert fields["zone"] == "warning"
        assert fields["supervisor_reason"]

    def test_a_partial_field_of_view_has_a_named_blind_arc(self) -> None:
        geometry = cs.Geometry(horizontal_fov_deg=87.0, min_range_m=0.2, max_range_m=3.0)
        behind = geometry.blind_regions()[0]
        assert "273 degrees uncovered" in behind


class TestTheLayerDoesNotNeedAPlanner:
    def test_a_stop_is_produced_with_no_planner_anywhere(self) -> None:
        """The module imports nothing from the agent runtime or the planner."""
        source = (ROOT / "hardware" / "safety" / "collision_supervisor.py").read_text(encoding="utf-8")
        for forbidden in (
            "workbench_agent_runtime",
            "planner",
            "nav2",
            "costmap",
        ):
            assert f"import {forbidden}" not in source
            assert f"from {forbidden}" not in source

    def test_the_stop_is_a_limit_and_a_request_not_a_motor_command(self) -> None:
        """It publishes a limit; honouring it is the base controller's job."""
        fields = cs.CollisionSupervisor().evaluate(0.5).as_record_fields()
        assert fields["stop_requested"] is True
        assert fields["limit_fraction"] == 0.0
        # No motor or actuator field: this layer must not claim it moved anything.
        assert not any("motor" in str(key) or "actuator" in key for key in fields)

    def test_it_does_not_claim_to_be_a_certified_function(self) -> None:
        source = (ROOT / "hardware" / "safety" / "collision_supervisor.py").read_text(encoding="utf-8")
        assert "not a certified safety function" in source
        assert "hardware e-stop" in source
