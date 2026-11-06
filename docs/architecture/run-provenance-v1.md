# Run provenance v1

Issue: #313. Status: implemented.

A run directory records what happened. Before this, it did not record the inputs
that decide what *could* have happened: the seed, the clock and time source its
events were stamped on, the ordering rule a reader must apply, the adapter
versions, and which environment class the run belongs to.

That gap is silent in both directions:

- two runs of one scenario with different seeds reduce to different world states
  under one identity, so comparing them looks meaningful when it is not;
- a scripted fixture and a Gazebo run can reduce to the same state hash, and a
  hash alone cannot tell a reviewer which class of evidence they hold.

`libs/kernel/workbench/kernel/run_provenance.py` records those inputs, hashes
them, and refuses a bundle that omits them or claims a class its runner could not
produce. `tools/scripts/check_run_provenance.py` is the gate;
`make run-provenance-check` runs it in CI after `make run-identity-check`.

## The field table

| Field | Values | Notes |
|---|---|---|
| `seed` | any non-blank scalar | Rendered as a string. A `bool` is refused, because `"True"` would collide with the string form of a seed-like value. |
| `clock_mode` | `monotonic`, `wall` | The shared contract's `ClockId` vocabulary, not a second one. |
| `time_source` | `fixed_base`, `host_clock` | How the wall time an event carries was produced. `fixed_base` is reproducible from the seed alone; `host_clock` is not. |
| `event_ordering` | `sequence_no`, `file_order` | The rule a reader must apply. `sequence_no` is the canonical total order the runtime writes; `file_order` is the persisted line order, which the #302 event-stream hash binds to. |
| `adapter_versions` | `name=version` pairs, sorted by name | An adapter with no known version is recorded as `unspecified`. An empty mapping is legitimate: a scenario that needs no adapter has no versions to name. |
| `environment_class` | `NOT_EXECUTED`, `BLOCKED`, `SCRIPTED_FIXTURE`, `GAZEBO`, `PHYSICAL` | The committed capability-matrix vocabulary. **Derived, not accepted**: see below. |

The field table is one committed order, `PROVENANCE_INPUTS`. Adding a field
changes every provenance digest, which is the point, and the order is asserted by
a test rather than left to dictionary iteration.

## The environment class is derived, never typed

`environment_class_for_runner(runner, status=...)` computes the class from the
runner that ran and how it finished:

| Runner | Status | Class |
|---|---|---|
| any | `NOT_EXECUTED` | `NOT_EXECUTED` |
| `scripted` | `SCRIPTED_FIXTURE` | `SCRIPTED_FIXTURE` |
| `gazebo`, `external` | `EXECUTED` | `GAZEBO` |
| `gazebo`, `external` | `FAILED`, `TIMED_OUT`, `INVALID_OUTPUT`, `INTERRUPTED` | `GAZEBO` |

A class a caller types is a label. A fixture that labels itself `GAZEBO` is
exactly the dishonesty the capability matrix refuses, so a bundle whose declared
class disagrees with its runner is refused as
`RUN_PROVENANCE_UNSAFE_ENVIRONMENT_CLAIM`.

Two rules are deliberate and are asserted directly:

- **A failed run is still evidence from its environment.** A Gazebo run that
  timed out is `GAZEBO`, not `NOT_EXECUTED`. Conflating the two would let a
  failure hide behind the class a fixture uses.
- **No runner may claim `PHYSICAL`.** Physical evidence enters through the
  hardware evidence path, not through a simulation runner. `PHYSICAL` is absent
  from every runner's allowed set on purpose.

A scripted runner that reports an executed status is refused rather than mapped
to the nearest class: a scripted run that claims to have executed is the
false-completion case.

## Composition with the run identity

Issue #302's identity answers *which scenario definition* produced a stream. This
module answers *what inputs* it was produced with. Both are needed before two runs
can be compared, so the identity composes this module's digest as one additional
input, `provenance_hash`, rather than duplicating its six fields.

The composition is the reason the identity input list grew by exactly one entry.

## Version and migration note

This change bumps the identity schema from `workbench-run-identity-v1` to
`workbench-run-identity-v2` and introduces `workbench-run-provenance-v1`.

Adding an identity input changes every identity hash by construction. A v1 bundle
is therefore **refused by version**, as `RUN_IDENTITY_SCHEMA_UNSUPPORTED`, and is
never re-hashed in place: re-hashing would silently bless an identity computed
from a different input list. The diagnostic names the version it was written under
and the version to re-run under, rather than reporting a missing field, because a
v1 bundle lacks `provenance_hash` by construction and a missing-field message
would hide the real cause.

Re-running the scenario under v2 records the input and produces the new hash.
No fixture, seed, scene hash or registry manifest changes.

## The published run ID is derived from those inputs (Issue #466)

`sim_cli.run` used to name its output directory with `uuid.uuid4()`. The bundle
identity and provenance were deterministic, but the directory name a reviewer
quotes was not, so the same inputs could not be rerun into the same place and no
evidence reference could name a `run_id` stably.

The directory name is now `<scenario_id>-<12 hex characters>` of a digest over
one ordered input list, `RUN_ID_INPUTS` in `tools/scripts/sim_cli.py`:

| Field | Why it is an input |
|---|---|
| `scenario_id` | The manifest ID. Two fixtures can share one registered definition, and they are different runs. |
| `registry_identity` | `id@version`. A definition bump is a different run even when the manifest ID is unchanged. |
| `seed` | The **effective** seed, `seed_base + manifest seed`. |
| `version` | The `--version` label the caller chose. |
| `commit` | `git rev-parse HEAD`, or `unspecified` outside a checkout. |
| `nonce` | `none` by default, or the caller's bounded rerun label. |

The digest is `run_identity(..., inputs=RUN_ID_INPUTS)` from
`libs/kernel/workbench/kernel/scenario_identity.py`. This is deliberate reuse:
the repository has one canonical-hash-and-material helper, and #466 adds an input
list to it rather than a second hashing scheme beside it.

Three properties follow:

- **Cross-process determinism.** The ID is a pure function of the inputs, so two
  processes with the same scenario, seed, version and commit publish the same
  name.
- **No concatenation ambiguity.** `scenario_id` is validated filesystem-safe and
  the suffix is all hex, so the final `-` is the only boundary between them. A
  scenario whose ID happens to equal another run's full name still gets a
  distinct directory, because its suffix is recomputed over the new ID.
- **Collision is a refusal, not a rename.** A second run into an output root that
  already holds this ID raises `SimulationInputError` ("refusing to overwrite
  existing run artifact") before any staging directory is created, and again at
  publication. The first bundle is left byte-identical. The message names both
  legal ways forward: a different output root, or an explicit `--nonce`.

### The nonce is explicit, bounded and recorded

`run_scenario(..., nonce=...)` and `sim_cli run --nonce ...` accept at most 64
ASCII, filesystem-safe characters. A nonce is never generated for the caller:
omitting it records `none`, so a rerun is always visible as one in
`metadata.json`. The refusal rules are:

| Value | Outcome |
|---|---|
| omitted, or `none` | Accepted; recorded as `none`, the reserved default. |
| blank or whitespace-only | Refused (`non-blank`). |
| padded with whitespace | Refused (`padded`); a silently stripped label is not the label asked for. |
| longer than 64 characters | Refused (`exceeds`). |
| non-ASCII or containing anything but `A-Za-z0-9._-` | Refused (`filesystem-safe`). |

### What a bundle records about its own name

`metadata.json` carries `run_nonce` and `run_id_derivation`, which holds the
input list, the exact material and the suffix length. A reviewer can recompute
the published directory name from a bundle without re-running it, so a name can
be checked rather than trusted. The derivation is not a second identity: it
names a directory and is not consulted by `check_run_identity.py` or
`check_run_provenance.py`.

### Operator consequence: a rerun needs a fresh root or a nonce

Because the name is stable, re-running the same command a second time into the
same output root now fails with exit code 2 instead of quietly creating a second
directory. That is the fail-closed behaviour the issue asks for, and it is what
`make run-identity-check` / `make run-provenance-check` now do on a second
invocation in the same checkout:

```text
NOT_EXECUTED: refusing to overwrite existing run artifact: runs/identity-check/normal-001-…;
the run ID is derived from the run's inputs, so a rerun needs a different output
directory or an explicit run nonce
```

The two legal reruns are `--output-dir` pointing somewhere fresh, or
`--nonce <label>` to publish a second, clearly labelled bundle beside the first.
CI is unaffected: each job starts from a clean workspace.

## What this gate does not claim

The gate is read-only. It starts no simulator, writes no run or event, and its
verdict never claims a run executed or that any evidence is physical. A matching
provenance states that a bundle declares its determinism inputs and that its
environment class is one its runner could produce. It is not physical evidence.

Two limits are worth stating plainly:

- A state hash alone does not separate every pair of vectors. In the committed
  vector set, a `timeout` and a `recovery_started` event reduce to the **same**
  world state hash while being different runs. That is the concrete reason this
  design binds the event stream and the determinism inputs as well as the state.
- `adapter_versions` is populated from the adapters the registry entry declares.
  None of them carries a version number today, so every value is `unspecified`.
  The field exists so that recording a real version later changes the identity,
  and so that a reviewer can see the value is unpopulated rather than absent.
