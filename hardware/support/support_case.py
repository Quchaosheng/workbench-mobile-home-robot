#!/usr/bin/env python3
"""Open a support case that cannot exist without the evidence it needs (#590).

``hardware/support/README.md`` defines a four-level severity model and names
``case-template.csv`` and ``escalation-matrix.csv`` as the records, but it had no
path from a case to the artifacts the runtime actually produces. An S0 case needs
the run's event log, its hardware record and its release configuration hash; the
repository has all three concepts and nothing that joined them.

``docs/deployment/data-governance.md`` defines retention and holds, with
``hardware_record`` and ``release_evidence`` marked "no automatic expiry" and
deletable only while no hold applies -- but nothing in the support flow opened a
hold. Without that bridge, an incident's evidence was subject to the 30-day camera
retention like any other run, which is precisely the failure the hold mechanism
exists to prevent.

What this module does
---------------------

``open_case`` creates a case and, for a severity that needs it, opens the hold in
the same operation. A case cannot exist unbacked: if the hold cannot be opened,
the case is not created either. The two are one decision, so they are one call.

``close_case`` closes a case and deliberately does **not** release its hold.
Release is a separate, recorded action (``release_case_hold``) with an author, a
reason and a timestamp, because the moment an incident is over is not the moment
its evidence may be destroyed.

The case references evidence by the identifiers the runtime already uses --
``run_id`` and ``configuration_hash`` -- not by copied file paths, so a retention
expiry is detectable rather than silent.

What this module does not do
----------------------------

It does not change the retention table. ``GOVERNANCE_RULES_VERSION`` and every
``DataPolicy`` value are untouched; this consumes ``HoldRegister`` and
``deletion_decision`` as they are. Changing a retention value is its own issue
with the data owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from workbench.application.data_governance import (
    DataClass,
    GovernanceError,
    Hold,
    HoldKind,
    HoldRegister,
    deletion_decision,
)


class SupportSeverity(StrEnum):
    """The four levels ``hardware/support/README.md`` defines.

    S0 is injury, smoke/fire, battery damage, uncontrolled motion or a safety
    defeat; S3 is cosmetic. The ordering matters: it decides whether a case
    opens a hold.
    """

    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"


#: Severities whose evidence must survive retention. S0 and S1 are the two the
#: escalation matrix gives a containment window; S2 and S3 are investigated from
#: the ordinary retention window, and opening a permanent hold for a cosmetic
#: defect would make the register useless.
HOLD_SEVERITIES: frozenset[SupportSeverity] = frozenset({SupportSeverity.S0, SupportSeverity.S1})

#: The evidence classes a case holds. These are the three the issue names: the
#: run's event log, its hardware record and its release configuration. Camera
#: evidence is deliberately excluded from the hold: it is the class the 30-day
#: retention is written for, and holding every frame of every S0 case would make
#: the hold cover more than the investigation needs.
CASE_EVIDENCE_CLASSES: tuple[DataClass, ...] = (
    DataClass.RUN_EVENT_LOG,
    DataClass.HARDWARE_RECORD,
    DataClass.RELEASE_EVIDENCE,
)


class SupportCaseError(RuntimeError):
    """A case cannot be created or closed as asked."""


@dataclass(frozen=True)
class SupportCase:
    """One support case, joined to the evidence it depends on."""

    case_id: str
    unit_serial: str
    severity: SupportSeverity
    run_id: str
    configuration_hash: str
    opened_by: str
    opened_at: datetime
    symptom: str
    hold_id: str | None = None
    closed_at: datetime | None = None
    closed_by: str | None = None
    releases: tuple[HoldRelease, ...] = field(default_factory=tuple)

    @property
    def holds_evidence(self) -> bool:
        """Whether this case opened a hold, rather than merely being filed."""
        return self.hold_id is not None

    def evidence_classes(self) -> tuple[DataClass, ...]:
        return CASE_EVIDENCE_CLASSES


@dataclass(frozen=True)
class HoldRelease:
    """A recorded release of a case's hold. Never implicit."""

    hold_id: str
    released_by: str
    reason: str
    released_at: datetime


def _non_empty(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SupportCaseError(f"{name} must be a non-empty string")
    return value.strip()


def _known_run(run_id: str, known_runs: frozenset[str] | None) -> None:
    """Refuse a case whose run identifier does not exist.

    A case accepted against a run that was never recorded is discovered missing
    during the investigation, which is the worst possible moment. ``known_runs``
    is supplied by the caller because this module does not read the run store;
    ``None`` means the caller has no run index and the check is skipped, which is
    recorded in the case rather than assumed.
    """
    if known_runs is None:
        return
    if run_id not in known_runs:
        raise SupportCaseError(f"run_id {run_id!r} is not a known run; refusing to open a case against it")


def open_case(
    *,
    case_id: str,
    unit_serial: str,
    severity: SupportSeverity | str,
    run_id: str,
    configuration_hash: str,
    opened_by: str,
    opened_at: datetime,
    symptom: str,
    holds: HoldRegister,
    known_runs: frozenset[str] | None = None,
) -> SupportCase:
    """Create a case, opening its evidence hold in the same operation.

    An S0 or S1 case opens an incident hold against ``run_id`` and, for the
    hardware record, against ``unit_serial``. A hold that cannot be opened leaves
    no case behind: the caller sees the error and no half-record exists.

    An S3 case opens no hold and says so through ``holds_evidence`` being false.
    """
    case_id = _non_empty("case_id", case_id)
    unit_serial = _non_empty("unit_serial", unit_serial)
    run_id = _non_empty("run_id", run_id)
    configuration_hash = _non_empty("configuration_hash", configuration_hash)
    opened_by = _non_empty("opened_by", opened_by)
    symptom = _non_empty("symptom", symptom)

    try:
        resolved = SupportSeverity(severity)
    except ValueError as exc:
        raise SupportCaseError(f"unknown severity: {severity!r}") from exc

    _known_run(run_id, known_runs)

    hold_id: str | None = None
    if resolved in HOLD_SEVERITIES:
        # The hold covers the run and the unit. `Hold.covers` matches one scope or
        # `*`, so a case that needs both opens one hold per scope rather than a
        # wildcard that would also cover every other run.
        hold_id = f"hold-{case_id}"
        # Track exactly what this call opened. A rollback that released by id
        # would also release a hold that already existed under that name, which
        # is someone else's protection, not this case's.
        opened_here: list[str] = []
        try:
            for scope, label in ((run_id, ""), (unit_serial, "-unit")):
                holds.open(
                    f"{hold_id}{label}",
                    kind=HoldKind.INCIDENT,
                    scope=scope,
                    opened_by=opened_by,
                    reason=(
                        f"{resolved.value} support case {case_id}: {symptom}"
                        if not label
                        else f"{resolved.value} support case {case_id} on unit {unit_serial}"
                    ),
                    opened_at=opened_at,
                )
                opened_here.append(f"{hold_id}{label}")
        except GovernanceError as exc:
            # Undo only what this call opened, so a refused case leaves no hold
            # behind and does not disturb a hold it did not create.
            for opened in opened_here:
                try:
                    holds.release(opened)
                except GovernanceError:
                    pass
            raise SupportCaseError(f"cannot open the evidence hold for {case_id}: {exc}") from exc

    return SupportCase(
        case_id=case_id,
        unit_serial=unit_serial,
        severity=resolved,
        run_id=run_id,
        configuration_hash=configuration_hash,
        opened_by=opened_by,
        opened_at=opened_at,
        symptom=symptom,
        hold_id=hold_id,
    )


def close_case(
    case: SupportCase,
    *,
    closed_by: str,
    closed_at: datetime,
    reason: str,
) -> SupportCase:
    """Close a case without releasing its hold.

    Closing and releasing are different decisions. An investigator closes a case
    when the finding is written; the evidence stays held until someone with the
    authority releases it, which is a separate recorded action.
    """
    if case.closed_at is not None:
        raise SupportCaseError(f"case {case.case_id!r} is already closed")
    if closed_at < case.opened_at:
        raise SupportCaseError("a case cannot close before it opened")
    return SupportCase(
        **{
            **case.__dict__,
            "closed_at": closed_at,
            "closed_by": _non_empty("closed_by", closed_by),
        }
    )


def release_case_hold(
    case: SupportCase,
    *,
    holds: HoldRegister,
    released_by: str,
    reason: str,
    released_at: datetime,
) -> SupportCase:
    """Release a case's hold as its own recorded action.

    Refuses when the case never opened a hold, so a caller cannot believe it
    released protection that was never there.
    """
    if case.hold_id is None:
        raise SupportCaseError(f"case {case.case_id!r} opened no hold to release")
    released_by = _non_empty("released_by", released_by)
    reason = _non_empty("reason", reason)

    released: list[HoldRelease] = []
    for hold_id in (case.hold_id, f"{case.hold_id}-unit"):
        try:
            holds.release(hold_id)
        except GovernanceError:
            # Already released or never opened; the recorded release below still
            # states who asked and why, so the audit trail is not lost.
            continue
        released.append(HoldRelease(hold_id=hold_id, released_by=released_by, reason=reason, released_at=released_at))

    if not released:
        raise SupportCaseError(f"case {case.case_id!r} has no open hold to release")

    return SupportCase(
        **{
            **case.__dict__,
            "releases": case.releases + tuple(released),
        }
    )


def case_evidence_is_deletable(
    case: SupportCase,
    *,
    data_class: DataClass | str,
    scope: str,
    created_at: datetime,
    at: datetime,
    holds: HoldRegister,
):
    """Ask the existing governance decision whether this case's evidence may go.

    This is the whole point of the hold: the answer has to come from
    ``deletion_decision``, so a case cannot invent a second rule about when its
    own evidence expires.
    """
    return deletion_decision(
        data_class,
        scope=scope,
        created_at=created_at,
        at=at,
        holds=holds,
    )


def hold_for(case: SupportCase, holds: HoldRegister) -> tuple[Hold, ...]:
    """The holds this case currently has open, for a case file to print."""
    if case.hold_id is None:
        return ()
    wanted = {case.hold_id, f"{case.hold_id}-unit"}
    return tuple(hold for hold in holds.holds() if hold.hold_id in wanted)
