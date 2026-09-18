# fault_injection

Executable fault coverage for the declared simulation and runtime faults.

The declaration lives in `tools/scripts/scenario_tools.py` (`P2_FAULT_TYPES`) and
`sim/scenarios/{frozen,expanded}`. The executable contract lives in
`libs/application/workbench/application/fault_injection.py`, and the tests that
drive it through the shipped runtime live in
`tests/integration/test_fault_injection_coverage.py`.

There is no `services/world_model/.../faults.py`: the earlier README described a
hook layer that was never implemented. Faults are injected at the real port
boundaries (`ActionAdapter`, `ObservationPort`, the safety MCU and the
post-action loop) so a fault test exercises the production rules instead of a
private copy of them.

## Declared faults

| Fault | Trigger | Expected safe behaviour |
| --- | --- | --- |
| `actuator_timeout` | the adapter raises `TimeoutError` | bounded retry, then a terminal failed state |
| `camera_dropout` | every observation reports `PERCEPTION_FAILURE` | refuse confirmation without fresh evidence |
| `grasp_failure` | the adapter returns a failed `ActionResult` | refuse confirmation without fresh evidence |
| `moving_target` | a fresh observation contradicts the claim | keep a `refuted` verification with its evidence |
| `occlusion` | the named entity's observation is withheld | refuse confirmation for that entity only |
| `stale_observation` | the observation is stamped at or before completion | re-observe before any dependent step |

Two runtime faults are declared separately because they are conditions of the
runtime rather than of one scenario file:

| Fault | Trigger | Expected safe behaviour |
| --- | --- | --- |
| `link_loss` | the safety MCU watchdog expires | the MCU reaches its fault state; only an explicit reset clears it |
| `process_restart` | the process restarts mid-run | the run's correlation is explicitly invalidated |

## Rules

- A fault never becomes a completion. The injectors can only remove evidence,
  delay an action, fail a result or move a target.
- Injection is seeded; the same seed replays the same injected sequence.
- Coverage is reported as `SIMULATED`, `HARDWARE_TESTED` or `NOT_EXECUTED`.
  A `HARDWARE_TESTED` claim without a named physical evidence reference is
  refused, and a fixture passing on a development host is never reported as
  physical evidence.

## Running the coverage

```bash
python -m pytest tests/integration/test_fault_injection_coverage.py -v
python -m pytest tests/unit tests/integration -k fault -v
make scenario-check
```

## Adding a fault class

1. Add the type string to `P2_FAULT_TYPES` in `tools/scripts/scenario_tools.py`
   and to the `FaultType` enum in the contract module.
2. Add a `FaultSpec` naming its trigger, safe behaviour, evidence requirement and
   declaring manifest directory.
3. Extend the injector that owns that boundary, or add one.
4. Add a scenario manifest that declares the fault, and a test asserting the
   safe behaviour against the shipped runtime.
