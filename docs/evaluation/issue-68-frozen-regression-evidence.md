# Issue #68 — frozen regression runner truthfulness

## Claim under test

`tests/regression/run_frozen_scenarios.py` must never report a pass when no
simulator executed. Historically it printed a placeholder marker and returned
`success=True` with a hard-coded `vtcr=0.95` for every manifest, so a release
or review could look green while no scenario ran.

## Reproduction on this commit

`runner=gazebo` with no `WORKBENCH_GAZEBO_COMMAND` configured:

```text
Frozen scenarios: 12 | executed=0 | scripted=0 | not_executed=12 | failed=0
  [NOT_EXECUTED] normal-001 - no Gazebo adapter configured; set WORKBENCH_GAZEBO_COMMAND or pass --command
  ...
```

- process exit code: `2`
- `NOT_EXECUTED` occurrences: 12 (one per frozen manifest)
- placeholder / VTCR occurrences: 0

An absent simulator is therefore reported as `NOT_EXECUTED`, never as a pass,
and no success rate or VTCR is manufactured.

## Guarding evidence

- `tests/unit/test_sim_cli.py::test_frozen_regression_without_gazebo_exits_nonzero_without_placeholder_metrics`
  asserts exit code `2`, the presence of `NOT_EXECUTED`, and the absence of the
  placeholder marker.
- `.github/workflows/ci.yml` runs the frozen matrix and asserts the same
  properties, so a skipped matrix cannot satisfy the release gate.
- The compatibility entry point delegates to `tools/scripts/sim_cli.run_scenarios`
  instead of a stub, and `make scenario-check` validates the manifests it reads.

## Scope

No simulator was added and no gate was relaxed. This record only pins the
truthful behaviour that already shipped, together with the evidence commands
in `docs/task_packets/issue-68-frozen-regression-truthfulness.json`.
