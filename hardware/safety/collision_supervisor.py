"""An independent speed-and-separation supervisor for the mobile base (#550).

The robot is **77.5 kg at 1.2 m/s** (`hardware/mechanical/generated/analysis.json`,
`REV-D-MASS-001`), and its only forward sensor is a head RGB-D camera. The
repository has no LiDAR: `grep -i lidar hardware/mechanical/design-spec.json`
matches nothing. A planner-side costmap is a **planning obstacle**, and
`robot/description/revision_d/README.md` says so in those words: collision geometry
"is not continuous collision safety, and it authorizes no execution."

So this module is deliberately not part of the planner. It consumes range directly,
reduces velocity inside a warning zone, stops inside a protective zone, and works with
the planner disabled. A supervisor that needed the planner to be healthy would be
another component that fails at the same moment the planner does.

The stopping distance is the reason the zones exist
---------------------------------------------------

At 1.2 m/s a 77.5 kg base needs a distance to stand still, and that distance is not
zero. The default protective zone is derived from a conservative deceleration rather
than picked to look safe, and the derivation is in the config file with its arithmetic
so a reader can check it.

The four rules this module keeps
--------------------------------

* **A stale or missing range source is the protective zone.** A sensor that stopped
  answering has range `unknown`, and unknown is not clear space. Treating it as clear
  is how a supervisor becomes a decoration.
* **Coverage is published.** The field of view and the blind regions travel with the
  decision, so a run record says what the layer could not see.
* **A false positive and a missed detection are different outcomes.** Neither is
  smoothed away: a deceleration that should not have happened is recorded as
  `false_positive`, and a stop that came too late is `missed_detection`.
* **The reduction is a function, not a table of magic numbers.** Inside the warning
  zone the limit is interpolated between the protective and warning distances, so the
  behaviour at any given range is predictable and testable.

What this does not claim
------------------------

It is not a certified safety function. It is not a substitute for the hardware e-stop
in `hardware/safety/README.md`, which is a separate circuit and has authority over
motion regardless of software state. It publishes a velocity limit and a stop request;
honouring them is the base controller's job, and whether it honours them is not
proven here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

#: The deceleration the protective distance assumes, in m/s^2. A floor value rather
#: than a measured one: the wheel/floor coefficient is not characterised, so this is
#: the conservative end of what a urethane tread on a hard floor provides.
DEFAULT_DECELERATION_MPS2 = 1.0

#: The base speed the repository documents, used to derive the protective distance.
DEFAULT_MAX_SPEED_MPS = 1.2

#: Reaction latency budget: sensor frame age plus one control cycle. Taken from the
#: camera's 15 Hz stream, which is the slowest input this layer reads.
DEFAULT_REACTION_S = 0.15


class Zone(StrEnum):
    """The three outcomes a range reading can produce."""

    CLEAR = "clear"
    WARNING = "warning"
    PROTECTIVE = "protective"


class Outcome(StrEnum):
    """What the supervisor decided, including the two failure kinds."""

    NOMINAL = "nominal"
    SLOWED = "slowed"
    STOP_REQUESTED = "stop_requested"
    #: Slowed or stopped when no obstacle warranted it.
    FALSE_POSITIVE = "false_positive"
    #: Did not stop in time for an obstacle that was there.
    MISSED_DETECTION = "missed_detection"


class SupervisorConfigError(RuntimeError):
    """The configuration would make the supervisor unsound, so it must not start."""


@dataclass(frozen=True)
class Geometry:
    """Where the sensor sees and where it does not.

    Published with every decision because a supervisor that reports "clear" over a
    blind region is lying by omission rather than by intent.
    """

    horizontal_fov_deg: float
    min_range_m: float
    max_range_m: float

    def blind_regions(self) -> tuple[str, ...]:
        """The regions this sensor cannot see, as named strings for a run record."""
        coverage = 360.0 - self.horizontal_fov_deg
        return (
            f"behind the sensor: {coverage:.0f} degrees uncovered",
            f"closer than {self.min_range_m:.2f} m",
            f"beyond {self.max_range_m:.2f} m",
        )


@dataclass(frozen=True)
class Zones:
    """The two thresholds, with the arithmetic that produced them."""

    warning_m: float
    protective_m: float
    max_speed_mps: float = DEFAULT_MAX_SPEED_MPS

    def validate(self) -> None:
        if self.protective_m <= 0 or self.warning_m <= 0:
            raise SupervisorConfigError("both zone distances must be positive")
        # The protective zone must be strictly inside the warning zone. Equal values
        # would make the warning zone unreachable, so a run would either always slow
        # or always stop, and neither is what the two-zone design means.
        if self.protective_m >= self.warning_m:
            raise SupervisorConfigError(
                f"protective_m ({self.protective_m}) must be strictly less than "
                f"warning_m ({self.warning_m}); equal thresholds make one zone unreachable"
            )
        if self.max_speed_mps <= 0:
            raise SupervisorConfigError("max_speed_mps must be positive")

    def classify(self, range_m: float | None) -> Zone:
        """Classify a reading. `None` means unknown, which is protective."""
        if range_m is None:
            return Zone.PROTECTIVE
        if range_m <= self.protective_m:
            return Zone.PROTECTIVE
        if range_m <= self.warning_m:
            return Zone.WARNING
        return Zone.CLEAR

    def stopped_distance_m(self, reaction_s: float = DEFAULT_REACTION_S) -> float:
        """The distance travelled before standstill from full speed.

        `v * t_reaction + v^2 / (2 a)`. The first term is how far the base travels
        while nothing has decided yet, which is why a supervisor cannot be evaluated
        on braking alone.
        """
        if reaction_s < 0:
            raise SupervisorConfigError("reaction_s cannot be negative")
        braking = (self.max_speed_mps**2) / (2.0 * DEFAULT_DECELERATION_MPS2)
        return self.max_speed_mps * reaction_s + braking


def default_zones() -> Zones:
    """The committed default, with the protective zone derived rather than chosen.

    From 1.2 m/s at 1.0 m/s^2 with a 0.15 s reaction budget the base needs
    `1.2 * 0.15 + 1.2^2 / 2` = 0.18 + 0.72 = **0.90 m** to stop. The protective zone
    is that distance with a margin, and the warning zone is where slowing begins,
    far enough out that the base can reach the protective distance at the reduced
    limit rather than at full speed.
    """
    zones = Zones(warning_m=2.4, protective_m=1.0)
    zones.validate()
    return zones


@dataclass(frozen=True)
class Decision:
    """One supervisor verdict, with everything a run record needs."""

    zone: Zone
    outcome: Outcome
    limit_fraction: float
    stop_requested: bool
    reason: str
    range_m: float | None
    blind_regions: tuple[str, ...]
    #: Set when the previous decision said `clear` and an obstacle then appeared
    #: inside the stopped distance, which is a detection that came too late.
    late_by_m: float | None = None

    def as_record_fields(self) -> dict[str, object]:
        return {
            "zone": self.zone.value,
            "outcome": self.outcome.value,
            "limit_fraction": self.limit_fraction,
            "stop_requested": self.stop_requested,
            "supervisor_reason": self.reason,
            "range_m": self.range_m,
            "blind_regions": list(self.blind_regions),
            "late_by_m": self.late_by_m,
        }


@dataclass
class CollisionSupervisor:
    """The supervisor. Holds no state that outlives a run except the last zone."""

    zones: Zones = field(default_factory=default_zones)
    geometry: Geometry = field(
        default_factory=lambda: Geometry(horizontal_fov_deg=87.0, min_range_m=0.2, max_range_m=3.0)
    )
    reaction_s: float = DEFAULT_REACTION_S
    _previous_zone: Zone | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.zones.validate()
        if not 0.0 < self.geometry.horizontal_fov_deg <= 360.0:
            raise SupervisorConfigError("horizontal_fov_deg must be in (0, 360]")
        if not 0.0 < self.geometry.min_range_m < self.geometry.max_range_m:
            raise SupervisorConfigError("min_range_m must be positive and below max_range_m")
        if self.reaction_s < 0:
            raise SupervisorConfigError("reaction_s cannot be negative")

    def evaluate(self, range_m: float | None) -> Decision:
        """Decide the velocity limit for one range reading.

        `range_m` of `None` means the source is stale or missing, which is treated as
        the protective zone. That is the single most important line in this module: a
        sensor that stopped answering must not read as clear space.
        """
        if range_m is not None:
            if range_m < 0:
                raise SupervisorConfigError("a negative range is not a measurement")
            # Beyond the sensor's own limit is unknown, not clear.
            if range_m > self.geometry.max_range_m:
                range_m = None

        zone = self.zones.classify(range_m)
        blind = self.geometry.blind_regions()

        if zone is Zone.CLEAR:
            decision = Decision(
                zone=zone,
                outcome=Outcome.NOMINAL,
                limit_fraction=1.0,
                stop_requested=False,
                reason="no obstacle inside the warning zone",
                range_m=range_m,
                blind_regions=blind,
            )
        elif zone is Zone.WARNING:
            # Interpolate: full speed at the warning edge, the protective limit at the
            # protective edge. A step function here would brake hard at one range and
            # not at all one centimetre further out.
            span = self.zones.warning_m - self.zones.protective_m
            fraction = (range_m - self.zones.protective_m) / span if range_m is not None else 0.0
            fraction = max(0.0, min(1.0, fraction))
            decision = Decision(
                zone=zone,
                outcome=Outcome.SLOWED,
                limit_fraction=round(fraction, 3),
                stop_requested=False,
                reason=(
                    f"range {range_m:.2f} m is inside the warning zone "
                    f"({self.zones.protective_m:.2f}-{self.zones.warning_m:.2f} m); "
                    f"limit reduced to {fraction:.0%}"
                ),
                range_m=range_m,
                blind_regions=blind,
            )
        else:
            late: float | None = None
            if range_m is not None:
                stopped = self.zones.stopped_distance_m(self.reaction_s)
                if range_m < stopped:
                    # The obstacle is closer than the distance needed to stop. The
                    # reading is real and the stop is still requested, but the run
                    # record must say the detection came too late.
                    late = round(stopped - range_m, 3)
            decision = Decision(
                zone=zone,
                outcome=Outcome.MISSED_DETECTION if late else Outcome.STOP_REQUESTED,
                limit_fraction=0.0,
                stop_requested=True,
                reason=(
                    f"range {range_m:.2f} m is inside the protective zone"
                    if range_m is not None
                    else "range source is stale or missing, which is the protective zone, not clear space"
                ),
                range_m=range_m,
                blind_regions=blind,
                late_by_m=late,
            )

        self._previous_zone = zone
        return decision

    def evaluate_with_expected(self, range_m: float | None, *, obstacle_present: bool) -> Decision:
        """Evaluate and label the decision against a known ground truth.

        This exists because the two failure directions are different findings and a
        supervisor that only reports "stopped" cannot tell them apart:

        * stopping or slowing with no obstacle is a **false positive**;
        * not stopping with an obstacle is a **missed detection**, and is reported as
          such rather than as a nominal outcome.
        """
        decision = self.evaluate(range_m)
        reacted = decision.outcome in {
            Outcome.STOP_REQUESTED,
            Outcome.SLOWED,
            Outcome.MISSED_DETECTION,
        }

        # Ground truth about the absence of an obstacle comes first. `evaluate` labels a
        # reading closer than the stopping distance as "late" on the assumption the
        # obstacle is real; when nothing is there, the reading was spurious and the
        # supervisor stopping for it is a false positive, not a late detection. A
        # missed detection needs something to have been missed.
        if reacted and not obstacle_present:
            return Decision(**{**decision.__dict__, "outcome": Outcome.FALSE_POSITIVE})
        if reacted:
            return decision

        # A nominal outcome with an obstacle present is the dangerous direction: the
        # layer was given a reading that did not trigger it while an obstacle was
        # there. Reported as a missed detection, never smoothed into nominal.
        if obstacle_present:
            return Decision(
                **{
                    **decision.__dict__,
                    "outcome": Outcome.MISSED_DETECTION,
                    "late_by_m": round(self.zones.stopped_distance_m(self.reaction_s), 3),
                }
            )
        return decision
