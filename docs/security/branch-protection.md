# Branch protection and the release safety matrix

`main` is the only branch that may publish a release, so the checks that guard
`main` are the release gate. This page defines the required matrix, how a change
to it is reviewed, and what happens when GitHub cannot enforce a rule.

## The declared matrix

The matrix is committed at `.github/rulesets/main-protection.json` in the repository root.
It is the declaration of intent; it is not itself the enforcement. The
enforcement lives in the repository settings, and `tools/scripts/check_branch_protection.py`
compares the two.

The eight checks below are `required_status_checks`: a pull request cannot merge
while one is red or missing.

| Job | Workflow | What it proves |
|---|---|---|
| `foundation-checks` | ci | lint, tests, contracts, scenarios, golden set, context, demos |
| `bsp-manifests` | ci | BSP manifest and readiness validation |
| `branch-protection` | ci | the committed matrix still matches the live protection, or records an explicit INCOMPLETE |
| `container-smoke` | ci | the container image builds and runs the goal end to end |
| `kernel-module` | ci | `wbcan.ko` builds and the fault suite reports real results |
| `mcu-qemu` | ci | the freestanding MCU target builds and runs under QEMU |
| `codeql-python` | security | static analysis for Python |
| `codeql-c-cpp` | security | static analysis for the kernel and MCU C code |
| `colcon-motion` | colcon-motion | `workbench_motion` builds and `colcon test` passes, or records an explicit not-applicable |
| `dependency-review` | security | newly introduced high/critical advisories |

One further check runs on every pull request and is declared
`observed_but_not_required`, so it does **not** block a merge:

| Job | Workflow | Why it is observed but not required |
|---|---|---|
| `static-and-cpu-image` | container-full-stack | full-stack image, CPU smoke, SBOM and vulnerability scan. It fails on base-layer CVEs a code change cannot fix, so requiring it would mean no pull request could ever be greener than `main`. Its result is still visible on every pull request; see `observed_but_not_required` in the declaration for the condition that revisits this. |

`strict_required_status_checks` is `true`, so a pull request must be up to date
with `main` before it merges.

The declaration sets `require_pull_request` to `false` and
`required_approving_review_count` to `0`, so an approving review is **not**
required, a stale approval is **not** dismissed on push, and unresolved review
conversations do **not** block the merge. That is deliberate and it is stated
here rather than implied, because the previous version of this paragraph claimed
the opposite of the committed declaration (issue #593).

The reason is in the declaration's `review_policy`: the repository has one active
maintainer and no reviewer who could approve, so the requirement was
unsatisfiable and satisfying it meant recording an administrator bypass before
every merge. A gate that is bypassed every time is not a gate. What actually
reviews a change is the status matrix — eight required checks including the
property and mutation gates — with administrator bypass disabled, so those checks
cannot be skipped either. See "Approving reviews" below for the condition that
restores the count to 1.

`tools/scripts/check_branch_protection.py` now reads this page and refuses a
declaration that contradicts a guarantee written here, so this paragraph cannot
drift away from `.github/rulesets/main-protection.json` again.

## Trigger policy: a required check must always report

A declared check is only a gate if the workflow that produces it is created for
every protected pull request. A `paths:` or `paths-ignore:` trigger filter does
the opposite: when no changed file matches, GitHub creates **no workflow run at
all**, so the required context never reports and there is nothing to gate on. A
pull request that changed only documentation, tooling, dashboard files or this
ruleset used to be exactly that case.

The rule that follows from it: **no workflow that produces a declared required
check may filter `pull_request` by path.** Expensive work is allowed to be
conditional, but it must be wrapped by an always-running job that evaluates
applicability and reports an explicit conclusion.

`static-and-cpu-image` is the worked example. The `container-full-stack` workflow
declares no `paths:` filter, and the job of that name always runs:

| Job | Role |
|---|---|
| `static-image-applicability` | always runs; decides whether the image path policy selects any changed path and records its reason |
| `static-image-build` | the expensive job: build, CPU smoke, dashboard health, SBOM and Grype scan, run only when applicable |
| `static-and-cpu-image` | always runs; owns the required check name and reports the conclusion |

The required name therefore reports one of three documented outcomes:

| Conclusion | Meaning |
|---|---|
| `success` | the build ran and every expensive step passed |
| `not-applicable` | the decision ran and selected no changed path, so there was no image of this change to test |
| `failure` | the build ran and failed, the decision did not run, or the build was cancelled |

`not-applicable` is a truthful statement about the gate, not a passing image
check. `tools/scripts/check_release_commit.py` still accepts only `success` on a
tagged commit, so a release that touches the image cannot ship on the strength of
a not-applicable run. A *skipped* build is never reported as any of the three:
the verdict job runs with `if: always()`, which is what makes the "the build was
skipped" case visible instead of silent.

The fifteen path patterns survive as `IMAGE_PATH_PATTERNS` inside the
applicability job. They decide how much work runs, never whether a result is
emitted.

## Changing the trigger or the matrix

Removing a path filter is a coverage *increase* and adding one is a coverage
*decrease*, so both need owner review:

1. a Task Packet naming the affected workflow jobs and their trigger policy;
2. the matching live repository setting updated in the same change window when
   the matrix itself changes;
3. a recorded `check_branch_protection.py` output showing `0` afterwards.

The declaration and the trigger policy are one decision, not two. Adding a name
to `.github/rulesets/main-protection.json` without an always-running job that
emits it creates an expected status that never arrives; adding a `paths:` filter
to a workflow that already emits a declared check removes release evidence that
the declaration still claims. Deleting a check from the declaration is reviewed
as a reduction in coverage, and the reviewer must say which risk is now
unguarded.

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

## Approving reviews

`required_approving_review_count` is 0. The repository has one active maintainer
and no reviewer who could approve, so the requirement was unsatisfiable: every
merge needed a recorded administrator bypass, and a gate that is bypassed every
time is not a gate.

What still reviews a change is the status matrix — eight required checks
including the property and mutation gates — and administrator bypass remains
disabled, so those checks cannot be skipped either. Removing the approval
requirement changed who may merge, not what has to pass.

The declaration records the condition for restoring it: a second maintainer with
write access who reviews pull requests. At that point the count returns to 1 and
`dismiss_stale_reviews_on_push` returns to true.

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

### Recorded bypass: 2026-11-11

**Reason.** `required_approving_review_count` is 1 and every collaborator is
inactive — the most recent collaborator commit is 2026-09-10 — so no pull request
can satisfy the approval requirement. The repository has one active maintainer.
This is a configuration gap, not an emergency in the sense the procedure above
anticipates, and it is corrected below rather than bypassed twice.

**Affected commit and pull request.** `08b4d39bcac35babe9ac54e17a8d1cc7c3ab316f`,
PR #535.

**Waived check.** The approving-review requirement only. All eight required status
checks were evaluated and passed on that commit; no check was waived.

**Approving human owner.** Quchaosheng, the repository owner, who authorised the
bypass in this session.

**What was done.** `enforce_admins` was set to `false`, the pull request was
merged, and `enforce_admins` was restored to `true` immediately afterwards. Every
other setting was left untouched.

**Results after the merge.** PR #535 merged as `3a138f8e0`; every required check
on the merged commit was green before the merge, and nothing was re-run.

### Recorded bypass: 2026-11-11 (dependency bumps)

**Reason.** The same approval gap. Two dependabot pull requests carried no
approving review for the same structural reason: one active maintainer.

**Affected commits and pull requests.** `dcf4d0e85eee97d0a127bdd1f80cb357439408c8`
(PR #536, python dependencies, plus the regenerated lock) and
`f5cc1f3ff275e8f117888ebe156faf824e17a1e7` (PR #537, `docker/login-action`, plus
its task packet).

**Waived check.** The approving-review requirement only. All eight required
status checks were evaluated and passed on both commits.

**Approving human owner.** Quchaosheng, repository owner.

**What was done.** `enforce_admins` was set to `false` for the two merges and
restored to `true` immediately afterwards.

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

## Division of authority

This page and the two checkers report only what the API and the workflow files
say. They grant no merge authority, no release authority, and no physical
validation authority, and they never waive a required human approval.
