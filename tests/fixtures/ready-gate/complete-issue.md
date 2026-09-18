## Owner

Owner: Quchaosheng. Reviewer: Quchaosheng, on the protected-branch review.

## Problem and objective

The repository has no committed Definition of Ready, so an issue could be assigned before its interfaces and evidence were agreed. This issue adds one and the deterministic check that reproduces it.

## Allowed paths

`docs/project-management/definition-of-ready.md`, `tools/scripts/check_ready_gate.py`, `tools/qa/governance/ready-gate-v1.json` and the tests beside them.

## Interfaces affected

None. No public interface, contract model, firmware boundary or workflow file changes.

## Acceptance criteria

- One committed Definition of Ready states each required field, the check that detects it and why it is required.
- `python3 tools/scripts/check_ready_gate.py --kind issue --body-file <issue>.md` exits 0 for this body and 1 for a body missing a field.

## Commands and evidence

- `python3 tools/scripts/check_ready_gate.py --kind issue --body-file tests/fixtures/ready-gate/complete-issue.md` -- PASS.
- `python3 -m pytest tests/unit/test_ready_gate.py -v` -- PASS.

## Dependencies and stop conditions

No blocking issue. Stop if meeting the acceptance requires editing a workflow file, a production module or a contract model.

## Definition of Ready

- Owner and reviewer are named.
- Dependencies and blocking issues are identified.
- Public interfaces and protected boundaries are listed.
- Test commands and expected evidence artifacts are specified.
- Rollback or disable path is documented.

## Exit Gate

- The Task Packet matches the final diff.
- Required unit, integration and replay tests pass.
- At least one failure path is exercised.
- No shared-runtime or safety boundary is bypassed.
- CI output and evidence links are attached.
