# Project plan

Baseline date: 2026-08-11. The schedule is a planning baseline, not proof that a task has started or completed.

The P2/P3 rows below predate the re-scoped product baseline in
[ADR-0007](../decisions/ADR-0007-household-mvp-baseline.md) (Issue #166), which
fixes the household MVP as a single-arm mobile system and puts dual-arm
coordination out of scope. The dates, gates and `NOT_READY` states are unchanged.
This table remains the historical planning baseline, and it is not evidence that
a dual-arm mobile product is in scope.

```mermaid
gantt
    title Workbench - 12 week delivery baseline
    dateFormat  YYYY-MM-DD
    axisFormat  %m-%d

    section P1 verification foundation
    Repair release evidence path          :crit, p1_release, 2026-08-11, 7d
    Establish security baseline           :p1_security, 2026-08-18, 7d
    Integrate Gazebo deterministic path    :crit, p1_sim, 2026-08-11, 21d
    Run formal 36-run P1 evaluation       :crit, p1_eval, after p1_sim, 7d
    Run external cold-start study         :p1_external, 2026-08-25, 14d
    P1 gate                               :milestone, p1_gate, 2026-09-07, 0d

    section P2 scale and observe
    Expand task and scenario coverage     :p2_tasks, 2026-09-08, 14d
    Add deployment observability          :p2_ops, 2026-09-08, 21d
    Run 90-run comparative evaluation     :crit, p2_eval, 2026-09-22, 14d
    Freeze hardware integration inputs    :crit, p2_hardware, 2026-09-08, 28d
    P2 gate                               :milestone, p2_gate, 2026-10-05, 0d

    section P3 hardware evidence
    Integrate physical adapters           :crit, p3_integrate, 2026-10-06, 14d
    Tune and validate real arm behavior   :crit, p3_tune, after p3_integrate, 14d
    Run formal hardware evaluation        :crit, p3_eval, 2026-10-20, 14d
    P3 release decision                   :milestone, p3_gate, 2026-11-02, 0d
```

## Milestones and gates

| Gate | Planned date | Entry dependency | Exit criteria | Current state |
|---|---|---|---|---|
| P1 | 2026-09-07 | integrated deterministic Gazebo path | false completion 0, collision 0, grasp >=90%, VTCR >=80%, 36 formal runs, external reproduction >=2/3 | NOT_READY |
| P2 | 2026-10-05 | P1 gate plus expanded task/scenario set | >=3 task types, 30 scenarios, 90 formal runs, P95 <60s, hardware inputs frozen | NOT_READY |
| P3 | 2026-11-02 | physical adapters and independent emergency stop | real false completion 0, collision 0, grasp >=70%, >=30 real runs, contract changes 0 | NOT_READY |

## Critical dependencies

| Dependency | Consumer | Control |
|---|---|---|
| Simulation world and arm composition | formal P1 evaluation | require a reproducible launch command and raw Gazebo logs before scheduling the gate review |
| Frozen schemas and matching Pydantic models | all producers and consumers | three-owner approval plus `make contract` in the same PR |
| Dated quotes, approved AVL, and incoming inspection | hardware order and P3 | keep order release blocked until external records exist |
| External participant records | reproducibility claim | retain all failures and validate unique participants |
| Human-owned release/tag decision | publication | AI and CI prepare evidence but never create the decision |

Schedule variance is measured against this baseline at every weekly review. Missing evidence changes the gate state, not the historical baseline.
