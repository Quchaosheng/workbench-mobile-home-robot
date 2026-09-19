# ADR-0007: Household MVP is a single-arm mobile baseline

Status: **proposed** (Issue #166; requires Product, Motion, Integration, Safety
and Hardware owner approval — see [Approval register](#approval-register)).
Until every owner records `approved`, this record grants no scope, procurement,
safety or release authority.

Date: 2026-09-19

Supersedes nothing. It re-scopes which of the repository's three existing
systems the household product baseline is built from. ADR-0001 and ADR-0004 stay
accepted and are not rewritten; this record links to them and names the stage
each one applies to.

## Context

The repository currently describes three valid but different systems, and they
are not interchangeable:

| # | System | Where it is authoritative |
|---|---|---|
| A | Fixed-tabletop single-arm P0 simulator | `docs/decisions/ADR-0001-p0-scope.md` |
| B | Fixed development bench: UR5e + Robotiq 2F-85 on a table | `docs/decisions/ADR-0004-arm-selection.md`, `robot/control/` |
| C | Mobile household product concept | `hardware/mechanical/`, Revision D |

Carrying all three into one household robot plan duplicates mechanical, power,
control, safety, simulation and procurement work, and it makes the resource
budgets requested by #79 meaningless: a budget cannot be stated against a
system whose mass, arm count and sensor count are not fixed.

The specific conflation is visible in committed numbers:

- the `hardware/mechanical` operations package describes a **55 kg** dual-arm
  U-cell planning ledger, retained as `mass-ledger-legacy.csv` with every row
  marked `SUPERSEDED` and `EXCLUDED`;
- the same package once described a separate **6.42 kg** compact enclosure model
  (`hardware/mechanical/README.md` at commit `9ca5a33`), which was a different,
  smaller machine with a 280 x 240 x 330 mm envelope and a two-wheel/caster
  drive;
- the current Revision D analytical model is **77.5 kg** (`REV-D-MASS-001`,
  `hardware/mechanical/generated/analysis.json`), and it is an `ESTIMATE` with
  `CONCEPT_PHYSICAL_VALIDATION_REQUIRED`, not a measurement.

55 kg, 6.42 kg and 77.5 kg are three different machines, not three estimates of
one. Blending them produces a robot mass that no analysis in this repository
supports.

## Decision

### 1. Three stages, three identifiers

| Stage | Identifier | What it is | Not what it is |
|---|---|---|---|
| A | `STAGE-P0-TABLETOP` | Fixed-tabletop single-arm simulator on Ubuntu 24.04 + ROS 2 Jazzy + Gazebo Harmonic (ADR-0001) | Not the product. No mobile base, no dual arm, no physical hardware. |
| B | `STAGE-BENCH-UR5E` | Fixed development bench: UR5e + Robotiq 2F-85 at a fixed base on a 1.20 x 0.80 m table (`robot/description/FRAMES.md`) | Bench hardware. It is not a validated mobile arm; ADR-0004's reachability evidence was measured with a fixed base. |
| C | `STAGE-HOUSEHOLD-MVP` | First household product: one indoor mobile base, one arm/gripper, one primary RGB-D camera, one onboard compute, one safety MCU | Not dual-arm. Not the U-cell. Not yet a measured machine. |

Stage A stays the deterministic teaching and regression fixture. Stage B stays
the development and motion-validation bench. Stage C is the product baseline
this record fixes. The fixed desk is preserved as a development fixture; it is
not deleted or demoted.

### 2. Household MVP scope

In scope, and only this:

- [#159](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/159)
  load one standard bag into a pre-open washer;
- [#163](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/163)
  load configured non-breakable dishes into a pre-open rack;
- [#164](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/164)
  retrieve one manifest-matched parcel from an indoor handoff shelf;
- [#152](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/152)
  bounded fixed-map Nav2 navigation.

Out of MVP scope: dual-arm coordination, guarded appliance door and rack
manipulation, elevators, stairs, public lockers, outdoor travel, bimanual
shared-workspace payloads, and arbitrary deformable or fragile objects.

**The one-arm baseline is explicit.** The MVP carries one arm and one gripper.
Dual-arm coordination is out of scope until measured task evidence proves one
arm insufficient — and "measured" means an eligible evidence class per
`docs/context/EVIDENCE_INDEX.md`, not an estimate or a design intent.

### 3. Target table

Every target names its value **or its explicit gap**, a source path, an owner
and a status. `status` uses the repository vocabulary: `ESTIMATE` (analytical,
unmeasured), `GAP` (no committed source states a value), `NOT_EXECUTED` (test
not run), `REQUIRED` (owner approval outstanding).

| Target | Value or gap | Source | Owner | Status |
|---|---|---|---|---|
| Payload | 2 kg at 650 mm continuous, 3 kg at 400 mm reduced speed | `hardware/mechanical/design-spec.json#manipulator` | Mechanical | ESTIMATE |
| Payload (parcel bay) | 5 kg | `hardware/mechanical/design-spec.json#torso.parcel_bay_payload_kg` | Mechanical | ESTIMATE |
| Reach | 720 mm arm reach | `hardware/mechanical/design-spec.json#manipulator.reach_mm` | Mechanical | ESTIMATE |
| Doorway width | **GAP** — no committed source states a required clear opening. The Revision D driving footprint (540 x 520 mm) is asserted to fit an indoor doorway in `hardware/mechanical/revision-c-architecture.md`, but no width is committed and no doorway is measured. | `hardware/mechanical/revision-c-architecture.md` | Product + Mechanical | GAP, NOT_EXECUTED |
| Base footprint | 540 x 520 mm envelope; four 140 mm steer-drive modules; 820 x 820 mm stabilized support polygon deployed only when stationary | `hardware/mechanical/design-spec.json#enclosure`, `#chassis` | Mechanical | ESTIMATE |
| Tip / stability margin | Worst-case analytical tip angle 27.5 deg (stowed navigation, `-Y`) and 37.1 deg (stabilized); target minimum dynamic stability factor 2.0 | `hardware/mechanical/generated/analysis.json#stability`, `design-spec.json#stability_gates` | Mechanical + Safety | ESTIMATE; analytical screen only — `drive_tip_angle_is_not_manipulation_release: true`; pull, slope, E-stop, brake-hold and stabilizer tests NOT_EXECUTED |
| Total mass | 77.5 kg Revision D analytical model (`REV-D-MASS-001`) | `hardware/mechanical/mass-ledger.csv` | Mechanical + Hardware + Product + Safety | ESTIMATE; `owner_approval_status: REQUIRED`; no serialized unit has been weighed |
| Battery / runtime | **GAP** — a 48 V pack is a planning allocation only. Pack, BMS, charger, contactors, precharge, fuse and PDU are unspecified; no runtime figure is committed. | `docs/hardware/hardware-selection-closure.md` | Electrical / Hardware | GAP, ORDER_RELEASE_BLOCKED |
| Compute | Jetson Orin Nano Super Developer Kit 8 GB + 512 GB NVMe (candidate) | `docs/hardware/hardware-selection-closure.md` | BSP | ESTIMATE; carrier, SKU lifecycle, power and cooling unapproved; sustained thermal/power capture NOT_EXECUTED |
| Sensor | One head-mounted Intel RealSense D435 over USB 3 (candidate) | `bsp/sensors/camera-head.yaml`, `hardware/cameras/README.md` | Perception | ESTIMATE; `physical_evidence_ready: false`; intrinsics/extrinsics NOT_ATTACHED, timestamp and mount validation NOT_EXECUTED |
| Safe speed | 1.2 m/s maximum navigation, 0.15 m/s precision docking, 0.25 m/s drive limit while the lift is raised | `hardware/mechanical/design-spec.json#chassis`, `#lifting_platform` | Motion + Safety | ESTIMATE; analytical parameter only, no guarded physical speed test |

Two rows are deliberate gaps. Recording a gap is the correct outcome: inventing
a doorway width or a battery runtime would produce a baseline that no source in
this repository supports.

### 4. UR5e is bench hardware

ADR-0004 selected **UR5e** for phase-1 Motion, and that decision stands. It is a
decision about the **bench** (Stage B), made against a fixed `base_joint` and a
1.20 x 0.80 m table, with reachability measured on a fixed base
(`docs/evaluation/phase1-reachability.json`).

For the household product (Stage C) the UR5e is **bench hardware and nothing
more**:

- no measured mobile payload evidence approves it — a mobile base adds tip,
  slip, suspension and floor-friction terms that a fixed base does not have;
- no measured mobile stability evidence approves it — ADR-0004's own trade-off
  note records that UR meshes carry a proprietary licence, and the reachability
  gate was a *fixed-base* kinematic gate;
- this record claims **no implicit arm swap**. It does not say "use UR5e on the
  mobile base", and it does not silently promote the bench arm to the product.

Promoting any bench arm to Stage C requires its own measured mobile payload and
stability evidence, in an eligible class, approved by Motion + Safety. Until
then the Stage C arm is unresolved, and `hardware/mechanical/design-spec.json`
describes a *supplier-class* seven-axis arm, not a selected part
(`hardware/release/**` remains `ORDER_RELEASE_BLOCKED`).

### 5. The 55 kg, 6.42 kg and 77.5 kg numbers

They are explained, never blended:

- **55 kg** — the superseded dual-arm U-cell planning case. Preserved in
  `hardware/mechanical/mass-ledger-legacy.csv`; every row is `SUPERSEDED` and
  `EXCLUDED` from release calculation. It is a historical operations-readiness
  contract and must not be used for structural sizing, harness or procurement.
- **6.42 kg** — the superseded compact enclosure model (280 x 240 x 330 mm,
  two-wheel plus caster drive), recorded in `hardware/mechanical/README.md` at
  commit `9ca5a33` and removed by `0a20b70`. It was a *different, smaller
  machine*, explicitly documented as "intentionally separate from the 55 kg
  dual-arm workbench design case". It is not a component of the household robot
  and not a Revision D figure.
- **77.5 kg** — the only current total, `REV-D-MASS-001`, an analytical
  estimate over nine `REV-D-ME-*` components, pending a serialized weighing.

No number in this ADR is a measured product mass.

### 6. Resource-budget hooks

Resource budgets are owned by
[#79](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/79).
This record only fixes what those budgets must be measured against — Stage C as
defined above, with:

- ROS 2, Nav2, MoveIt and perception measurements on the target-class compute;
- the local Ollama route **disabled by default**
  (`docs/context/MODEL_POLICY.yaml#routes.local`), because a model-runtime
  process competing for the same CPU and memory would make a Stage C budget
  unattributable. Enabling it for a measurement requires a named owner decision
  and a separate budget line;
- the existing scripted-only policy
  (`docs/performance/software-budget-policy-scripted-v1.json`) kept as the
  software regression input, never presented as a target-hardware result.

Current status: the only committed measurement is
`docs/performance/monitoring-local-2026-08-19.md`, taken on an Intel
i7-14650HX development host. Target-class Jetson CPU/RSS/wakeup measurements and
calibrated E-stop, BMS, CAN, Nav2, Motion and perception source evidence are
**NOT_EXECUTED**. A development-host number must not be reported as a Stage C
budget.

### 7. Phases

| Phase | Content |
|---|---|
| MVP (this record) | #159, #163, #164, #152, single arm, single RGB-D camera |
| Phase 2 | Guarded appliance door and dishwasher-rack interaction ([#160](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/160)) |
| Later | Elevators, stairs, public lockers, outdoor travel, bimanual shared-workspace coordination, arbitrary deformable or fragile objects |

Phase 2 and later phases are named so that MVP work is not expanded by
accident. Naming a phase does not schedule it and does not approve its
hardware.

## Superseded scope statements

Documents that previously conflicted with this baseline link here rather than
being rewritten:

| Statement | Where it was | Disposition |
|---|---|---|
| `docs/context/CONSTRAINTS.yaml#project.p0_scope: fixed-tabletop-single-arm-simulator` | Context constraints | Still true for Stage A; now explicitly scoped to `STAGE-P0-TABLETOP` and does not describe the household product |
| P2/P3 milestones that imply a dual-arm mobile product | `docs/project-management/plan.md` | Linked to this record; the milestone table keeps its historical baseline dates and its `NOT_READY` states |
| "Do not promise ... general-purpose autonomous household operation" | `docs/product/product-brief.md` | Kept verbatim in force; linked to this record as the still-current product boundary |

ADR-0001 and ADR-0004 are **not** rewritten. Their content stays as accepted;
this record only states which stage each one governs.

## Consequence

- Stage A and Stage B keep working unchanged; no CAD, firmware, PCB, schema or
  procured part changes because of this record.
- Stage C work must now be stated against one base, one arm, one camera, one
  computer and one MCU. A task that requires a second arm needs new measured
  evidence and a new ADR.
- The 55 kg U-cell and 6.42 kg enclosure figures cannot be quoted as the
  household MVP mass. Reviewers can reject a document that blends them.
- The baseline is **proposed**, not accepted. The household MVP remains blocked
  for scope and procurement decisions until the approval register is complete.

## Approval register

Per the Issue acceptance and the repository rule that only a named human owner
accepts scope, risk, release, licensing, procurement or physical-safety
decisions, this record is `proposed` until each owner below records `approved`
or an **explicit blocking objection**. A missing row is not approval.

| Role | Owner | Status | Blocking objection |
|---|---|---|---|
| Product | Product Owner | `REQUIRED` | — |
| Motion | Motion Owner | `REQUIRED` | — |
| Integration | Integration Owner | `REQUIRED` | — |
| Safety | Safety Owner | `REQUIRED` | — |
| Hardware | Hardware Owner | `REQUIRED` | — |

This register mirrors the `owner_approvals` block in
`hardware/mechanical/design-spec.json#mass_model`, which independently requires
Product, Mechanical, Hardware and Safety approval before a release decision.

## Revisit trigger

Revisit this record when: an owner raises a blocking objection; measured mobile
payload or stability evidence for a candidate arm exists; a serialized unit is
weighed; #79 produces target-class measurements; or #152 establishes a measured
navigation envelope that changes the doorway or safe-speed targets.

## What this record is not

It is a decision record and a requirements baseline. It is not evidence that a
physical household robot exists, not a procurement approval, not an arm
selection, not a safety case, and not a measured mass, runtime or performance
result.
