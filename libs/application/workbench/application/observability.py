"""One versioned contract for correlated, bounded, failure-aware telemetry.

The repository already answers two observability questions elsewhere: what may
leave the process (``workbench.application.redaction``) and how current health is
derived (``workbench.application.monitoring``).  Neither answers the three
questions an operator asks of a telemetry line: which run, action and attempt
produced it; which distinct failure it counts; and whether the line was
truncated before it was written.

This module answers those as data, so a logger, a metrics exporter, a trace view
and a test all consult one definition:

* :data:`OBSERVABILITY_SCHEMA_VERSION` names the record contract.  Every record
  that was written under it can be read back under it.
* :class:`CorrelationFields` is the required correlation envelope.  A line
  without it cannot be joined to a run, so it is not evidence.
* :class:`FailureClass` and :func:`classify_failure` separate the five failures
  an operator must count separately: timeout, rejection, transport loss, stale
  evidence and verification failure.  An unknown input raises instead of being
  counted as success.
* :func:`bounded_payload` projects a value into a bounded shape and *reports
  every omission*.  Silent truncation looks identical to absence, so an
  over-long string is cut to :data:`MAX_STRING_BYTES` and its path is reported
  rather than being written whole.
* :class:`FailureCounter` stores that taxonomy with a bounded series count.
* :func:`trace_fields` joins one correlation record to its retries, evidence
  references, MCU frames and final verification.
* :class:`JsonlSink` writes records under an exclusive lock and rotates between
  complete lines, so concurrent writers and rotation stay parseable.  It scrubs
  each record again at the write, because the sink is the last point where
  untrusted text becomes a file on disk; a caller that forgot to redact must not
  be able to leak by forgetting.

Three rules are deliberate and must not be papered over:

* Only the identifiers the redactor already freezes are copied verbatim.
  ``component``, ``attempt_id``, ``schema_version`` and every other field are
  still scanned as untrusted text, because a credential placed in a field we
  declared "safe" would bypass the redactor.
* A value that cannot be serialized as finite JSON raises.  A record a parser
  cannot read is not evidence, and writing it anyway would hide the defect.
* Truncation is in-band.  A projected record carries the omission report, so a
  reader can never mistake a shortened record for a complete one.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from workbench_contracts import (
    ActionOutcome,
    DeviceState,
    DispatchState,
    McuFaultCode,
    ReasonCode,
    VerificationStatus,
)
from workbench_task_utils import exclusive_file_lock

from workbench.application.redaction import (
    REDACTION_MARKER_KEY,
    REDACTION_RULES_VERSION,
    redact_mapping,
)

OBSERVABILITY_SCHEMA_VERSION = "observability-v1"

# Bounds are small on purpose.  A telemetry record is a line an operator reads
# and a script parses, not the place to carry a payload.
MAX_IDENTIFIER_LENGTH = 64
MAX_RECORD_BYTES = 16 * 1024
MAX_STRING_BYTES = 2048
MAX_DETAIL_KEYS = 32
MAX_DETAIL_DEPTH = 4
MAX_LIST_ITEMS = 64
MAX_COUNTER_SERIES = 64
MAX_SINK_BYTES = 1024 * 1024
MAX_SINK_FILES = 3

TRUNCATION_MARKER_KEY = "truncation"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


class ObservabilityError(ValueError):
    """A record, counter or payload violates the observability contract."""


class FailureClass(StrEnum):
    """One class per distinct failure an operator must be able to count.

    Collapsing these would hide the difference between a network that dropped a
    frame and a device that refused it, which need different responses.
    """

    TIMEOUT = "timeout"
    REJECTION = "rejection"
    TRANSPORT_LOSS = "transport_loss"
    STALE_EVIDENCE = "stale_evidence"
    VERIFICATION_FAILURE = "verification_failure"


# Precedence is a contract, not a preference: the class that explains the others
# wins.  A lost link is why a timeout happened; a stale observation is why a
# verification failed.  Ordering the checks this way keeps one incident from
# being counted as three.
_TRANSPORT_LOSS_FAULTS = frozenset({McuFaultCode.LINK_LOST.value, McuFaultCode.WATCHDOG_EXPIRED.value})
_TIMEOUT_FAULTS = frozenset({McuFaultCode.ACK_TIMEOUT.value, McuFaultCode.STOP_TIMEOUT.value})
_REJECTION_FAULTS = frozenset(
    {McuFaultCode.STOP_REJECTED.value, McuFaultCode.MALFORMED_FRAME.value, McuFaultCode.DUPLICATE_FRAME.value}
)
_STALE_REASONS = frozenset(
    {
        ReasonCode.STALE_OBSERVATION.value,
        ReasonCode.TARGET_NOT_OBSERVED.value,
        ReasonCode.CONFLICTING_OBSERVATIONS.value,
        ReasonCode.EVIDENCE_MISSING.value,
        ReasonCode.CONFIDENCE_BELOW_THRESHOLD.value,
    }
)
_OUTCOMES = frozenset(outcome.value for outcome in ActionOutcome)
_DISPATCH_STATES = frozenset(state.value for state in DispatchState)
_DEVICE_STATES = frozenset(state.value for state in DeviceState)
_FAULT_CODES = frozenset(code.value for code in McuFaultCode)
_REASON_CODES = frozenset(code.value for code in ReasonCode)
_VERIFICATION_STATUSES = frozenset(status.value for status in VerificationStatus)

_OMITTED = object()


def _checked_identifier(value: object, name: str) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ObservabilityError(f"{name} must be a non-empty bounded identifier")
    return value


def _enum_value(value: object, name: str, allowed: frozenset[str]) -> str | None:
    """Normalize one optional contract enum, rejecting unknown members.

    An unrecognized member is a contract drift, not an absence: counting it as
    "no failure" is how a renamed status silently stops being alerted on.
    """
    if value is None:
        return None
    if isinstance(value, StrEnum):
        value = value.value
    if type(value) is not str or value not in allowed:
        raise ObservabilityError(f"{name} is not a known contract value: {value!r}")
    return value


@dataclass(frozen=True)
class CorrelationFields:
    """The correlation envelope a telemetry record must carry to be joinable.

    ``run_id``, ``component`` and ``schema_version`` are always present.  An
    ``attempt_id`` without an ``action_id`` is refused: a retry that cannot name
    the action it retried is not a trace.
    """

    run_id: str
    component: str
    schema_version: str = OBSERVABILITY_SCHEMA_VERSION
    action_id: str | None = None
    attempt_id: str | None = None
    monotonic_ns: int | None = None

    def __post_init__(self) -> None:
        _checked_identifier(self.run_id, "run_id")
        _checked_identifier(self.component, "component")
        _checked_identifier(self.schema_version, "schema_version")
        if self.schema_version != OBSERVABILITY_SCHEMA_VERSION:
            raise ObservabilityError(f"unsupported observability schema version: {self.schema_version!r}")
        if self.action_id is not None:
            _checked_identifier(self.action_id, "action_id")
        if self.attempt_id is not None:
            if self.action_id is None:
                raise ObservabilityError("attempt_id requires action_id: a retry with no action cannot be joined")
            _checked_identifier(self.attempt_id, "attempt_id")
        if self.monotonic_ns is not None and (type(self.monotonic_ns) is not int or not 0 <= self.monotonic_ns < 2**63):
            raise ObservabilityError("monotonic_ns must be a non-negative 64-bit integer")

    @property
    def has_trace(self) -> bool:
        return self.action_id is not None

    def as_dict(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "run_id": self.run_id,
            "component": self.component,
            "schema_version": self.schema_version,
        }
        for name in ("action_id", "attempt_id", "monotonic_ns"):
            value = getattr(self, name)
            if value is not None:
                fields[name] = value
        return fields


def classify_failure(
    *,
    outcome: ActionOutcome | str | None = None,
    dispatch_state: DispatchState | str | None = None,
    device_state: DeviceState | str | None = None,
    fault_code: McuFaultCode | str | None = None,
    reason_code: ReasonCode | str | None = None,
    verification_status: VerificationStatus | str | None = None,
) -> FailureClass | None:
    """Return the single failure class that explains these contract values.

    ``None`` means the inputs describe no failure.  That is a different answer
    from "unknown", which raises.  ``ActionOutcome.FAILED`` maps to
    :attr:`FailureClass.VERIFICATION_FAILURE` because the more specific classes
    already claim the cases the evidence distinguishes; an operator cancel and a
    deliberate safe stop are not failures and return ``None``.
    """
    normalized_outcome = _enum_value(outcome, "outcome", _OUTCOMES)
    normalized_dispatch = _enum_value(dispatch_state, "dispatch_state", _DISPATCH_STATES)
    normalized_device = _enum_value(device_state, "device_state", _DEVICE_STATES)
    normalized_fault = _enum_value(fault_code, "fault_code", _FAULT_CODES)
    normalized_reason = _enum_value(reason_code, "reason_code", _REASON_CODES)
    normalized_verification = _enum_value(verification_status, "verification_status", _VERIFICATION_STATUSES)

    if normalized_dispatch == DispatchState.SEND_FAILED.value or normalized_fault in _TRANSPORT_LOSS_FAULTS:
        return FailureClass.TRANSPORT_LOSS
    if normalized_outcome == ActionOutcome.TIMEOUT.value or normalized_fault in _TIMEOUT_FAULTS:
        return FailureClass.TIMEOUT
    if normalized_device == DeviceState.REJECTED.value or normalized_fault in _REJECTION_FAULTS:
        return FailureClass.REJECTION
    if normalized_reason in _STALE_REASONS:
        return FailureClass.STALE_EVIDENCE
    if normalized_verification == VerificationStatus.REFUTED.value:
        return FailureClass.VERIFICATION_FAILURE
    if normalized_verification == VerificationStatus.INSUFFICIENT_EVIDENCE.value:
        return FailureClass.VERIFICATION_FAILURE
    if normalized_outcome == ActionOutcome.FAILED.value:
        return FailureClass.VERIFICATION_FAILURE
    return None


@dataclass(frozen=True)
class PayloadReport:
    """What a projection withheld, so a reader never guesses whether it was cut."""

    byte_size: int
    omitted_paths: tuple[str, ...] = ()
    truncated_paths: tuple[str, ...] = ()

    @property
    def omitted(self) -> bool:
        return bool(self.omitted_paths or self.truncated_paths)

    def as_dict(self) -> dict[str, Any]:
        return {
            "byte_size": self.byte_size,
            "omitted_paths": list(self.omitted_paths),
            "truncated_paths": list(self.truncated_paths),
        }


def _project(value: Any, path: str, depth: int, omitted: list[str], truncated: list[str]) -> Any:
    if depth > MAX_DETAIL_DEPTH:
        omitted.append(path or "$")
        return _OMITTED
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: str(item[0]))
        if len(items) > MAX_DETAIL_KEYS:
            for key, _ in items[MAX_DETAIL_KEYS:]:
                omitted.append(f"{path}.{key}" if path else str(key))
            items = items[:MAX_DETAIL_KEYS]
        projected: dict[Any, Any] = {}
        for key, item in items:
            child = _project(item, f"{path}.{key}" if path else str(key), depth + 1, omitted, truncated)
            if child is not _OMITTED:
                projected[key] = child
        return projected
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) > MAX_STRING_BYTES:
            truncated.append(path or "$")
            return encoded[:MAX_STRING_BYTES].decode("utf-8", errors="ignore")
        return value
    if isinstance(value, list | tuple):
        if len(value) > MAX_LIST_ITEMS:
            truncated.append(path or "$")
        projected_items = []
        for index, item in enumerate(value[:MAX_LIST_ITEMS]):
            child = _project(item, f"{path}[{index}]", depth + 1, omitted, truncated)
            if child is not _OMITTED:
                projected_items.append(child)
        return projected_items
    return value


def _finite_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ObservabilityError(f"record is not finite JSON: {exc}") from exc


def bounded_payload(value: object) -> tuple[Any, PayloadReport]:
    """Project ``value`` into the bounded contract and report every omission.

    Strings over :data:`MAX_STRING_BYTES` are cut and reported, so an ordinary
    over-long message is bounded instead of failing the caller.  A projection
    that still exceeds :data:`MAX_RECORD_BYTES` raises rather than being
    silently shortened, because a line a parser cannot finish is worse than a
    visible failure.
    """
    omitted: list[str] = []
    truncated: list[str] = []
    projected = _project(value, "", 0, omitted, truncated)
    if projected is _OMITTED:
        raise ObservabilityError("payload is empty after projection")
    encoded = _finite_json(projected)
    byte_size = len(encoded.encode("utf-8"))
    if byte_size > MAX_RECORD_BYTES:
        raise ObservabilityError(f"payload of {byte_size} bytes exceeds the {MAX_RECORD_BYTES}-byte record budget")
    return projected, PayloadReport(
        byte_size=byte_size,
        omitted_paths=tuple(sorted(set(omitted))),
        truncated_paths=tuple(sorted(set(truncated))),
    )


@dataclass(frozen=True)
class FailureSeries:
    """One counted series, carrying the correlation fields a metric must show."""

    failure: str
    component: str
    run_id: str | None
    count: int
    last_monotonic_ns: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "failure": self.failure,
            "component": self.component,
            "run_id": self.run_id,
            "count": self.count,
            "last_monotonic_ns": self.last_monotonic_ns,
        }


class FailureCounter:
    """Bounded, thread-safe counts for the five failure classes.

    A series is one (failure class, component, run) triple so a metric can be
    attributed to the run that produced it.  The series count is bounded so a
    failure loop cannot turn the counter into the unbounded memory leak that
    monitoring exists to detect.
    """

    def __init__(self, *, max_series: int = MAX_COUNTER_SERIES) -> None:
        if type(max_series) is not int or not 1 <= max_series <= 1024:
            raise ObservabilityError("max_series must be between 1 and 1024")
        self.max_series = max_series
        self._counts: dict[tuple[str, str, str | None], tuple[int, int | None]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _normalized(failure: FailureClass | str) -> str:
        if isinstance(failure, StrEnum):
            failure = failure.value
        if type(failure) is not str or failure not in {member.value for member in FailureClass}:
            raise ObservabilityError(f"unknown failure class: {failure!r}")
        return failure

    def record(
        self,
        failure: FailureClass | str,
        *,
        component: str,
        run_id: str | None = None,
        monotonic_ns: int | None = None,
    ) -> int:
        normalized_failure = self._normalized(failure)
        normalized_component = _checked_identifier(component, "component")
        normalized_run = None if run_id is None else _checked_identifier(run_id, "run_id")
        if monotonic_ns is not None and (type(monotonic_ns) is not int or not 0 <= monotonic_ns < 2**63):
            raise ObservabilityError("monotonic_ns must be a non-negative 64-bit integer")
        key = (normalized_failure, normalized_component, normalized_run)
        with self._lock:
            if key not in self._counts and len(self._counts) >= self.max_series:
                raise ObservabilityError("failure counter series limit reached")
            count, observed = self._counts.get(key, (0, None))
            updated = count + 1
            if monotonic_ns is None:
                self._counts[key] = (updated, observed)
            elif observed is None or monotonic_ns >= observed:
                self._counts[key] = (updated, monotonic_ns)
            else:
                self._counts[key] = (updated, observed)
            return updated

    def count(self, failure: FailureClass | str, *, component: str, run_id: str | None = None) -> int:
        normalized_failure = self._normalized(failure)
        return self._counts.get((normalized_failure, component, run_id), (0, None))[0]

    def series(self) -> tuple[FailureSeries, ...]:
        with self._lock:
            items = [
                FailureSeries(failure, component, run_id, count, observed)
                for (failure, component, run_id), (count, observed) in sorted(
                    self._counts.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "")
                )
            ]
        return tuple(items)

    def counts(self) -> tuple[tuple[str, str, int], ...]:
        """Per (class, component) totals, aggregating runs of the same component."""
        totals: dict[tuple[str, str], int] = {}
        for item in self.series():
            key = (item.failure, item.component)
            totals[key] = totals.get(key, 0) + item.count
        return tuple((failure, component, count) for (failure, component), count in sorted(totals.items()))

    def classes(self) -> tuple[str, ...]:
        """Every class with a non-zero count, so a dropped class is visible."""
        return tuple(sorted({item.failure for item in self.series()}))

    def as_document(self) -> dict[str, Any]:
        """The versioned form a metrics exporter publishes."""
        return {
            "schema_version": OBSERVABILITY_SCHEMA_VERSION,
            "failure_classes": [member.value for member in FailureClass],
            "series": [item.as_dict() for item in self.series()],
        }


@dataclass(frozen=True)
class TraceJoin:
    """One correlation record projected into the fields a trace view needs."""

    correlation_ref: str
    run_id: str
    action_id: str
    attempts: int
    retries: int
    frame_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    verification_id: str | None
    verification_status: str | None
    fault_codes: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return self.verification_status == VerificationStatus.CONFIRMED.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "correlation_ref": self.correlation_ref,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "attempts": self.attempts,
            "retries": self.retries,
            "frame_ids": list(self.frame_ids),
            "evidence_refs": list(self.evidence_refs),
            "verification_id": self.verification_id,
            "verification_status": self.verification_status,
            "fault_codes": list(self.fault_codes),
        }


def _attr(source: object, name: str) -> Any:
    if not hasattr(source, name):
        raise ObservabilityError(f"correlation record is missing {name!r}: it cannot be joined to a run")
    return getattr(source, name)


def trace_fields(record: object) -> TraceJoin:
    """Join one correlation record to its retries, frames, evidence and verdict.

    The record is read structurally so this contract does not depend on the
    ledger's storage, and a missing field raises rather than producing a trace
    that silently omits the thing it was asked to correlate.
    """
    run_id = _checked_identifier(_attr(record, "run_id"), "run_id")
    action_id = _checked_identifier(_attr(record, "action_id"), "action_id")
    correlation_ref = _checked_identifier(_attr(record, "correlation_ref"), "correlation_ref")

    attempts = tuple(_attr(record, "transport_attempts") or ())
    frame_ids: list[str] = []
    retries = 0
    for attempt in attempts:
        frame_ids.append(_checked_identifier(_attr(attempt, "frame_id"), "frame_id"))
        retry_count = getattr(attempt, "retry_count", None)
        if retry_count is None:
            continue
        if type(retry_count) is not int or retry_count < 0:
            raise ObservabilityError("retry_count must be a non-negative integer")
        retries += retry_count

    evidence = set(_attr(record, "execution_evidence_refs") or ())
    evidence |= set(_attr(record, "verification_evidence_refs") or ())
    for reference in evidence:
        _checked_identifier(reference, "evidence_ref")

    fault_codes = sorted({str(_attr(fault, "fault_code")) for fault in _attr(record, "faults") or ()})

    verification = getattr(record, "verification", None)
    verification_id: str | None = None
    verification_status: str | None = None
    if verification is not None:
        verification_id = _checked_identifier(_attr(verification, "verification_id"), "verification_id")
        status = _attr(verification, "status")
        verification_status = status.value if isinstance(status, StrEnum) else str(status)

    return TraceJoin(
        correlation_ref=correlation_ref,
        run_id=run_id,
        action_id=action_id,
        attempts=len(attempts),
        retries=retries,
        frame_ids=tuple(sorted(frame_ids)),  # one entry per attempt; a repeat is real evidence
        evidence_refs=tuple(sorted(evidence)),
        verification_id=verification_id,
        verification_status=verification_status,
        fault_codes=tuple(fault_codes),
    )


@dataclass
class JsonlSink:
    """Append JSONL records under a lock, rotating only between whole lines.

    Rotation happens before a write that would cross the size bound, while the
    exclusive lock is held, so no reader ever observes a half-written line and
    no two writers can interleave one.  Rotated files are complete JSONL
    documents; the number kept is bounded.
    """

    path: Path
    max_bytes: int = MAX_SINK_BYTES
    max_files: int = MAX_SINK_FILES
    _lock_path: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if type(self.max_bytes) is not int or not 1024 <= self.max_bytes <= 2**40:
            raise ObservabilityError("max_bytes must be between 1024 bytes and 1 TiB")
        if type(self.max_files) is not int or not 1 <= self.max_files <= 32:
            raise ObservabilityError("max_files must be between 1 and 32")
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    def _rotated(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _rotate_locked(self, incoming: int) -> None:
        if not self.path.exists() or self.path.stat().st_size + incoming <= self.max_bytes:
            return
        for index in range(self.max_files, 1, -1):
            previous = self._rotated(index - 1)
            if previous.exists():
                previous.replace(self._rotated(index))
        if self.path.exists():
            self.path.replace(self._rotated(1))

    def write(self, record: Mapping[str, Any]) -> dict[str, Any]:
        # Redaction runs first on purpose. Raw bytes and prompt text must become
        # reference markers before the record is measured or serialized, or the
        # sink would reject the very payload the redactor exists to replace.
        if not isinstance(record, Mapping):
            raise ObservabilityError("a telemetry record must be a mapping")
        sanitized, findings = redact_mapping(dict(record))
        if findings:
            sanitized[REDACTION_MARKER_KEY] = {"rules": REDACTION_RULES_VERSION, "redacted_values": findings}
        projected, report = bounded_payload(sanitized)
        if not isinstance(projected, dict):
            raise ObservabilityError("a telemetry record must project to an object")
        if report.omitted:
            projected[TRUNCATION_MARKER_KEY] = report.as_dict()
        line = _finite_json(projected) + "\n"
        encoded = line.encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self._lock_path):
            self._rotate_locked(len(encoded))
            with self.path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
        return projected

    def read_all(self) -> list[dict[str, Any]]:
        """Read every record this sink wrote, refusing a malformed line.

        A line that does not parse raises instead of being skipped: a silently
        dropped record is indistinguishable from one that was never emitted.
        """
        records: list[dict[str, Any]] = []
        for candidate in [self._rotated(index) for index in range(self.max_files, 0, -1)] + [self.path]:
            if not candidate.exists():
                continue
            for number, line in enumerate(candidate.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ObservabilityError(f"{candidate.name}:{number} is not a parseable record: {exc}") from exc
                if not isinstance(decoded, dict):
                    raise ObservabilityError(f"{candidate.name}:{number} is not an object record")
                records.append(decoded)
        return records


def observation_failures(events: Iterable[Mapping[str, Any]]) -> tuple[tuple[str, int], ...]:
    """Count the failure classes present in a stream of telemetry records.

    This is the reader side of the contract: it recomputes the classification
    from the recorded fields instead of trusting a counter, so a wrong count in
    a produced artifact is detectable.
    """
    counter = FailureCounter()
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise ObservabilityError(f"telemetry event {index} is not a mapping")
        component = event.get("component")
        if component is None:
            raise ObservabilityError(f"telemetry event {index} has no component and cannot be counted")
        failure = classify_failure(
            outcome=event.get("outcome"),
            dispatch_state=event.get("dispatch_state"),
            device_state=event.get("device_state"),
            fault_code=event.get("fault_code"),
            reason_code=event.get("reason_code"),
            verification_status=event.get("verification_status"),
        )
        if failure is not None:
            counter.record(failure, component=component)
    counts: dict[str, int] = {}
    for failure, _component, count in counter.counts():
        counts[failure] = counts.get(failure, 0) + count
    return tuple(sorted(counts.items()))


def failure_projection(
    failure: FailureClass | str,
    *,
    component: str,
    run_id: str | None = None,
    action_id: str | None = None,
    attempt_id: str | None = None,
    monotonic_ns: int | None = None,
) -> dict[str, Any]:
    """Build the versioned metric record for one classified failure.

    This is the bridge between the two halves of the contract: a classified
    failure and the correlation envelope that makes the metric attributable.
    """
    normalized = FailureCounter._normalized(failure)
    correlation = CorrelationFields(
        run_id=run_id or "system",
        component=component,
        action_id=action_id,
        attempt_id=attempt_id,
        monotonic_ns=monotonic_ns,
    )
    return {"failure": normalized, "value": 1, **correlation.as_dict()}
