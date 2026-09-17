# System architecture

```text
Simulation camera -> Perception Observation -> World Model -> Agent Runtime
                                                  ^                 |
                                                  |                 v
Dashboard <- Read-only HTTP API <- Event Store <- ActionResult <- Motion / Virtual MCU
```

## Runtime units

1. `robot/`: ROS 2, Gazebo adapter, MoveIt and semantic action execution.
2. `services/perception/`: produces `Observation`; it never writes WorldState.
3. `services/world_model/`: reducer, verifier, event store and replay read model.
4. `services/agent_runtime/`: converts a user goal into a typed `TaskGraph`.
5. `firmware/virtual_mcu/`: protocol and safe-state simulator.
6. `services/backend/`: health/readiness plus a read-only projection of ordered event streams.
7. `apps/dashboard/`: task status, four expression states, evidence inspection and deterministic replay.

The runtime has two planner providers. The template provider is fully offline and deterministic. The optional Ollama
provider is localhost/container-network-only: it returns a five-way route decision, then trusted deterministic builders
emit the `TaskGraph`. A model response never becomes a joint, velocity, firmware, emergency-stop or completion command.

## Critical flow

1. Perception emits an `Observation` with frame, confidence and source.
2. World Model records a `WorldEvent`, applies the deterministic reducer and exposes `WorldState`.
3. Agent Runtime emits a typed semantic action from a `TaskGraph`.
4. Motion returns `ActionResult` with evidence references.
5. World Model verifies the expected result. Only it emits `VerificationResult`.
6. Backend/replay displays facts and evidence; it does not derive a second WorldState or accept control writes.

## Hardware runtime boundary (planned)

The physical path is intentionally a separate, owner-gated layer:

```text
device / Linux driver -> DeviceAdapter -> bounded DeviceRuntime
  -> ROS 2 typed boundary -> selected RMW (Fast DDS deployment)
  -> validation + provenance -> World Model / evidence -> read-only API
```

CAN, camera/touch, arm and safety adapters share lifecycle, cancellation,
queue and provenance rules, but remain separate plugins. Fast DDS is a
deployment transport choice, not a dependency of the adapter contract. E-stop,
watchdog, safe-enable and direct motion authority remain outside DDS. This
boundary is proposed in [ADR-0005](../decisions/ADR-0005-hardware-device-runtime.md);
no Fast DDS or physical hardware implementation is present in this repository
yet.

## Operational boundary

- `/healthz` reports process health; `/readyz` checks that the event source is readable.
- `/api/v1/runs` and `/api/v1/runs/{run_id}/events` expose ordered read models;
  `/api/*` remains a compatibility alias. The checked-in OpenAPI contract is
  `docs/api-openapi-v1.json`.
- Event JSONL files are cached by path, modification time and size; changed files invalidate automatically.
- Static responses use ETags, while versioned vendored assets use immutable caching.
- `POST`, `PUT`, `PATCH` and `DELETE` return `405 read_only`.
- Service logs are JSON Lines with `service`, `source`, `run_id` and per-run `sequence_no` fields. The same record shape accepts `simulation` and `hardware` sources without changing analysis code. Every record passes through the versioned rule set in
  `workbench.application.redaction` before it is written, and a scrubbed record records the rule
  version it was scrubbed by. Raw evidence stays in the referenced store; see
  [Security hardening](../security/hardening.md).
- Stage telemetry uses `event=stage_completed`, `details.stage` and `details.duration_ms`; `analyze_telemetry.py` computes P50/P95 for both sources.
- A controller may use `WORKBENCH_EVENT_SOURCE_URL` to read the simulation event source over HTTP. Its readiness is false when the peer is unavailable or returns malformed events.
- `apps/dashboard/data/` is fixture data for offline UI and API tests. It is never eligible as physical release evidence.

## Safety boundary

Agent Runtime cannot issue joint positions, velocity commands, emergency-stop decisions or physical completion claims. Those belong to Motion, Virtual MCU and the World Model verifier respectively.

## Hardware-in-the-loop boundary

```text
Linux host -- SocketCAN -- J5/J6 isolated CAN -- controller/fixture
     |                         |
     |                    J10 dual E-stop --> U8 --> J11 safe enable
     `-- evidence logger -----+---- scope/CAN/thermal raw files
                                      |
                         signed evidence register --> release gate
```

The HIL host may request semantic actions and record evidence. It cannot bypass
U8, synthesize a safe-enable pass, or convert a missing capture into completion.
The physical procedure and exact connector map are in the hardware wiring and
bring-up pages.
