---
name: Feature or integration task
about: A bounded implementation task
---

<!--
This form is the Definition of Ready. Reproduce the verdict before the issue is
assigned:

    python3 tools/scripts/check_ready_gate.py --kind issue --body-file <issue>.md

Exit codes: 0 READY, 1 BLOCKED, 2 INCOMPLETE. The checklist at
tools/qa/governance/ready-gate-v1.json is the source of truth and
docs/project-management/definition-of-ready.md explains each field. Answering a
field with "none" is allowed when it is true; leaving it empty is a BLOCKED
verdict. A READY verdict does not schedule the work or approve a design.
-->

## Owner

## Problem and objective

## Allowed paths

## Interfaces affected

## Acceptance criteria

## Commands and evidence

## Dependencies and stop conditions

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
