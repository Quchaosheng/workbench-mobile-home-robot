"""Fail-closed execution-controller behaviour for Issue #59."""

from __future__ import annotations

import copy
import sys
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "libs/contracts"),
    str(ROOT / "services/agent_runtime"),
]

from workbench_agent_runtime.execution_controller import (
    ActionAdapter,
    ExecutionController,
    ExecutionReasonCode,
    ExecutionReport,
    ExecutionState,
    StepExecutionRecord,
    StopRequestStatus,
)
from workbench_agent_runtime.policy_validator import PolicyReport, PolicyValidator
from workbench_contracts import (
    ActionOutcome,
    ActionResult,
    ActionType,
    DeviceState,
    DispatchState,
    SemanticAction,
    TaskGraph,
    TaskStep,
    VerificationResult,
)

POLICY_CONFIG = {
    "policy_version": "execution-controller-test-v1",
    "high_impact_actions": frozenset(),
}


def _action(
    action_id: str,
    action_type: ActionType = ActionType.OBSERVE,
    *,
    target_id: str | None = None,
    parameters: dict | None = None,
) -> SemanticAction:
    return SemanticAction(
        action_id=action_id,
        action_type=action_type,
        target_id=target_id,
        parameters={} if parameters is None else parameters,
    )


def _graph(
    *actions: SemanticAction,
    step_ids: tuple[str, ...] | None = None,
    dependencies: tuple[tuple[str, ...], ...] | None = None,
) -> TaskGraph:
    ids = step_ids or tuple(f"step-{index}" for index in range(1, len(actions) + 1))
    deps = dependencies or tuple(() if index == 0 else (ids[index - 1],) for index in range(len(actions)))
    return TaskGraph(
        task_id="task-execution-controller",
        goal="exercise deterministic orchestration",
        steps=[
            TaskStep(step_id=step_id, action=action, depends_on=list(step_dependencies))
            for step_id, action, step_dependencies in zip(ids, actions, deps, strict=True)
        ],
        planner="test",
        model_route="template",
    )


def _result(action_id: str, outcome: ActionOutcome = ActionOutcome.COMPLETED) -> ActionResult:
    device_state = {
        ActionOutcome.COMPLETED: DeviceState.CONFIRMED,
        ActionOutcome.FAILED: DeviceState.REJECTED,
        ActionOutcome.TIMEOUT: DeviceState.UNCONFIRMED,
        ActionOutcome.CANCELED: DeviceState.UNCONFIRMED,
        ActionOutcome.SAFE_STOP: DeviceState.STOPPED,
    }[outcome]
    return ActionResult(
        result_id=f"result-{action_id}",
        action_id=action_id,
        run_id="run-controller-test",
        outcome=outcome,
        dispatch_state=DispatchState.SENT,
        device_state=device_state,
        started_at="2026-08-28T00:00:00Z",
        ended_at="2026-08-28T00:00:01Z",
        evidence_refs=[f"evidence-{action_id}"],
    )


class FakeAdapter:
    def __init__(
        self,
        behaviours: dict[str, object] | None = None,
        *,
        before_return: Callable[[SemanticAction], None] | None = None,
    ) -> None:
        self.behaviours = {} if behaviours is None else behaviours
        self.before_return = before_return
        self.calls: list[str] = []
        self.action_payloads: list[dict] = []
        self.returned_results: list[ActionResult] = []

    def dispatch(self, action: SemanticAction) -> ActionResult:
        self.action_payloads.append(copy.deepcopy(action.model_dump(mode="json")))
        self.calls.append(action.action_id)
        if self.before_return is not None:
            self.before_return(action)
        behaviour = self.behaviours.get(action.action_id, _result(action.action_id))
        if isinstance(behaviour, BaseException):
            raise behaviour
        if callable(behaviour):
            behaviour = behaviour(action)
        if isinstance(behaviour, ActionResult):
            self.returned_results.append(behaviour)
        return behaviour  # type: ignore[return-value]


class PolicySpy(PolicyValidator):
    def __init__(self, *, high_impact_actions: frozenset[ActionType] = frozenset()) -> None:
        super().__init__(
            policy_config={
                "policy_version": POLICY_CONFIG["policy_version"],
                "high_impact_actions": high_impact_actions,
            }
        )
        self.calls: list[tuple[tuple[str, ...], frozenset[str]]] = []
        self.confirmation_objects: list[frozenset[str]] = []
        self.reports: list[PolicyReport] = []

    def check(
        self,
        graph: TaskGraph,
        *,
        confirmed_action_ids: frozenset[str] = frozenset(),
    ) -> PolicyReport:
        self.calls.append((tuple(step.step_id for step in graph.steps), confirmed_action_ids))
        self.confirmation_objects.append(confirmed_action_ids)
        report = super().check(graph, confirmed_action_ids=confirmed_action_ids)
        self.reports.append(report)
        return report


class MutatingPolicyValidator(PolicyValidator):
    def __init__(
        self,
        mutation: Callable[[SemanticAction], None],
        *,
        target_action_type: ActionType,
        high_impact_actions: frozenset[ActionType] = frozenset(),
    ) -> None:
        super().__init__(
            policy_config={
                "policy_version": POLICY_CONFIG["policy_version"],
                "high_impact_actions": high_impact_actions,
            }
        )
        self._mutation = mutation
        self._target_action_type = target_action_type

    def check(
        self,
        graph: TaskGraph,
        *,
        confirmed_action_ids: frozenset[str] = frozenset(),
    ) -> PolicyReport:
        report = super().check(graph, confirmed_action_ids=confirmed_action_ids)
        if len(graph.steps) == 1 and graph.steps[0].action.action_type is self._target_action_type:
            self._mutation(graph.steps[0].action)
        return report


class RaisingPolicyValidator(PolicyValidator):
    def __init__(self, *, raise_on_call: int) -> None:
        super().__init__(policy_config=POLICY_CONFIG)
        self._raise_on_call = raise_on_call
        self.calls = 0

    def check(
        self,
        graph: TaskGraph,
        *,
        confirmed_action_ids: frozenset[str] = frozenset(),
    ) -> PolicyReport:
        self.calls += 1
        if self.calls == self._raise_on_call:
            raise RuntimeError("policy exploded")
        return super().check(
            graph,
            confirmed_action_ids=confirmed_action_ids,
        )


class InvalidReportPolicyValidator(PolicyValidator):
    def check(
        self,
        graph: TaskGraph,
        *,
        confirmed_action_ids: frozenset[str] = frozenset(),
    ) -> PolicyReport:
        return object()  # type: ignore[return-value]


class MalformedPolicyReportValidator(PolicyValidator):
    def __init__(self, mutation: str) -> None:
        super().__init__(policy_config=POLICY_CONFIG)
        self._mutation = mutation

    def check(
        self,
        graph: TaskGraph,
        *,
        confirmed_action_ids: frozenset[str] = frozenset(),
    ) -> PolicyReport:
        report = super().check(
            graph,
            confirmed_action_ids=confirmed_action_ids,
        )
        decisions = report.decisions
        if self._mutation == "empty":
            return PolicyReport(())
        if self._mutation == "missing":
            return PolicyReport(decisions[:-1])
        if self._mutation == "duplicate":
            return PolicyReport((decisions[0], decisions[0]))
        if self._mutation == "wrong_id":
            return PolicyReport((replace(decisions[0], action_id="act-unrelated"), *decisions[1:]))
        if self._mutation == "wrong_order":
            return PolicyReport(tuple(reversed(decisions)))
        raise AssertionError(f"unknown report mutation: {self._mutation}")


def _controller(
    adapter: FakeAdapter,
    *,
    validator: PolicyValidator | None = None,
) -> ExecutionController:
    return ExecutionController(
        policy_validator=validator or PolicyValidator(policy_config=POLICY_CONFIG),
        adapter=adapter,
    )


@pytest.mark.parametrize("rejection", ["deny", "confirmation_required"])
def test_policy_rejection_prevents_first_dispatch(rejection: str) -> None:
    adapter = FakeAdapter()
    if rejection == "deny":
        graph = _graph(_action("act-denied", parameters={"joint_velocity": 1.0}))
        validator = PolicyValidator(policy_config=POLICY_CONFIG)
    else:
        graph = _graph(_action("act-confirm"))
        validator = PolicyValidator(
            policy_config={
                "policy_version": "execution-controller-test-v1",
                "high_impact_actions": frozenset({ActionType.OBSERVE}),
            }
        )

    report = _controller(adapter, validator=validator).execute(graph)

    assert isinstance(report, ExecutionReport)
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.policy_report is not None
    assert report.policy_report.is_valid is False
    assert adapter.calls == []


def test_graph_structure_errors_prevent_first_dispatch() -> None:
    first = _action("act-1")
    second = _action("act-2")
    cases: tuple[tuple[str, object], ...] = (
        ("non_task_graph", {"task_id": "not-typed"}),
        ("duplicate_step_id", _graph(first, second, step_ids=("same", "same"))),
        ("duplicate_action_id", _graph(first, first.model_copy(), step_ids=("one", "two"))),
        ("missing_dependency", _graph(first, dependencies=(("missing",),))),
        ("self_dependency", _graph(first, dependencies=(("step-1",),))),
        ("forward_dependency", _graph(first, second, dependencies=(("step-2",), ()))),
        (
            "cyclic_dependency",
            _graph(first, second, dependencies=(("step-2",), ("step-1",))),
        ),
    )

    for name, graph in cases:
        adapter = FakeAdapter()
        report = _controller(adapter).execute(graph)  # type: ignore[arg-type]
        assert isinstance(report, ExecutionReport), name
        assert report.terminal_state is ExecutionState.FAILED, name
        assert report.reason_code in {
            ExecutionReasonCode.INVALID_INPUT,
            ExecutionReasonCode.INVALID_GRAPH,
        }, name
        assert report.details, name
        assert adapter.calls == [], name


def test_valid_graph_dispatches_strictly_in_order_and_records_transitions() -> None:
    actions = (_action("act-1"), _action("act-2"), _action("act-3"))
    graph = _graph(*actions)
    adapter = FakeAdapter()
    validator = PolicySpy()

    report = _controller(adapter, validator=validator).execute(graph)

    assert report.terminal_state is ExecutionState.SUCCEEDED
    assert report.reason_code is ExecutionReasonCode.SEQUENCE_SUCCEEDED
    assert adapter.calls == [action.action_id for action in actions]
    assert [record.action_id for record in report.records] == adapter.calls
    assert all(
        record.transitions
        == (
            ExecutionState.PENDING,
            ExecutionState.DISPATCHED,
            ExecutionState.SUCCEEDED,
        )
        for record in report.records
    )
    assert [steps for steps, _ in validator.calls] == [
        ("step-1", "step-2", "step-3"),
        ("step-1",),
        ("step-2",),
        ("step-3",),
    ]


def test_action_is_revalidated_immediately_before_dispatch() -> None:
    graph = _graph(_action("act-1"), _action("act-2"))
    confirmed_action_ids = frozenset({"some-other-action"})

    def mutate_later_action(action: SemanticAction) -> None:
        if action.action_id == "act-1":
            graph.steps[1].action.parameters["joint_velocity"] = 1.0

    adapter = FakeAdapter(before_return=mutate_later_action)
    validator = PolicySpy()

    report = _controller(adapter, validator=validator).execute(
        graph,
        confirmed_action_ids=confirmed_action_ids,
    )

    assert adapter.calls == ["act-1"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_GRAPH
    assert report.records[1].reason_code is ExecutionReasonCode.INVALID_GRAPH
    assert report.records[1].transitions == (ExecutionState.PENDING, ExecutionState.FAILED)
    assert report.details == ("TaskGraph mutated during execution",)
    assert [steps for steps, _ in validator.calls] == [
        ("step-1", "step-2"),
        ("step-1",),
        ("step-2",),
    ]
    assert all(item == confirmed_action_ids for item in validator.confirmation_objects)
    assert all(item is not confirmed_action_ids for item in validator.confirmation_objects)


def test_action_id_mutated_after_preflight_cannot_redispatch() -> None:
    graph = _graph(_action("act-1"), _action("act-2"))

    def duplicate_later_action_id(action: SemanticAction) -> None:
        if action.action_id == "act-1":
            graph.steps[1].action.action_id = action.action_id

    adapter = FakeAdapter(before_return=duplicate_later_action_id)

    report = _controller(adapter).execute(graph)

    assert (
        adapter.calls,
        report.terminal_state,
        report.reason_code,
    ) == (
        ["act-1"],
        ExecutionState.FAILED,
        ExecutionReasonCode.DUPLICATE_ACTION,
    )
    duplicate_record = report.records[1]
    assert duplicate_record.action_id == "act-2"
    assert duplicate_record.transitions == (
        ExecutionState.PENDING,
        ExecutionState.FAILED,
    )
    assert duplicate_record.reason_code is ExecutionReasonCode.DUPLICATE_ACTION
    assert duplicate_record.details == ("action_id already dispatched: act-1",)
    assert duplicate_record.result is None


@pytest.mark.parametrize(
    ("outcome", "state", "reason"),
    [
        (ActionOutcome.FAILED, ExecutionState.FAILED, ExecutionReasonCode.ACTION_FAILED),
        (ActionOutcome.TIMEOUT, ExecutionState.TIMED_OUT, ExecutionReasonCode.ACTION_TIMED_OUT),
        (ActionOutcome.CANCELED, ExecutionState.CANCELLED, ExecutionReasonCode.ACTION_CANCELLED),
        (ActionOutcome.SAFE_STOP, ExecutionState.STOPPED, ExecutionReasonCode.ACTION_STOPPED),
    ],
)
def test_terminal_action_result_stops_later_steps(
    outcome: ActionOutcome,
    state: ExecutionState,
    reason: ExecutionReasonCode,
) -> None:
    graph = _graph(_action("act-terminal"), _action("act-later"))
    adapter = FakeAdapter({"act-terminal": _result("act-terminal", outcome)})

    report = _controller(adapter).execute(graph)

    assert adapter.calls == ["act-terminal"]
    assert report.terminal_state is state
    assert report.reason_code is reason
    assert report.records[0].transitions == (
        ExecutionState.PENDING,
        ExecutionState.DISPATCHED,
        state,
    )
    assert report.records[1].transitions == (ExecutionState.PENDING,)


@pytest.mark.parametrize(
    ("error", "state", "reason"),
    [
        (TimeoutError("adapter deadline"), ExecutionState.TIMED_OUT, ExecutionReasonCode.ADAPTER_TIMEOUT),
        (RuntimeError("adapter exploded"), ExecutionState.FAILED, ExecutionReasonCode.ADAPTER_EXCEPTION),
    ],
)
def test_adapter_exceptions_become_typed_terminal_records(
    error: BaseException,
    state: ExecutionState,
    reason: ExecutionReasonCode,
) -> None:
    graph = _graph(_action("act-error"), _action("act-later"))
    adapter = FakeAdapter({"act-error": error})

    report = _controller(adapter).execute(graph)

    assert adapter.calls == ["act-error"]
    assert report.terminal_state is state
    assert report.reason_code is reason
    assert isinstance(report.records[0], StepExecutionRecord)
    assert report.records[0].result is None
    assert report.records[0].reason_code is reason
    assert report.records[1].transitions == (ExecutionState.PENDING,)


def test_unknown_navigate_waypoint_is_an_explicit_adapter_failure() -> None:
    graph = _graph(
        _action(
            "act-navigate-unknown",
            ActionType.NAVIGATE,
            target_id="not-configured",
        )
    )
    unknown_waypoint = ActionResult(
        result_id="result-navigate-unknown",
        action_id="act-navigate-unknown",
        run_id="run-controller-test",
        outcome=ActionOutcome.FAILED,
        dispatch_state=DispatchState.SENT,
        device_state=DeviceState.REJECTED,
        error_reason="unknown_waypoint",
        started_at="2026-09-03T00:00:00Z",
        ended_at="2026-09-03T00:00:01Z",
    )
    adapter = FakeAdapter({"act-navigate-unknown": unknown_waypoint})

    report = _controller(adapter).execute(graph)

    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.ACTION_FAILED
    assert adapter.calls == ["act-navigate-unknown"]
    assert adapter.action_payloads == [
        {
            "action_id": "act-navigate-unknown",
            "action_type": "navigate",
            "target_id": "not-configured",
            "parameters": {},
        }
    ]
    assert report.records[0].result is not None
    assert report.records[0].result.error_reason == "unknown_waypoint"


def test_navigate_does_not_change_independent_stop_preemption() -> None:
    graph = _graph(
        _action("act-navigate", ActionType.NAVIGATE, target_id="workbench_home"),
        _action("act-after-navigate"),
    )
    stop_action = _action("act-stop", ActionType.STOP)
    controller: ExecutionController

    def request_stop_after_navigate(action: SemanticAction) -> None:
        if action.action_id == "act-navigate":
            assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED

    adapter = FakeAdapter(
        {stop_action.action_id: _result(stop_action.action_id)},
        before_return=request_stop_after_navigate,
    )
    controller = _controller(adapter)

    report = controller.execute(graph)

    assert adapter.calls == ["act-navigate", "act-stop"]
    assert report.terminal_state is ExecutionState.STOPPED
    assert report.reason_code is ExecutionReasonCode.STOP_DISPATCHED
    assert report.records[1].transitions == (ExecutionState.PENDING,)
    assert report.records[-1].action_id == "act-stop"


@pytest.mark.parametrize(
    ("action_type", "target_id"),
    [
        (ActionType.OPEN, "washer_door_fixture"),
        (ActionType.CLOSE, "dishwasher_rack_fixture"),
    ],
    ids=["open", "close"],
)
def test_open_and_close_forward_only_typed_intent(action_type: ActionType, target_id: str) -> None:
    action = _action(f"act-{action_type.value}", action_type, target_id=target_id)
    adapter = FakeAdapter()

    report = _controller(adapter).execute(_graph(action))

    assert report.terminal_state is ExecutionState.SUCCEEDED
    assert adapter.calls == [action.action_id]
    assert adapter.action_payloads == [
        {
            "action_id": action.action_id,
            "action_type": action_type.value,
            "target_id": target_id,
            "parameters": {},
        }
    ]


@pytest.mark.parametrize(
    ("action_type", "target_id"),
    [
        (ActionType.OPEN, "not-configured-door"),
        (ActionType.CLOSE, "not-configured-rack"),
    ],
    ids=["open", "close"],
)
def test_unknown_articulated_target_is_an_explicit_adapter_failure(
    action_type: ActionType,
    target_id: str,
) -> None:
    action_id = f"act-{action_type.value}-unknown"
    graph = _graph(_action(action_id, action_type, target_id=target_id))
    unknown_target = ActionResult(
        result_id=f"result-{action_id}",
        action_id=action_id,
        run_id="run-controller-test",
        outcome=ActionOutcome.FAILED,
        dispatch_state=DispatchState.SENT,
        device_state=DeviceState.REJECTED,
        error_reason="unknown_articulated_target",
        started_at="2026-09-05T00:00:00Z",
        ended_at="2026-09-05T00:00:01Z",
    )
    adapter = FakeAdapter({action_id: unknown_target})

    report = _controller(adapter).execute(graph)

    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.ACTION_FAILED
    assert adapter.calls == [action_id]
    assert report.records[0].result is not None
    assert report.records[0].result.error_reason == "unknown_articulated_target"


@pytest.mark.parametrize("action_type", [ActionType.OPEN, ActionType.CLOSE], ids=lambda item: item.value)
def test_open_and_close_preserve_independent_stop_preemption(action_type: ActionType) -> None:
    action = _action(f"act-{action_type.value}", action_type, target_id="fixture-target")
    stop_action = _action("act-stop", ActionType.STOP)
    controller: ExecutionController

    def request_stop_after_action(dispatched: SemanticAction) -> None:
        if dispatched.action_id == action.action_id:
            assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED

    adapter = FakeAdapter(
        {stop_action.action_id: _result(stop_action.action_id)},
        before_return=request_stop_after_action,
    )
    controller = _controller(adapter)

    report = controller.execute(_graph(action, _action("act-after")))

    assert adapter.calls == [action.action_id, stop_action.action_id]
    assert report.terminal_state is ExecutionState.STOPPED
    assert report.reason_code is ExecutionReasonCode.STOP_DISPATCHED
    assert report.records[1].transitions == (ExecutionState.PENDING,)
    assert report.records[-1].action_id == stop_action.action_id


@pytest.mark.parametrize(
    "invalid_result",
    [object(), _result("wrong-action-id")],
    ids=["non_action_result", "mismatched_action_id"],
)
def test_invalid_adapter_result_fails_closed(invalid_result: object) -> None:
    graph = _graph(_action("act-invalid"), _action("act-later"))
    adapter = FakeAdapter({"act-invalid": invalid_result})

    report = _controller(adapter).execute(graph)

    assert adapter.calls == ["act-invalid"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_ADAPTER_RESULT
    assert report.records[0].transitions == (
        ExecutionState.PENDING,
        ExecutionState.DISPATCHED,
        ExecutionState.FAILED,
    )
    assert report.records[0].result is None
    assert report.records[1].transitions == (ExecutionState.PENDING,)


def test_stop_request_preempts_remaining_steps_and_is_idempotent() -> None:
    graph = _graph(_action("act-first"), _action("act-pending"))
    stop_action = _action("act-stop", ActionType.STOP, parameters={"reason": "operator request"})
    stop_result = _result(stop_action.action_id)
    request_statuses: list[StopRequestStatus] = []
    controller: ExecutionController

    def request_stop_after_first_dispatch(action: SemanticAction) -> None:
        if action.action_id == "act-first":
            request_statuses.append(controller.request_stop(stop_action))
            request_statuses.append(controller.request_stop(stop_action))

    adapter = FakeAdapter(
        {stop_action.action_id: stop_result},
        before_return=request_stop_after_first_dispatch,
    )
    validator = PolicySpy(high_impact_actions=frozenset({ActionType.STOP}))
    controller = _controller(adapter, validator=validator)

    report = controller.execute(
        graph,
        confirmed_action_ids=frozenset({stop_action.action_id}),
    )

    assert request_statuses == [StopRequestStatus.ACCEPTED, StopRequestStatus.DUPLICATE]
    assert adapter.calls == ["act-first", "act-stop"]
    assert report.terminal_state is ExecutionState.STOPPED
    assert report.reason_code is ExecutionReasonCode.STOP_DISPATCHED
    assert report.records[1].transitions == (ExecutionState.PENDING,)
    stop_record = next(record for record in report.records if record.action_id == stop_action.action_id)
    assert stop_record.transitions == (
        ExecutionState.PENDING,
        ExecutionState.DISPATCHED,
        ExecutionState.SUCCEEDED,
    )
    assert stop_record.result == stop_result
    assert stop_record.result is not stop_result
    assert controller.request_stop(stop_action) is StopRequestStatus.DUPLICATE
    assert adapter.calls.count(stop_action.action_id) == 1


def test_stop_action_id_mutated_after_revalidation_cannot_redispatch() -> None:
    graph = _graph(_action("act-first"), _action("act-pending"))
    stop_action = _action("act-stop", ActionType.STOP)
    controller: ExecutionController

    def request_stop_after_first_dispatch(action: SemanticAction) -> None:
        if action.action_id == "act-first":
            assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED

    adapter = FakeAdapter(before_return=request_stop_after_first_dispatch)
    validator = MutatingPolicyValidator(
        lambda action: setattr(action, "action_id", "act-first"),
        target_action_type=ActionType.STOP,
    )
    controller = _controller(adapter, validator=validator)

    report = controller.execute(graph)

    assert adapter.calls == ["act-first"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.records[1].transitions == (ExecutionState.PENDING,)
    stop_record = report.records[2]
    assert stop_record.action_id == "act-stop"
    assert stop_record.transitions == (
        ExecutionState.PENDING,
        ExecutionState.FAILED,
    )
    assert stop_record.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.details == ("PolicyValidator mutated policy input",)
    assert stop_record.result is None


def test_duplicate_execute_request_never_redispatches_action_ids() -> None:
    graph = _graph(_action("act-1"), _action("act-2"))
    adapter = FakeAdapter()
    controller = _controller(adapter)

    first = controller.execute(graph)
    duplicate = controller.execute(graph)
    repeated_duplicate = controller.execute(graph)

    assert first.terminal_state is ExecutionState.SUCCEEDED
    assert adapter.calls == ["act-1", "act-2"]
    assert duplicate == repeated_duplicate
    assert duplicate.terminal_state is ExecutionState.FAILED
    assert duplicate.reason_code is ExecutionReasonCode.DUPLICATE_ACTION
    assert all(record.transitions == (ExecutionState.PENDING,) for record in duplicate.records)


def test_controller_does_not_mutate_task_graph_or_actions() -> None:
    actions = (
        _action("act-1", parameters={"attributes": ["presence"]}),
        _action("act-2"),
    )
    graph = _graph(*actions)
    action_result = _result("act-1")
    adapter = FakeAdapter({"act-1": action_result})
    validator = PolicySpy()
    graph_before = graph.model_dump(mode="json")
    action_before = [action.model_dump(mode="json") for action in actions]
    parameters_before = copy.deepcopy([action.parameters for action in actions])
    result_before = action_result.model_dump(mode="json")
    preflight_report = validator.check(graph)

    report = _controller(adapter, validator=validator).execute(graph)

    assert report.terminal_state is ExecutionState.SUCCEEDED
    assert graph.model_dump(mode="json") == graph_before
    assert [action.model_dump(mode="json") for action in actions] == action_before
    assert [action.parameters for action in actions] == parameters_before
    assert action_result.model_dump(mode="json") == result_before
    assert preflight_report == validator.reports[0]
    assert report.policy_report == validator.reports[1]


def test_controller_report_is_orchestration_only() -> None:
    result = _result("act-1")
    adapter = FakeAdapter({"act-1": result})

    report = _controller(adapter).execute(_graph(_action("act-1")))

    assert isinstance(report, ExecutionReport)
    assert not isinstance(report, VerificationResult)
    assert report.records[0].result == result
    assert report.records[0].result is not result
    report_fields = {item.name for item in fields(report)}
    assert report_fields.isdisjoint(
        {
            "world_state",
            "world_event",
            "verification",
            "verified_complete",
            "task_complete",
            "motion_complete",
            "mcu_complete",
            "simulation_complete",
            "physical_execution",
        }
    )
    with pytest.raises(FrozenInstanceError):
        report.task_id = "mutated"  # type: ignore[misc]


def test_execution_controller_public_exports_are_available() -> None:
    import workbench_agent_runtime as package
    import workbench_agent_runtime.execution_controller as module

    expected = {
        "ActionAdapter",
        "ExecutionController",
        "ExecutionReasonCode",
        "ExecutionReport",
        "ExecutionState",
        "StepExecutionRecord",
        "StopRequestStatus",
    }
    assert expected <= set(package.__all__)
    assert all(getattr(package, name) is getattr(module, name) for name in expected)
    assert isinstance(FakeAdapter(), ActionAdapter)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda action: setattr(action, "action_id", "act-fresh-unconfirmed"),
        lambda action: action.parameters.update({"joint_velocity": 1.0}),
        lambda action: setattr(action, "action_type", ActionType.STOP),
    ],
    ids=["fresh_id", "parameters", "action_type"],
)
def test_dispatch_policy_input_mutation_fails_closed(
    mutation: Callable[[SemanticAction], None],
) -> None:
    graph = _graph(_action("act-1"), _action("act-2"))
    adapter = FakeAdapter()
    validator = MutatingPolicyValidator(
        mutation,
        target_action_type=ActionType.OBSERVE,
        high_impact_actions=frozenset({ActionType.OBSERVE}),
    )

    report = _controller(adapter, validator=validator).execute(
        graph,
        confirmed_action_ids=frozenset({"act-1", "act-2"}),
    )

    assert adapter.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.details == ("PolicyValidator mutated policy input",)
    assert report.records[0].transitions == (
        ExecutionState.PENDING,
        ExecutionState.FAILED,
    )
    assert report.records[0].reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.records[1].transitions == (ExecutionState.PENDING,)


@pytest.mark.parametrize(
    ("raise_on_call", "first_transitions"),
    [
        (1, (ExecutionState.PENDING,)),
        (2, (ExecutionState.PENDING, ExecutionState.FAILED)),
    ],
    ids=["full_preflight", "dispatch_revalidation"],
)
def test_policy_validator_exception_fails_closed(
    raise_on_call: int,
    first_transitions: tuple[ExecutionState, ...],
) -> None:
    adapter = FakeAdapter()
    validator = RaisingPolicyValidator(raise_on_call=raise_on_call)

    report = _controller(adapter, validator=validator).execute(_graph(_action("act-1"), _action("act-2")))

    assert adapter.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.details == ("PolicyValidator.check raised",)
    assert report.records[0].transitions == first_transitions
    assert report.records[1].transitions == (ExecutionState.PENDING,)


def test_invalid_policy_report_fails_closed() -> None:
    adapter = FakeAdapter()
    validator = InvalidReportPolicyValidator(policy_config=POLICY_CONFIG)

    report = _controller(adapter, validator=validator).execute(_graph(_action("act-1")))

    assert adapter.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.records[0].transitions == (ExecutionState.PENDING,)
    assert report.details == ("PolicyValidator.check returned object",)


@pytest.mark.parametrize(
    "mutation",
    ["empty", "missing", "duplicate", "wrong_id", "wrong_order"],
)
def test_incomplete_or_mismatched_policy_report_fails_closed(mutation: str) -> None:
    adapter = FakeAdapter()
    validator = MalformedPolicyReportValidator(mutation)

    report = _controller(adapter, validator=validator).execute(_graph(_action("act-1"), _action("act-2")))

    assert adapter.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.records[0].transitions == (ExecutionState.PENDING,)
    assert report.records[1].transitions == (ExecutionState.PENDING,)
    assert report.details == ("PolicyValidator.check decisions do not exactly match policy input",)


def test_invalid_mutated_task_graph_snapshot_fails_before_dispatch() -> None:
    graph = _graph(_action("act-1"))
    graph.steps.clear()
    adapter = FakeAdapter()

    report = _controller(adapter).execute(graph)

    assert adapter.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_GRAPH
    assert report.records == ()
    assert report.details == ("TaskGraph snapshot validation failed: ValidationError",)


def test_string_action_type_is_not_normalized_at_typed_boundary() -> None:
    graph = _graph(_action("act-string-type"))
    graph.steps[0].action.action_type = "observe"  # type: ignore[assignment]
    adapter = FakeAdapter()
    validator = PolicySpy()

    report = _controller(adapter, validator=validator).execute(graph)

    assert adapter.calls == []
    assert validator.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_GRAPH
    assert report.details == ("TaskGraph snapshot validation failed: PydanticSerializationError",)


def test_python_payload_type_is_preserved_for_policy_validation() -> None:
    graph = _graph(
        _action(
            "act-confirm",
            ActionType.ASK_CONFIRM,
            parameters={"question": "continue?"},
        )
    )
    graph.steps[0].action.parameters["question"] = Decimal("7.5")
    adapter = FakeAdapter()
    validator = PolicyValidator(policy_config=POLICY_CONFIG)

    direct_report = validator.check(graph)
    report = _controller(adapter, validator=validator).execute(graph)

    assert direct_report.is_valid is False
    assert adapter.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.policy_report is not None
    assert report.policy_report.is_valid is False


@pytest.mark.parametrize(
    "mutation_name",
    ["remove", "append", "replace", "dependency", "action"],
)
def test_live_task_graph_inventory_mutation_fails_closed(
    mutation_name: str,
) -> None:
    graph = _graph(_action("act-1"), _action("act-2"))
    controller: ExecutionController

    def mutate_graph_after_first_dispatch(action: SemanticAction) -> None:
        if action.action_id != "act-1":
            return
        if mutation_name == "remove":
            graph.steps.clear()
        elif mutation_name == "append":
            graph.steps.append(
                TaskStep(
                    step_id="step-3",
                    action=_action("act-3"),
                    depends_on=["step-2"],
                )
            )
        elif mutation_name == "replace":
            graph.steps[1] = TaskStep(
                step_id="step-2",
                action=_action("act-replaced"),
                depends_on=["step-1"],
            )
        elif mutation_name == "dependency":
            graph.steps[1].depends_on.clear()
        else:
            graph.steps[1].action.target_id = "changed-target"

    adapter = FakeAdapter(before_return=mutate_graph_after_first_dispatch)
    controller = _controller(adapter)

    report = controller.execute(graph)

    assert adapter.calls == ["act-1"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_GRAPH
    assert report.details == ("TaskGraph mutated during execution",)
    assert report.records[0].transitions == (
        ExecutionState.PENDING,
        ExecutionState.DISPATCHED,
        ExecutionState.SUCCEEDED,
    )
    assert report.records[1].transitions == (
        ExecutionState.PENDING,
        ExecutionState.FAILED,
    )
    assert report.records[1].reason_code is ExecutionReasonCode.INVALID_GRAPH


def test_live_task_graph_mutation_after_last_step_cannot_report_success() -> None:
    graph = _graph(_action("act-1"))

    def append_after_dispatch(action: SemanticAction) -> None:
        graph.steps.append(
            TaskStep(
                step_id="step-added",
                action=_action("act-added"),
                depends_on=["step-1"],
            )
        )

    adapter = FakeAdapter(before_return=append_after_dispatch)

    report = _controller(adapter).execute(graph)

    assert adapter.calls == ["act-1"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_GRAPH
    assert report.details == ("TaskGraph mutated during execution",)
    assert len(report.records) == 1
    assert report.records[0].transitions[-1] is ExecutionState.SUCCEEDED


def test_shared_dependency_graph_uses_stable_inventory_order() -> None:
    graph = _graph(
        _action("act-1"),
        _action("act-2"),
        _action("act-3"),
        _action("act-4"),
        dependencies=(
            (),
            ("step-1",),
            ("step-1",),
            ("step-2", "step-3"),
        ),
    )
    adapter = FakeAdapter()

    report = _controller(adapter).execute(graph)

    assert report.terminal_state is ExecutionState.SUCCEEDED
    assert report.reason_code is ExecutionReasonCode.SEQUENCE_SUCCEEDED
    assert adapter.calls == ["act-1", "act-2", "act-3", "act-4"]


@pytest.mark.parametrize(
    "mutation_name",
    ["action_type", "parameters", "action_id"],
)
def test_stop_request_uses_independent_contract_snapshot(
    mutation_name: str,
) -> None:
    graph = _graph(_action("act-first"), _action("act-pending"))
    stop_action = _action("act-stop", ActionType.STOP)
    original_payload = copy.deepcopy(stop_action.model_dump(mode="json"))
    controller: ExecutionController

    def request_and_mutate_stop(action: SemanticAction) -> None:
        if action.action_id != "act-first":
            return
        assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED
        if mutation_name == "action_type":
            stop_action.action_type = ActionType.OBSERVE
        elif mutation_name == "parameters":
            stop_action.parameters["joint_velocity"] = 1.0
        else:
            stop_action.action_id = "act-mutated"

    adapter = FakeAdapter(before_return=request_and_mutate_stop)
    controller = _controller(adapter)

    report = controller.execute(graph)

    assert adapter.calls == ["act-first", "act-stop"]
    assert adapter.action_payloads[-1] == original_payload
    assert report.terminal_state is ExecutionState.STOPPED
    assert report.reason_code is ExecutionReasonCode.STOP_DISPATCHED
    stop_record = report.records[2]
    assert stop_record.action_id == "act-stop"
    assert stop_record.result is not None
    assert stop_record.result.action_id == "act-stop"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda action: action.parameters.update({"joint_velocity": 1.0}),
        lambda action: setattr(action, "action_type", ActionType.OBSERVE),
    ],
    ids=["parameters", "action_type"],
)
def test_stop_policy_input_mutation_fails_closed(
    mutation: Callable[[SemanticAction], None],
) -> None:
    graph = _graph(_action("act-first"), _action("act-pending"))
    stop_action = _action("act-stop", ActionType.STOP)
    controller: ExecutionController

    def request_stop(action: SemanticAction) -> None:
        if action.action_id == "act-first":
            assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED

    adapter = FakeAdapter(before_return=request_stop)
    validator = MutatingPolicyValidator(
        mutation,
        target_action_type=ActionType.STOP,
    )
    controller = _controller(adapter, validator=validator)

    report = controller.execute(graph)

    assert adapter.calls == ["act-first"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.POLICY_REJECTED
    stop_record = report.records[2]
    assert stop_record.transitions == (
        ExecutionState.PENDING,
        ExecutionState.FAILED,
    )
    assert stop_record.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.details == ("PolicyValidator mutated policy input",)


def test_stop_policy_rejection_prevents_stop_dispatch() -> None:
    graph = _graph(_action("act-first"), _action("act-pending"))
    stop_action = _action("act-stop", ActionType.STOP)
    controller: ExecutionController

    def request_stop(action: SemanticAction) -> None:
        if action.action_id == "act-first":
            assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED

    adapter = FakeAdapter(before_return=request_stop)
    validator = PolicyValidator(
        policy_config={
            "policy_version": POLICY_CONFIG["policy_version"],
            "high_impact_actions": frozenset({ActionType.STOP}),
        }
    )
    controller = _controller(adapter, validator=validator)

    report = controller.execute(graph)

    assert adapter.calls == ["act-first"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.records[1].transitions == (ExecutionState.PENDING,)
    assert report.records[2].transitions == (
        ExecutionState.PENDING,
        ExecutionState.FAILED,
    )


def test_stop_policy_exception_prevents_stop_dispatch() -> None:
    graph = _graph(_action("act-first"))
    stop_action = _action("act-stop", ActionType.STOP)
    controller: ExecutionController

    def request_stop(action: SemanticAction) -> None:
        assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED

    adapter = FakeAdapter(before_return=request_stop)
    validator = RaisingPolicyValidator(raise_on_call=3)
    controller = _controller(adapter, validator=validator)

    report = controller.execute(graph)

    assert adapter.calls == ["act-first"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.POLICY_REJECTED
    assert report.details == ("PolicyValidator.check raised",)
    assert report.records[1].reason_code is ExecutionReasonCode.POLICY_REJECTED


def test_stop_requested_by_last_step_dispatches_at_final_boundary() -> None:
    graph = _graph(_action("act-last"))
    stop_action = _action("act-stop", ActionType.STOP)
    controller: ExecutionController

    def request_stop(action: SemanticAction) -> None:
        if action.action_id == "act-last":
            assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED

    adapter = FakeAdapter(before_return=request_stop)
    controller = _controller(adapter)

    report = controller.execute(graph)

    assert adapter.calls == ["act-last", "act-stop"]
    assert report.terminal_state is ExecutionState.STOPPED
    assert report.reason_code is ExecutionReasonCode.STOP_DISPATCHED
    assert [record.action_id for record in report.records] == ["act-last", "act-stop"]


def test_corrupted_pending_stop_type_fails_closed_at_boundary() -> None:
    stop_action = _action("act-stop", ActionType.STOP)
    adapter = FakeAdapter()
    controller = _controller(adapter)
    assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED
    assert controller._pending_stop is not None
    controller._pending_stop.action_type = ActionType.OBSERVE

    report = controller.execute(_graph(_action("act-pending")))

    assert adapter.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_INPUT
    assert report.records[0].transitions == (ExecutionState.PENDING,)
    assert report.records[1].reason_code is ExecutionReasonCode.INVALID_INPUT
    assert report.details == ("pending STOP snapshot is invalid",)


@pytest.mark.parametrize(
    "mutation_name",
    [
        "outcome",
        "dispatch_state",
        "device_state",
        "outcome_string",
        "dispatch_state_string",
        "device_state_string",
    ],
)
def test_mutated_action_result_is_revalidated_and_rejected(
    mutation_name: str,
) -> None:
    result = _result("act-result")
    if mutation_name in {"outcome", "outcome_string"}:
        result.outcome = "bogus" if mutation_name == "outcome" else "completed"  # type: ignore[assignment]
    elif mutation_name in {"dispatch_state", "dispatch_state_string"}:
        result.dispatch_state = (
            DispatchState.SEND_FAILED if mutation_name == "dispatch_state" else "sent"  # type: ignore[assignment]
        )
    else:
        result.device_state = (
            DeviceState.UNCONFIRMED if mutation_name == "device_state" else "confirmed"  # type: ignore[assignment]
        )
    adapter = FakeAdapter({"act-result": result})

    report = _controller(adapter).execute(_graph(_action("act-result")))

    assert adapter.calls == ["act-result"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_ADAPTER_RESULT
    assert report.records[0].transitions == (
        ExecutionState.PENDING,
        ExecutionState.DISPATCHED,
        ExecutionState.FAILED,
    )
    assert report.records[0].reason_code is ExecutionReasonCode.INVALID_ADAPTER_RESULT
    assert report.records[0].result is None
    assert report.details == ()


def test_report_keeps_independent_action_result_snapshot() -> None:
    result = _result("act-result")
    adapter = FakeAdapter({"act-result": result})

    report = _controller(adapter).execute(_graph(_action("act-result")))
    saved_result = report.records[0].result
    assert saved_result is not None
    saved_payload = saved_result.model_dump(mode="json")

    result.action_id = "mutated-after-return"
    result.outcome = "bogus"  # type: ignore[assignment]
    result.evidence_refs.append("late-mutation")

    assert report.terminal_state is ExecutionState.SUCCEEDED
    assert report.reason_code is ExecutionReasonCode.SEQUENCE_SUCCEEDED
    assert report.records[0].result is saved_result
    assert saved_result is not result
    assert saved_result.model_dump(mode="json") == saved_payload


def test_adapter_action_mutation_cannot_change_expected_dispatch_id() -> None:
    def mutate_dispatched_action(action: SemanticAction) -> None:
        action.action_id = "act-mutated-by-adapter"

    adapter = FakeAdapter(before_return=mutate_dispatched_action)

    report = _controller(adapter).execute(_graph(_action("act-original")))

    assert adapter.calls == ["act-original"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_ADAPTER_RESULT
    assert report.records[0].action_id == "act-original"
    assert report.records[0].result is None


def test_constructor_rejects_invalid_dependencies() -> None:
    adapter = FakeAdapter()
    validator = PolicyValidator(policy_config=POLICY_CONFIG)

    with pytest.raises(TypeError, match="policy_validator"):
        ExecutionController(policy_validator=object(), adapter=adapter)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="adapter"):
        ExecutionController(policy_validator=validator, adapter=object())  # type: ignore[arg-type]


def test_request_stop_rejects_invalid_duplicate_and_competing_requests() -> None:
    controller = _controller(FakeAdapter())
    malformed_stop = _action("act-malformed", ActionType.STOP)
    malformed_stop.action_id = 7  # type: ignore[assignment]

    assert controller.request_stop(object()) is StopRequestStatus.REJECTED  # type: ignore[arg-type]
    assert controller.request_stop(_action("act-observe")) is StopRequestStatus.REJECTED
    string_stop = _action("act-string-stop", ActionType.STOP)
    string_stop.action_type = "stop"  # type: ignore[assignment]
    assert controller.request_stop(string_stop) is StopRequestStatus.REJECTED
    assert controller.request_stop(malformed_stop) is StopRequestStatus.REJECTED

    accepted = _action("act-stop", ActionType.STOP)
    assert controller.request_stop(accepted) is StopRequestStatus.ACCEPTED
    assert controller.request_stop(_action("act-other-stop", ActionType.STOP)) is StopRequestStatus.REJECTED
    assert controller.request_stop(_action("act-stop", ActionType.STOP)) is StopRequestStatus.DUPLICATE


def test_request_stop_rejects_already_dispatched_action_id() -> None:
    controller = _controller(FakeAdapter())
    report = controller.execute(_graph(_action("act-used")))
    assert report.terminal_state is ExecutionState.SUCCEEDED

    status = controller.request_stop(_action("act-used", ActionType.STOP))

    assert status is StopRequestStatus.DUPLICATE


@pytest.mark.parametrize(
    "confirmed_action_ids",
    [
        ["act-confirm"],
        {"act-confirm": True},
        "act-confirm",
        {7},
        {" "},
    ],
    ids=["list", "dict", "string", "non_string_member", "blank_member"],
)
def test_malformed_confirmation_collection_fails_before_policy(
    confirmed_action_ids: object,
) -> None:
    adapter = FakeAdapter()
    validator = PolicySpy(high_impact_actions=frozenset({ActionType.OBSERVE}))

    report = _controller(adapter, validator=validator).execute(
        _graph(_action("act-confirm")),
        confirmed_action_ids=confirmed_action_ids,  # type: ignore[arg-type]
    )

    assert adapter.calls == []
    assert validator.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_INPUT
    assert report.details == ("confirmed_action_ids must be a set or frozenset of non-blank strings",)


def test_confirmation_snapshot_exception_fails_closed() -> None:
    class ExplodingConfirmations(set[str]):
        def __iter__(self) -> object:
            raise RuntimeError("confirmation iteration exploded")

    adapter = FakeAdapter()
    validator = PolicySpy()

    report = _controller(adapter, validator=validator).execute(
        _graph(_action("act-1")),
        confirmed_action_ids=ExplodingConfirmations({"act-1"}),  # type: ignore[arg-type]
    )

    assert adapter.calls == []
    assert validator.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_INPUT
    assert report.records[0].transitions == (ExecutionState.PENDING,)
    assert report.details == ("confirmed_action_ids must be a set or frozenset of non-blank strings",)


def test_pending_stop_does_not_hide_live_graph_mutation() -> None:
    graph = _graph(_action("act-first"), _action("act-pending"))
    stop_action = _action("act-stop", ActionType.STOP)
    controller: ExecutionController

    def request_stop_and_mutate_graph(action: SemanticAction) -> None:
        if action.action_id == "act-first":
            assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED
            graph.steps[1].action.target_id = "changed-target"

    adapter = FakeAdapter(before_return=request_stop_and_mutate_graph)
    controller = _controller(adapter)

    report = controller.execute(graph)

    assert adapter.calls == ["act-first"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.INVALID_GRAPH
    assert report.details == ("TaskGraph mutated during execution",)
    assert len(report.records) == 2
    assert report.records[1].transitions == (
        ExecutionState.PENDING,
        ExecutionState.FAILED,
    )


def test_terminal_stop_result_is_reported_without_ordinary_redispatch() -> None:
    graph = _graph(_action("act-first"), _action("act-pending"))
    stop_action = _action("act-stop", ActionType.STOP)
    controller: ExecutionController

    def request_stop(action: SemanticAction) -> None:
        if action.action_id == "act-first":
            assert controller.request_stop(stop_action) is StopRequestStatus.ACCEPTED

    adapter = FakeAdapter(
        {"act-stop": _result("act-stop", ActionOutcome.FAILED)},
        before_return=request_stop,
    )
    controller = _controller(adapter)

    report = controller.execute(graph)

    assert adapter.calls == ["act-first", "act-stop"]
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.ACTION_FAILED
    assert report.records[1].transitions == (ExecutionState.PENDING,)
    assert report.records[2].reason_code is ExecutionReasonCode.ACTION_FAILED


def test_final_dispatch_boundary_rechecks_instance_duplicate_state() -> None:
    controller: ExecutionController

    class DuplicateAtDispatchValidator(PolicyValidator):
        def check(
            self,
            graph: TaskGraph,
            *,
            confirmed_action_ids: frozenset[str] = frozenset(),
        ) -> PolicyReport:
            report = super().check(
                graph,
                confirmed_action_ids=confirmed_action_ids,
            )
            if len(graph.steps) == 1:
                controller._dispatched_action_ids.add(graph.steps[0].action.action_id)
            return report

    adapter = FakeAdapter()
    validator = DuplicateAtDispatchValidator(policy_config=POLICY_CONFIG)
    controller = _controller(adapter, validator=validator)

    report = controller.execute(_graph(_action("act-1"), _action("act-2")))

    assert adapter.calls == []
    assert report.terminal_state is ExecutionState.FAILED
    assert report.reason_code is ExecutionReasonCode.DUPLICATE_ACTION
    assert report.records[0].transitions == (
        ExecutionState.PENDING,
        ExecutionState.FAILED,
    )
    assert report.records[0].reason_code is ExecutionReasonCode.DUPLICATE_ACTION
    assert report.records[1].transitions == (ExecutionState.PENDING,)
