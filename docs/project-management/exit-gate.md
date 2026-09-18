# Exit Gate

The Exit Gate is the set of conditions a pull request must satisfy before it is
reviewed. It is the pull-request half of the same checklist that
[Definition of Ready](definition-of-ready.md) applies to issues: one file,
`tools/qa/governance/ready-gate-v1.json`, defines both, and the detector reads
that file so the two halves cannot drift apart.

## The sections

A pull request body must carry every section below. The `Detected as` column
names the evidence the detector looks for; a reviewer still judges the quality of
what is written.

| Section | Detected as | Why it is required |
|---|---|---|
| Related Issue | issue or commit reference | A change without an issue has no agreed acceptance criteria to check. |
| What changed | non-empty | A reviewer must be able to read the intent without opening the diff first. |
| Interfaces affected | non-empty, and names at least one path | `AGENTS.md` routes interface changes to three human approvals, so an empty answer must be explicit rather than implied. |
| Tests and commands | a command and an evidence reference | Every deterministic behaviour change needs a command and its recorded result. |
| Failure path exercised | a failure, reject, rollback or disable reference, plus evidence | A change that only shows the happy path has not shown that the new rejection fires. |
| Evidence | an evidence reference | `AGENTS.md` forbids claiming completion without a command, a result and an evidence reference. |
| Risks and rollback | non-empty | A reviewer needs the disable path before the change lands, not after it breaks. |

## The checklist

Every box must be ticked, and a ticked box is not evidence by itself: the entry
decides what the tick needs to be supported by.

- **I changed only the paths the Task Packet allows.** The Task Packet is the
  write boundary for the change.
- **The Task Packet matches the final diff.** A packet that describes a different
  change than the diff is not a contract.
- **Tests and contract checks pass on this commit.** Requires a command and its
  recorded result. Passing checks on an earlier commit do not describe this one.
- **I covered normal and failure behaviour.** Requires a failure path.
- **I updated examples and docs when an interface changed.** `CONTRIBUTING.md`
  requires the schema, the model, the example and the docs to move together.
- **I did not add secrets, private data or unreviewed assets.** `SECURITY.md`
  forbids committing credentials and licence-encumbered assets.

## Checking a body

```bash
python3 tools/scripts/check_ready_gate.py --body-file path/to/pull-request.md
python3 tools/scripts/check_ready_gate.py --check-template
```

Exit codes are `0 READY`, `1 BLOCKED` and `2 INCOMPLETE`, the same as the issue
half. `--check-template` verifies that the committed issue form and pull request
template still contain every heading and checklist item the gate requires, and
that each template names the detector and the gate definition so a reviewer can
reproduce the verdict from the form itself.

## The closing conditions

A pull request may be merged when all of the following hold. The gate detects
the first four; the remaining ones are human decisions that no script makes.

1. The body reports `READY`.
2. The Task Packet matches the final diff.
3. The required unit, integration and replay tests pass on the commit being
   merged.
4. At least one failure path is exercised by a test that fails when the
   behaviour is removed.
5. No shared-runtime or safety boundary is bypassed, and no required human
   approval is skipped.
6. CI output and evidence links are attached to the pull request.

## What this checklist does not grant

A `READY` verdict on a pull request body does **not**:

- merge the pull request, automatically or otherwise;
- approve a release (that belongs to
  `tools/scripts/release_eligibility.py`);
- certify a physical validation (only attested hardware evidence may claim
  that);
- waive a required check or a required human approval.

The gate definition states this in its `authority` block, and the detector
refuses a body that claims any of these powers. A checklist that could grant the
authority it checks would be able to approve itself.
