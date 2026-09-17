# Release eligibility

A run may be published as release evidence only when its provenance can be
recomputed from the evidence itself. This page defines that gate, the inputs it
binds, and the reasons it refuses.

The predicate lives in `tools/scripts/release_eligibility.py` and is applied by
all three consumers:

| Consumer | Role |
|---|---|
| `run_evaluation.py` | builds `provenance.json` from the validated logs and stamps the verdict into `summary.json` |
| `collect_metrics.py` | recomputes the verdict from the logs and the provenance record; never trusts `summary.json` |
| `generate_report.py` | reports the verdict and names every refusal reason |
| `check_evaluation_eligibility.py` | independent re-check for a promotion step; exits non-zero when refused |

## The rule

A run is eligible only when **all** of the following hold:

- the per-event `evaluation` metadata inside the log declares the `external`
  runner - not `scripted`, and not a summary field;
- that metadata names a commit, and the commit is not `unknown`;
- a provenance record exists for the run, and its runner and commit agree with
  the log;
- the log content still matches the provenance `event_log_sha256`;
- the scenario manifest still matches the provenance `manifest_sha256`;
- a human audit names a reviewer, a timestamp, and an oracle status of
  `confirmed` or `refuted` for every run;
- no run claims completion while its audited oracle status says otherwise.

Anything missing, stale or disagreeing fails closed. A scripted run can never be
eligible: the runner identity is refused before any other field is considered.

## Provenance record

`run_evaluation.py` writes `provenance.json` next to `summary.json`:

```json
{
  "format": "workbench-evaluation-provenance",
  "format_version": 1,
  "commit": "abc123",
  "runner": "external",
  "environment": {"platform": "...", "python": "3.12.x", "machine": "..."},
  "generated_at": "2026-09-17T00:00:00+00:00",
  "runs": {
    "v0.2-A--normal-001": {
      "runner": "external",
      "commit": "abc123",
      "scenario_id": "normal-001",
      "seed": 1101,
      "event_log_sha256": "<hash of the canonical event content>",
      "manifest_sha256": "<hash of the scenario manifest bytes>",
      "manifest_path": "sim/scenarios/frozen/normal-001.json"
    }
  }
}
```

The event-log hash is computed over canonical JSON (sorted keys, fixed
separators), so it depends on values rather than formatting. The manifest is
recorded by path, so a verifier checks the same file the runner hashed instead of
searching for a file that happens to declare the same `scenario_id`.

The hash detects an accidentally or casually edited log. It is not a signature:
producing the record is an approval step, and a deployment that needs a stronger
guarantee must sign the record and verify the signature before promotion.

## Human audit

The false-completion audit is the human statement that the oracle agreed with
each run. It must be attributable and complete:

```json
{
  "format": "workbench-false-completion-audit",
  "format_version": 1,
  "reviewed_by": "reviewer name",
  "reviewed_at": "2026-09-17T00:00:00+00:00",
  "runs": {"v0.2-A--normal-001": {"oracle_status": "confirmed"}}
}
```

An audit without a reviewer or a timestamp is not an audit, so it is refused
rather than treated as an anonymous pass. `oracle_status` must be `confirmed` or
`refuted` for every run; a run whose log claims completion while the audit says
`refuted` is counted as a false completion and refused.

## Refusal reasons

Reasons are stable strings, so an operator can search for them and a test can
assert on them:

| Reason | Meaning |
|---|---|
| no provenance record binds this run | the run has no entry in the provenance record |
| runner identity is missing or not external | the log does not declare an eligible runner |
| scripted runs are pipeline fixtures, never release evidence | the log declares a scripted runner |
| provenance does not name a commit | the log commit is empty or `unknown` |
| provenance commit disagrees with the event log | the record and the log disagree |
| provenance runner disagrees with the event log | the record and the log disagree |
| event log no longer matches the provenance hash | the log content changed after the record was written |
| scenario manifest no longer matches the provenance hash | the manifest changed or is missing |
| no human false-completion audit was supplied | no audit was provided |
| human audit is missing reviewer, timestamp or per-run oracle status | the audit cannot be attributed |
| human audit does not cover every run | the audit is incomplete |
| human audit found a false completion | a claim disagrees with the audited oracle |

`collect_metrics.py` adds one more: a `summary.json` that claims eligibility the
evidence does not support is itself reported, so a hand-written summary is
visible as a finding rather than silently believed.

## Checking a run

```bash
python3 tools/scripts/check_evaluation_eligibility.py \
  --run-dir runs/nightly-scripted/v0.2-A \
  --provenance runs/nightly-scripted/provenance.json \
  --human-audit runs/nightly-scripted/audit.json \
  --output runs/nightly-scripted/eligibility.json
```

The command exits `0` only for an eligible run, `1` when it refuses, and `2` when
an input cannot be read at all. It never repairs an input: each refusal names the
input that disagreed.

## Limits

- `--runner scripted` exists for pipeline tests. It writes
  `release_eligible: false` and stays that way through every downstream stage.
- The hash binds content, not authorship. A human or an approval workflow must
  produce the provenance record, and a signed record is a deployment decision.
- This gate decides whether a run may be *published as evidence*. It does not
  judge whether the robot performed a task safely; that remains the World Model
  verifier plus the physical validation described in
  [failure cases](failure-cases.md).
