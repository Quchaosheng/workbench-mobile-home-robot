"""Derive bounded, deterministic alerts from robot health snapshots.

``workbench.application.monitoring`` answers "what is true right now". This module
answers the operator's next two questions: "which of those conditions is an alert,
and how did it get that way". Issue #170 needs both before a backend endpoint or a
Dashboard view can show anything honest.

Three rules shape the design:

* An alert is derived from a snapshot, never asserted by it. Every alert names the
  metric, the observed value, the severity and a correlation reference, so a
  reader can go back to the evidence instead of trusting a colour.
* Missing data is never healthy and never zero. A stale or missing critical input
  is reported as ``unknown`` or ``degraded``; it is never silently a pass.
* Flapping is bounded by debounce and hysteresis on monotonic samples. A condition
  must persist before it opens, and must clear for a period before it closes, so a
  single noisy sample cannot page anyone.

The module holds no clock and no history beyond explicit limits. A caller supplies
snapshots and monotonic timestamps; the tracker owns only the bounded state it was
asked to keep.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .monitoring import DomainHealth, HealthSnapshot

ALERT_RULES_VERSION = "robot-alerts-v1"

MAX_ACTIVE_ALERTS = 256
MAX_HISTORY_ENTRIES = 512
MAX_HISTORY_AGE_S = 24 * 60 * 60.0


class AlertError(ValueError):
    """An alert input or limit is not usable as monitoring evidence."""


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertState(StrEnum):
    ACTIVE = "active"
    CLEARED = "cleared"


class AlertCondition(StrEnum):
    """One stable condition per distinct failure the operator must distinguish."""

    ESTOP_UNAVAILABLE = "estop_unavailable"
    ESTOP_DISAGREEMENT = "estop_disagreement"
    WATCHDOG_LOSS = "watchdog_loss"
    BMS_FAULT = "bms_fault"
    CONTACTOR_DENIED = "contactor_denied"
    CAN_BUS_OFF = "can_bus_off"
    CAN_LINK_LOSS = "can_link_loss"
    CONTROLLER_FAULT = "controller_fault"
    STOP_FAULT = "stop_fault"
    LOCALIZATION_STALE = "localization_stale"
    PERCEPTION_STALE = "perception_stale"
    EVENT_STORE_INTEGRITY = "event_store_integrity"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    DISK_PRESSURE = "disk_pressure"
    SOURCE_MISSING = "source_missing"
    SOURCE_STALE = "source_stale"
    SOURCE_FAULT = "source_fault"


_CRITICAL = AlertSeverity.CRITICAL
_WARNING = AlertSeverity.WARNING
_INFO = AlertSeverity.INFO


@dataclass(frozen=True)
class AlertRule:
    """One metric-to-condition mapping with explicit severity.

    ``fault_values`` and ``degraded_values`` are compared by value equality and
    type, so ``False`` and ``0`` are not interchangeable.
    """

    metric: str
    condition: AlertCondition
    severity: AlertSeverity
    fault_values: tuple[object, ...] = ()
    degraded_values: tuple[object, ...] = ()
    summary: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.metric, str) or not self.metric.strip():
            raise AlertError("alert rule metric must be a non-empty string")
        if not isinstance(self.condition, AlertCondition):
            raise AlertError("alert rule condition must be an AlertCondition")
        if not isinstance(self.severity, AlertSeverity):
            raise AlertError("alert rule severity must be an AlertSeverity")
        if not self.fault_values and not self.degraded_values:
            raise AlertError(f"alert rule {self.condition.value} matches nothing")


ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        metric="safety.estop_channels_ok",
        condition=AlertCondition.ESTOP_UNAVAILABLE,
        severity=_CRITICAL,
        fault_values=(False,),
        summary="An E-stop channel is unavailable.",
    ),
    AlertRule(
        metric="safety.contactor_permission",
        condition=AlertCondition.CONTACTOR_DENIED,
        severity=_CRITICAL,
        fault_values=(False,),
        degraded_values=("unknown",),
        summary="Contactors are not permitted to close.",
    ),
    AlertRule(
        metric="safety.mcu_watchdog_ok",
        condition=AlertCondition.WATCHDOG_LOSS,
        severity=_CRITICAL,
        fault_values=(False,),
        summary="The safety MCU watchdog is not heartbeating.",
    ),
    AlertRule(
        metric="power.bms_state",
        condition=AlertCondition.BMS_FAULT,
        severity=_CRITICAL,
        fault_values=("FAULT_LATCHED",),
        degraded_values=("DERATE", "SERVICE"),
        summary="The battery management state is not normal.",
    ),
    AlertRule(
        metric="can.bus_state",
        condition=AlertCondition.CAN_BUS_OFF,
        severity=_CRITICAL,
        fault_values=("bus-off",),
        degraded_values=("warning", "error"),
        summary="The CAN bus is not in the active state.",
    ),
    AlertRule(
        metric="can.link_ok",
        condition=AlertCondition.CAN_LINK_LOSS,
        severity=_CRITICAL,
        fault_values=(False,),
        summary="The CAN link is down.",
    ),
    AlertRule(
        metric="motion.controller_ok",
        condition=AlertCondition.CONTROLLER_FAULT,
        severity=_CRITICAL,
        fault_values=(False,),
        summary="The motion controller is not healthy.",
    ),
    AlertRule(
        metric="motion.stop_state",
        condition=AlertCondition.STOP_FAULT,
        severity=_CRITICAL,
        fault_values=("fault",),
        degraded_values=("requested",),
        summary="The STOP path is not in a normal state.",
    ),
    AlertRule(
        metric="nav.localization_ok",
        condition=AlertCondition.LOCALIZATION_STALE,
        severity=_CRITICAL,
        fault_values=(False,),
        summary="Localisation is unavailable.",
    ),
    AlertRule(
        metric="perception.fresh",
        condition=AlertCondition.PERCEPTION_STALE,
        severity=_CRITICAL,
        fault_values=(False,),
        summary="Perception is not producing fresh observations.",
    ),
    AlertRule(
        metric="event_store.integrity_ok",
        condition=AlertCondition.EVENT_STORE_INTEGRITY,
        severity=_CRITICAL,
        fault_values=(False,),
        summary="The event store failed its integrity check.",
    ),
    AlertRule(
        metric="backend.available",
        condition=AlertCondition.BACKEND_UNAVAILABLE,
        severity=_WARNING,
        fault_values=(False,),
        summary="The backend is unavailable.",
    ),
    AlertRule(
        metric="compute.disk_free_bytes",
        condition=AlertCondition.DISK_PRESSURE,
        severity=_WARNING,
        degraded_values=(0,),
        summary="The disk is at or below the configured pressure level.",
    ),
)

_RULES_BY_METRIC = {rule.metric: rule for rule in ALERT_RULES}


def _same_value(value: object, candidates: tuple[object, ...]) -> bool:
    return any(type(value) is type(candidate) and value == candidate for candidate in candidates)


@dataclass(frozen=True)
class Alert:
    """One derived alert with the evidence needed to act on it."""

    alert_id: str
    condition: AlertCondition
    severity: AlertSeverity
    state: AlertState
    metric: str
    domain: str
    source_status: str
    observed_value: object
    unit: str
    first_seen_at: float
    last_seen_at: float
    count: int
    evidence_ref: str
    summary: str
    rules_version: str = ALERT_RULES_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "alert_id": self.alert_id,
            "condition": self.condition.value,
            "severity": self.severity.value,
            "state": self.state.value,
            "metric": self.metric,
            "domain": self.domain,
            "source_status": self.source_status,
            "observed_value": self.observed_value,
            "unit": self.unit,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "count": self.count,
            "evidence_ref": self.evidence_ref,
            "summary": self.summary,
            "rules_version": self.rules_version,
        }


@dataclass(frozen=True)
class AlertPolicy:
    """Debounce and hysteresis bounds, in monotonic seconds."""

    open_after_s: float = 0.0
    clear_after_s: float = 0.0
    history_age_s: float = MAX_HISTORY_AGE_S
    max_history: int = MAX_HISTORY_ENTRIES

    def __post_init__(self) -> None:
        for name in ("open_after_s", "clear_after_s", "history_age_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
                raise AlertError(f"{name} must be a finite non-negative number")
        if not isinstance(self.max_history, int) or isinstance(self.max_history, bool):
            raise AlertError("max_history must be an integer")
        if not 1 <= self.max_history <= MAX_HISTORY_ENTRIES:
            raise AlertError(f"max_history must be between 1 and {MAX_HISTORY_ENTRIES}")
        if self.history_age_s > MAX_HISTORY_AGE_S:
            raise AlertError(f"history_age_s must not exceed {MAX_HISTORY_AGE_S}")


@dataclass
class _Tracked:
    condition: AlertCondition
    severity: AlertSeverity
    metric: str
    domain: str
    unit: str
    summary: str
    opened: bool
    first_seen_at: float
    last_seen_at: float
    count: int
    observed_value: object
    source_status: str
    pending_since: float | None = None


def derive_alerts(snapshot: HealthSnapshot) -> tuple[Alert, ...]:
    """Return the alerts implied by one snapshot, without debounce state.

    This is the stateless view: it reports every condition that is true right
    now. :class:`AlertTracker` adds timing on top of it.
    """
    if not isinstance(snapshot, HealthSnapshot):
        raise AlertError("snapshot must be a HealthSnapshot")
    alerts: list[Alert] = []
    for domain, health in snapshot.domains:
        if not isinstance(health, DomainHealth):  # pragma: no cover - snapshot invariant
            raise AlertError("snapshot domains must contain DomainHealth values")
        for view in health.metrics:
            rule = _RULES_BY_METRIC.get(view.name)
            if rule is None:
                continue
            severity, condition = _classify(rule, view)
            if severity is None or condition is None:
                continue
            alerts.append(
                Alert(
                    alert_id=_alert_id(view.name, condition, view.source or view.expected_source),
                    condition=condition,
                    severity=severity,
                    state=AlertState.ACTIVE,
                    metric=view.name,
                    domain=domain,
                    source_status=view.source_status,
                    observed_value=view.value,
                    unit=view.unit,
                    first_seen_at=view.observed_at if view.observed_at is not None else snapshot.collected_at,
                    last_seen_at=snapshot.collected_at,
                    count=1,
                    evidence_ref=_evidence_ref(snapshot, domain, view.name),
                    summary=rule.summary,
                )
            )
    if len(alerts) > MAX_ACTIVE_ALERTS:
        raise AlertError(f"snapshot produced more than {MAX_ACTIVE_ALERTS} alerts")
    return tuple(sorted(alerts, key=lambda alert: (alert.severity.value, alert.metric, alert.alert_id)))


def _classify(rule: AlertRule, view) -> tuple[AlertSeverity | None, AlertCondition | None]:
    """Return the severity for one metric view, or ``None`` when it is fine."""
    # A missing or stale critical input is its own condition: "we do not know" is
    # a different finding from "we know it is broken".
    if view.missing:
        if rule.severity is AlertSeverity.CRITICAL:
            return AlertSeverity.CRITICAL, AlertCondition.SOURCE_MISSING
        return AlertSeverity.WARNING, AlertCondition.SOURCE_MISSING
    if view.stale:
        if rule.severity is AlertSeverity.CRITICAL:
            return AlertSeverity.CRITICAL, AlertCondition.SOURCE_STALE
        return AlertSeverity.WARNING, AlertCondition.SOURCE_STALE
    if view.state == "conflict":
        return AlertSeverity.CRITICAL, AlertCondition.SOURCE_FAULT
    if _same_value(view.value, rule.fault_values):
        return rule.severity, rule.condition
    if _same_value(view.value, rule.degraded_values):
        # A degraded value is at most one step below its rule's severity, so a
        # warning-level rule stays a warning and a critical rule degrades to one.
        severity = AlertSeverity.WARNING if rule.severity is AlertSeverity.CRITICAL else rule.severity
        return severity, rule.condition
    return None, None


def _alert_id(metric: str, condition: AlertCondition, source: str) -> str:
    return f"{metric}:{condition.value}:{source}"


def _evidence_ref(snapshot: HealthSnapshot, domain: str, metric: str) -> str:
    return f"health-snapshot://{snapshot.clock_id}/{int(snapshot.collected_at)}/{domain}/{metric}"


class AlertTracker:
    """Track alert state across snapshots with bounded debounce and history.

    The tracker is deliberately imperative and single-threaded: it consumes an
    ordered stream of snapshots at monotonic timestamps, which is exactly the
    order the collector produces them in. Determinism comes from the ordering, not
    from locks.
    """

    def __init__(self, policy: AlertPolicy | None = None) -> None:
        resolved = policy if policy is not None else AlertPolicy()
        if not isinstance(resolved, AlertPolicy):
            raise AlertError("policy must be an AlertPolicy")
        self.policy = resolved
        self._tracked: dict[str, _Tracked] = {}
        self._history: list[Alert] = []
        self._last_collected_at: float | None = None

    def observe(self, snapshot: HealthSnapshot) -> tuple[Alert, ...]:
        """Fold one snapshot in and return the alerts that changed or persist."""
        if not isinstance(snapshot, HealthSnapshot):
            raise AlertError("snapshot must be a HealthSnapshot")
        now = snapshot.collected_at
        if now != now or now < 0:  # NaN or negative
            raise AlertError("snapshot collected_at must be a finite non-negative number")
        if self._last_collected_at is not None and now < self._last_collected_at:
            raise AlertError("snapshots must arrive in monotonic order")
        self._last_collected_at = now

        observed = {alert.alert_id: alert for alert in derive_alerts(snapshot)}
        changed: list[Alert] = []

        for alert_id, alert in observed.items():
            tracked = self._tracked.get(alert_id)
            if tracked is None:
                tracked = _Tracked(
                    condition=alert.condition,
                    severity=alert.severity,
                    metric=alert.metric,
                    domain=alert.domain,
                    unit=alert.unit,
                    summary=alert.summary,
                    opened=False,
                    first_seen_at=alert.first_seen_at,
                    last_seen_at=now,
                    count=0,
                    observed_value=alert.observed_value,
                    source_status=alert.source_status,
                )
                self._tracked[alert_id] = tracked
            tracked.last_seen_at = now
            tracked.observed_value = alert.observed_value
            tracked.source_status = alert.source_status
            tracked.count += 1
            # The condition is true again, so any clear window restarts.
            tracked.pending_since = None
            if not tracked.opened and now - tracked.first_seen_at >= self.policy.open_after_s:
                tracked.opened = True
            changed.append(self._materialize(alert_id, tracked))

        for alert_id, tracked in self._tracked.items():
            if alert_id in observed or not tracked.opened:
                continue
            if tracked.pending_since is None:
                # The condition stopped being true: hold it open until it has
                # been clear for clear_after_s, so one noisy sample cannot
                # close an alert that is still real.
                tracked.pending_since = now
            if now - tracked.pending_since >= self.policy.clear_after_s:
                tracked.opened = False
                tracked.last_seen_at = now
                self._record_history(self._materialize(alert_id, tracked))
            changed.append(self._materialize(alert_id, tracked))

        self._expire_history(now)
        return tuple(changed)

    def active(self) -> tuple[Alert, ...]:
        """Currently active alerts, ordered by severity then metric."""
        active = [self._materialize(alert_id, tracked) for alert_id, tracked in self._tracked.items() if tracked.opened]
        return tuple(sorted(active, key=lambda alert: (alert.severity.value, alert.metric, alert.alert_id)))

    def history(self, *, since: float | None = None) -> tuple[Alert, ...]:
        """Cleared alerts, newest last, bounded by count and age."""
        entries = self._history
        if since is not None:
            if since != since or since < 0:
                raise AlertError("since must be a finite non-negative number")
            entries = [alert for alert in entries if alert.last_seen_at >= since]
        return tuple(entries)

    def clear(self, alert_id: str) -> bool:
        """Explicitly clear one alert. Used by tests and by an operator action."""
        tracked = self._tracked.get(alert_id)
        if tracked is None or not tracked.opened:
            return False
        tracked.opened = False
        self._record_history(self._materialize(alert_id, tracked))
        return True

    def as_dict(self) -> dict[str, object]:
        return {
            "rules_version": ALERT_RULES_VERSION,
            "policy": {
                "open_after_s": self.policy.open_after_s,
                "clear_after_s": self.policy.clear_after_s,
                "history_age_s": self.policy.history_age_s,
                "max_history": self.policy.max_history,
            },
            "active": [alert.as_dict() for alert in self.active()],
            "history": [alert.as_dict() for alert in self.history()],
        }

    def _materialize(self, alert_id: str, tracked: _Tracked) -> Alert:
        return Alert(
            alert_id=alert_id,
            condition=tracked.condition,
            severity=tracked.severity,
            state=AlertState.ACTIVE if tracked.opened else AlertState.CLEARED,
            metric=tracked.metric,
            domain=tracked.domain,
            source_status=tracked.source_status,
            observed_value=tracked.observed_value,
            unit=tracked.unit,
            first_seen_at=tracked.first_seen_at,
            last_seen_at=tracked.last_seen_at,
            count=tracked.count,
            evidence_ref=f"alert://{alert_id}",
            summary=tracked.summary,
        )

    def _record_history(self, alert: Alert) -> None:
        self._history.append(alert)
        if len(self._history) > self.policy.max_history:
            del self._history[: len(self._history) - self.policy.max_history]

    def _expire_history(self, now: float) -> None:
        cutoff = now - self.policy.history_age_s
        self._history = [alert for alert in self._history if alert.last_seen_at >= cutoff]


def summarize_alerts(alerts: Iterable[Alert]) -> dict[str, object]:
    """Count alerts by severity and condition for a header or a metric."""
    by_severity: dict[str, int] = {severity.value: 0 for severity in AlertSeverity}
    by_condition: dict[str, int] = {}
    total = 0
    for alert in alerts:
        if not isinstance(alert, Alert):  # pragma: no cover - caller contract
            raise AlertError("summarize_alerts expects Alert values")
        total += 1
        by_severity[alert.severity.value] = by_severity.get(alert.severity.value, 0) + 1
        by_condition[alert.condition.value] = by_condition.get(alert.condition.value, 0) + 1
    return {
        "total": total,
        "by_severity": by_severity,
        "by_condition": dict(sorted(by_condition.items())),
        "rules_version": ALERT_RULES_VERSION,
    }


def overall_status(snapshot: HealthSnapshot, alerts: Sequence[Alert]) -> str:
    """Map a snapshot plus its alerts to one operator-facing status word."""
    if not isinstance(snapshot, HealthSnapshot):
        raise AlertError("snapshot must be a HealthSnapshot")
    for alert in alerts:
        if alert.state is AlertState.ACTIVE and alert.severity is AlertSeverity.CRITICAL:
            return "fault"
    if any(alert.state is AlertState.ACTIVE and alert.severity is AlertSeverity.WARNING for alert in alerts):
        return "degraded"
    current = snapshot.overall.value
    # A snapshot with no alerts is still not healthy if the collector itself could
    # not establish the state of a critical input.
    if current == "unknown" and not alerts:
        return "unknown"
    return current


__all__ = [
    "ALERT_RULES",
    "ALERT_RULES_VERSION",
    "MAX_ACTIVE_ALERTS",
    "MAX_HISTORY_AGE_S",
    "MAX_HISTORY_ENTRIES",
    "Alert",
    "AlertCondition",
    "AlertError",
    "AlertPolicy",
    "AlertRule",
    "AlertSeverity",
    "AlertState",
    "AlertTracker",
    "derive_alerts",
    "overall_status",
    "summarize_alerts",
]
