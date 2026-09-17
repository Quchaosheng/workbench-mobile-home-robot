from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "libs/application"),
    str(ROOT / "libs/contracts"),
    str(ROOT / "robot/control/workbench_motion"),
    str(ROOT / "services/agent_runtime"),
    str(ROOT / "services/world_model"),
]

from workbench.application.correlation_ledger import CorrelationLedger
from workbench_agent_runtime import build_template_plan
from workbench_agent_runtime.execution_controller import ExecutionController, ExecutionState
from workbench_agent_runtime.policy_validator import PolicyValidator
from workbench_contracts import (
    ActionOutcome,
    ActionResult,
    ActionType,
    DeviceState,
    DispatchState,
    McuFrame,
    SemanticAction,
    TaskGraph,
    VerificationStatus,
    WorldEvent,
    WorldEventType,
)
from workbench_motion.evidence import ExecutionEvent
from workbench_world_model import (
    FreshnessThresholds,
    ObservationAgingBoundary,
    ObservationFreshnessPolicy,
)
from workbench_world_model.event_store import SQLiteEventStore
from workbench_world_model.motion_evidence_adapter import MotionEvidenceAdapter
from workbench_world_model.reducer import create_world_state_snapshot, reduce_events
from workbench_world_model.verifier import VerificationContext, verify_object_in_tray


def mcu_frame(payload: dict[str, object]) -> McuFrame:
    common = {
        "protocol_version": "1.0",
        "sent_at_us": 1_000,
        "clock_id": "monotonic",
    }
    common.update(payload)
    return McuFrame.model_validate(common)


class CorrelatedPlaceAdapter:
    def __init__(self, store: SQLiteEventStore, ledger: CorrelationLedger, run_id: str, task_id: str) -> None:
        self.store = store
        self.ledger = ledger
        self.run_id = run_id
        self.task_id = task_id

    def dispatch(self, action: SemanticAction) -> ActionResult:
        self.ledger.record_dispatch(
            run_id=self.run_id,
            task_id=self.task_id,
            action=action,
            occurred_at="2026-08-31T11:00:00Z",
        )
        command = mcu_frame(
            {
                "frame_kind": "command",
                "frame_id": "mcu-frame-place-command-001",
                "command_id": 71,
                "opcode": "move",
                "retry_count": 0,
            }
        )
        acknowledgement = mcu_frame(
            {
                "frame_kind": "ack",
                "frame_id": "mcu-frame-place-ack-001",
                "command_id": 71,
                "opcode": "move",
                "result_code": 0,
                "fault_code": "none",
                "device_mode": "idle",
                "retry_count": 0,
            }
        )
        self.ledger.record_transport_frame(
            run_id=self.run_id,
            action_id=action.action_id,
            frame=command,
            occurred_at="2026-08-31T11:00:01Z",
        )
        self.ledger.record_transport_frame(
            run_id=self.run_id,
            action_id=action.action_id,
            frame=acknowledgement,
            occurred_at="2026-08-31T11:00:02Z",
        )
        result = ActionResult(
            result_id="result-place-001",
            action_id=action.action_id,
            run_id=self.run_id,
            outcome=ActionOutcome.COMPLETED,
            dispatch_state=DispatchState.SENT,
            device_state=DeviceState.CONFIRMED,
            started_at="2026-08-31T11:00:00Z",
            ended_at="2026-08-31T11:00:03Z",
            entity_id=action.target_id,
            resulting_location="in:tray",
            evidence_refs=[command.root.frame_id, acknowledgement.root.frame_id],
        )
        execution_event = ExecutionEvent(
            event_type="action_result",
            run_id=self.run_id,
            action_id=action.action_id,
            payload=result.model_dump(mode="json"),
        )
        reference = MotionEvidenceAdapter(self.store).append(execution_event)
        self.ledger.record_motion_result(result=result, evidence_ref=reference)
        return result


def test_observe_plan_dispatch_mcu_motion_world_event_verification_lookup(tmp_path: Path) -> None:
    run_id = "run-e2e-correlation-001"
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    store.append_allocated(
        event_id="observation-red-block-before-plan",
        run_id=run_id,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-31T10:59:58Z",
        payload={
            "entity_id": "red_block",
            "entity_type": "block",
            "location": "on:table",
            "confidence": 0.99,
        },
        evidence_refs=["camera-frame-before-plan-001"],
    )
    observed = reduce_events(run_id, store.list_run(run_id))
    assert observed.entity_locations["red_block"] == "on:table"

    planned = build_template_plan("Place the red block in the tray")
    planned_place = next(step for step in planned.steps if step.action.action_type is ActionType.PLACE)
    place_step = planned_place.model_copy(update={"depends_on": []}, deep=True)
    task_id = planned.task_id
    action = place_step.action
    graph = TaskGraph(
        task_id=task_id,
        goal=planned.goal,
        steps=[place_step],
        planner=planned.planner,
        model_route=planned.model_route,
    )
    ledger = CorrelationLedger(store)
    controller = ExecutionController(
        policy_validator=PolicyValidator(
            policy_config={"policy_version": "integration-v1", "high_impact_actions": frozenset()}
        ),
        adapter=CorrelatedPlaceAdapter(store, ledger, run_id, task_id),
    )

    report = controller.execute(graph)
    assert report.terminal_state is ExecutionState.SUCCEEDED

    store.append_allocated(
        event_id="observation-red-block-post-action",
        run_id=run_id,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-31T11:00:04Z",
        payload={
            "entity_id": "red_block",
            "entity_type": "block",
            "location": "in:tray",
            "confidence": 0.99,
            "observed_at": "2026-08-31T11:00:04Z",
            "source": "camera",
            "clock_id": "wall",
        },
        evidence_refs=["camera-frame-post-action-001"],
    )
    store.append_allocated(
        event_id="observation-tray-post-action",
        run_id=run_id,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-31T11:00:04Z",
        payload={
            "entity_id": "tray",
            "entity_type": "container",
            "location": "on:table",
            "confidence": 0.99,
            "observed_at": "2026-08-31T11:00:04Z",
            "source": "camera",
            "clock_id": "wall",
        },
        evidence_refs=["camera-frame-tray-001"],
    )
    events = store.list_run(run_id)
    # Post-action confirmation requires an explicit, replayable freshness
    # boundary; the reducer must not silently trust unbounded observation age.
    freshness_policy = ObservationFreshnessPolicy(
        rules={
            ("camera", "block"): FreshnessThresholds(stale_after_s=60.0, lost_after_s=120.0),
            ("camera", "container"): FreshnessThresholds(stale_after_s=60.0, lost_after_s=120.0),
        }
    )
    aging_boundary = ObservationAgingBoundary(as_of="2026-08-31T11:00:04Z", clock_id="wall")
    state = reduce_events(
        run_id,
        events,
        freshness_policy=freshness_policy,
        aging_boundary=aging_boundary,
    )
    # The verification boundary is injected: identity comes from the canonical
    # state hash and the timestamp is a real wall-clock reading.
    snapshot = create_world_state_snapshot(run_id, events)
    checked = verify_object_in_tray(
        state,
        task_id,
        "red_block",
        "tray",
        context=VerificationContext(
            state_hash=snapshot.state_hash,
            verified_at="2026-08-31T11:00:05Z",
            clock_id="wall",
        ),
    )
    assert checked.status is VerificationStatus.CONFIRMED
    ledger.record_verification(action_id=action.action_id, result=checked)

    record = ledger.lookup_verification(checked)
    assert record is not None
    assert record.action == action
    assert record.result is not None
    assert record.result.result_id == "result-place-001"
    assert record.world_event_id == "motion-result:run-e2e-correlation-001:result-place-001"
    assert [item.frame_id for item in record.transport_attempts] == [
        "mcu-frame-place-command-001",
        "mcu-frame-place-ack-001",
    ]
    assert record.verification == checked
    assert record.execution_evidence_refs == (
        "mcu-frame-place-command-001",
        "mcu-frame-place-ack-001",
    )
    assert record.verification_evidence_refs == ("camera-frame-post-action-001",)
    assert not set(record.execution_evidence_refs) & set(record.verification_evidence_refs)

    motion_event = MotionEvidenceAdapter(store).resolve(record.motion_evidence_ref or "")
    assert motion_event is not None
    assert motion_event.event_type is WorldEventType.ACTION_RESULT
    assert motion_event.payload["result_id"] == record.result.result_id
    assert all(isinstance(event, WorldEvent) for event in store.list_run(run_id))
    store.close()
