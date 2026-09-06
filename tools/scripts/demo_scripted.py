import argparse
import io
import json
import tempfile
import time
from pathlib import Path
from typing import TextIO

from _paths import enable_local_packages

enable_local_packages()

from workbench_agent_runtime import build_template_plan
from workbench_backend.logging import StructuredLogger
from workbench_contracts import ActionOutcome, ActionResult, DeviceState, DispatchState, WorldEvent, WorldEventType
from workbench_virtual_mcu import VirtualMcu
from workbench_world_model import SQLiteEventStore, reduce_events, verify_object_in_tray


def event(
    event_id: str,
    run_id: str,
    sequence_no: int,
    event_type: WorldEventType,
    payload: dict,
    evidence: list[str],
) -> WorldEvent:
    return WorldEvent(
        event_id=event_id,
        run_id=run_id,
        sequence_no=sequence_no,
        event_type=event_type,
        occurred_at="2026-08-04T00:00:00Z",
        payload=payload,
        evidence_refs=evidence,
    )


def run_once(run_id: str, logger: StructuredLogger) -> dict:
    end_to_end_started = time.perf_counter()
    started = time.perf_counter()
    plan = build_template_plan("Place the red block in the tray")
    logger.emit(
        "stage_completed",
        "bounded semantic plan created",
        run_id=run_id,
        details={"stage": "planning", "duration_ms": (time.perf_counter() - started) * 1000},
    )
    started = time.perf_counter()
    mcu = VirtualMcu()
    mcu.command("execute")
    logger.emit(
        "stage_completed",
        "virtual MCU accepted semantic execution",
        run_id=run_id,
        details={"stage": "dispatch", "duration_ms": (time.perf_counter() - started) * 1000},
    )
    events = [
        event(
            "evt-001",
            run_id,
            1,
            WorldEventType.OBSERVATION,
            {"entity_id": "red_block", "entity_type": "block", "confidence": 0.98},
            ["camera-frame-001"],
        ),
        event(
            "evt-002",
            run_id,
            2,
            WorldEventType.OBSERVATION,
            {"entity_id": "tray", "entity_type": "tray", "location": "on:table", "confidence": 0.99},
            ["camera-frame-002"],
        ),
        event(
            "evt-003",
            run_id,
            3,
            WorldEventType.ACTION_RESULT,
            ActionResult(
                result_id="result-003",
                action_id="act-scripted-place",
                run_id=run_id,
                outcome=ActionOutcome.COMPLETED,
                dispatch_state=DispatchState.SENT,
                device_state=DeviceState.CONFIRMED,
                started_at="2026-08-04T00:00:00Z",
                ended_at="2026-08-04T00:00:01Z",
                entity_id="red_block",
                resulting_location="in:tray",
                evidence_refs=["action-result-003"],
            ).model_dump(mode="json"),
            ["action-result-003"],
        ),
        event(
            "evt-004",
            run_id,
            4,
            WorldEventType.OBSERVATION,
            {
                "entity_id": "red_block",
                "entity_type": "block",
                "location": "in:tray",
                "confidence": 0.97,
            },
            ["camera-frame-004"],
        ),
    ]
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteEventStore(Path(directory) / "events.sqlite")
        for item in events:
            store.append(item)
        stored_events = store.list_run(run_id)
        logger.emit(
            "stage_completed",
            "events persisted and read back",
            run_id=run_id,
            details={"stage": "event_store", "duration_ms": (time.perf_counter() - started) * 1000},
        )
        started = time.perf_counter()
        state = reduce_events(run_id, stored_events)
        logger.emit(
            "stage_completed",
            "world state reduced",
            run_id=run_id,
            details={"stage": "state_reduction", "duration_ms": (time.perf_counter() - started) * 1000},
        )
        started = time.perf_counter()
        verification = verify_object_in_tray(state, plan.task_id, "red_block", "tray")
        store.close()
    logger.emit(
        "stage_completed",
        "task evidence verified",
        run_id=run_id,
        details={"stage": "verification", "duration_ms": (time.perf_counter() - started) * 1000},
    )
    if verification.completed:
        mcu.command("complete")

    logger.emit(
        "stage_completed",
        "scripted pipeline completed",
        run_id=run_id,
        details={"stage": "end_to_end", "duration_ms": (time.perf_counter() - end_to_end_started) * 1000},
    )
    return {
        "run_id": run_id,
        "task_id": plan.task_id,
        "steps": [step.action.action_type.value for step in plan.steps],
        "mcu_state": mcu.state.value,
        "verified_complete": verification.completed,
        "evidence_refs": verification.evidence_refs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic observe-plan-verify pipeline")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--telemetry", type=Path)
    args = parser.parse_args()
    if args.iterations <= 0:
        raise ValueError("iterations must be positive")
    telemetry_stream: TextIO
    if args.telemetry:
        args.telemetry.parent.mkdir(parents=True, exist_ok=True)
        telemetry_stream = args.telemetry.open("w", encoding="utf-8")
    else:
        telemetry_stream = io.StringIO()
    try:
        logger = StructuredLogger("scripted-pipeline", telemetry_stream)
        outputs = [run_once(f"dry-run-{index:03d}", logger) for index in range(1, args.iterations + 1)]
    finally:
        telemetry_stream.close()
    if not all(output["verified_complete"] for output in outputs):
        raise RuntimeError("dry run failed verification")
    print(json.dumps({"run_count": len(outputs), "runs": outputs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
