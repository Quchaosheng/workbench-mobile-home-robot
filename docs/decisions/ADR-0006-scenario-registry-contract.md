# ADR-0006: Multi-scenario registry contract and ownership boundaries

Status: **proposed** (Issue #308; requires Architecture, Motion, Perception,
World Model and Release owner review before the registry implementation lands)

Date: 2026-09-19

Supersedes nothing. Extends the boundary in
[ADR-0005](ADR-0005-hardware-device-runtime.md) one layer up: ADR-0005 decides
how a *device* reaches the runtime; this record decides how a *scenario* reaches
it.

## Context

The runtime currently runs one task family at a time. A scenario is implicit:
`sim/scenarios/**/*.json` carries `scenario_id`, `seed`, `task_id`,
`world_version`, `fault_type`, `timeout_s` and `oracle_allowed`, and the task
family is selected by `task_id` inside `tools/scripts/scenario_tools.py`
(`TASK_PROFILES`). The verifier is chosen by the caller, not by the scenario.

The Epic (#309) adds a Scenario Registry so several task families share one
deterministic runtime. That creates a new authorship boundary: a scenario
manifest becomes code-adjacent input read before a run exists. Three things can
go wrong, and all three have precedent in this repository:

1. **A manifest smuggles control authority.** If a manifest can carry joint
   values, a controller goal or an emergency-stop decision, the Agent Runtime
   boundary documented in `docs/architecture/system.md` is bypassed by data
   rather than by code.
2. **A scenario grows a second implementation** of policy, verification or
   evidence handling. This is the same failure PR #127 fixed for the event
   store: two implementations of one contract drift, and only one of them is
   tested.
3. **Identity drifts.** Without an exact `scenario_version` in run metadata, a
   replay can silently run against a different scenario body than the run it
   claims to reproduce, and the resulting artifact still validates.

Issue #308 is a contract spike. It deliberately does **not** implement the
registry, the migration, or any scenario. Its output is the boundary those
issues must satisfy.

## Decision

### 1. The contract is one machine-readable file, and it is executable

The field list, diagnostic codes, bounds and ownership table live in
`docs/architecture/scenario-contract-v1.json`. A contract that only exists in
prose cannot be enforced, so `tools/scripts/check_scenario_contract.py` reads
that file and validates manifests against it, exiting `0 PASS`, `1 FAIL` or
`2 INCOMPLETE`.

The detector imports the shared `ActionType` vocabulary from
`libs/contracts/` rather than copying it. A scenario manifest therefore cannot
drift into a private action vocabulary, which is the second failure mode above.

### 2. Required manifest fields

| Field | Meaning | Forbidden content |
|---|---|---|
| `scenario_id` | Stable identity, `^[a-z0-9]+(?:[-_.][a-z0-9]+)*$` | path separators, unbounded IDs |
| `scenario_version` | Exact-match `MAJOR.MINOR`; part of run and replay identity | ranges, wildcards, implicit upgrade |
| `goal` | Operator-readable statement of intended outcome | controller targets |
| `semantic_actions` | Non-empty list from the shared `ActionType` | joint/velocity/torque values, controller goals |
| `required_adapters` | Semantic capability names | device handles, topic names, QoS |
| `evidence_policy` | Which observations and `ActionResult` fields verification requires | raw payloads |
| `verifier` | Repository-relative `path.py::function` entry point | inline code, second verifier implementation |
| `recovery_policy` | Declared bounded recovery behaviour | unbounded retry, scenario-owned `safe_stop` |
| `evidence_status` | One value from the declared vocabulary | implied or merged evidence classes |
| `non_goals` | What the scenario explicitly does not claim | — |

`task_id`, `timeout_s`, `world_version` and `owner` are optional so the five
existing task families can migrate without inventing metadata (#301).

### 3. Version matching is exact

`compatibility.version_match` is `exact`, `implicit_upgrade` is `false`, and an
unknown version is rejected. There is no "closest match" path, because a replay
that resolves to a different scenario body than its run is the failure this
ADR exists to prevent. Discovery order is lexicographic by `scenario_id`, and a
duplicate `(scenario_id, scenario_version)` pair fails closed.

### 4. Evidence classes are never merged

`evidence_status` accepts `SCRIPTED_FIXTURE`, `GAZEBO`, `PHYSICAL`,
`NOT_EXECUTED` and `BLOCKED`. The first, fourth and fifth are non-release
eligible, stated in the contract as `non_release_eligible_status`, so a scripted
fixture can never be rendered as physical success. This is the same distinction
`tools/scripts/sim_cli.py` already enforces with its `NOT_EXECUTED` runner status.

### 5. Forbidden field names fail before any value is read

`forbidden_field_names` and `forbidden_field_substrings` reject a field by name:
joints, velocities, torques, effort, CAN frames, controller goals, trajectories,
emergency stop, safe enable, stop authority and motor commands. The check runs on
key names at any depth, so a nested `{"params": {"joint_positions": []}}` fails
the same way a top-level one does. `policy`, `verifier_impl` and similar keys are
rejected because they would declare a second policy or verifier implementation.

## Field ownership

Every field has exactly one validator and one owner. A field validated in two
places is a field that can disagree with itself.

| Concern | Owner | Validated at |
|---|---|---|
| Scenario identity and manifest fields | Task / Simulation | registry load, before Event Store initialization |
| Semantic action contract | Agent Runtime | policy validator, before action dispatch |
| Verifier entry point and verification status | World Model | `verify()` call, at evidence read time |
| Adapter implementation and `ActionResult` | Motion / Perception / MCU | adapters return the shared `ActionResult` contract |
| Evidence status and release eligibility | Release / QA | release eligibility gate |
| Run and replay identity | Integration | run record creation, before the first event is written |
| Read-only projection | Dashboard | HTTP read model, never a writer |

The manifest owns *declaration*. It never owns *authority*. Motion and MCU
retain controller and emergency-stop authority; the World Model retains
verification; Release retains eligibility.

## Scenario selection through replay

```text
  operator                 registry                 runtime                 verifier
     |                        |                        |                       |
     |-- list / describe ---->|                        |                       |
     |<-- scenario_id@version-|                        |                       |
     |                        |                        |                       |
     |-- run scenario_id@ver->|                        |                       |
     |                        |-- validate manifest -->|                       |
     |                        |   fields, actions,     |                       |
     |                        |   adapters, verifier,  |                       |
     |                        |   forbidden names      |                       |
     |                        |                        |                       |
     |                        |   FAIL CLOSED -------->|  (no run, no event)   |
     |                        |                        |                       |
     |                        |-- resolved manifest -->|                       |
     |                        |   + scenario_version   |                       |
     |                        |                        |-- create run record ->|
     |                        |                        |   identity includes   |
     |                        |                        |   scenario_id,        |
     |                        |                        |   scenario_version,   |
     |                        |                        |   seed, config hash   |
     |                        |                        |                       |
     |                        |                        |-- semantic actions -->|
     |                        |                        |   (policy validated)  |
     |                        |                        |                       |
     |                        |                        |<-- ActionResult ------|
     |                        |                        |    + evidence refs    |
     |                        |                        |                       |
     |                        |                        |-- verify(manifest) -->|
     |                        |                        |                       |
     |                        |                        |<-- VerificationResult-|
     |                        |                        |                       |
     |                        |<-- ordered events -----|                       |
     |<-- read-only projection|                        |                       |
```

Replay reads the identity recorded before the first event. A bundle whose
`scenario_version` no longer resolves, or resolves to a different body, is
rejected rather than re-run against whatever currently occupies that ID.

## Compatibility with existing task IDs and run artifacts

- **`scenario_id` is unchanged for the twelve frozen manifests.** No existing
  fixture is renamed, re-seeded or re-hashed by this ADR.
- **`task_id` is an optional field**, so `pick_place`, `kitting`, `inspection`,
  `assembly` and `parcels` migrate by adding a manifest beside the existing
  `sim/scenarios/**` entry rather than replacing it (#301). Until that migration
  lands, the existing `TASK_PROFILES` path remains authoritative.
- **`scenario_version` is new.** Existing manifests have no version; the
  migration assigns `1.0` at first registration and records it in new run
  records only. Artifacts written before this ADR have no version field and are
  not retroactively relabelled as versioned runs.
- **No public interface schema changes.** `interfaces/json_schema/scenario.schema.json`
  and the `ScenarioManifest` model keep their current fields. Adding registry
  fields to the public schema is a separately approved follow-up, as #308
  requires.
- **`clean_workspace` is not an existing `ActionType`.** The cleaning example
  declares it and the contract lists it under `pending_semantic_actions`, so the
  detector reports `SCENARIO_PENDING_ACTION` and the example is
  `NOT_EXECUTABLE` rather than failing. The shared action vocabulary is extended
  by #306, not by a manifest.

## The QEMU limit, stated plainly

A passing contract check proves a manifest is *well formed*. It does not prove a
verifier exists, that a scenario runs, that an adapter is implemented, or that
any evidence is physical. The detector never imports the runtime, never starts
ROS or Gazebo, and never writes a run or an event.

The cleaning example is committed with `evidence_status: NOT_EXECUTED` for
exactly this reason: it documents a shape this repository cannot yet execute.

## Alternatives rejected

- **Prose-only ADR.** The three failure modes above are all enforcement
  problems. A prose boundary cannot reject anything.
- **Python manifests only.** Convenient for the five existing families, but it
  makes the contract executable by import and hides field validation inside
  scenario code. `scenario-contract-v1.json` is data, so it is readable and
  diffable by a reviewer who does not run the runtime.
- **Both Python and JSON.** Two registration formats is two validators, and the
  drift is the thing being prevented. #308 asks for the smallest format; JSON
  plus one detector is it.
- **Semantic-version ranges with closest-match resolution.** Reproduces the
  identity drift in a friendlier spelling.
- **Merge fixture, Gazebo and physical evidence into one status.** The one
  outcome this project must never produce is a fixture presented as a physical
  validation.

## Consequences

- #300 implements the registry against this contract and this detector.
- #301, #302, #305 and #306 add fields only by amending the contract file, in a
  reviewed change, with the detector updated in the same commit.
- #311's capability matrix must have a row for every registered
  `scenario_id@scenario_version`, and a registry entry without a row fails a
  deterministic command.
- #304 wires `--require-executable` into CI so a manifest that declares
  non-executable actions cannot pass registry gating.
- Adding a field to this contract is an interface change for every consumer.
  It is a reviewed decision, not a convenience.

## Open decisions

The registry implementation must settle, with the named owners: the concrete
manifest discovery root, whether `scenario_version` is required or defaulted for
migrated fixtures, and the exact CI job that runs the registry gate. Until then
this ADR is a design proposal and the contract file carries
`status: proposed`.
