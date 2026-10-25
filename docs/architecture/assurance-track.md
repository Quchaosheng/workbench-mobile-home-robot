# Assurance Track

The runtime-assurance register for this repository: the invariants the runtime is
claimed to hold, the fault cases those invariants must survive, and the committed
test that proves each claim. The tables are **generated** from
`docs/assurance/invariants-v1.json` and `docs/assurance/acceptance-matrix-v1.json`.

Regenerate and verify with:

```bash
python3 tools/scripts/check_assurance.py --check-generated
```

## What this track claims, and what it does not

It claims **runtime assurance**: bounded actions, fail-closed cancellation, correlated
device acknowledgements, declared terminal states under fault, and three-valued
verification where missing evidence yields `insufficient_evidence` rather than a guess.

It does **not** claim a functional-safety certification. No row here is a SIL, ASIL or
ISO 26262 claim, and no row is physical evidence: every current row is `SIMULATED`.
A scripted or unit-level fault proves the rule, not the machine.

## How to read a row

- **Status** is `ENFORCED` (a resolving enforcement symbol and at least one resolving test)
  or `NOT_IMPLEMENTED` (a stated gap, no evidence). A gap is visible on purpose.
- **Expected terminal state** names one state from a declared vocabulary. A fault that
  reaches no declared state is a failure, not a pass.
- **Expected verdict** is three-valued. Only the nominal case may expect `confirmed`;
  every injected fault must expect `insufficient_evidence` or `refuted`.
- **Coverage** is `SIMULATED`, `HARDWARE_TESTED` or `NOT_EXECUTED`. It never inflates:
  an executed case must cite a resolving test, an unexecuted case must state its gap.

## Register

<!-- Generated from docs/assurance/*.json. Do not edit by hand. -->

## Invariants

| ID | Statement | Boundary | Status | Enforced at | Evidence |
| --- | --- | --- | --- | --- | --- |
| `INV-01` | A scenario the registry cannot resolve to exactly one identity is refused before a runner starts, and no evidence directory is published. | Scenario registry and run admission | ENFORCED | `tools/scripts/sim_cli.py::_identity_entry` | `tests/unit/test_assurance_matrix.py::test_unregistered_scenario_cannot_start_a_run` |
| `INV-02` | A PolicyProposal that carries raw joint, velocity or torque parameters is refused before dispatch. | Policy validation | ENFORCED | `services/agent_runtime/workbench_agent_runtime/policy_validator.py::PolicyValidator.check` | `tests/unit/test_policy_validator.py::PolicyValidatorRawControlTests::test_disguised_raw_control_identifiers_are_denied` |
| `INV-03` | After STOP or cancel, no later step is dispatched and a repeated request cannot re-arm the sequence. | Execution controller | ENFORCED | `services/agent_runtime/workbench_agent_runtime/execution_controller.py::ExecutionController.request_stop` | `tests/unit/test_execution_controller.py::test_stop_request_preempts_remaining_steps_and_is_idempotent`<br>`tests/unit/test_post_action_gate.py::FailClosedStateTests::test_cancellation_blocks_every_step` |
| `INV-04` | An acknowledgement that arrives after its command timed out cannot confirm that command or a later one. | Device runtime and CAN transport | ENFORCED | `libs/hardware/workbench/hardware/can_driver_safe.py::SafeCANBus.service_once` | `tests/unit/test_can_transport.py::test_late_ack_does_not_confirm_after_command_timeout` |
| `INV-05` | Losing the device link fails closed and no command is admitted until an explicit recovery. | Device runtime and CAN transport | ENFORCED | `libs/hardware/workbench/hardware/can_driver_safe.py::SafeCANBus.service_once` | `tests/unit/test_can_transport.py::test_receive_link_loss_is_fail_closed_until_recovery` |
| `INV-06` | A timeout reaches a declared terminal state at both the action and the run level; it never becomes a success. | Execution controller and run admission | ENFORCED | `services/agent_runtime/workbench_agent_runtime/execution_controller.py::ExecutionController.execute` | `tests/unit/test_execution_controller.py::test_terminal_action_result_stops_later_steps`<br>`tests/unit/test_sim_cli.py::SimulationCliTests::test_timeout_records_status_and_does_not_leave_partial_directory` |
| `INV-07` | After a runtime restart, the run correlation is invalidated and the loop refuses to gate a step for the stale run. | Post-action loop | ENFORCED | `services/agent_runtime/workbench_agent_runtime/post_action.py::PostActionLoop.note_restart` | `tests/unit/test_post_action_gate.py::FailClosedStateTests::test_restart_invalidates_run_correlation_fail_closed` |
| `INV-08` | A verifier that lacks the observation supporting a completion claim returns insufficient_evidence, never confirmed. | World Model verification | ENFORCED | `services/world_model/workbench_world_model/verifier.py::verify_object_in_tray` | `tests/unit/test_world_model.py::WorldModelTests::test_action_result_without_spatial_observation_is_insufficient` |
| `INV-09` | Two runs that claim the same robot resource cannot be active at the same time. | Run resources | NOT_IMPLEMENTED | gap: ADR-0009 defines the claim and lease state machine but no implementation exists yet; no enforcement point can be named. | - |

## Acceptance matrix

| Case | Fault | Expected terminal state | Expected verdict | Invariants | Coverage | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| `ack-late` | ack_late | `can_link:safe_stopped` | `insufficient_evidence` | INV-04 | SIMULATED | `tests/unit/test_can_transport.py::test_late_ack_does_not_confirm_after_command_timeout` |
| `ack-lost` | ack_lost | `can_link:safe_stopped` | `insufficient_evidence` | INV-06 | SIMULATED | `tests/unit/test_can_transport.py::test_retry_keeps_correlation_and_timeout_escalates_to_stop` |
| `cancel-mid-run` | cancel | `execution:cancelled` | `insufficient_evidence` | INV-03 | SIMULATED | `tests/unit/test_post_action_gate.py::FailClosedStateTests::test_cancellation_blocks_every_step`<br>`tests/unit/test_execution_controller.py::test_stop_request_preempts_remaining_steps_and_is_idempotent` |
| `device-disconnect` | device_disconnect | `can_link:link_lost` | `insufficient_evidence` | INV-05 | SIMULATED | `tests/unit/test_can_transport.py::test_receive_link_loss_is_fail_closed_until_recovery` |
| `evidence-gap` | evidence_gap | `execution:succeeded` | `insufficient_evidence` | INV-08 | SIMULATED | `tests/unit/test_world_model.py::WorldModelTests::test_action_result_without_spatial_observation_is_insufficient` |
| `nominal` | none | `run_status:SCRIPTED_FIXTURE` | `confirmed` | INV-01, INV-02 | SIMULATED | `tests/unit/test_sim_cli.py::SimulationCliTests::test_scripted_fixture_publishes_complete_non_release_artifact` |
| `process-death` | process_death | `gate_reason:restart_invalidated_run` | `insufficient_evidence` | INV-07 | SIMULATED | `tests/unit/test_post_action_gate.py::FailClosedStateTests::test_restart_invalidates_run_correlation_fail_closed` |
| `resource-conflict` | resource_conflict | `run_status:NOT_EXECUTED` | `insufficient_evidence` | INV-09 | NOT_EXECUTED | gap: No claim or lease implementation exists; ADR-0009 is proposed and this row is the acceptance target for it. |
| `task-timeout` | task_timeout | `run_status:TIMED_OUT` | `insufficient_evidence` | INV-06 | SIMULATED | `tests/unit/test_sim_cli.py::SimulationCliTests::test_timeout_records_status_and_does_not_leave_partial_directory` |
