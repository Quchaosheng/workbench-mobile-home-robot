# ADR-0008: Learning-data plane and guarded learned-policy boundary

Status: **proposed** (Epic #345 Milestone 0; requires Runtime, Motion, Safety,
Evaluation and Legal owner review before any learning-plane code lands — see
[Approval register](#approval-register)). Until every owner records `approved`,
this record grants no scope, dependency, license, safety or release authority.

Date: 2026-09-20

Supersedes nothing. It extends the boundary in
[ADR-0006](ADR-0006-scenario-registry-contract.md) with a second input plane:
ADR-0006 decides how a *scenario* reaches the runtime; this record decides how
*high-rate episodes and learned policies* reach it without gaining control,
verification or completion authority.

## Context

Workbench already has an evidence-first control plane: bounded `SemanticAction`
tools, fail-closed policy validation, an append-only event store, deterministic
replay, and a verifier that distinguishes `confirmed`, `refuted` and
`insufficient_evidence`. The Epic (#345) adds a learning plane for high-rate
robot episodes and a controlled way to evaluate learned policies, using
[Hugging Face LeRobot](https://github.com/huggingface/lerobot) and
[StarVLA](https://github.com/starVLA/starVLA) as reference designs.

Both upstreams are useful and both are easy to misuse:

- They load datasets, weights and checkpoints whose terms are **not** the code
  license, and a vendored copy silently becomes a redistribution decision.
- They ship inference servers and action heads that can emit joint, velocity or
  end-effector commands directly. If such an output reaches Motion or the MCU,
  the runtime's authority boundary is bypassed by data rather than by code —
  the same failure class ADR-0006 guards one layer up.
- They are high-rate and image-carrying. Writing that volume into the SQLite
  `WorldEvent` store would corrupt the store's retention and replay guarantees.

This record is Milestone 0 of #345. It deliberately implements nothing.
Its output is the boundary Milestones 1–4 must satisfy.

## Decision

### 1. A learned policy is a proposal source, never an authority

A worked policy may only produce a `PolicyProposal`. It is **not** the verifier,
not the safety authority, not the emergency-stop authority, and not a source of
physical completion truth. Concretely:

- `ActionResult.outcome=completed` and a successful model response are not
  sufficient for task success. Only a Workbench verifier may declare success.
- A policy response must not become raw joint, velocity, firmware, safe-enable,
  emergency-stop or completion authority through the Agent Runtime.
- Physical policy execution is out of scope until a separate owner-approved
  Motion/Safety task exists.

Milestone 3 (shadow mode) must be *mechanically* unable to dispatch control,
not merely configured not to.

### 2. Two planes, one evidence chain

High-rate images, robot states and actions belong to a **separate learning-data
plane** and must not be embedded in the SQLite `WorldEvent` store. World events
retain bounded metadata, hashes, time ranges, provenance and evidence
references that *point at* learning artifacts. The event store stays the
append-only index; the learning plane holds the bulk payload.

### 3. Proposed, commanded and executed are recorded separately

Training targets must never silently use an unexecuted proposal. Episode
records keep at least `action.proposed`, `action.commanded` and
`action.executed` distinct, together with the applicable `ActionResult` and
`VerificationResult` references. Default imitation-learning selection includes
only explicitly eligible executed/verified samples; failed episodes stay
available for analysis without becoming positive targets.

### 4. Metadata is versioned and checked fail-closed

Before inference, the provider must present a validated manifest covering:
model/checkpoint digest, training dataset digest, code revision, camera
contract (names, order, frame IDs), state/action key order and dimensions,
units, coordinate conventions, normalization revision/statistics, action
horizon, control rate, supported skill and resource requirements. Unknown or
mismatched metadata fails closed *before* inference. The same applies to an
episode export: schema/layout mismatch, missing calibration, non-finite values,
duplicate samples, timestamp regression or incomplete action provenance fails
closed.

### 5. Dependencies are optional and the demo never depends on them

The deterministic scripted runner, the existing verifier, replay and the
canonical offline demo must remain fully usable when LeRobot, StarVLA,
PyTorch, CUDA, Transformers or any other ML dependency is absent. Unit and
fixture tests run without GPU, Hub credentials or network access. Making any ML
stack a mandatory dependency of the core offline runtime is a non-goal.

### 6. Third-party terms are reviewed artifact-by-artifact

LeRobot code is Apache-2.0; StarVLA code is MIT. Those are **code** licenses.
Model weights, datasets and checkpoints are reviewed separately and are not
covered by them. Per the register rule in the repository-root `THIRD_PARTY_REVIEW.md`, neither
source, weights nor datasets are vendored, and the integration prefers an adapter or process
boundary over copied code.

## Non-goals

- Online learning or self-modifying policies.
- Direct VLA joint control from `services/agent_runtime/`.
- Replacing Workbench events, replay, policy validation or the verifier with
  LeRobot/StarVLA abstractions.
- Direct physical replay of dataset actions.
- Making PyTorch, CUDA, Transformers, LeRobot or StarVLA mandatory for the core
  offline runtime.
- Claiming real-robot capability from scripted or shadow-mode results.

## Suggested initial paths (not authorized by this record)

```
integrations/lerobot/
services/policy_runtime/
evaluation/policy/
tests/integration/
```

Shared `interfaces/`, `robot/control/`, `firmware/` and physical deployment
paths are **not** part of the first implementation slice. Any later change to
those paths requires the repository's existing approvals and validation rules.

## Approval register

Per the repository rule that only a named human owner accepts scope, risk,
release, licensing, procurement or physical-safety decisions, this record is
`proposed` until each owner below records `approved` or an **explicit blocking
objection**. A missing row is not approval.

| Role | Owner | Status | Blocking objection |
|---|---|---|---|
| Runtime | Runtime Owner | `REQUIRED` | — |
| Motion | Motion Owner | `REQUIRED` | — |
| Safety | Safety Owner | `REQUIRED` | — |
| Evaluation | Evaluation Owner | `REQUIRED` | — |
| Legal | Legal Owner | `REQUIRED` | — |

## Revisit trigger

Revisit this record when: an owner raises a blocking objection; Milestone 1
lands and the real artifact layout is known; an upstream code license changes;
a dataset, weight or checkpoint is proposed for adoption; or a bounded
learned-policy hardware pilot is proposed.

## What this record is not

It is a boundary decision. It is not evidence that a learned policy runs, not a
license grant for any dataset or checkpoint, not a safety case, not a
dependency approval, and not authorization to implement Milestones 1–4.
