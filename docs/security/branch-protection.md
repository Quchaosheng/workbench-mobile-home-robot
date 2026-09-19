# Branch protection and the release safety matrix

`main` is the only branch that may publish a release, so the checks that guard
`main` are the release gate. This page defines the required matrix, how a change
to it is reviewed, and what happens when GitHub cannot enforce a rule.

## The declared matrix

The matrix is committed at `.github/rulesets/main-protection.json` in the repository root.
It is the declaration of intent; it is not itself the enforcement. The
enforcement lives in the repository settings, and `tools/scripts/check_branch_protection.py`
compares the two.

| Job | Workflow | What it proves |
|---|---|---|
| `foundation-checks` | ci | lint, tests, contracts, scenarios, golden set, context, demos |
| `bsp-manifests` | ci | BSP manifest and readiness validation |
| `container-smoke` | ci | the container image builds and runs the goal end to end |
| `kernel-module` | ci | `wbcan.ko` builds and the fault suite reports real results |
| `mcu-qemu` | ci | the freestanding MCU target builds and runs under QEMU |
| `static-and-cpu-image` | container-full-stack | full-stack image, CPU smoke, SBOM and vulnerability scan |
| `codeql-python` | security | static analysis for Python |
| `codeql-c-cpp` | security | static analysis for the kernel and MCU C code |
| `dependency-review` | security | newly introduced high/critical advisories |

`strict_required_status_checks` is `true`, so a pull request must be up to date
with `main` before it merges. A stale approval is dismissed when new commits
arrive, and unresolved review conversations block the merge.

## Verifying the live configuration

```bash
GITHUB_REPOSITORY=owner/repo GITHUB_TOKEN=... \
  python3 tools/scripts/check_branch_protection.py
```

Exit codes are the contract, matching the other gates in this repository:

| Code | Meaning |
|---|---|
| `0` | every declared requirement was observed on the live repository |
| `1` | the live configuration contradicts the declaration |
| `2` | the live configuration could not be observed |

An unobservable configuration is never reported as a pass. A missing token, a
rate-limited API or an offline runner produces `2`, because "we could not look"
and "we looked and it was right" are different findings.

Pass `--live <file>` to check a recorded API payload, which is how the unit tests
exercise the comparison without network access.

## Administrator bypass

Administrator bypass is **disabled** in the declaration, so the check fails when
the live configuration still reports `enforce_admins` as false.

If an emergency genuinely requires bypassing the required checks, the operator
must:

1. record the reason, the affected commit, the waived checks and the approving
   human owner in this file before exercising the bypass;
2. re-run the waived checks against the merged commit afterwards and record the
   results;
3. open a follow-up issue if the bypass revealed a gap in the matrix itself.

An unrecorded bypass is an incident under
[incident response](incident-response.md), not a shortcut. Bypass must never be
used to publish a release image.

## Release promotion

A tag can be pushed onto any commit, including one that never saw a pull request,
so passing checks on `main` says nothing about the tagged commit on its own. The
release workflow therefore refuses to build until
`tools/scripts/check_release_commit.py` confirms both:

- the tagged commit is reachable from protected `main`; and
- every check in the declared matrix succeeded **on that exact commit**.

`skipped`, `neutral`, `in_progress` and `failure` conclusions all refuse the
release. The same exit-code contract applies: `0` promote, `1` refuse, `2`
unobservable, and `2` is treated as a refusal because it cannot prove the matrix
ran.

The release workflow additionally runs `check_task_packet.py --all`,
`make contract`, `make scenario-check` and `make context-check` before the image
is built, so a malformed Task Packet or a drifted contract cannot reach a
published artifact.

## Changing the matrix

A change to `.github/rulesets/main-protection.json` is a change to the release
gate. It requires:

1. a Task Packet naming the affected workflow jobs;
2. the matching live repository setting updated in the same change window;
3. a recorded `check_branch_protection.py` output showing `0` afterwards.

Deleting a check from the declaration is not the same as deleting the job: the
declaration is reviewed as a reduction in coverage, and the reviewer must say
which risk is now unguarded.

## Division of authority

This page and the two checkers report only what the API and the workflow files
say. They grant no merge authority, no release authority, and no physical
validation authority, and they never waive a required human approval.
