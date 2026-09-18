"""Bounded, read-only health history and active alerts for the backend.

Issue #170 asks for the operator-facing half of monitoring: one endpoint that
shows the current robot health, the alerts that follow from it, and a small
recent history. The snapshots themselves come from
``workbench.application.monitoring`` (Issue #169); the alert rules come from
``workbench.application.alerts``.

This module is a *trust boundary*, not a cache. A health document is as
untrusted as a run log, so nothing here trusts a serialized ``overall`` or
``status`` field:

* every metric value is re-validated against the fixed metric registry, and an
  unknown metric, a wrong type, a non-finite number or a duplicate domain is
  refused;
* the snapshot and domain statuses are recomputed from the metric states with
  the same precedence the collector uses, so a document cannot claim ``healthy``
  while carrying a faulted or missing critical metric;
* history is bounded by count and by document bytes, and a regression in the
  monotonic clock is refused instead of silently reordering the timeline.

The store is thread-safe and does no I/O of its own: a caller feeds it parsed
snapshots, and the HTTP layer only reads.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workbench.application.alerts import (
    ALERT_RULES_VERSION,
    MAX_HISTORY_AGE_S,
    MAX_HISTORY_ENTRIES,
    AlertError,
    AlertPolicy,
    AlertTracker,
    overall_status,
    summarize_alerts,
)
from workbench.application.monitoring import (
    DEFAULT_SPECS,
    DomainHealth,
    HealthSnapshot,
    HealthStatus,
    MetricView,
)

HEALTH_HISTORY_VERSION = "backend-health-history-v1"

MAX_SNAPSHOTS = 256
MAX_HEALTH_DOCUMENT_BYTES = 1024 * 1024
MAX_METRICS_PER_DOMAIN = 128

_SPECS_BY_NAME = {spec.name: spec for spec in DEFAULT_SPECS}
# Every validated snapshot carries the full registry, so the expected
# (domain -> metric names) shape is fixed. A document that is missing one of
# these has been truncated, not legitimately updated.
_DOMAIN_METRICS = {
    domain: frozenset(spec.name for spec in DEFAULT_SPECS if spec.domain == domain)
    for domain in {spec.domain for spec in DEFAULT_SPECS}
}
_STATUS_VALUES = {status.value for status in HealthStatus}
_STATUS_PRECEDENCE = (HealthStatus.FAULT, HealthStatus.UNKNOWN, HealthStatus.DEGRADED, HealthStatus.HEALTHY)


def _worst(states: list[HealthStatus]) -> HealthStatus:
    """Return the most severe status, using the collector's fixed precedence.

    The order is a contract, not a preference: fault outranks unknown, which
    outranks degraded, which outranks healthy. It is re-implemented here so this
    trust boundary does not depend on a private collector symbol.
    """
    for status in _STATUS_PRECEDENCE:
        if status in states:
            return status
    return HealthStatus.UNKNOWN


class HealthHistoryError(ValueError):
    """A health document or snapshot is not usable as monitoring evidence."""


def _finite_number(value: object) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _status_from_state(state: str, *, critical: bool, missing: bool, stale: bool, fault: bool) -> HealthStatus:
    """Recompute one metric's status from its validated fields.

    The precedence mirrors ``HealthSnapshotCollector.snapshot`` so a document
    cannot disagree with a live collector about what the same samples mean.
    """
    if state == "conflict":
        return HealthStatus.FAULT if critical else HealthStatus.DEGRADED
    if missing:
        return HealthStatus.UNKNOWN if critical else HealthStatus.DEGRADED
    if state == "fault" or fault:
        return HealthStatus.FAULT
    if stale:
        return HealthStatus.UNKNOWN if critical else HealthStatus.DEGRADED
    if state == "degraded":
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


def _metric_view(document: object, source: str) -> MetricView:
    if not isinstance(document, dict):
        raise HealthHistoryError(f"health metric must be an object: {source}")
    name = document.get("name")
    spec = _SPECS_BY_NAME.get(name) if isinstance(name, str) else None
    if spec is None:
        raise HealthHistoryError(f"health document contains an unregistered metric: {source}")
    if "value" not in document:
        raise HealthHistoryError(f"health metric has no value: {source}")
    value = document["value"]
    missing = bool(document.get("missing", False))
    stale = bool(document.get("stale", False))
    observed_at = document.get("observed_at")
    age_s = document.get("age_s")
    if missing:
        if value is not None:
            raise HealthHistoryError(f"a missing metric must not carry a value: {source}")
        value = None
        observed_at = None
        age_s = None
    else:
        if not _finite_number(observed_at) or not _finite_number(age_s):
            raise HealthHistoryError(f"health metric timestamps must be finite: {source}")
    state = document.get("state")
    if not isinstance(state, str) or state not in {"fresh", "degraded", "fault", "stale", "conflict", "missing"}:
        raise HealthHistoryError(f"health metric has an unknown state: {source}")
    if missing and state != "missing":
        raise HealthHistoryError(f"a missing metric must use the missing state: {source}")
    # Staleness is orthogonal to value classification: the collector reports a
    # stale fault as state="fault" with stale=true, and a stale healthy-ish value
    # as state="stale". Only freshness claims are impossible on a stale sample.
    if stale and state not in {"stale", "fault", "conflict"}:
        raise HealthHistoryError(f"a stale metric must not claim a fresh state: {source}")
    if not stale and state == "stale":
        raise HealthHistoryError(f"a metric may not claim staleness it does not have: {source}")
    # A document may not smuggle a healthy state onto a faulted, degraded or
    # conflicting value: recompute from the value with the registry's rules.
    fault = _spec_matches(spec.fault_values, value)
    degraded = _spec_matches(spec.degraded_values, value)
    if state == "fresh" and (fault or degraded or missing or stale):
        raise HealthHistoryError(f"health metric contradicts its value: {source}")
    if state == "fault" and not fault:
        raise HealthHistoryError(f"health metric claims a fault it does not have: {source}")
    if state == "degraded" and not degraded and not fault:
        raise HealthHistoryError(f"health metric claims a degraded value it does not have: {source}")
    source_name = document.get("source")
    if source_name is not None and source_name != spec.source:
        raise HealthHistoryError(f"health metric reports the wrong source: {source}")
    expected_source = document.get("expected_source", spec.source)
    if expected_source != spec.source:
        raise HealthHistoryError(f"health metric reports an unexpected source: {source}")
    source_status = document.get("source_status", "unknown")
    if source_status not in {"available", "stale", "missing", "conflict"}:
        raise HealthHistoryError(f"health metric has an unknown source status: {source}")
    if state == "missing" and source_status != "missing":
        raise HealthHistoryError(f"a missing metric must report a missing source: {source}")
    return MetricView(
        name=spec.name,
        value=None if state == "conflict" else value,
        unit=spec.unit,
        expected_source=spec.source,
        source=source_name,
        observed_at=observed_at,
        age_s=age_s,
        state=state,
        source_status=source_status,
        missing=missing,
        stale=stale,
    )


def _spec_matches(candidates: tuple[object, ...], value: object) -> bool:
    return any(type(value) is type(candidate) and value == candidate for candidate in candidates)


def snapshot_from_document(document: object, *, source: str = "health-document") -> HealthSnapshot:
    """Validate one serialized snapshot and rebuild it from first principles."""
    if not isinstance(document, dict):
        raise HealthHistoryError(f"health snapshot must be an object: {source}")
    collected_at = document.get("collected_at")
    if not _finite_number(collected_at) or collected_at < 0:
        raise HealthHistoryError(f"health snapshot has a non-finite collected_at: {source}")
    clock_id = document.get("clock_id", "monotonic")
    if clock_id != "monotonic":
        raise HealthHistoryError(f"health snapshot uses an incompatible clock: {source}")
    raw_domains = document.get("domains")
    if not isinstance(raw_domains, dict) or not raw_domains:
        raise HealthHistoryError(f"health snapshot has no domains: {source}")
    if len(raw_domains) > MAX_METRICS_PER_DOMAIN:
        raise HealthHistoryError(f"health snapshot has too many domains: {source}")
    domains: list[tuple[str, DomainHealth]] = []
    for domain_name, raw_domain in raw_domains.items():
        if not isinstance(domain_name, str) or not domain_name:
            raise HealthHistoryError(f"health snapshot has an invalid domain name: {source}")
        if not isinstance(raw_domain, dict):
            raise HealthHistoryError(f"health domain must be an object: {source}")
        raw_metrics = raw_domain.get("metrics")
        if not isinstance(raw_metrics, list) or not raw_metrics:
            raise HealthHistoryError(f"health domain has no metrics: {source}")
        if len(raw_metrics) > MAX_METRICS_PER_DOMAIN:
            raise HealthHistoryError(f"health domain has too many metrics: {source}")
        views = tuple(_metric_view(metric, source) for metric in raw_metrics)
        names = [view.name for view in views]
        if len(names) != len(set(names)):
            raise HealthHistoryError(f"health domain has duplicate metrics: {source}")
        states = [
            _status_from_state(
                view.state,
                critical=_SPECS_BY_NAME[view.name].critical,
                missing=view.missing,
                stale=view.stale,
                fault=_spec_matches(_SPECS_BY_NAME[view.name].fault_values, view.value),
            )
            for view in views
        ]
        domains.append((domain_name, DomainHealth(_worst(states), views)))
    domains.sort(key=lambda item: item[0])
    _require_complete_coverage(domains, source)
    overall = _worst([health.status for _, health in domains])
    return HealthSnapshot(float(collected_at), "monotonic", overall, tuple(domains))


def _require_complete_coverage(domains: list[tuple[str, DomainHealth]], source: str) -> None:
    """Refuse a snapshot that drops a registered domain or metric.

    The collector always emits the whole fixed registry: a missing critical
    input is encoded as a metric with ``missing=true``, never as an absent
    metric. So an absent domain or metric is a truncated document, and computing
    a status from it could turn a dropped fault into a healthy robot.
    """
    seen = {domain: {view.name for view in health.metrics} for domain, health in domains}
    if set(seen) != set(_DOMAIN_METRICS):
        raise HealthHistoryError(f"health snapshot has an unexpected set of domains: {source}")
    for domain, expected in _DOMAIN_METRICS.items():
        if seen[domain] != expected:
            raise HealthHistoryError(f"health snapshot for {domain} is missing registered metrics: {source}")


def load_health_documents(path: str | Path, *, max_bytes: int = MAX_HEALTH_DOCUMENT_BYTES) -> list[HealthSnapshot]:
    """Strictly load a JSONL health document, refusing oversize or malformed input."""
    health_path = Path(path)

    def _reject_constant(name: str) -> None:
        raise HealthHistoryError(f"health document uses the non-standard JSON constant {name}")

    def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise HealthHistoryError("health document has a duplicate JSON key")
            payload[key] = value
        return payload

    try:
        size = health_path.stat().st_size
    except OSError as exc:
        raise HealthHistoryError(f"health document is unavailable: {health_path.name}") from exc
    if size > max_bytes:
        raise HealthHistoryError(f"health document exceeds {max_bytes} bytes: {health_path.name}")
    try:
        text = health_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HealthHistoryError(f"health document is unavailable or not UTF-8: {health_path.name}") from exc
    snapshots: list[HealthSnapshot] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise HealthHistoryError(f"health document is not valid JSONL: {health_path.name}") from exc
        snapshots.append(snapshot_from_document(document, source=health_path.name))
    if not snapshots:
        raise HealthHistoryError(f"health document is empty: {health_path.name}")
    return snapshots


@dataclass(frozen=True)
class _StoredSnapshot:
    collected_at: float
    clock_id: str
    overall: str
    domains: tuple[tuple[str, str], ...]


class HealthStore:
    """Bounded snapshot history and alert state, safe to read concurrently."""

    def __init__(
        self,
        *,
        max_snapshots: int = MAX_SNAPSHOTS,
        policy: AlertPolicy | None = None,
        source: str = "health-document",
    ) -> None:
        if type(max_snapshots) is not int or not 1 <= max_snapshots <= MAX_SNAPSHOTS:
            raise HealthHistoryError(f"max_snapshots must be between 1 and {MAX_SNAPSHOTS}")
        resolved = policy if policy is not None else AlertPolicy()
        if not isinstance(resolved, AlertPolicy):
            raise HealthHistoryError("policy must be an AlertPolicy")
        self.max_snapshots = max_snapshots
        self.policy = resolved
        self.source = source
        self._tracker = AlertTracker(resolved)
        self._lock = threading.RLock()
        self._snapshots: list[_StoredSnapshot] = []
        self._current: HealthSnapshot | None = None
        self._latest_bytes = 0
        self._last_collected_at: float | None = None
        self._restarts = 0
        self._malformed = 0

    # -- ingestion ---------------------------------------------------------

    def ingest(self, snapshot: HealthSnapshot) -> None:
        """Fold one validated snapshot into the history and alert state."""
        if not isinstance(snapshot, HealthSnapshot):
            raise HealthHistoryError("health store expects a HealthSnapshot")
        if snapshot.clock_id != "monotonic":
            raise HealthHistoryError("only monotonic snapshots can be compared")
        with self._lock:
            if self._last_collected_at is not None and snapshot.collected_at < self._last_collected_at:
                # A clock that moves backwards is a source restart, not a new
                # sample: refusing it keeps the timeline monotonic and the alert
                # debounce meaningful.
                raise HealthHistoryError("health snapshots must arrive in monotonic order")
            self._last_collected_at = snapshot.collected_at
            self._current = snapshot
            try:
                self._tracker.observe(snapshot)
            except AlertError as exc:
                raise HealthHistoryError(f"health snapshot produced invalid alerts: {exc}") from exc
            self._snapshots.append(
                _StoredSnapshot(
                    collected_at=snapshot.collected_at,
                    clock_id=snapshot.clock_id,
                    overall=snapshot.overall.value,
                    domains=tuple((domain, health.status.value) for domain, health in snapshot.domains),
                )
            )
            if len(self._snapshots) > self.max_snapshots:
                del self._snapshots[: len(self._snapshots) - self.max_snapshots]

    def ingest_documents(self, snapshots: list[HealthSnapshot], *, source: str | None = None) -> None:
        """Ingest a validated batch, as produced by :func:`load_health_documents`."""
        if source is not None:
            self.source = source
        for snapshot in snapshots:
            self.ingest(snapshot)

    def note_malformed(self) -> None:
        with self._lock:
            self._malformed += 1

    def note_restart(self) -> None:
        with self._lock:
            self._restarts += 1

    # -- read side ---------------------------------------------------------

    @property
    def has_data(self) -> bool:
        with self._lock:
            return self._current is not None

    def status(self) -> str:
        with self._lock:
            if self._current is None:
                return "unknown"
            return overall_status(self._current, self._tracker.active())

    def current_payload(self) -> dict[str, Any]:
        """The payload for the current-health endpoint."""
        with self._lock:
            current = self._current
            active = self._tracker.active()
            stored = tuple(self._snapshots)
            malformed = self._malformed
            restarts = self._restarts
        payload: dict[str, Any] = {
            "read_only": True,
            "rules_version": ALERT_RULES_VERSION,
            "history_version": HEALTH_HISTORY_VERSION,
            "status": overall_status(current, active) if current is not None else "unknown",
            "source": self.source,
            "current": current.as_dict() if current is not None else None,
            "alerts": {
                "summary": summarize_alerts(active),
                "active": [alert.as_dict() for alert in active],
            },
            "history": {
                "snapshot_count": len(stored),
                "oldest_collected_at": stored[0].collected_at if stored else None,
                "newest_collected_at": stored[-1].collected_at if stored else None,
                "limits": {
                    "max_snapshots": self.max_snapshots,
                    "max_history_entries": MAX_HISTORY_ENTRIES,
                    "max_history_age_s": MAX_HISTORY_AGE_S,
                    "max_document_bytes": MAX_HEALTH_DOCUMENT_BYTES,
                },
            },
            "integrity": {"malformed_documents": malformed, "source_restarts": restarts},
        }
        if current is None:
            # "We have never seen a snapshot" is unknown, never healthy.
            payload["reason"] = "no_health_snapshot"
        return payload

    def history_payload(self, *, since: float | None = None) -> dict[str, Any]:
        """The payload for the bounded recent-history endpoint."""
        with self._lock:
            stored = tuple(self._snapshots)
            cleared = self._tracker.history(since=since)
        return {
            "read_only": True,
            "rules_version": ALERT_RULES_VERSION,
            "history_version": HEALTH_HISTORY_VERSION,
            "source": self.source,
            "snapshots": [
                {
                    "collected_at": entry.collected_at,
                    "clock_id": entry.clock_id,
                    "overall": entry.overall,
                    "domains": dict(entry.domains),
                }
                for entry in stored
            ],
            "cleared_alerts": [alert.as_dict() for alert in cleared],
            "limits": {
                "max_snapshots": self.max_snapshots,
                "max_history_entries": MAX_HISTORY_ENTRIES,
                "max_history_age_s": MAX_HISTORY_AGE_S,
                "max_document_bytes": MAX_HEALTH_DOCUMENT_BYTES,
            },
        }

    def snapshot_count(self) -> int:
        with self._lock:
            return len(self._snapshots)


class HealthReadModel:
    """Serve the health document at one path, reloading only when it changes.

    The dashboard read model reads run logs; this reads the health history the
    monitoring layer writes. It follows the same fail-closed rules: a missing
    file is "unknown" rather than an error, but a present-but-malformed or
    oversized file is a 503, never a partial projection. The parsed store is
    retained so a page of history is not re-ingested on every request, and the
    cache key is the file's size and mtime so an atomic replace is picked up.
    """

    data_source = "health-document"

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        policy: AlertPolicy | None = None,
        max_snapshots: int = MAX_SNAPSHOTS,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._store = HealthStore(max_snapshots=max_snapshots, policy=policy)
        self._lock = threading.RLock()
        self._stat: tuple[int, int] | None = None
        self._loaded = False

    def refresh(self) -> None:
        """Load the document when it exists and has changed since the last read."""
        with self._lock:
            if self.path is None:
                self._loaded = True
                return
            try:
                stat = self.path.stat()
            except OSError:
                # Absent is a valid state: the robot may simply never have
                # written health yet. It stays "unknown", not an error.
                self._stat = None
                self._loaded = True
                return
            signature = (stat.st_mtime_ns, stat.st_size)
            if self._loaded and signature == self._stat:
                return
            snapshots = load_health_documents(self.path)
            self._store = HealthStore(max_snapshots=self._store.max_snapshots, policy=self._store.policy)
            self._store.ingest_documents(snapshots, source=self.path.name)
            self._stat = signature
            self._loaded = True

    def current_payload(self) -> dict[str, Any]:
        self.refresh()
        return self._store.current_payload()

    def history_payload(self, *, since: float | None = None) -> dict[str, Any]:
        self.refresh()
        return self._store.history_payload(since=since)

    def ready(self) -> bool:
        try:
            self.refresh()
        except HealthHistoryError:
            return False
        return True


__all__ = [
    "HEALTH_HISTORY_VERSION",
    "MAX_HEALTH_DOCUMENT_BYTES",
    "MAX_SNAPSHOTS",
    "HealthHistoryError",
    "HealthReadModel",
    "HealthStore",
    "load_health_documents",
    "snapshot_from_document",
]
