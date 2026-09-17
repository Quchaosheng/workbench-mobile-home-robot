from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "libs/application"),
    str(ROOT / "libs/contracts"),
    str(ROOT / "services/world_model"),
]

from workbench.application.correlation_ledger import CorrelationLedger, CorrelationLedgerError
from workbench_contracts import (
    ActionOutcome,
    ActionResult,
    ActionType,
    DeviceState,
    DispatchState,
    McuFrame,
    ReasonCode,
    RecoveryHint,
    SemanticAction,
    VerificationResult,
    VerificationStatus,
    WorldEventType,
)
from workbench_world_model.event_store import SQLiteEventStore
from workbench_world_model.reducer import reduce_events

RUN_ID = "run-correlation-001"
TASK_ID = "task-correlation-001"
ACTION_ID = "action-place-001"
OCCURRED_AT = "2026-08-31T10:00:00Z"


def action(action_id: str = ACTION_ID, *, action_type: ActionType = ActionType.PLACE) -> SemanticAction:
    parameters = {"destination_id": "tray"} if action_type is ActionType.PLACE else {}
    return SemanticAction(
        action_id=action_id,
        action_type=action_type,
        target_id="red_block",
        parameters=parameters,
    )


def command(frame_id: str, *, command_id: int = 41, retry_count: int = 0) -> McuFrame:
    return McuFrame.model_validate(
        {
            "protocol_version": "1.0",
            "frame_kind": "command",
            "frame_id": frame_id,
            "command_id": command_id,
            "opcode": "grip_open",
            "retry_count": retry_count,
            "sent_at_us": 1_000 + retry_count,
            "clock_id": "monotonic",
        }
    )


def ack(
    frame_id: str,
    *,
    command_id: int = 41,
    retry_count: int = 0,
    fault_code: str = "none",
    result_code: int = 0,
) -> McuFrame:
    return McuFrame.model_validate(
        {
            "protocol_version": "1.0",
            "frame_kind": "ack",
            "frame_id": frame_id,
            "command_id": command_id,
            "opcode": "grip_open",
            "result_code": result_code,
            "fault_code": fault_code,
            "device_mode": "idle" if result_code == 0 else "faulted",
            "retry_count": retry_count,
            "sent_at_us": 2_000 + retry_count,
            "clock_id": "monotonic",
        }
    )


def telemetry(frame_id: str, fault_code: str) -> McuFrame:
    return McuFrame.model_validate(
        {
            "protocol_version": "1.0",
            "frame_kind": "telemetry",
            "frame_id": frame_id,
            "sequence_no": 9,
            "fault_code": fault_code,
            "device_mode": "idle" if fault_code == "none" else "faulted",
            "sent_at_us": 3_000,
            "clock_id": "monotonic",
        }
    )


def stop(frame_id: str, *, command_id: int = 32_768) -> McuFrame:
    return McuFrame.model_validate(
        {
            "protocol_version": "1.0",
            "frame_kind": "stop",
            "frame_id": frame_id,
            "command_id": command_id,
            "opcode": "stop",
            "retry_count": 0,
            "sent_at_us": 4_000,
            "clock_id": "monotonic",
        }
    )


def stop_ack(frame_id: str, *, command_id: int = 32_768) -> McuFrame:
    return McuFrame.model_validate(
        {
            "protocol_version": "1.0",
            "frame_kind": "stop_ack",
            "frame_id": frame_id,
            "command_id": command_id,
            "opcode": "stop",
            "result_code": 0,
            "fault_code": "none",
            "device_mode": "stopped",
            "retry_count": 0,
            "sent_at_us": 4_100,
            "clock_id": "monotonic",
        }
    )


def result(*, action_id: str = ACTION_ID, run_id: str = RUN_ID, result_id: str = "result-001") -> ActionResult:
    return ActionResult(
        result_id=result_id,
        action_id=action_id,
        run_id=run_id,
        outcome=ActionOutcome.COMPLETED,
        dispatch_state=DispatchState.SENT,
        device_state=DeviceState.CONFIRMED,
        started_at="2026-08-31T10:00:01Z",
        ended_at="2026-08-31T10:00:02Z",
        entity_id="red_block",
        resulting_location="in:tray",
        evidence_refs=["mcu-frame-command-001", "mcu-frame-ack-001"],
    )


def verification(*, run_id: str = RUN_ID, verification_id: str = "verification-001") -> VerificationResult:
    return VerificationResult(
        verification_id=verification_id,
        run_id=run_id,
        task_id=TASK_ID,
        claim="red_block is in tray",
        status=VerificationStatus.CONFIRMED,
        reason_code=ReasonCode.GOAL_SATISFIED,
        completeness=1.0,
        evidence_refs=["camera-frame-post-action-001"],
        recovery_hint=RecoveryHint.NONE,
        verified_at="2026-08-31T10:00:03Z",
        rule_version="world-model-verifier-v1",
    )


def dispatched_ledger(tmp_path: Path) -> tuple[SQLiteEventStore, CorrelationLedger]:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    ledger = CorrelationLedger(store)
    ledger.record_dispatch(
        run_id=RUN_ID,
        task_id=TASK_ID,
        action=action(),
        occurred_at=OCCURRED_AT,
    )
    return store, ledger


def add_successful_transport(ledger: CorrelationLedger) -> None:
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=ACTION_ID,
        frame=command("mcu-frame-command-001"),
        occurred_at=OCCURRED_AT,
    )
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=ACTION_ID,
        frame=ack("mcu-frame-ack-001"),
        occurred_at=OCCURRED_AT,
    )


def persist_result(store: SQLiteEventStore, value: ActionResult) -> str:
    event = store.append_allocated(
        event_id=f"motion-result:{value.run_id}:{value.result_id}",
        run_id=value.run_id,
        event_type=WorldEventType.ACTION_RESULT,
        occurred_at=value.ended_at,
        payload=value.model_dump(mode="json"),
        evidence_refs=list(value.evidence_refs),
    )
    return f"world-event:{event.event_id}"


def test_dispatch_creates_one_stable_record(tmp_path: Path) -> None:
    store, ledger = dispatched_ledger(tmp_path)
    first = ledger.get(RUN_ID, ACTION_ID)
    ledger.record_dispatch(run_id=RUN_ID, task_id=TASK_ID, action=action(), occurred_at=OCCURRED_AT)

    assert first == ledger.get(RUN_ID, ACTION_ID)
    assert first is not None
    assert first.correlation_ref == f"correlation:{RUN_ID}:{ACTION_ID}"
    assert first.task_id == TASK_ID
    assert len(store.list_run(RUN_ID)) == 1

    with pytest.raises(CorrelationLedgerError, match="conflict"):
        ledger.record_dispatch(run_id=RUN_ID, task_id="task-other", action=action(), occurred_at=OCCURRED_AT)
    store.close()


def test_transport_retries_preserve_identity_and_distinguish_attempts(tmp_path: Path) -> None:
    store, ledger = dispatched_ledger(tmp_path)
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=ACTION_ID,
        frame=command("mcu-frame-command-001"),
        occurred_at=OCCURRED_AT,
    )
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=ACTION_ID,
        frame=command("mcu-frame-command-002", retry_count=1),
        occurred_at=OCCURRED_AT,
    )

    record = ledger.get(RUN_ID, ACTION_ID)
    assert record is not None
    assert [(item.command_id, item.frame_id, item.retry_count) for item in record.transport_attempts] == [
        (41, "mcu-frame-command-001", 0),
        (41, "mcu-frame-command-002", 1),
    ]
    store.close()


def test_structured_ids_join_without_message_parsing(tmp_path: Path) -> None:
    store, ledger = dispatched_ledger(tmp_path)
    add_successful_transport(ledger)
    value = result()
    evidence_ref = persist_result(store, value)
    ledger.record_motion_result(result=value, evidence_ref=evidence_ref)
    ledger.record_verification(action_id=ACTION_ID, result=verification())

    payload = ledger.get(RUN_ID, ACTION_ID).as_dict()  # type: ignore[union-attr]
    assert payload["run_id"] == RUN_ID
    assert payload["task_id"] == TASK_ID
    assert payload["action_id"] == ACTION_ID
    assert payload["transport_attempts"][0]["command_id"] == 41
    assert payload["result_id"] == "result-001"
    assert payload["world_event_id"] == "motion-result:run-correlation-001:result-001"
    assert payload["verification_id"] == "verification-001"
    assert "message" not in str(payload).lower()
    store.close()


def test_cross_run_or_action_rebinding_fails_closed(tmp_path: Path) -> None:
    store, ledger = dispatched_ledger(tmp_path)
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=ACTION_ID,
        frame=command("mcu-frame-command-001"),
        occurred_at=OCCURRED_AT,
    )
    before = len(store.list_run(RUN_ID))

    with pytest.raises(CorrelationLedgerError):
        ledger.record_transport_frame(
            run_id=RUN_ID,
            action_id="action-other",
            frame=ack("mcu-frame-ack-001"),
            occurred_at=OCCURRED_AT,
        )
    with pytest.raises(CorrelationLedgerError):
        ledger.record_motion_result(result=result(run_id="run-other"), evidence_ref="world-event:missing")

    other_run = "run-correlation-002"
    other_action = "action-place-002"
    ledger.record_dispatch(
        run_id=other_run,
        task_id="task-correlation-002",
        action=action(other_action),
        occurred_at=OCCURRED_AT,
    )
    other_before = list(store.list_run(other_run))
    with pytest.raises(CorrelationLedgerError, match="command_id"):
        ledger.record_transport_frame(
            run_id=other_run,
            action_id=other_action,
            frame=command("mcu-frame-cross-run-command", command_id=41),
            occurred_at=OCCURRED_AT,
        )

    assert len(store.list_run(RUN_ID)) == before
    assert store.list_run(other_run) == other_before
    store.close()


def test_duplicate_and_late_results_cannot_rebind(tmp_path: Path) -> None:
    store, ledger = dispatched_ledger(tmp_path)
    add_successful_transport(ledger)
    value = result()
    evidence_ref = persist_result(store, value)
    ledger.record_motion_result(result=value, evidence_ref=evidence_ref)
    count = len(store.list_run(RUN_ID))

    ledger.record_motion_result(result=value, evidence_ref=evidence_ref)
    with pytest.raises(CorrelationLedgerError):
        ledger.record_motion_result(
            result=result(action_id="action-other"),
            evidence_ref=evidence_ref,
        )

    assert len(store.list_run(RUN_ID)) == count
    assert ledger.get(RUN_ID, ACTION_ID).result == value  # type: ignore[union-attr]
    store.close()


def test_stop_and_watchdog_fault_scope_is_explicit(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    ledger = CorrelationLedger(store)
    ledger.record_dispatch(run_id=RUN_ID, task_id=TASK_ID, action=action(), occurred_at=OCCURRED_AT)
    before_invalid_stop = list(store.list_run(RUN_ID))
    with pytest.raises(CorrelationLedgerError, match="STOP action"):
        ledger.record_transport_frame(
            run_id=RUN_ID,
            action_id=ACTION_ID,
            frame=stop("mcu-frame-stop-on-place"),
            occurred_at=OCCURRED_AT,
        )
    assert store.list_run(RUN_ID) == before_invalid_stop

    stop_action = action("action-stop-001", action_type=ActionType.STOP)
    ledger.record_dispatch(run_id=RUN_ID, task_id=TASK_ID, action=stop_action, occurred_at=OCCURRED_AT)
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=stop_action.action_id,
        frame=stop("mcu-frame-stop-001"),
        occurred_at=OCCURRED_AT,
    )
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=stop_action.action_id,
        frame=stop_ack("mcu-frame-stop-ack-001"),
        occurred_at=OCCURRED_AT,
    )
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=None,
        frame=telemetry("mcu-frame-watchdog-001", "watchdog_expired"),
        occurred_at=OCCURRED_AT,
    )

    place_record = ledger.get(RUN_ID, ACTION_ID)
    scoped = ledger.get(RUN_ID, stop_action.action_id)
    assert place_record is not None and scoped is not None
    assert place_record.faults == () and scoped.faults == ()
    assert ledger.list_run_faults(RUN_ID)[-1].scope == "run"

    ledger.record_dispatch(
        run_id=RUN_ID,
        task_id=TASK_ID,
        action=action("action-place-002"),
        occurred_at=OCCURRED_AT,
    )
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=ACTION_ID,
        frame=telemetry("mcu-frame-link-lost-001", "link_lost"),
        occurred_at=OCCURRED_AT,
    )
    assert ledger.list_run_faults(RUN_ID)[-1].scope == "run"
    assert ledger.list_run_faults(RUN_ID)[-1].action_id is None
    store.close()


def test_reopen_replays_identical_record(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite"
    store = SQLiteEventStore(database)
    ledger = CorrelationLedger(store)
    ledger.record_dispatch(run_id=RUN_ID, task_id=TASK_ID, action=action(), occurred_at=OCCURRED_AT)
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=ACTION_ID,
        frame=command("mcu-frame-command-001"),
        occurred_at=OCCURRED_AT,
    )
    original = ledger.get(RUN_ID, ACTION_ID)
    store.close()

    reopened = SQLiteEventStore(database)
    assert CorrelationLedger(reopened).get(RUN_ID, ACTION_ID) == original
    assert reopened.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == [
        ("world_events",)
    ]
    reopened.close()


def test_malformed_correlation_fails_before_append_or_reduction(tmp_path: Path) -> None:
    store, ledger = dispatched_ledger(tmp_path)
    before_events = store.list_run(RUN_ID)
    before_state = reduce_events(RUN_ID, before_events)

    with pytest.raises(CorrelationLedgerError):
        ledger.record_transport_frame(
            run_id=" ",
            action_id=ACTION_ID,
            frame=command("mcu-frame-command-001"),
            occurred_at=OCCURRED_AT,
        )
    with pytest.raises(CorrelationLedgerError):
        ledger.record_transport_frame(
            run_id=RUN_ID,
            action_id=ACTION_ID,
            frame=ack("mcu-frame-ack-orphan"),
            occurred_at=OCCURRED_AT,
        )

    with pytest.raises(CorrelationLedgerError, match="healthy telemetry"):
        ledger.record_transport_frame(
            run_id=RUN_ID,
            action_id=None,
            frame=telemetry("mcu-frame-healthy-001", "none"),
            occurred_at=OCCURRED_AT,
        )

    assert store.list_run(RUN_ID) == before_events
    assert reduce_events(RUN_ID, store.list_run(RUN_ID)) == before_state
    store.close()


def test_ledger_separates_execution_and_verification_evidence(tmp_path: Path) -> None:
    store, ledger = dispatched_ledger(tmp_path)
    add_successful_transport(ledger)
    value = result()
    ledger.record_motion_result(result=value, evidence_ref=persist_result(store, value))
    checked = verification()
    ledger.record_verification(action_id=ACTION_ID, result=checked)

    record = ledger.lookup_verification(checked)
    verification_links = [event for event in store.list_run(RUN_ID) if event.payload.get("stage") == "verification"]
    assert record is not None
    assert len(verification_links) == 1
    assert verification_links[0].event_type is WorldEventType.TOOL_CALL
    assert record.execution_evidence_refs == tuple(value.evidence_refs)
    assert record.verification_evidence_refs == tuple(checked.evidence_refs)
    assert not set(record.execution_evidence_refs) & set(record.verification_evidence_refs)
    assert not hasattr(record, "physical_success")
    assert not hasattr(record, "task_complete")
    store.close()


def test_completed_result_rejects_explicit_failed_ack_evidence(tmp_path: Path) -> None:
    store, ledger = dispatched_ledger(tmp_path)
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=ACTION_ID,
        frame=command("mcu-frame-command-001"),
        occurred_at=OCCURRED_AT,
    )
    ledger.record_transport_frame(
        run_id=RUN_ID,
        action_id=ACTION_ID,
        frame=ack(
            "mcu-frame-ack-001",
            result_code=1,
            fault_code="duplicate_frame",
        ),
        occurred_at=OCCURRED_AT,
    )
    value = result()
    evidence_ref = persist_result(store, value)
    before = list(store.list_run(RUN_ID))

    with pytest.raises(CorrelationLedgerError, match="contradicts"):
        ledger.record_motion_result(result=value, evidence_ref=evidence_ref)

    assert store.list_run(RUN_ID) == before
    assert ledger.get(RUN_ID, ACTION_ID).result is None  # type: ignore[union-attr]
    store.close()
