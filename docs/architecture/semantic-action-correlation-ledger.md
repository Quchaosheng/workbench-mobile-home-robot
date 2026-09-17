# Semantic action correlation ledger

Issue #64 adds an Integration-owned traceability projection. It answers a
bounded question: which structured dispatch, transport, Motion result,
persisted WorldEvent, and verification identifiers belong to one
`(run_id, action_id)` pair?

It does not answer whether the physical action happened, whether the
ActionResult caused an observed state, or whether the task is complete.
Verification remains a World Model decision supported by Observation evidence.

## Storage and replay

`CorrelationLedger` appends correlation metadata as ordinary `WorldEvent`
records through the existing `SQLiteEventStore`. It creates no table, mutable
side database, duplicate WorldState, or hidden cache. Every query replays the
run in `sequence_no` order and reconstructs an immutable projection.

The private payload discriminator is
`semantic-action-correlation-v1`. It is an implementation detail inside
`libs/application`; it is not a new shared interface or contract.

The recorded stages are:

| Stage | Existing WorldEvent type | Structured identities |
| --- | --- | --- |
| dispatch | `action_request` | `run_id`, `task_id`, `action_id`, full `SemanticAction` |
| transport | `tool_call` or `fault` | `action_id` when known, `command_id`, `frame_id`, `retry_count`, MCU fields |
| Motion result link | `tool_call` | `result_id`, `action_id`, Motion evidence ref, ActionResult WorldEvent id |
| verification link | `tool_call` | `verification_id`, `run_id`, `task_id`, explicit `action_id` lookup key |

Deterministic internal event ids are SHA-256 digests of typed identity fields.
They prevent delimiter ambiguity while `frame_id`, `result_id`, and
`verification_id` remain visible in the structured payload.

## Binding rules

A dispatch establishes exactly one task and action payload for a
`(run_id, action_id)` key. An identical retry is idempotent. Reusing the key
with a different task or action fails before append.

A command or STOP frame requires an explicit prior action binding. A retry
preserves `command_id`, action, and opcode, while its distinct `frame_id` and
`retry_count` remain visible as a separate attempt. An ACK or `stop_ack`
requires a retained request with the same command, opcode, retry count, run,
and action.

Protocol v1 has no session epoch. Within one retained event-store lifetime,
each `command_id` therefore has exactly one run/action owner and rebinding is
rejected. Reuse after transport restart or identifier wrap remains unsupported
until a shared session identity is approved; the ledger never guesses across
that boundary.

`frame_id` is a stable evidence identity. Reusing it with different content,
run, action, or scope fails closed. A retained late marker is transport-owner
classification only; the ledger does not reinterpret raw bus timing.

A Motion result link is accepted only when:

1. the action was dispatched;
2. the reference resolves to an existing `action_result` WorldEvent;
3. the WorldEvent and supplied `ActionResult` have equal run, action, result,
   payload, and evidence identities; and
4. every MCU evidence reference in the result is already bound to the same
   action.

When a result claims confirmed device state and explicitly cites ACK evidence,
at least one cited ACK must be successful. An explicit set containing only
failed ACKs contradicts that claim and is rejected. A successful ACK remains
device acceptance evidence only; it is never promoted to physical success.

A terminal result may be replayed identically but cannot be replaced or moved
to another action.

## Fault scope

STOP and `stop_ack` use their explicit command-to-action binding.

Watchdog and link-loss telemetry have no `command_id`. They bind to an action
only when exactly one dispatched action without a terminal Motion result is
present in that run. A caller-supplied action hint cannot override ambiguity.
With zero or multiple candidates, the ledger records an explicit run-scoped
fault with `action_id = null`. It never guesses.

Healthy telemetry is not action-correlation evidence and is rejected before
append; health/telemetry storage belongs to its owning data path.

Run-scoped faults are available through `list_run_faults`; action-scoped faults
appear on the corresponding correlation record.

## Evidence separation

Execution evidence and verification evidence are deliberately separate:

- `execution_evidence_refs` comes only from the `ActionResult`;
- `verification_evidence_refs` comes only from the `VerificationResult`.

The ledger exposes both lists for traceability but never merges them and has no
`physical_success`, `caused_state`, or `task_complete` field. In particular,
an MCU ACK is not physical-success evidence and an ActionResult is not an
Observation.

A verification link uses `tool_call`, not the shared domain `verification`
event type. This keeps the private correlation envelope invisible to Backend
and evaluation consumers that interpret domain verification payloads.

## Ownership and review

This implementation changes only Integration-owned `libs/application` code,
its tests, and this design note. It consumes existing Agent Runtime, MCU,
Motion, World Model, and shared-contract APIs without modifying them.

Under the approved submit-then-review plan, the PR must request review from:

- Integration Owner, for ledger ownership and orchestration semantics;
- Agent Runtime Owner, for dispatch identity consumption;
- MCU or Hardware Owner, for command, retry, STOP, late, and fault semantics;
- Motion Owner, for `ActionResult` and Motion evidence references; and
- World Model Owner, for WorldEvent persistence and verification separation.

If a future producer needs a public ledger envelope or new on-wire/session
identifier, that work must stop here and open the corresponding shared
interface task.
