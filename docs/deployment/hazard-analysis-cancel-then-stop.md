# Worked hazard analysis: cancel during a physical action

Status: **demonstration of method on one function.** This is not a machine-level risk
assessment and does not discharge ISO 12100 for the product. See
[standard positioning](../security/functional-safety-boundary.md) for what is and is
not claimed here.

Every claim below names a file, symbol or test in this repository. A claim that could
not be pointed at code was left out rather than softened.

## 1. Scope

**Function under analysis:** a task is executing a physical action (`grasp`, `place`,
`navigate`, `open`, `close`) when a cancel or STOP arrives.

**Out of scope, deliberately:** hazard identification for the whole product, the
mobile base's drive system, the arm's torque and pinch limits, power loss, and every
mechanical hazard. Those need the machine's real geometry and environment, which this
repository does not describe.

| Concept | Where |
| --- | --- |
| Physical action vocabulary | `ActionType` in `libs/contracts/workbench_contracts/models.py` |
| Stop request entry point | `ExecutionController.request_stop` |
| Stop dispatch | `ExecutionController._dispatch_stop` |
| Terminal state after stop | `ExecutionState.STOPPED`, `ExecutionReasonCode.STOP_DISPATCHED` |
| Post-action gating | `PostActionLoop` in `services/agent_runtime/workbench_agent_runtime/post_action.py` |

## 2. Hazard identification

Applied as method, in words: a hazard is a potential source of harm, and the task is
to ask what could go wrong in this specific window.

| ID | Hazardous situation | Harm that could result | Mechanism |
| --- | --- | --- | --- |
| H1 | Cancel arrives while an arm action is already moving | crushing, pinch | the motion continues after the operator believes it has stopped |
| H2 | STOP is dispatched but the device never acknowledges | crushing, pinch | nobody knows whether the stop took effect |
| H3 | A queued step is dispatched after STOP | unexpected motion | the sequence resumes on its own |
| H4 | The same STOP is issued twice | none directly | duplicate stop traffic can mask a lost acknowledgment |
| H5 | Operator cannot tell stopped from still-moving | indirect | a false belief about state leads to reaching into the workspace |

H5 is the one most easily forgotten, and the one this runtime can actually address:
it is an *information* hazard, not a motion hazard.

## 3. Risk estimation — BLOCKED

The standard asks for severity, probability of occurrence and possibility of
avoidance. **No numbers are assigned here**, because assigning them requires the
machine's mass, speed, geometry, the user population and the exposure — none of which
this repository contains.

| ID | Severity | Exposure | Avoidance | Why no number |
| --- | --- | --- | --- | --- |
| H1 | potentially severe | every cancel during motion | limited once motion started | depends on arm mass, speed, pinch geometry |
| H2 | potentially severe | every stop | operator may not know | depends on indication and device behaviour |
| H3 | moderate | every multi-step task | good | detectable from evidence |
| H4 | negligible | frequent | n/a | inconvenience, not harm |
| H5 | moderate | every stop | poor without indication | depends on HMI design |

Recording this step as blocked *is* the honest output. A risk assessment that
produced a number here would be inventing one.

## 4. Risk reduction, in the standard's order

### 4.1 Inherently safe design — partly addressed

| Measure | In code? | Evidence |
| --- | --- | --- |
| STOP is examined at every dispatch boundary | yes | `ExecutionController.execute` re-checks the pending stop before each action |
| A queued step cannot be dispatched after STOP | yes | `test_stop_request_preempts_remaining_steps_and_is_idempotent` |
| A duplicate stop cannot re-arm the sequence | yes | same test |
| A mutated action id cannot redispatch | yes | `test_stop_action_id_mutated_after_revalidation_cannot_redispatch` |
| Post-action verification cannot advance after cancellation | yes | `test_cancellation_blocks_every_step` |

**Not addressed:** the physical stopping time of the arm. The runtime issues STOP; it
cannot make a moving joint decelerate faster than its drive allows.

### 4.2 Safeguarding — not addressed

No protective stop category, enabling device, presence sensing or
speed-and-separation monitoring exists or is claimed.

### 4.3 Information for use — partly addressed

| Measure | In code? | Evidence |
| --- | --- | --- |
| The terminal state is recorded, not inferred | yes | `report.terminal_state is ExecutionState.STOPPED` |
| The reason is distinguishable from normal completion | yes | `STOP_DISPATCHED` vs `ACTION_STOPPED` |
| Missing evidence says so instead of passing | yes | `insufficient_evidence`; `GateSequenceTests.test_insufficient_evidence_stops_when_the_budget_is_spent` |

## 5. Verification: what is actually proven

| Requirement | Verification | Level |
| --- | --- | --- |
| R1 no step dispatched after STOP | `test_stop_request_preempts_remaining_steps_and_is_idempotent` | unit, simulated adapter |
| R2 duplicate STOP is idempotent | same test | unit |
| R3 stop re-validated immediately before dispatch | `test_stop_action_id_mutated_after_revalidation_cannot_redispatch` | unit |
| R4 cancellation blocks dependent steps | `test_cancellation_blocks_every_step` | unit |
| R5 a lost ACK escalates to a stop | `test_retry_keeps_correlation_and_timeout_escalates_to_stop` | unit |

Every row is `SIMULATED`. None is physical evidence. Per ISO 13849-2, validation would
start from a fault list derived from this analysis; that derivation has not been done,
so the fault injection here is an *input* to such an activity, not the activity.

## 6. What would have to happen for this to be a safety case

1. Replace §3 with a real estimation using the machine's mass, speed and environment.
2. Decide which of H1–H5 the control system must control, and which are handled by
   guards, procedures or instructions.
3. For each control-system measure, define a safety function and assign a PLr.
4. Classify the achieved architecture, MTTFd and DC — needs hardware data.
5. Perform ISO 13849-2 validation against a fault list derived from step 2.

Steps 1–2 are analysis and could be done next without new hardware. Steps 3–5 cannot
be done by software alone.

## 7. Why keep this document

The most likely failure of a project like this is not a missing feature. It is a
reviewer — or the author, six months later — reading "fail-closed STOP, tested under
fault" and concluding "safe". This document makes the distance between those two
statements explicit and points at the code supporting the smaller claim.
