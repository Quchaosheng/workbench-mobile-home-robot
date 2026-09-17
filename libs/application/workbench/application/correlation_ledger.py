"""Append-only traceability across semantic action execution boundaries.

The ledger is Integration-owned glue.  It projects structured correlation
metadata from the existing World Model event store; it does not add a second
store, alter owning module contracts, or turn execution evidence into observed
world truth.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError
from workbench_contracts import (
    ActionResult,
    ActionType,
    DeviceState,
    McuFaultCode,
    McuFrame,
    SemanticAction,
    VerificationResult,
    WorldEvent,
    WorldEventType,
)
from workbench_world_model.event_store import EventStoreIntegrityError, SQLiteEventStore

_SCHEMA = "semantic-action-correlation-v1"
_REFERENCE_PREFIX = "world-event:"
_CORRELATION_EVENT_PREFIX = "correlation-"
_TRANSPORT_KINDS = frozenset({"command", "ack", "telemetry", "stop", "stop_ack"})
_REQUEST_KINDS = frozenset({"command", "stop"})
_RESPONSE_KINDS = frozenset({"ack", "stop_ack"})
_FAULT_CODES = frozenset({McuFaultCode.WATCHDOG_EXPIRED.value, McuFaultCode.LINK_LOST.value})


class CorrelationLedgerError(ValueError):
    """Correlation data is malformed, ambiguous, or conflicts with replay."""


@dataclass(frozen=True)
class TransportAttempt:
    """One retained logical protocol frame, including retry identity."""

    frame_id: str
    frame_kind: str
    command_id: int | None
    opcode: str | None
    retry_count: int | None
    result_code: int | None
    fault_code: str | None
    device_mode: str | None
    late: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "frame_kind": self.frame_kind,
            "command_id": self.command_id,
            "opcode": self.opcode,
            "retry_count": self.retry_count,
            "result_code": self.result_code,
            "fault_code": self.fault_code,
            "device_mode": self.device_mode,
            "late": self.late,
        }


@dataclass(frozen=True)
class FaultLink:
    """A fault with explicit action or run scope; never a guessed binding."""

    frame_id: str
    fault_code: str
    scope: Literal["action", "run"]
    action_id: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "fault_code": self.fault_code,
            "scope": self.scope,
            "action_id": self.action_id,
        }


@dataclass(frozen=True)
class CorrelationRecord:
    """Replay-derived traceability record, not a completion or causation claim."""

    correlation_ref: str
    run_id: str
    task_id: str
    action_id: str
    action: SemanticAction
    transport_attempts: tuple[TransportAttempt, ...] = ()
    result: ActionResult | None = None
    motion_evidence_ref: str | None = None
    world_event_id: str | None = None
    verification: VerificationResult | None = None
    faults: tuple[FaultLink, ...] = ()

    @property
    def execution_evidence_refs(self) -> tuple[str, ...]:
        return () if self.result is None else tuple(self.result.evidence_refs)

    @property
    def verification_evidence_refs(self) -> tuple[str, ...]:
        return () if self.verification is None else tuple(self.verification.evidence_refs)

    def as_dict(self) -> dict[str, object]:
        return {
            "correlation_ref": self.correlation_ref,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "action_id": self.action_id,
            "action": self.action.model_dump(mode="json"),
            "transport_attempts": [item.as_dict() for item in self.transport_attempts],
            "result_id": None if self.result is None else self.result.result_id,
            "result": None if self.result is None else self.result.model_dump(mode="json"),
            "motion_evidence_ref": self.motion_evidence_ref,
            "world_event_id": self.world_event_id,
            "verification_id": None if self.verification is None else self.verification.verification_id,
            "verification": (None if self.verification is None else self.verification.model_dump(mode="json")),
            "execution_evidence_refs": list(self.execution_evidence_refs),
            "verification_evidence_refs": list(self.verification_evidence_refs),
            "faults": [item.as_dict() for item in self.faults],
        }


@dataclass
class _RecordBuilder:
    run_id: str
    task_id: str
    action: SemanticAction
    transport_attempts: list[TransportAttempt] = field(default_factory=list)
    result: ActionResult | None = None
    motion_evidence_ref: str | None = None
    world_event_id: str | None = None
    verification: VerificationResult | None = None
    faults: list[FaultLink] = field(default_factory=list)

    def freeze(self) -> CorrelationRecord:
        action = self.action.model_copy(deep=True)
        result = None if self.result is None else self.result.model_copy(deep=True)
        verification = None if self.verification is None else self.verification.model_copy(deep=True)
        return CorrelationRecord(
            correlation_ref=f"correlation:{self.run_id}:{action.action_id}",
            run_id=self.run_id,
            task_id=self.task_id,
            action_id=action.action_id,
            action=action,
            transport_attempts=tuple(self.transport_attempts),
            result=result,
            motion_evidence_ref=self.motion_evidence_ref,
            world_event_id=self.world_event_id,
            verification=verification,
            faults=tuple(self.faults),
        )


class CorrelationLedger:
    """Validate, append, and replay structured correlation WorldEvents."""

    def __init__(self, store: SQLiteEventStore) -> None:
        if not isinstance(store, SQLiteEventStore):
            raise TypeError("store must be a SQLiteEventStore")
        self._store = store
        self._global_request_bindings()

    def record_dispatch(
        self,
        *,
        run_id: str,
        task_id: str,
        action: SemanticAction,
        occurred_at: str,
    ) -> CorrelationRecord:
        run_id = _non_blank(run_id, "run_id")
        task_id = _non_blank(task_id, "task_id")
        occurred_at = _non_blank(occurred_at, "occurred_at")
        if not isinstance(action, SemanticAction):
            raise CorrelationLedgerError("action must be a SemanticAction")
        action_snapshot = SemanticAction.model_validate(action.model_dump(mode="python"))
        _non_blank(action_snapshot.action_id, "action.action_id")

        existing = self.get(run_id, action_snapshot.action_id)
        if existing is not None:
            if existing.task_id == task_id and existing.action == action_snapshot:
                return existing
            raise CorrelationLedgerError("dispatch conflicts with the existing run/action binding")

        payload = {
            "correlation_schema": _SCHEMA,
            "stage": "dispatch",
            "task_id": task_id,
            "action_id": action_snapshot.action_id,
            "action": action_snapshot.model_dump(mode="json"),
        }
        self._append(
            event_id=_event_id("dispatch", run_id, action_snapshot.action_id),
            run_id=run_id,
            event_type=WorldEventType.ACTION_REQUEST,
            occurred_at=occurred_at,
            payload=payload,
        )
        return self._required_record(run_id, action_snapshot.action_id)

    def record_transport_frame(
        self,
        *,
        run_id: str,
        action_id: str | None,
        frame: McuFrame,
        occurred_at: str,
        late: bool = False,
    ) -> str:
        run_id = _non_blank(run_id, "run_id")
        occurred_at = _non_blank(occurred_at, "occurred_at")
        if type(late) is not bool:
            raise CorrelationLedgerError("late must be a boolean")
        if not isinstance(frame, McuFrame):
            raise CorrelationLedgerError("frame must be an McuFrame")
        frame_snapshot = McuFrame.model_validate(frame.model_dump(mode="python"))
        value = frame_snapshot.root
        if value.frame_kind not in _TRANSPORT_KINDS:
            raise CorrelationLedgerError("unsupported MCU frame kind")

        records = self._records(run_id)
        resolved_action_id = None if action_id is None else _non_blank(action_id, "action_id")
        scope: Literal["action", "run"] = "action"

        if value.frame_kind in _REQUEST_KINDS | _RESPONSE_KINDS:
            if resolved_action_id is None:
                raise CorrelationLedgerError(f"{value.frame_kind} requires an explicit action_id")
            if resolved_action_id not in records:
                raise CorrelationLedgerError("transport frame has no prior dispatch binding")
            self._validate_command_binding(run_id, resolved_action_id, frame_snapshot)
        elif value.frame_kind == "telemetry":
            if value.fault_code.value not in _FAULT_CODES:
                raise CorrelationLedgerError("healthy telemetry is not action-correlation evidence")
            active = [record for record in records.values() if record.result is None]
            if len(active) == 1:
                sole_action_id = active[0].action_id
                if resolved_action_id is not None and resolved_action_id != sole_action_id:
                    raise CorrelationLedgerError("telemetry action_id conflicts with the sole active action")
                resolved_action_id = sole_action_id
            else:
                resolved_action_id = None
                scope = "run"

        task_id = None if resolved_action_id is None else records[resolved_action_id].task_id
        payload = {
            "correlation_schema": _SCHEMA,
            "stage": "transport",
            "task_id": task_id,
            "action_id": resolved_action_id,
            "scope": scope,
            "late": late,
            "frame": frame_snapshot.model_dump(mode="json"),
        }
        event_id = _event_id("frame", value.frame_id)
        existing = self._store.get_event(event_id)
        if existing is not None:
            if _event_matches(existing, run_id, payload):
                return existing.event_id
            raise CorrelationLedgerError("frame_id conflicts with an existing correlation binding")

        fault_code = getattr(value, "fault_code", McuFaultCode.NONE)
        event_type = WorldEventType.FAULT if fault_code is not McuFaultCode.NONE else WorldEventType.TOOL_CALL
        stored = self._append(
            event_id=event_id,
            run_id=run_id,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
            evidence_refs=[value.frame_id],
        )
        return stored.event_id

    def record_motion_result(self, *, result: ActionResult, evidence_ref: str) -> CorrelationRecord:
        if not isinstance(result, ActionResult):
            raise CorrelationLedgerError("result must be an ActionResult")
        result_snapshot = ActionResult.model_validate(result.model_dump(mode="python"))
        run_id = _non_blank(result_snapshot.run_id, "result.run_id")
        action_id = _non_blank(result_snapshot.action_id, "result.action_id")
        evidence_ref = _non_blank(evidence_ref, "evidence_ref")
        record = self._required_record(run_id, action_id)

        if not evidence_ref.startswith(_REFERENCE_PREFIX):
            raise CorrelationLedgerError("motion evidence_ref must be a world-event reference")
        world_event_id = evidence_ref.removeprefix(_REFERENCE_PREFIX)
        if not world_event_id:
            raise CorrelationLedgerError("motion evidence_ref is missing its WorldEvent id")
        world_event = self._store.get_event(world_event_id)
        if world_event is None:
            raise CorrelationLedgerError("motion evidence_ref does not resolve")
        if (
            world_event.event_type is not WorldEventType.ACTION_RESULT
            or world_event.run_id != run_id
            or world_event.payload != result_snapshot.model_dump(mode="json")
            or world_event.evidence_refs != result_snapshot.evidence_refs
        ):
            raise CorrelationLedgerError("motion WorldEvent conflicts with the supplied ActionResult")

        retained_frames = {attempt.frame_id for attempt in record.transport_attempts}
        missing_frames = [
            reference
            for reference in result_snapshot.evidence_refs
            if reference.startswith("mcu-frame-") and reference not in retained_frames
        ]
        if missing_frames:
            raise CorrelationLedgerError("ActionResult refers to MCU frames not bound to this action")

        referenced_attempts = [
            attempt for attempt in record.transport_attempts if attempt.frame_id in result_snapshot.evidence_refs
        ]
        referenced_acks = [attempt for attempt in referenced_attempts if attempt.frame_kind in _RESPONSE_KINDS]
        if result_snapshot.device_state is DeviceState.CONFIRMED and referenced_acks:
            if not any(_is_successful_ack(attempt) for attempt in referenced_acks):
                raise CorrelationLedgerError("confirmed ActionResult contradicts its explicit failed ACK evidence")

        if record.result is not None:
            if (
                record.result == result_snapshot
                and record.motion_evidence_ref == evidence_ref
                and record.world_event_id == world_event_id
            ):
                return record
            raise CorrelationLedgerError("terminal result conflicts with the existing action binding")

        payload = {
            "correlation_schema": _SCHEMA,
            "stage": "motion_result",
            "task_id": record.task_id,
            "action_id": action_id,
            "result": result_snapshot.model_dump(mode="json"),
            "motion_evidence_ref": evidence_ref,
            "world_event_id": world_event_id,
        }
        event_id = _event_id("result", result_snapshot.result_id)
        existing = self._store.get_event(event_id)
        if existing is not None:
            if not _event_matches(existing, run_id, payload):
                raise CorrelationLedgerError("result_id conflicts with an existing correlation binding")
        else:
            self._append(
                event_id=event_id,
                run_id=run_id,
                event_type=WorldEventType.TOOL_CALL,
                occurred_at=result_snapshot.ended_at,
                payload=payload,
                evidence_refs=[evidence_ref, *result_snapshot.evidence_refs],
            )
        return self._required_record(run_id, action_id)

    def record_verification(self, *, action_id: str, result: VerificationResult) -> CorrelationRecord:
        action_id = _non_blank(action_id, "action_id")
        if not isinstance(result, VerificationResult):
            raise CorrelationLedgerError("result must be a VerificationResult")
        result_snapshot = VerificationResult.model_validate(result.model_dump(mode="python"))
        record = self._required_record(result_snapshot.run_id, action_id)
        if record.task_id != result_snapshot.task_id:
            raise CorrelationLedgerError("verification task_id conflicts with the action binding")
        if record.verification is not None:
            if record.verification == result_snapshot:
                return record
            raise CorrelationLedgerError("verification conflicts with the existing action binding")

        payload = {
            "correlation_schema": _SCHEMA,
            "stage": "verification",
            "task_id": record.task_id,
            "action_id": action_id,
            "verification": result_snapshot.model_dump(mode="json"),
        }
        event_id = _event_id("verification", result_snapshot.verification_id)
        existing = self._store.get_event(event_id)
        if existing is not None:
            if not _event_matches(existing, result_snapshot.run_id, payload):
                raise CorrelationLedgerError("verification_id conflicts with an existing correlation binding")
        else:
            self._append(
                event_id=event_id,
                run_id=result_snapshot.run_id,
                event_type=WorldEventType.TOOL_CALL,
                occurred_at=result_snapshot.verified_at,
                payload=payload,
                evidence_refs=list(result_snapshot.evidence_refs),
            )
        return self._required_record(result_snapshot.run_id, action_id)

    def get(self, run_id: str, action_id: str) -> CorrelationRecord | None:
        run_id = _non_blank(run_id, "run_id")
        action_id = _non_blank(action_id, "action_id")
        return self._records(run_id).get(action_id)

    def lookup_verification(self, result: VerificationResult) -> CorrelationRecord | None:
        if not isinstance(result, VerificationResult):
            raise CorrelationLedgerError("result must be a VerificationResult")
        matches = [
            record
            for record in self._records(result.run_id).values()
            if record.task_id == result.task_id and record.verification == result
        ]
        if len(matches) > 1:
            raise CorrelationLedgerError("verification identity is ambiguously bound")
        return matches[0] if matches else None

    def list_run_faults(self, run_id: str) -> tuple[FaultLink, ...]:
        run_id = _non_blank(run_id, "run_id")
        _, run_faults = self._project(run_id)
        return run_faults

    def _required_record(self, run_id: str, action_id: str) -> CorrelationRecord:
        record = self.get(run_id, action_id)
        if record is None:
            raise CorrelationLedgerError("correlation has no prior dispatch binding")
        return record

    def _records(self, run_id: str) -> dict[str, CorrelationRecord]:
        records, _ = self._project(run_id)
        return records

    def _project(self, run_id: str) -> tuple[dict[str, CorrelationRecord], tuple[FaultLink, ...]]:
        builders: dict[str, _RecordBuilder] = {}
        run_faults: list[FaultLink] = []
        for event in self._store.list_run(run_id):
            if not _is_correlation_event(event):
                continue
            payload = event.payload
            stage = payload.get("stage")
            if stage == "dispatch":
                _require_event_type(event, WorldEventType.ACTION_REQUEST)
                _require_keys(payload, {"correlation_schema", "stage", "task_id", "action_id", "action"})
                task_id = _non_blank(payload["task_id"], "payload.task_id")
                action_id = _non_blank(payload["action_id"], "payload.action_id")
                try:
                    action = SemanticAction.model_validate_json(_strict_json(payload["action"]))
                except ValidationError as error:
                    raise CorrelationLedgerError("persisted dispatch action is malformed") from error
                if action.action_id != action_id or action_id in builders:
                    raise CorrelationLedgerError("persisted dispatch binding is conflicting or duplicated")
                builders[action_id] = _RecordBuilder(run_id, task_id, action)
                continue

            if stage == "transport":
                _require_keys(
                    payload,
                    {"correlation_schema", "stage", "task_id", "action_id", "scope", "late", "frame"},
                )
                scope = payload["scope"]
                if scope not in {"action", "run"} or type(payload["late"]) is not bool:
                    raise CorrelationLedgerError("persisted transport scope or late marker is malformed")
                try:
                    frame = McuFrame.model_validate(payload["frame"])
                except ValidationError as error:
                    raise CorrelationLedgerError("persisted transport frame is malformed") from error
                attempt = _transport_attempt(frame, payload["late"])
                if attempt.frame_kind == "telemetry" and attempt.fault_code not in _FAULT_CODES:
                    raise CorrelationLedgerError("persisted healthy telemetry is not correlation evidence")
                expected_event_type = (
                    WorldEventType.FAULT
                    if attempt.fault_code is not None and attempt.fault_code != McuFaultCode.NONE.value
                    else WorldEventType.TOOL_CALL
                )
                _require_event_type(event, expected_event_type)
                action_id = payload["action_id"]
                fault_code = attempt.fault_code
                if scope == "run":
                    if action_id is not None or payload["task_id"] is not None:
                        raise CorrelationLedgerError("run-scoped transport must not name an action or task")
                    if fault_code not in _FAULT_CODES:
                        raise CorrelationLedgerError("run-scoped transport must be an explicit link/watchdog fault")
                    run_faults.append(FaultLink(attempt.frame_id, fault_code, "run", None))
                    continue
                action_id = _non_blank(action_id, "payload.action_id")
                builder = builders.get(action_id)
                if builder is None or payload["task_id"] != builder.task_id:
                    raise CorrelationLedgerError("persisted transport has no matching dispatch")
                _validate_action_frame_kind(builder.action, attempt.frame_kind)
                builder.transport_attempts.append(attempt)
                if fault_code is not None and fault_code != McuFaultCode.NONE.value:
                    builder.faults.append(FaultLink(attempt.frame_id, fault_code, "action", action_id))
                continue

            if stage == "motion_result":
                _require_event_type(event, WorldEventType.TOOL_CALL)
                _require_keys(
                    payload,
                    {
                        "correlation_schema",
                        "stage",
                        "task_id",
                        "action_id",
                        "result",
                        "motion_evidence_ref",
                        "world_event_id",
                    },
                )
                action_id = _non_blank(payload["action_id"], "payload.action_id")
                builder = builders.get(action_id)
                if builder is None or payload["task_id"] != builder.task_id or builder.result is not None:
                    raise CorrelationLedgerError("persisted Motion result has no unique matching dispatch")
                try:
                    result = ActionResult.model_validate_json(_strict_json(payload["result"]))
                except ValidationError as error:
                    raise CorrelationLedgerError("persisted ActionResult is malformed") from error
                if result.run_id != run_id or result.action_id != action_id:
                    raise CorrelationLedgerError("persisted ActionResult identity conflicts with its binding")
                builder.result = result
                builder.motion_evidence_ref = _non_blank(payload["motion_evidence_ref"], "payload.motion_evidence_ref")
                builder.world_event_id = _non_blank(payload["world_event_id"], "payload.world_event_id")
                continue

            if stage == "verification":
                _require_event_type(event, WorldEventType.TOOL_CALL)
                _require_keys(
                    payload,
                    {"correlation_schema", "stage", "task_id", "action_id", "verification"},
                )
                action_id = _non_blank(payload["action_id"], "payload.action_id")
                builder = builders.get(action_id)
                if builder is None or payload["task_id"] != builder.task_id or builder.verification is not None:
                    raise CorrelationLedgerError("persisted verification has no unique matching dispatch")
                try:
                    checked = VerificationResult.model_validate_json(_strict_json(payload["verification"]))
                except ValidationError as error:
                    raise CorrelationLedgerError("persisted VerificationResult is malformed") from error
                if checked.run_id != run_id or checked.task_id != builder.task_id:
                    raise CorrelationLedgerError("persisted VerificationResult identity conflicts with its binding")
                builder.verification = checked
                continue

            raise CorrelationLedgerError("persisted correlation stage is unknown")

        return ({action_id: builder.freeze() for action_id, builder in builders.items()}, tuple(run_faults))

    def _validate_command_binding(self, run_id: str, action_id: str, frame: McuFrame) -> None:
        value = frame.root
        record = self._required_record(run_id, action_id)
        _validate_action_frame_kind(record.action, value.frame_kind)

        global_bindings = self._global_request_bindings()
        existing_binding = global_bindings.get(value.command_id)
        if existing_binding is not None and existing_binding[:2] != (run_id, action_id):
            raise CorrelationLedgerError("command_id is already bound to a different run/action")

        attempts = [attempt for record in self._records(run_id).values() for attempt in record.transport_attempts]
        same_command = [attempt for attempt in attempts if attempt.command_id == value.command_id]
        owning_actions = {
            record.action_id
            for record in self._records(run_id).values()
            if any(attempt.command_id == value.command_id for attempt in record.transport_attempts)
        }
        if owning_actions and owning_actions != {action_id}:
            raise CorrelationLedgerError("command_id is already bound to a different action")

        if value.frame_kind in _REQUEST_KINDS:
            requests = [attempt for attempt in same_command if attempt.frame_kind in _REQUEST_KINDS]
            if requests and any(attempt.opcode != value.opcode for attempt in requests):
                raise CorrelationLedgerError("command retry changes the bound opcode")
            if requests and value.retry_count < max(attempt.retry_count or 0 for attempt in requests):
                raise CorrelationLedgerError("command retry_count moves backwards")
            return

        requests = [attempt for attempt in same_command if attempt.frame_kind in _REQUEST_KINDS]
        expected_kind = "stop" if value.frame_kind == "stop_ack" else "command"
        matching = [
            attempt
            for attempt in requests
            if attempt.frame_kind == expected_kind
            and attempt.opcode == value.opcode
            and attempt.retry_count == value.retry_count
        ]
        if not matching:
            raise CorrelationLedgerError("acknowledgement has no matching command attempt")

    def _global_request_bindings(self) -> dict[int, tuple[str, str, str, str]]:
        """Return fail-closed command ownership across the one existing store.

        Protocol v1 has no session epoch, so a command_id cannot be safely
        rebound during the lifetime represented by one SQLiteEventStore.
        """
        bindings: dict[int, tuple[str, str, str, str]] = {}
        rows = self._store.connection.execute(
            "SELECT event_json FROM world_events ORDER BY run_id, sequence_no"
        ).fetchall()
        for (event_json,) in rows:
            try:
                event = WorldEvent.model_validate_json(event_json)
            except ValidationError as error:
                raise CorrelationLedgerError("persisted WorldEvent is malformed") from error
            if not _is_correlation_event(event) or event.payload.get("stage") != "transport":
                continue
            try:
                frame = McuFrame.model_validate(event.payload.get("frame"))
            except ValidationError as error:
                raise CorrelationLedgerError("persisted transport frame is malformed") from error
            value = frame.root
            if value.frame_kind not in _REQUEST_KINDS:
                continue
            action_id = _non_blank(event.payload.get("action_id"), "payload.action_id")
            candidate = (event.run_id, action_id, value.frame_kind, value.opcode)
            existing = bindings.get(value.command_id)
            if existing is not None and existing != candidate:
                raise CorrelationLedgerError("command_id has conflicting persisted run/action ownership")
            bindings[value.command_id] = candidate
        return bindings

    def _append(
        self,
        *,
        event_id: str,
        run_id: str,
        event_type: WorldEventType,
        occurred_at: str,
        payload: dict[str, Any],
        evidence_refs: list[str] | None = None,
    ) -> WorldEvent:
        try:
            return self._store.append_allocated(
                event_id=event_id,
                run_id=run_id,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload,
                evidence_refs=evidence_refs,
            )
        except (EventStoreIntegrityError, TypeError, ValueError) as error:
            raise CorrelationLedgerError("correlation append conflicts with persisted evidence") from error


def _transport_attempt(frame: McuFrame, late: bool) -> TransportAttempt:
    value = frame.root
    fault = getattr(value, "fault_code", None)
    mode = getattr(value, "device_mode", None)
    return TransportAttempt(
        frame_id=value.frame_id,
        frame_kind=value.frame_kind,
        command_id=getattr(value, "command_id", None),
        opcode=getattr(value, "opcode", None),
        retry_count=getattr(value, "retry_count", None),
        result_code=getattr(value, "result_code", None),
        fault_code=None if fault is None else fault.value,
        device_mode=None if mode is None else mode.value,
        late=late,
    )


def _validate_action_frame_kind(action: SemanticAction, frame_kind: str) -> None:
    if frame_kind in {"stop", "stop_ack"} and action.action_type is not ActionType.STOP:
        raise CorrelationLedgerError("STOP transport requires a STOP action")
    if frame_kind in {"command", "ack"} and action.action_type is ActionType.STOP:
        raise CorrelationLedgerError("STOP action requires STOP transport")


def _is_successful_ack(attempt: TransportAttempt) -> bool:
    if attempt.frame_kind not in _RESPONSE_KINDS:
        return False
    if attempt.result_code != 0 or attempt.fault_code != McuFaultCode.NONE.value:
        return False
    if attempt.frame_kind == "stop_ack":
        return attempt.device_mode == "stopped"
    return attempt.device_mode != "faulted"


def _is_correlation_event(event: WorldEvent) -> bool:
    schema = event.payload.get("correlation_schema")
    if event.event_id.startswith(_CORRELATION_EVENT_PREFIX) or schema is not None:
        if schema != _SCHEMA:
            raise CorrelationLedgerError("persisted correlation schema is missing or unsupported")
        return True
    return False


def _event_matches(event: WorldEvent, run_id: str, payload: dict[str, Any]) -> bool:
    return event.run_id == run_id and event.payload == payload


def _event_id(kind: str, *parts: object) -> str:
    canonical = json.dumps([kind, *parts], allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"correlation-{kind}:{digest}"


def _non_blank(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise CorrelationLedgerError(f"{field_name} must be a non-empty string")
    return value


def _require_event_type(event: WorldEvent, expected: WorldEventType) -> None:
    if event.event_type is not expected:
        raise CorrelationLedgerError(f"persisted {event.payload.get('stage')} event has the wrong event_type")


def _require_keys(payload: dict[str, Any], expected: set[str]) -> None:
    if set(payload) != expected:
        raise CorrelationLedgerError("persisted correlation payload fields are malformed")


def _strict_json(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
