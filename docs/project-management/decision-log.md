# Decision log

Architecture decisions remain in `docs/decisions/`. This index adds delivery and gate decisions without duplicating their rationale.

| ID | Date | Decision | Status | Evidence / record | Review trigger |
|---|---|---|---|---|---|
| ADR-0001 | existing | keep P0 scope evidence-first and bounded | accepted | `docs/decisions/ADR-0001-p0-scope.md` | deterministic path cannot meet gate |
| ADR-0002 | existing | keep license decision explicit | pending review | `docs/decisions/ADR-0002-license-pending.md` | code/model/asset release composition changes |
| ADR-0003 | existing | use RISC-V QEMU for MCU baseline | accepted | `docs/decisions/ADR-0003-mcu-riscv-qemu.md` | selected physical MCU changes |
| ADR-0004 | existing | use UR5e plus Robotiq baseline | accepted | `docs/decisions/ADR-0004-arm-selection.md` | vendor package or reachability invalidates choice |
| ADR-0006 | 2026-09-19 | freeze the multi-scenario registry contract before implementation | proposed | `docs/decisions/ADR-0006-scenario-registry-contract.md`, issue #308 | a registry field is added or identity drift is observed |
| ADR-0007 | 2026-09-19 | freeze the household MVP as one mobile base, one arm, one RGB-D sensor, one compute and one safety MCU; keep the tabletop simulator and the UR5e bench as separate stages | proposed; owner approval register incomplete | `docs/decisions/ADR-0007-household-mvp-baseline.md`, issue #166 | a Product, Motion, Integration, Safety or Hardware owner approves, or measured mobile payload/stability evidence or a weighed serialized unit changes a target |
| D-2026-001 | 2026-08-11 | retain `contents: read` and disable implicit SBOM release-asset upload | implemented in merged PR #21; release verification pending | issue #20, failed run `31406815969`, merge `889f699` | next human-owned tag run |

## New decision record

```text
ID / date / owner:
Decision needed:
Options considered:
Decision and rationale:
Affected milestones, risks, contracts, and owners:
Evidence reviewed:
Revisit trigger/date:
Human approver:
```

AI may draft this record. Only the named human approver can accept scope, risk, release, licensing, procurement, or physical-safety decisions.
