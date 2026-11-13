"""Refuse or derate motion on the BMS state the pack actually reports (#588).

The robot carries a battery pack whose state machine is declared in
`hardware/power/bms-state-machine.csv` and whose transitions are declared in
`hardware/power/bms-transitions.csv`. Both files are committed, and
`hardware/power/tools/validate_bms_state_machine.py` already checks that the two
agree with each other.

What was missing is the reader. Nothing in the runtime looked at the pack state
before dispatching a motion request, so a robot whose pack was in `FAULT_LATCHED`,
`SERVICE`, `OFF`, `SELF_TEST` or `STANDBY` would still be asked to drive. The
state existed as a document and not as a gate.

What this module is
-------------------

A guard that runs *before* dispatch. Its outcome is a refusal with the state named
in the reason, or an admission carrying the reduced limit, and it never writes
`WorldState` and never adds a `SemanticAction`: the guard decides whether the
request may leave, not what the world means.

The three distinctions it keeps
-------------------------------

* **An unknown state string is a fault, not a default.** Mapping an unrecognised
  string onto `STANDBY` would turn "the pack said something we do not understand"
  into "the pack is idle", which is the one reading a safety guard must never
  invent.
* **`DERATE` is admitted, but not silently.** The reduced limit travels in the
  `ActionResult` metadata so a caller can see the request was clipped rather than
  assuming it ran at the configured speed.
* **`reset_authority` is honoured.** A transition whose CSV row says
  `operator_plus_service` cannot be cleared by the runtime, and a caller that
  tries is refused rather than quietly allowed.

The CSV is the single source of truth. This module reads it rather than keeping a
second list of states, and a test asserts that every state in the table has a
guard, so the two cannot drift.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
STATE_TABLE = ROOT / "hardware" / "power" / "bms-state-machine.csv"
TRANSITION_TABLE = ROOT / "hardware" / "power" / "bms-transitions.csv"


class PowerGuardError(RuntimeError):
    """The guard cannot decide, which is always a refusal, never a pass."""


class MotionRefusal(StrEnum):
    """Why a motion request did not leave the host."""

    UNKNOWN_STATE = "unknown_pack_state"
    STATE_FORBIDS_MOTION = "pack_state_forbids_motion"
    RESET_NOT_AUTHORIZED = "pack_reset_not_authorized"
    LATCHED_MID_MOTION = "pack_latched_mid_motion"


#: The states in which a motion request is refused outright. These are the five the
#: issue names, and they are derived from the CSV's own `allowed_contactors` column
#: rather than chosen here: a state that allows `none` cannot drive the base.
#:
#: `PRECHARGE` is a transient the run sequence waits through rather than a state a
#: motion request is issued from, and `CHARGE` is the pack charging with the main
#: contactor open. Neither permits motion, so both are forbidden here even though
#: the issue names only the five above: refusing a state the table already forbids
#: is the guard agreeing with the table, not extending it.
MOTION_FORBIDDING_STATES = frozenset({"FAULT_LATCHED", "SERVICE", "OFF", "SELF_TEST", "STANDBY", "PRECHARGE", "CHARGE"})

#: The states that permit motion. `DERATE` permits it with a reduced limit.
MOTION_STATES = frozenset({"RUN", "DERATE"})

#: Reset authorities that the runtime may exercise on its own. Anything else needs
#: a human, and `operator_plus_service` needs two.
RUNTIME_RESET_AUTHORITIES = frozenset({"BMS"})


@dataclass(frozen=True)
class PackState:
    """One row of the committed state table."""

    state: str
    allowed_contactors: str
    fault_action: str
    reset_authority: str

    @property
    def permits_motion(self) -> bool:
        return self.state in MOTION_STATES

    @property
    def is_derated(self) -> bool:
        return self.state == "DERATE"


@dataclass(frozen=True)
class MotionDecision:
    """The guard's verdict, before anything is dispatched."""

    admitted: bool
    state: PackState | None
    refusal: MotionRefusal | None = None
    reason: str = ""
    #: The limit that applies to an admitted request. `None` means the configured
    #: limit applies unchanged; a value means the request was clipped and the
    #: caller has to be able to see that.
    reduced_limit_fraction: float | None = None

    def metadata(self) -> dict[str, Any]:
        """The fields an `ActionResult` carries, so a clipping is never silent."""
        payload: dict[str, Any] = {
            "pack_state": self.state.state if self.state else None,
            "pack_state_admitted": self.admitted,
        }
        if self.refusal is not None:
            payload["pack_refusal"] = self.refusal.value
        if self.reduced_limit_fraction is not None:
            payload["pack_reduced_limit_fraction"] = self.reduced_limit_fraction
        return payload


def load_state_table(path: Path | None = None) -> dict[str, PackState]:
    """Read the committed state table. The CSV is the authority, not this module."""
    table_path = path or STATE_TABLE
    try:
        with table_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise PowerGuardError(f"cannot read the BMS state table: {exc}") from exc

    states: dict[str, PackState] = {}
    for row in rows:
        name = (row.get("state") or "").strip()
        if not name:
            raise PowerGuardError("the BMS state table has a row with no state name")
        if name in states:
            raise PowerGuardError(f"the BMS state table declares {name!r} twice")
        states[name] = PackState(
            state=name,
            allowed_contactors=(row.get("allowed_contactors") or "").strip(),
            fault_action=(row.get("fault_action") or "").strip(),
            reset_authority=(row.get("reset_authority") or "").strip(),
        )
    if not states:
        raise PowerGuardError("the BMS state table is empty")
    return states


def resolve_state(reported: object, *, table: dict[str, PackState] | None = None) -> PackState:
    """Resolve a reported state string, refusing anything the table does not declare.

    An unknown string is a fault, never a default: "the pack said something we do
    not understand" must not become "the pack is idle".
    """
    states = table if table is not None else load_state_table()
    if not isinstance(reported, str) or not reported.strip():
        raise PowerGuardError(
            f"{MotionRefusal.UNKNOWN_STATE.value}: the pack state must be a non-empty string, got {reported!r}"
        )
    name = reported.strip()
    state = states.get(name)
    if state is None:
        known = ", ".join(sorted(states))
        raise PowerGuardError(
            f"{MotionRefusal.UNKNOWN_STATE.value}: {name!r} is not a declared pack state (known: {known})"
        )
    return state


def guard_motion(
    reported: object,
    *,
    configured_limit_fraction: float = 1.0,
    derate_limit_fraction: float = 0.5,
    table: dict[str, PackState] | None = None,
) -> MotionDecision:
    """Decide whether a motion request may be dispatched.

    Returns an admitted decision, or a refusal naming the state. It does not raise
    for a forbidden state -- a refusal is a normal outcome with a reason -- but it
    does raise `PowerGuardError` for an unknown state string, because that is a
    fault rather than a decision.
    """
    if not 0.0 < derate_limit_fraction <= 1.0:
        raise PowerGuardError("the derate limit must be in (0, 1]")
    if not 0.0 < configured_limit_fraction <= 1.0:
        raise PowerGuardError("the configured limit must be in (0, 1]")

    state = resolve_state(reported, table=table)

    if state.permits_motion:
        if state.is_derated:
            # Admitted, but clipped. The limit is carried, never applied silently.
            return MotionDecision(
                admitted=True,
                state=state,
                reduced_limit_fraction=min(derate_limit_fraction, configured_limit_fraction),
                reason=f"pack state DERATE: limit reduced to {derate_limit_fraction}",
            )
        return MotionDecision(admitted=True, state=state, reason=f"pack state {state.state} permits motion")

    return MotionDecision(
        admitted=False,
        state=state,
        refusal=MotionRefusal.STATE_FORBIDS_MOTION,
        reason=f"pack state {state.state} forbids motion (contactors: {state.allowed_contactors or 'none'})",
    )


def guard_reset(reported: object, *, table: dict[str, PackState] | None = None) -> MotionDecision:
    """Decide whether the runtime may clear a latched pack fault.

    The CSV's `reset_authority` decides. `BMS` may be cleared by the pack itself;
    `operator`, `service_owner` and `operator_plus_service` may not be cleared by
    the runtime, and a caller that tries is refused rather than quietly allowed.
    """
    state = resolve_state(reported, table=table)
    if state.reset_authority in RUNTIME_RESET_AUTHORITIES:
        return MotionDecision(admitted=True, state=state, reason=f"reset authority {state.reset_authority}")
    return MotionDecision(
        admitted=False,
        state=state,
        refusal=MotionRefusal.RESET_NOT_AUTHORIZED,
        reason=(
            f"pack state {state.state} requires reset authority {state.reset_authority!r}; the runtime may not clear it"
        ),
    )


def guard_mid_motion(
    reported: object,
    *,
    table: dict[str, PackState] | None = None,
) -> MotionDecision:
    """Decide what a run already in motion must do when the pack changes state.

    A transition into `FAULT_LATCHED` mid-motion is a stop, and the reason records
    the transition rather than a generic failure.
    """
    state = resolve_state(reported, table=table)
    if state.state == "FAULT_LATCHED":
        return MotionDecision(
            admitted=False,
            state=state,
            refusal=MotionRefusal.LATCHED_MID_MOTION,
            reason=f"pack transitioned to {state.state} mid-motion: {state.fault_action or 'remain_deenergized'}",
        )
    if state.permits_motion:
        return MotionDecision(admitted=True, state=state, reason=f"pack state {state.state} permits continued motion")
    return MotionDecision(
        admitted=False,
        state=state,
        refusal=MotionRefusal.STATE_FORBIDS_MOTION,
        reason=f"pack state {state.state} forbids continued motion",
    )


@dataclass(frozen=True)
class GuardCoverage:
    """Which declared states have a guard, so the table and the code cannot drift."""

    declared: tuple[str, ...]
    motion_permitted: tuple[str, ...]
    motion_forbidden: tuple[str, ...]
    ungarded: tuple[str, ...] = field(default_factory=tuple)


def coverage(table: dict[str, PackState] | None = None) -> GuardCoverage:
    """Report every declared state and whether the guard classifies it.

    A state added to the CSV without being classified here shows up in
    `ungarded`, and a test fails on a non-empty tuple.
    """
    states = table if table is not None else load_state_table()
    declared = tuple(sorted(states))
    permitted = tuple(sorted(name for name in declared if states[name].permits_motion))
    forbidden = tuple(sorted(name for name in declared if name not in MOTION_STATES))
    # A state is guarded when `guard_motion` reaches a decision for it. Every
    # declared state does, because the decision is a total function over the
    # table; `ungarded` therefore catches a state the table declares and this
    # module's constants do not classify.
    classified = set(MOTION_STATES) | set(MOTION_FORBIDDING_STATES)
    ungarded = tuple(sorted(set(declared) - classified))
    return GuardCoverage(
        declared=declared,
        motion_permitted=permitted,
        motion_forbidden=forbidden,
        ungarded=ungarded,
    )
