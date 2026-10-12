# ROS 2 runtime TaskEvent integration

One-way adapter from the speech/agent task runtime
(`embodied-agent-runtime`) into the Workbench evidence chain, so a product-side
run and a runtime task share one correlation identity instead of being matched
by timestamp.

## What it does

`ingest_capture(store, path)` reads a JSONL capture of the runtime's terminal
`TaskEvent` messages on `/runtime/task_events` and appends one
`action_result` `WorldEvent` per line:

- `run_id` = the runtime's `request_id` + `workflow_id` + `task_id`, length-prefixed
  so two different field tuples cannot render to one identity.
- `action_id` = the ROS 2 `goal_id` of the `ExecuteTask` goal.
- `outcome` / `device_state` from the runtime's terminal outcome: `COMPLETED` →
  `confirmed`, `CANCELED`/`SAFE_STOP` → `stopped`, `DEVICE_FAULT` → `rejected`.
- `evidence_refs` = the runtime's `goal_id`, action status and error code.

Re-delivery is safe: the event identity is the runtime's own, so a repeated or
re-captured line replays instead of writing a second result. A line is refused
(not written) when the correlation fields are missing or a field is unusable.

## What it deliberately does not do

- No completion claim. A TaskEvent proves an Action terminal status and a
  protocol outcome, never that an actuator moved; that stays with the World
  Model verifier and observation evidence.
- No live DDS subscription. ROS 2 is not a Workbench dependency, so the capture
  is written by the runtime side and this module only reads files.
- No new event type. It reuses `action_result`, which the kernel store,
  contracts and read model already understand.

## Capture format

`ros2 topic echo --once --full-length /runtime/task_events --output json` per
event, or any logger that writes one message per line, with the default
`yaml`/`json` field names:

```json
{"stamp": {"sec": 1791790000, "nanosec": 123456789}, "task_id": "task-1",
 "target_id": "dock_a", "request_id": "request-1", "workflow_id": "single_task",
 "goal_id": "1a2b...", "action_status": 1, "outcome": 0, "error_code": 0,
 "duration_ms": 37, "message": "task completed through Device Bridge"}
```

## Quick start

```python
from integrations.ros2_runtime import ingest_capture
from workbench_world_model.event_store import SQLiteEventStore

store = SQLiteEventStore("runs/run-1/events.sqlite")
accepted = ingest_capture(store, "capture/task_events.jsonl")
```

## Runtime side requirement

`request_id`, `workflow_id` and `goal_id` on `TaskEvent`, and `request_id` /
`workflow_id` on `ExecuteTask.Goal`, are the correlation contract this adapter
reads. A capture from an older runtime build is rejected rather than bound by
guesswork.
