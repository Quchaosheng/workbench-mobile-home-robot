"""Lifecycle rules for run evidence: retention, access, holds and export.

The repository already says what may leave the process
(``workbench.application.redaction``) and how a damaged store is recovered
(``docs/deployment/event-evidence-recovery.md``). Neither answers the operator's
questions: how long may this be kept, who may see it, what may be exported, and
what must not be deleted while an investigation is open.

This module answers those four questions as data rather than as prose, so a
deletion tool, an export tool and a test all consult one table:

* :data:`DATA_CLASSES` classifies every artifact the runtime produces and names
  its retention period, its owner, its access level and its hold rule.
* :class:`HoldRegister` records release and incident holds, so a deletion is
  refused while a hold covers the data it would remove.
* :func:`export_projection` produces the publishable form of a run and reports
  what it withheld, so an export carries hashes, provenance and references
  without copying restricted payloads.
* :func:`deletion_decision` is the single verdict a deletion tool must respect.

Three rules are deliberate:

* Public dashboard data never contains raw sensitive evidence. It contains
  references, and the reference is not resolvable from the public surface.
* Audit and safety records are never deletable while a hold is active, and a
  hold is never quietly expired by the code that wants to delete.
* Every withheld field is reported. Silent omission looks identical to absence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from workbench.application.redaction import (
    EVIDENCE_REDACTED,
    REDACTED,
    REDACTION_RULES_VERSION,
    redact_mapping,
)

GOVERNANCE_RULES_VERSION = "data-governance-v1"
PUBLIC_PROJECTION_VERSION = "public-evidence-projection-v1"


class GovernanceError(ValueError):
    """Raised when a lifecycle operation is not authorized or is malformed."""


class AccessLevel(StrEnum):
    """Who may read an artifact, from widest to narrowest."""

    PUBLIC = "public"
    OPERATOR = "operator"
    RESTRICTED = "restricted"


_ACCESS_RANK = {AccessLevel.PUBLIC: 0, AccessLevel.OPERATOR: 1, AccessLevel.RESTRICTED: 2}


class DataClass(StrEnum):
    """One lifecycle class per artifact family the runtime produces."""

    RUN_EVENT_LOG = "run_event_log"
    TASK_TIMING = "task_timing"
    CAMERA_EVIDENCE = "camera_evidence"
    MODEL_TRACE = "model_trace"
    MODEL_PROMPT = "model_prompt"
    HARDWARE_RECORD = "hardware_record"
    RELEASE_EVIDENCE = "release_evidence"
    INCIDENT_RECORD = "incident_record"


@dataclass(frozen=True)
class DataPolicy:
    """The lifecycle rule for one data class."""

    data_class: DataClass
    retention_days: int | None
    owner: str
    access_level: AccessLevel
    holds_apply: bool
    deletable: bool
    description: str

    def retention_until(self, created_at: datetime) -> datetime | None:
        """When the retention period ends, or ``None`` when it never expires."""
        if self.retention_days is None:
            return None
        return created_at + timedelta(days=self.retention_days)


# One owner per class, matching the ownership in docs/context/EVIDENCE_INDEX.md.
# ``retention_days=None`` means "no automatic expiry": the artifact is kept until
# a human decides otherwise, which is the honest answer for audit records.
DATA_CLASSES: Mapping[DataClass, DataPolicy] = {
    DataClass.RUN_EVENT_LOG: DataPolicy(
        DataClass.RUN_EVENT_LOG,
        retention_days=90,
        owner="Integration",
        access_level=AccessLevel.OPERATOR,
        holds_apply=True,
        deletable=True,
        description="Ordered task events for one run, redacted at the point of writing.",
    ),
    DataClass.TASK_TIMING: DataPolicy(
        DataClass.TASK_TIMING,
        retention_days=180,
        owner="Integration",
        access_level=AccessLevel.OPERATOR,
        holds_apply=True,
        deletable=True,
        description="Stage duration telemetry used for performance baselines.",
    ),
    DataClass.CAMERA_EVIDENCE: DataPolicy(
        DataClass.CAMERA_EVIDENCE,
        retention_days=30,
        owner="Perception",
        access_level=AccessLevel.RESTRICTED,
        holds_apply=True,
        deletable=True,
        description="Frames and recordings behind an evidence reference; never copied into a public artifact.",
    ),
    DataClass.MODEL_TRACE: DataPolicy(
        DataClass.MODEL_TRACE,
        retention_days=90,
        owner="Runtime",
        access_level=AccessLevel.OPERATOR,
        holds_apply=True,
        deletable=True,
        description="Model call records such as latency, route and token counts.",
    ),
    DataClass.MODEL_PROMPT: DataPolicy(
        DataClass.MODEL_PROMPT,
        retention_days=30,
        owner="Runtime",
        access_level=AccessLevel.RESTRICTED,
        holds_apply=True,
        deletable=True,
        description="Raw prompts and model payloads; retained by reference under the deployment's authorization.",
    ),
    DataClass.HARDWARE_RECORD: DataPolicy(
        DataClass.HARDWARE_RECORD,
        retention_days=None,
        owner="Hardware Owner",
        access_level=AccessLevel.RESTRICTED,
        holds_apply=True,
        deletable=False,
        description="Calibrated physical captures; no automatic expiry and no automatic deletion.",
    ),
    DataClass.RELEASE_EVIDENCE: DataPolicy(
        DataClass.RELEASE_EVIDENCE,
        retention_days=None,
        owner="Product Owner",
        access_level=AccessLevel.OPERATOR,
        holds_apply=True,
        deletable=False,
        description="Evidence behind a published release; kept while the release is supported.",
    ),
    DataClass.INCIDENT_RECORD: DataPolicy(
        DataClass.INCIDENT_RECORD,
        retention_days=None,
        owner="Security Owner",
        access_level=AccessLevel.RESTRICTED,
        holds_apply=True,
        deletable=False,
        description="Incident and audit trail; never deleted by an automated lifetime rule.",
    ),
}


class HoldKind(StrEnum):
    RELEASE = "release"
    INCIDENT = "incident"


@dataclass(frozen=True)
class Hold:
    """An active reason a covered artifact must not be deleted."""

    hold_id: str
    kind: HoldKind
    scope: str
    opened_by: str
    opened_at: datetime
    reason: str
    expires_at: datetime | None = None

    def covers(self, scope: str, *, at: datetime) -> bool:
        """Report whether this hold protects ``scope`` at ``at``."""
        if self.expires_at is not None and at >= self.expires_at:
            return False
        return self.scope == scope or self.scope == "*"


class HoldRegister:
    """A bounded, explicit register of release and incident holds.

    Expiry is evaluated against a caller-supplied time rather than the wall clock,
    so a test can move time forward without sleeping and an operator can see
    exactly when a hold stops applying.
    """

    def __init__(self) -> None:
        self._holds: dict[str, Hold] = {}

    def open(
        self,
        hold_id: str,
        *,
        kind: HoldKind,
        scope: str,
        opened_by: str,
        reason: str,
        opened_at: datetime,
        expires_at: datetime | None = None,
    ) -> Hold:
        """Record a hold. Re-opening an existing id is refused, not merged."""
        if hold_id in self._holds:
            raise GovernanceError(f"hold {hold_id!r} already exists")
        for field_name, value in (("hold_id", hold_id), ("scope", scope), ("opened_by", opened_by), ("reason", reason)):
            if not isinstance(value, str) or not value.strip():
                raise GovernanceError(f"hold {field_name} must be a non-empty string")
        if expires_at is not None and expires_at <= opened_at:
            raise GovernanceError("hold expiry must be after the hold was opened")
        hold = Hold(
            hold_id=hold_id,
            kind=kind,
            scope=scope,
            opened_by=opened_by,
            opened_at=opened_at,
            reason=reason,
            expires_at=expires_at,
        )
        self._holds[hold_id] = hold
        return hold

    def release(self, hold_id: str) -> Hold:
        """Close a hold explicitly. Nothing expires a hold implicitly."""
        try:
            return self._holds.pop(hold_id)
        except KeyError as exc:
            raise GovernanceError(f"hold {hold_id!r} is not open") from exc

    def holds(self) -> tuple[Hold, ...]:
        return tuple(self._holds[key] for key in sorted(self._holds))

    def covering(self, scope: str, *, at: datetime) -> tuple[Hold, ...]:
        return tuple(hold for hold in self.holds() if hold.covers(scope, at=at))


def policy_for(data_class: DataClass | str) -> DataPolicy:
    """Return the policy for a data class, refusing an unknown class."""
    try:
        resolved = DataClass(data_class)
    except ValueError as exc:
        raise GovernanceError(f"unknown data class: {data_class!r}") from exc
    return DATA_CLASSES[resolved]


@dataclass(frozen=True)
class DeletionDecision:
    """The single verdict a deletion tool must respect."""

    allowed: bool
    reasons: tuple[str, ...]
    policy: DataPolicy
    covering_holds: tuple[str, ...]


def deletion_decision(
    data_class: DataClass | str,
    *,
    scope: str,
    created_at: datetime,
    at: datetime,
    holds: HoldRegister | None = None,
) -> DeletionDecision:
    """Decide whether one artifact may be deleted now.

    An active hold outranks retention: a run past its retention period still
    cannot be deleted while an incident or release hold covers it.
    """
    policy = policy_for(data_class)
    reasons: list[str] = []
    covering: tuple[str, ...] = ()

    if not policy.deletable:
        reasons.append(f"{policy.data_class.value} is retained and never deleted by a lifetime rule")
    if holds is not None:
        covering = tuple(hold.hold_id for hold in holds.covering(scope, at=at))
        if covering and policy.holds_apply:
            reasons.append(f"active hold protects this scope: {', '.join(covering)}")
    if policy.retention_days is not None:
        expires_at = policy.retention_until(created_at)
        if expires_at is not None and at < expires_at:
            reasons.append(f"retention period is still active until {expires_at.isoformat()}")
    return DeletionDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        policy=policy,
        covering_holds=covering,
    )


def authorize_export(
    data_class: DataClass | str,
    *,
    requester_access: AccessLevel,
) -> None:
    """Refuse an export the requester's access level does not cover.

    Export authorization is a check, not a filter: a caller that is not allowed to
    export restricted data must fix the request rather than receive a subset it
    did not ask for.
    """
    policy = policy_for(data_class)
    if _ACCESS_RANK[requester_access] < _ACCESS_RANK[policy.access_level]:
        raise GovernanceError(
            f"{policy.data_class.value} requires {policy.access_level.value} access, "
            f"requester has {requester_access.value}"
        )


def _digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Fields a public projection keeps even when their value looks raw: a reference
# is the whole point of evidence-by-reference, and a hash is not the content.
PUBLIC_RETAINED_FIELDS = frozenset({"evidence_refs", "run_id", "event_id", "sequence_no", "occurred_at", "event_type"})


def export_projection(
    run: Mapping[str, Any],
    *,
    requester_access: AccessLevel = AccessLevel.PUBLIC,
) -> dict[str, Any]:
    """Return the publishable form of a run plus a report of what was withheld.

    The projection carries hashes, provenance and evidence references and never
    copies restricted payloads. Every withheld field is named, because a silently
    missing field looks the same as a field that was never there.
    """
    if requester_access is AccessLevel.RESTRICTED:
        sanitized, redacted_count = redact_mapping(run)
        return {
            "projection_version": PUBLIC_PROJECTION_VERSION,
            "governance_rules": GOVERNANCE_RULES_VERSION,
            "redaction_rules": REDACTION_RULES_VERSION,
            "requires_authorization": False,
            "withheld_fields": [],
            "redacted_value_count": redacted_count,
            "content_sha256": _digest(run),
            "run": sanitized,
        }

    withheld: list[str] = []
    projected: dict[str, Any] = {}
    for key, value in run.items():
        if key in PUBLIC_RETAINED_FIELDS:
            projected[key] = value
        else:
            withheld.append(str(key))
            projected[key] = EVIDENCE_REDACTED
    # Defense in depth: a retained reference list is still scrubbed for secrets,
    # because a reference string is producer-supplied text like any other.
    projected, redacted_count = redact_mapping(projected)
    if redacted_count:
        withheld.append(REDACTED)
    return {
        "projection_version": PUBLIC_PROJECTION_VERSION,
        "governance_rules": GOVERNANCE_RULES_VERSION,
        "redaction_rules": REDACTION_RULES_VERSION,
        "requires_authorization": True,
        "required_access": AccessLevel.RESTRICTED.value,
        "withheld_fields": sorted(set(withheld)),
        "redacted_value_count": redacted_count,
        "content_sha256": _digest(run),
        "run": projected,
    }


@dataclass(frozen=True)
class LifecycleEvent:
    """One authorized lifecycle action, recorded so the action is auditable."""

    action: str
    data_class: DataClass
    scope: str
    actor: str
    at: datetime
    authorized: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "data_class": self.data_class.value,
            "scope": self.scope,
            "actor": self.actor,
            "at": self.at.isoformat(),
            "authorized": self.authorized,
            "detail": self.detail,
            "governance_rules": GOVERNANCE_RULES_VERSION,
        }


class LifecycleLog:
    """An append-only record of deletion and restore decisions.

    A refused action is recorded too: "we tried and were stopped" is the finding
    an investigator needs, and a log that only holds successes cannot show it.
    """

    def __init__(self) -> None:
        self._events: list[LifecycleEvent] = []

    def record(
        self,
        action: str,
        data_class: DataClass | str,
        *,
        scope: str,
        actor: str,
        at: datetime,
        authorized: bool,
        detail: str,
    ) -> LifecycleEvent:
        if not isinstance(actor, str) or not actor.strip():
            raise GovernanceError("a lifecycle action requires a named actor")
        if not isinstance(scope, str) or not scope.strip():
            raise GovernanceError("a lifecycle action requires a scope")
        event = LifecycleEvent(
            action=action,
            data_class=policy_for(data_class).data_class,
            scope=scope,
            actor=actor,
            at=at,
            authorized=authorized,
            detail=detail,
        )
        self._events.append(event)
        return event

    def events(self) -> tuple[LifecycleEvent, ...]:
        return tuple(self._events)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [event.as_dict() for event in self._events]


def retention_summary(
    *,
    created_at: datetime,
    at: datetime,
    data_classes: Iterable[DataClass] | None = None,
) -> list[dict[str, Any]]:
    """Report every class's retention state, for an operator dashboard or test."""
    classes = list(data_classes) if data_classes is not None else list(DATA_CLASSES)
    summary: list[dict[str, Any]] = []
    for data_class in classes:
        policy = policy_for(data_class)
        expires_at = policy.retention_until(created_at)
        summary.append(
            {
                "data_class": policy.data_class.value,
                "owner": policy.owner,
                "access_level": policy.access_level.value,
                "retention_days": policy.retention_days,
                "retention_until": expires_at.isoformat() if expires_at else None,
                "expired": expires_at is not None and at >= expires_at,
                "deletable": policy.deletable,
                "holds_apply": policy.holds_apply,
            }
        )
    return summary
