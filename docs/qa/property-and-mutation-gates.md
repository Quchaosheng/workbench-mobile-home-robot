# Property and mutation gates

The example-based suites answer "does this input behave?" The property and
mutation gates answer two different questions:

- **Property** — for a generated family of inputs around one fail-closed
  boundary, does the boundary hold for every member of the family?
- **Mutation** — if a rejection or a threshold in that boundary is removed, does
  a named test actually notice?

A property suite that never fails and a mutation that is never detected are both
worthless, so the two gates are designed to be adversarial to each other.

## What is covered

Five boundaries, one suite each:

| Boundary | Module under test | Suite |
| --- | --- | --- |
| Event ordering | `services/world_model/workbench_world_model/reducer.py` | `tests/property/test_property_event_ordering.py` |
| Schema validation | `services/world_model/workbench_world_model/event_payloads.py` | `tests/property/test_property_schema_validation.py` |
| Policy rejection | `services/agent_runtime/workbench_agent_runtime/policy_validator.py` | `tests/property/test_property_policy_rejection.py` |
| Frame decoding | `libs/hardware/workbench/hardware/can_driver_safe.py` | `tests/property/test_property_frame_decoding.py` |
| State transitions | `libs/kernel/.../lifecycle.py`, `firmware/virtual_mcu/.../state_machine.py` | `tests/property/test_property_state_transitions.py` |

The suites are seeded and dependency-free. `hypothesis` is not installed and the
repository does not add a dependency to make a safety boundary testable, so
`tests/property/_generator.py` provides a splitmix64 stream, a shrinker and the
corpus accounting. The same seed produces the same corpus on every host.

`reduce_events` is not the only entry point worth testing. `apply_event` is public
API and is exercised on its own, because a mutation to its per-event guard would
stay invisible if the suite only drove the stream-level preflight.

## Quarantine

A failing suite is either fixed or quarantined; it is never retried. Quarantine
entries live in `tools/qa/quarantine-v1.json` and each one needs an owner, a
reason and a future expiry date. The gate stops when an entry is expired, when its
owner or reason is blank, when a named test is quarantined inside a mutation
probe, when the quarantined test no longer exists, or when it currently *passes*
— a stale entry hides a green test from the gate and would let a real regression
be quarantined unnoticed. This file is empty today because every suite passes.

## Evidence

Both gates write machine-readable summaries under `runs/qa/`:

- `runs/qa/property-gate/summary.json` — generator version, case schema version,
  seeds, per-suite case count, corpus digest, shrunk counterexamples, and the gate
  verdict;
- `runs/qa/mutation-gate/summary.json` — one record per mutation with its
  boundary, file, named tests, control and mutated durations, the failing tests
  it produced, and whether the repository copy was left byte-identical.

Exit codes are the contract:

| Code | Meaning |
| --- | --- |
| 0 | PASS |
| 1 | FAIL — a property case failed, or a mutation survived |
| 2 | INCOMPLETE — a budget ran out, a literal did not apply exactly once, a named test was already failing, or the sandbox could not isolate the copy |

An INCOMPLETE run is never rendered as a pass. A missing summary, an unapplied
mutation, an exhausted budget and a sandbox that would have tested unmutated code
all land here.

## How a mutation probe stays safe

Each probe copies the repository into a throwaway directory, neuters exactly one
literal there, runs the named tests, and restores the file. The real checkout is
only ever read: the gate hashes every file named in the registry before and after
the probes and reports any difference.

The copy is import-isolated on purpose. The editable install points at the real
checkout, so without isolation a probe would silently test unmutated code and
report a false pass. The gate rewrites `sys.meta_path` inside the copy, then
asserts that every gate-relevant module resolves under the copy root before it
runs a single mutation.

`firmware/` and `robot/control/` are excluded from the registry because AGENTS.md
keeps them out of AI write tasks, and a probe edits its target even inside the
copy. The VirtualMcu transition boundary is still covered by the state-transition
suite, which only reads it.

## Evidence class

This is software-only evidence on a developer machine or a hosted runner. It says
nothing about target hardware, real ROS/Gazebo behaviour or a physical robot.
Those measurements remain NOT_EXECUTED here.
