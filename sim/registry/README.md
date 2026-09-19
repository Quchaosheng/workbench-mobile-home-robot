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

Issue #301 owns migrating the five existing task families onto this path and
proving that the old entry points resolve to byte-for-byte equivalent
definitions. Until that lands, the two roots are deliberately separate and
`sim/scenarios/**` remains authoritative for regression runs.

Adding a manifest here does not change any frozen fixture, seed or hash.
