# ADR-0009: Run resources, concurrency and scenario lifecycle boundaries

Status: **proposed** (Issue #312; requires Runtime, Motion and Safety owner
review before any orchestration code lands — see
[Approval register](#approval-register)). Until every owner records `approved`,
this record grants no scope, concurrency, lease, safety or release authority.

Date: 2026-09-20

Supersedes nothing. It extends
[ADR-0006](ADR-0006-scenario-registry-contract.md) and
[ADR-0008](ADR-0008-learning-data-plane.md) with the resource and lifecycle
rules that decide *whether two scenarios may run at once* and *what happens to
a claimed resource when a run fails or the process restarts*. ADR-0006 decides
how a scenario reaches the runtime; ADR-0008 decides how high-rate episodes
reach it. Neither decides who owns the robot, cameras, workspace or MCU while a
run is active.

## Context

The registry (ADR-0006) lets several scenarios be registered and validated, and
the readiness report already publishes more than one validated scenario. That
made a previously theoretical problem real: two registered scenarios can name
the same robot, the same camera set, the same workspace cell, the same MCU
channel or the same event store, and the runtime has no stated rule for what
happens when they are both active.

The failure this record exists to prevent is not a crash. It is *conflicting
work that looks successful*:

- two scenarios driving one arm, so the second command sequence is interpreted
  against a world state the first one already moved;
- a camera claimed twice, so an observation is attributed to the wrong run;
- a run whose process died still holding a claim, so the next run silently
  inherits a resource nobody released;
- a retry loop that is bounded in the scenario but unbounded in the adapter, so
  a timeout becomes an invisible flood of commands toward the MCU.

Every one of those produces plausible events and a wrong completion claim. The
repository's existing evidence rules cannot repair it after the fact, because
the events themselves are ambiguous about which run owned the resource.

This record is the decision for Issue #312. It deliberately implements nothing.
Its output is the state machine, the resource table and the timeout vocabulary
that a later implementation slice must satisfy.

## Decision

### 1. The first release permits one active run per robot

The first release allows **one active run per robot**, not bounded concurrency
across a shared robot. Concurrency is admitted only between runs whose resource
claims are **disjoint** — different robot, or a workspace and camera set that do
not intersect. A second run that claims any resource already held by an active
run fails **before** its first action is dispatched.

Bounded concurrency is a later, separately approved decision. It requires a
measured arbitration policy; it is not granted by this record.

### 2. Resource claims are declared, ordered and finite

A scenario declares its resource requirements; it never acquires resources
itself. Every run holds a **claim** covering at least:

| Resource class | Granularity | Claimed by |
|---|---|---|
| Robot / base | one robot identity | run |
| Arm / manipulator | one arm identity | run |
| Camera | camera identity and stream | run |
| Workspace / cell | workspace identity and region | run |
| MCU / bus channel | channel identity | run |
| Event store / run identity | run id | run |
| Runtime worker slot | bounded slot count | orchestrator |

Rules that apply to every claim:

- **Acquisition order is fixed and total**, so two runs cannot deadlock by
  claiming the same two resources in opposite order. A claim that would require
  out-of-order acquisition is refused rather than reordered silently.
- **Claims are leases with a finite deadline.** No claim is indefinite, and no
  claim is renewed implicitly by activity alone.
- **Failure releases.** A run that fails, is cancelled, times out or is
  preempted releases its claims as part of the terminal transition, not as a
  later cleanup that may never happen.
- **A restart does not resurrect a claim.** Recovery after process restart
  treats a claim whose owning run is no longer live as reclaimable, and records
  the recovery as a lifecycle event rather than assuming the resource is free.
- **No implicit takeover.** A second run may not take a resource by assuming the
  first run is dead. The claim must be observably expired or explicitly released.

### 3. Timeouts are four distinct budgets, not one number

The runtime must keep four separate, finite budgets:

| Budget | Owns | Fails as |
|---|---|---|
| Scenario timeout | the whole run | scenario-level timeout |
| Semantic action timeout | one `SemanticAction` | action timeout |
| Adapter timeout | one device round-trip | adapter timeout |
| Recovery budget | retries and re-observation | recovery exhausted |

Conflating them is what lets a bounded scenario hide an unbounded adapter. A
per-adapter retry that is not charged against the recovery budget is a defect,
not an optimization. The recovery budget is finite and, when exhausted, the run
ends in an explicit failure state rather than continuing.

### 4. Cancellation, preemption and safe-stop are runtime-owned

Scenario code declares a resource requirement and a semantic action. It does
not implement cancellation, preemption or safe-stop.

- **Cancellation** ends a run and releases its claims; it is not the same as
  safe-stop, because a cancelled run may leave the arm where it is.
- **Preemption** is a runtime decision, permitted only where the displaced run
  reaches a defined safe state first.
- **Safe-stop** is *requested* by orchestration as `safe_stop` and *implemented*
  by Motion/MCU. Orchestration may request it and must record the request and
  its result; orchestration must never implement stop, hold torque or command
  the bus itself.
- **Resume** is permitted only for a run whose world state can be re-observed
  after the interruption. A run that cannot re-observe deterministically ends
  instead of resuming from an assumed state.

### 5. Lifecycle transitions are recorded for deterministic replay

Every lifecycle transition is written to the shared event store under the run
identity, so replay can reconstruct which run held which resource at any point:
claim requested, claim granted, claim refused, run started, action dispatched,
action completed, cancellation requested, safe-stop requested, safe-stop
observed, claim released, run terminated. A transition that is not recorded is
not a transition the replay can justify. The resource and lifecycle events stay
bounded metadata (identity, time range, reason, evidence reference), consistent
with the event-store boundary in ADR-0008.

### 6. Motion, MCU and safety authority are unchanged

Scenario code declares resource requirements and semantic actions only. Motion
and the MCU retain controller and emergency-stop authority. Orchestration can
request `safe_stop` but cannot implement it. No unbounded queue, no unbounded
retry loop and no implicit resource takeover is allowed at any layer. This
record adds a resource-ownership rule; it removes no Motion/Safety authority and
grants orchestration none.

## Non-goals

- Implementing the scheduler, lease store or state machine.
- Admitting bounded concurrency across a shared robot in the first release.
- Defining controller, torque, velocity or emergency-stop behavior.
- Changing the event schema, `ActionResult`, `VerificationResult` or any shared
  `interfaces/` or `libs/contracts/` model.
- Claiming multi-scenario concurrency is validated; only single-run behavior is
  currently validated.

## Suggested initial paths (not authorized by this record)

```
services/agent_runtime/
libs/kernel/
services/world_model/
tests/integration/
docs/architecture/
```

Shared `interfaces/`, `libs/contracts/`, `robot/control/`, `firmware/` and
physical deployment paths are **not** part of the first implementation slice.
Any later change to those paths requires the repository's existing approvals and
validation rules.

## Approval register

Per the repository rule that only a named human owner accepts scope, risk,
release, concurrency or physical-safety decisions, this record is `proposed`
until each owner below records `approved` or an **explicit blocking objection**.
A missing row is not approval.

| Role | Owner | Status | Blocking objection |
|---|---|---|---|
| Runtime | Runtime Owner | `REQUIRED` | — |
| Motion | Motion Owner | `REQUIRED` | — |
| Safety | Safety Owner | `REQUIRED` | — |

## Revisit trigger

Revisit this record when: an owner raises a blocking objection; bounded
concurrency across a shared robot is proposed; a lease or timeout value is
proposed as a product-level guarantee; a crash/restart test reveals an orphaned
claim; or a scenario is registered that cannot declare its resources.

## What this record is not

It is a boundary decision. It is not evidence that concurrency works, not a
performance claim, not a safety case, and not authorization to implement the
scheduler, lease store or state machine.
