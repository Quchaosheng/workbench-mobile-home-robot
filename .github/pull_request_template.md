<!--
This body is the Exit Gate. Reproduce the verdict locally before you ask for a
review, and paste the command and its result into "Tests and commands":

    python3 tools/scripts/check_ready_gate.py --body-file <your-body>.md
    python3 tools/scripts/check_ready_gate.py --check-template

Exit codes: 0 READY, 1 BLOCKED, 2 INCOMPLETE. The checklist at
tools/qa/governance/ready-gate-v1.json is the source of truth; the definitions
behind it are docs/project-management/definition-of-ready.md and
docs/project-management/exit-gate.md. A READY verdict grants review readiness
only. It never grants merge, release or physical-validation authority, and it
never replaces a required human approval.
-->

## Related Issue

Closes #

## What changed

## Interfaces affected

Say "none" if no public interface, contract model or firmware boundary changed.

## Tests and commands

## Failure path exercised

## Evidence

## Risks and rollback

## Checklist

- [ ] I changed only the paths the Task Packet allows.
- [ ] The Task Packet matches the final diff.
- [ ] Tests and contract checks pass on this commit.
- [ ] I covered normal and failure behaviour.
- [ ] I updated examples and docs when an interface changed.
- [ ] I did not add secrets, private data or unreviewed assets.
