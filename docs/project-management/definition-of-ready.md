# Definition of Ready

A task is **ready** when someone can start it without inventing an interface, a
boundary or an acceptance test. This page is the repository-wide Definition of
Ready for the multi-scenario epic and its child issues, and it is the reference
for the `Definition of Ready` section in the issue form.

Readiness is a statement about the *issue body*, not about the person who wrote
it. It grants no design approval, no scheduling and no release authority.

## The fields

The gate definition at `tools/qa/governance/ready-gate-v1.json` is the single
source of truth. Each entry below names the field, the check that detects it, and
why it is required.

| Field | Detected as | Why it is required |
|---|---|---|
| Owner | non-empty | A task without a named human owner and reviewer has no one accountable for the acceptance decision. |
| Problem and objective | non-empty | An issue that cannot state the problem it removes cannot be checked for usefulness. |
| Allowed paths | non-empty, and names at least one path | `AGENTS.md` bounds every AI write by a path list; an issue that omits it defers the boundary to the implementer. |
| Interfaces affected | non-empty | Interface changes route to three human approvals, so an empty answer must be explicit rather than implied. |
| Acceptance criteria | non-empty | Criteria must be stated before coding so "done" is not redefined afterwards. |
| Commands and evidence | a command and an evidence reference | `AGENTS.md` forbids claiming completion without a command, a result and an evidence artifact. |
| Definition of Ready | non-empty, and names a rollback or failure path | The readiness question list names the rollback or disable path, which a reviewer needs before work starts. |
| Exit Gate | non-empty, and names a rollback or failure path | The closing conditions must be listed before work starts so the pull request body can be checked against them. |
| Dependencies and stop conditions | non-empty | A blocked issue must name what unblocks it so it is not started on a guess. |

Answering a field with "none" is allowed when it is true. Leaving a field empty
is not: an empty answer is indistinguishable from a question nobody asked.

## The five questions

The issue form asks these under `Definition of Ready`. They are the questions a
reviewer would otherwise have to remember to ask:

1. Are the owner and the reviewer named?
2. Are the dependencies and blocking issues identified?
3. Are the public interfaces and protected boundaries listed?
4. Are the test commands and the expected evidence artifacts specified?
5. Is the rollback or disable path documented?

## Checking a body

```bash
python3 tools/scripts/check_ready_gate.py --kind issue --body-file path/to/issue.md
python3 tools/scripts/check_ready_gate.py --stdin --kind issue < path/to/issue.md
```

Exit codes are the contract:

| Code | Verdict | Meaning |
|---|---|---|
| 0 | `READY` | Every required field is present and none claims authority the gate withholds. |
| 1 | `BLOCKED` | The body was judged and at least one field is missing, empty or overclaimed. |
| 2 | `INCOMPLETE` | The body could not be judged: unreadable file, empty body, or a gate definition that does not validate. |

`INCOMPLETE` is deliberately separate from `BLOCKED`. A gate that reports
`BLOCKED` when it could not read its input trains reviewers to ignore it, and a
gate that reports `READY` when it could not read its input is worse.

The detector reads text only. It never executes a command it finds, never opens a
link, and never contacts GitHub, so it is safe to run against a body from an
untrusted pull request.

## Relationship to the Task Packet

The Definition of Ready decides whether an issue may be *started*; the
[Task Packet](../task_packets/README.md)
validator decides whether a *change* stayed inside its declared write boundary.
Both are needed, and neither replaces protected-branch review.

## Migration

Existing issues are not retroactively blocked. Apply the Definition of Ready to
new issues, and to an existing issue when it is re-scoped or re-assigned. When an
existing issue is edited, run the detector and record the verdict in the issue.

## What this page does not grant

A `READY` verdict does not approve a design, schedule work, merge a pull request,
authorise a release, or certify a physical validation. Those decisions belong to
the human owner and to protected-branch review.
