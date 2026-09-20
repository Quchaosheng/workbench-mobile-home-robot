# Evaluation

The evaluation surfaces answer different questions, and they are deliberately
separate rather than one report that guesses:

| Surface | Question it answers | Owner |
|---|---|---|
| [`multi-scenario-readiness.md`](multi-scenario-readiness.md) | What is the committed software ready for, per registered scenario identity? | Release / QA |
| [`readiness-report-v1.json`](readiness-report-v1.json) | The machine-generated artifact behind that page, with the configuration hash it was built from | Release / QA |
| [`release-eligibility.md`](release-eligibility.md) | May a run be published as release evidence? | Release / QA |
| [`failure-cases.md`](failure-cases.md) | Which failure paths are covered, and by which test? | World Model |
| [`cold-start-test.md`](cold-start-test.md) | How external cold start is measured and recorded | Product Owner |
| [`status-understanding-test.md`](status-understanding-test.md) | Whether an operator can tell confirmed from insufficient evidence | Product Owner |

Delivery phases are tracked separately, in [`docs/releases/`](../releases/README.md):
`phase-gates.md` states which phase proves itself today and what evidence class it
reaches, while this directory states what the committed software is ready for.

## Multi-scenario readiness

The readiness report is generated, never typed:

```bash
python3 tools/scripts/readiness_report.py --output docs/evaluation/readiness-report-v1.json
python3 tools/scripts/check_readiness_report.py
python3 -m pytest tests/unit/test_readiness_report.py -v
```

It reports four **independent** evidence classes per registered identity:

- `software` - the definition is registered, contract valid, and proven by a
  committed test for every required conformance dimension;
- `scripted_fixture` - a committed scripted fixture exercised the scenario;
- `gazebo` - a real Gazebo run exercised the scenario;
- `physical` - attested hardware evidence exercised the scenario.

Only `gazebo` and `physical` can make a row `release_eligible`. A row may never
claim a stronger class than its manifest `evidence_status` supports, and
`NOT_EXECUTED`, `BLOCKED` and `release_eligible: false` are never rendered as a
success. The gate compares the committed artifact against a regeneration, so a
hand-edited outcome, a stale page or a scenario registered without a row fails
`python3 tools/scripts/check_readiness_report.py`.

The report grants no release approval, replaces no required check and is not
physical evidence.
