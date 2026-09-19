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
