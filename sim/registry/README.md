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

## Registry-wide conformance gate

Adding a manifest here adds it to the conformance matrix automatically. There is
no allow-list to edit, and that is deliberate: a hand-maintained list is how a new
scenario quietly skips the shared boundaries.

```bash
python3 tools/scripts/check_scenario_conformance.py
python3 tools/scripts/check_scenario_conformance.py --json
make scenario-conformance
```

The gate joins three sources:

- this registry, loaded through the Issue #300 reader, which supplies the
  identity set;
- `tools/qa/scenario-conformance-v1.json`, the committed case set, which names
  one committed test per identity per required dimension;
- the World Model verifiers, driven by derived probes that mutate a confirmed
  state and require a refusal.

Every registered scenario must prove the same seven dimensions: the actions are
policy valid, stale evidence cannot confirm, conflicting evidence cannot confirm,
missing provenance cannot confirm, replay is deterministic, a raw-control or
bypass field is refused, and a scripted fixture is not release eligible.

A scenario that is registered but has no case set fails with its identity and the
missing dimension, so CI names the scenario and the rule rather than leaving a
reader to guess which manifest is wrong. A case must resolve to a real test
function; prose cannot satisfy a dimension.

The gate is read-only. It starts no simulator, writes no run or event, and its
verdict never claims that a scenario ran or that any evidence is physical.
