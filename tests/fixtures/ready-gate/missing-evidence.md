## Related Issue

Closes #310. The Task Packet is `docs/task_packets/issue-310-definition-of-ready.json` and it describes commit `a1b2c3d`.

## What changed

Adds the readiness detector, the gate definition that both templates and the detector read, and the unit tests that pin them together.

## Interfaces affected

None. No public interface, contract model, firmware boundary or workflow file changed. The new files are `tools/scripts/check_ready_gate.py` and `tools/qa/governance/ready-gate-v1.json`.

## Tests and commands

I ran the tests locally and they were fine.

## Failure path exercised

The rejected path is exercised by `tests/fixtures/ready-gate/unticked-checkbox.md`: the detector reports BLOCKED and exits 1 instead of READY.

## Evidence

## Risks and rollback

Risk: a template edit drifts from the gate definition. Rollback: revert this commit. The detector is additive and no runtime path depends on it.

## Checklist

- [x] I changed only the paths the Task Packet allows.
- [x] The Task Packet matches the final diff.
- [x] Tests and contract checks pass on this commit.
- [x] I covered normal and failure behaviour.
- [x] I updated examples and docs when an interface changed.
- [x] I did not add secrets, private data or unreviewed assets.
