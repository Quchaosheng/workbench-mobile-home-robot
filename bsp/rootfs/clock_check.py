"""Refuse an evidence record whose wall timestamp cannot be believed (#594).

The robot has no committed time configuration. `bsp/rootfs/services.yaml` declared
six services and none synchronised the clock, so a Jetson without an RTC battery
boots at a plausible wrong wall time and every wall-stamped evidence record is then
wrong by a constant offset.

The failure is quiet rather than loud, which is what makes it worth a module. The
monotonic path is correct; the tests use injected clocks; so nothing in the
repository notices that `occurred_at` is hours off. `observed_at` is not changed by
this issue — the clock id vocabulary (`monotonic`, `wall`) is unchanged — what
changes is that a record stamped from a clock nobody verified is **refused rather
than accepted**.

The three questions this module answers
---------------------------------------

1. Is the wall clock earlier than the build date? A clock that predates the
   software running on it has not been set.
2. Is the wall clock later than the monotonic uptime can explain? A boot at time T
   with an uptime of U cannot have a wall time greater than T + U; a bigger value
   means the clock was stepped forward without the uptime agreeing.
3. Is the offset from the reference within the tolerance the bring-up step records?

Each is reported rather than stamped. An unverified clock does not make the record
`NOT_EXECUTED` — the record may be perfectly good evidence about the device — it
marks the **timestamp** as unusable and names which check failed, so a reader can
still use the record for what it does support.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class ClockError(RuntimeError):
    """The clock report itself is malformed, which is always a refusal."""


#: The tolerance a verified clock must stay inside. Chosen at one second because the
#: bring-up step records a measured offset and a wall stamp is only useful to about
#: that resolution; a tighter bound would refuse honest hardware.
DEFAULT_TOLERANCE = timedelta(seconds=1)


@dataclass(frozen=True)
class ClockCheck:
    """The verdict for one boot's clock, with the reason it failed if it did."""

    trusted: bool
    reason: str
    offsets: dict[str, float | str | bool | None]

    def as_record_fields(self) -> dict[str, object]:
        """The fields a bring-up record carries, matching what the plan requires."""
        return {
            "wall_clock_trusted": self.trusted,
            "wall_clock_reason": self.reason,
            **{key: value for key, value in self.offsets.items() if value is not None},
        }


def _parse(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        # `Z` is a valid RFC 3339 suffix that `fromisoformat` did not accept before
        # 3.11; normalise it so a committed record is not refused for its spelling.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ClockError(f"{field} is not an RFC 3339 timestamp: {value!r}") from exc
    else:
        raise ClockError(f"{field} must be an RFC 3339 timestamp, got {value!r}")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def check_boot_clock(
    *,
    wall_now: object,
    monotonic_uptime_seconds: object,
    build_date: object,
    tolerance: timedelta = DEFAULT_TOLERANCE,
) -> ClockCheck:
    """Decide whether a boot's wall clock can be believed.

    ``wall_now`` is the wall clock the device reports; ``monotonic_uptime_seconds``
    is how long the device has been up by its own monotonic clock; ``build_date`` is
    the configured build date the clock cannot legitimately predate.
    """
    wall = _parse(wall_now, "wall_now")
    built = _parse(build_date, "build_date")
    if not isinstance(monotonic_uptime_seconds, (int, float)) or isinstance(monotonic_uptime_seconds, bool):
        raise ClockError("monotonic_uptime_seconds must be a number")
    if monotonic_uptime_seconds < 0:
        raise ClockError("monotonic_uptime_seconds cannot be negative")
    if tolerance <= timedelta(0):
        raise ClockError("tolerance must be positive")

    offsets: dict[str, float | str | bool | None] = {
        "timestamp_utc": wall.isoformat(),
        "build_date": built.isoformat(),
        "monotonic_uptime_seconds": float(monotonic_uptime_seconds),
    }

    # 1. A clock that predates the software running on it has not been set.
    if wall < built:
        offsets["clock_lag_seconds"] = (built - wall).total_seconds()
        return ClockCheck(
            trusted=False,
            reason=(
                f"wall clock {wall.isoformat()} predates the build date {built.isoformat()}; the clock was never set"
            ),
            offsets=offsets,
        )

    # 2. A boot at a wall time of `boot` with an uptime of U must still read
    #    `boot + U`. Reading further ahead means the clock was stepped without the
    #    uptime agreeing, which is exactly what a wrong RTC plus a later sync does.
    boot_wall = wall - timedelta(seconds=monotonic_uptime_seconds)
    if boot_wall < built:
        offsets["boot_wall_utc"] = boot_wall.isoformat()
        offsets["clock_lead_seconds"] = (built - boot_wall).total_seconds()
        return ClockCheck(
            trusted=False,
            reason=(
                f"implied boot wall time {boot_wall.isoformat()} predates the build date "
                f"{built.isoformat()} given an uptime of {monotonic_uptime_seconds}s; "
                "the clock was stepped forward after boot"
            ),
            offsets=offsets,
        )

    offsets["boot_wall_utc"] = boot_wall.isoformat()
    return ClockCheck(
        trusted=True,
        reason=f"wall clock {wall.isoformat()} is consistent with an uptime of {monotonic_uptime_seconds}s",
        offsets=offsets,
    )


def check_reference_offset(
    *,
    board_time: object,
    reference_time: object,
    tolerance: timedelta = DEFAULT_TOLERANCE,
) -> ClockCheck:
    """Compare the board clock to an external reference before any capture.

    The bring-up plan already requires a `timestamp_utc` and a `raw_capture_hash`;
    this adds the measured offset beside them, so a wrong clock is a recorded
    number rather than an absence.
    """
    board = _parse(board_time, "board_time")
    reference = _parse(reference_time, "reference_time")
    if tolerance <= timedelta(0):
        raise ClockError("tolerance must be positive")

    delta = (board - reference).total_seconds()
    trusted = abs(delta) <= tolerance.total_seconds()
    return ClockCheck(
        trusted=trusted,
        reason=(
            f"board clock is {delta:+.3f}s from the reference, "
            f"{'within' if trusted else 'outside'} the {tolerance.total_seconds():.3f}s tolerance"
        ),
        offsets={
            "timestamp_utc": board.isoformat(),
            "reference_utc": reference.isoformat(),
            "clock_offset_seconds": round(delta, 6),
        },
    )


def evidence_timestamp_allowed(check: ClockCheck) -> bool:
    """Whether a wall timestamp from this clock may be stamped on a record.

    A refused clock does not discard the record: it marks the timestamp unusable.
    Stated as a function so the caller cannot decide for itself that a small
    discrepancy is fine.
    """
    if not isinstance(check, ClockCheck):
        raise ClockError("evidence_timestamp_allowed requires a ClockCheck")
    return check.trusted
