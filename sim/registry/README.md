# Scenario registry manifests

This directory is the registry-native manifest root read by
`workbench.kernel.scenario_registry` and by `sim_cli.py list` / `sim_cli.py describe`.

A manifest here is validated against `docs/architecture/scenario-contract-v1.json`
before a run exists. An invalid manifest raises with a stable diagnostic code and
the whole load fails; there is no partial catalog and no skipped file.

```bash
python3 tools/scripts/sim_cli.py list
python3 tools/scripts/sim_cli.py describe pick-place-red-block@1.0
```

## Why this is separate from `sim/scenarios/`

`sim/scenarios/**` is the frozen and expanded **regression** corpus: twelve frozen
fixtures plus six expanded variants per scene task, whose seeds, labels and
distribution are asserted by `make scenario-check`. Those manifests predate the
registry contract and carry simulator-only fields (`seed`, `fault_type`,
`scene_variant`, `oracle_allowed`).

Issue #301 migrated the five existing task families onto this path. Every family
now has a manifest here, and the equivalence is checked rather than asserted:

```bash
python3 tools/scripts/check_scenario_migration.py
python3 tools/scripts/sim_cli.py migration
```

The two roots stay separate on purpose. `sim/scenarios/**` remains the frozen and
expanded regression corpus, and this directory is the editable definition. What
`check_scenario_migration.py` guarantees is that they cannot drift: it pins the
verifier entry point and the digest of each family's legacy corpus (scenario IDs,
seeds and materialized scene parameters), so registering a family cannot quietly
change what its frozen fixtures mean, and adding a manifest here without a legacy
entry point fails.

The `task_id` key is the backward-compatible bridge. It is deprecated in favour
of the scenario identity, resolves through the registry, and is scheduled for
removal in v0.4; the registry identity became authoritative in v0.3.

Adding a manifest here does not change any frozen fixture, seed or hash.
