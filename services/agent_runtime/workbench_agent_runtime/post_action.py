"""Fail-closed post-action observe, reduce and verify gate.

Issue #151. The scripted pipeline dispatches an action, persists one
``ACTION_RESULT``, reduces state and immediately verifies. Nothing required a
*fresh* observation between the action and the verification, so an actuator
reporting ``completed`` was the only thing standing between a claim and a
completion claim.

This module adds the smallest rule that closes that gap: a physical,
state-changing action is followed by an observation that is strictly newer than
the action's completion, that observation is reduced through an injected World
Model port, and only then is the claim verified. Actuator success alone never
advances the task.

The loop owns ordering and the gate. It deliberately owns none of the layers it
calls:

* dispatch and duplicate suppression are ``ExecutionController``'s;
* observation production and freshness at ingestion are the perception
  boundary's (``workbench_perception.ingestion``);
* reduction and verification are the World Model's, injected as
  :class:`PostActionVerifier` and never imported here.

The contract's ``Observation`` carries no action identifier, so the observation
port returns an :class:`ObservationSample` that names the action it answers.
Without that correlation a stale observation from a previous action of the same
run would satisfy the gate, which is exactly the failure this issue exists to
prevent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from workbench_contracts import (
    ActionType,
    Observation,
    SemanticAction,
    TaskGraph,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)

from .execution_controller import (
    ExecutionController,
    ExecutionReasonCode,
    ExecutionState,
    StepExecutionRecord,
    StopRequestStatus,
)

POST_ACTION_LOOP_VERSION = "post-action-verify-v1"

#: Actions that change the physical world and therefore require a fresh
#: observation before their result may be treated as verified. ``observe``,
#: ``ask_confirm`` and ``express`` change nothing; ``stop`` is handled on its own
#: path because a stop must never wait for perception.
STATE_CHANGING_ACTIONS = frozenset(
    {
        ActionType.GRASP,
        ActionType.PLACE,
        ActionType.NAVIGATE,
        ActionType.OPEN,
        ActionType.CLOSE,
    }
)

MAX_OBSERVATION_ATTEMPTS = 8


class PostActionError(ValueError):
    """A gate input or configuration is not usable as fail-closed evidence."""


class GateOutcome(StrEnum):
    """The gate's verdict for one action. Never a completion claim by itself."""

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    STOPPED = "stopped"
    NOT_APPLICABLE = "not_applicable"


class GateReasonCode(StrEnum):
    """Stable reasons, including one per distinct fail-closed state."""

    POST_ACTION_CONFIRMED = "post_action_confirmed"
    NOT_A_STATE_CHANGING_ACTION = "not_a_state_changing_action"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    NOT_EXECUTED = "not_executed"
    ACTION_NOT_COMPLETED = "action_not_completed"
    ACTION_STOPPED = "action_stopped"
    STOP_DISPATCHED = "stop_dispatched"
    CANCELLED = "cancelled"
    RESTART_INVALIDATED_RUN = "restart_invalidated_run"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    OBSERVATION_MISSING = "observation_missing"
    OBSERVATION_TIMEOUT = "observation_timeout"
    PERCEPTION_FAILURE = "perception_failure"
    OBSERVATION_STALE = "observation_stale"
    OBSERVATION_WRONG_RUN = "observation_wrong_run"
    OBSERVATION_WRONG_ACTION = "observation_wrong_action"
    OBSERVATION_WRONG_ENTITY = "observation_wrong_entity"
    OBSERVATION_CLOCK_MISMATCH = "observation_clock_mismatch"
    OBSERVATION_UNREADABLE_TIME = "observation_unreadable_time"
    VERIFIER_FAILED = "verifier_failed"
    VERIFICATION_REFUTED = "verification_refuted"
    VERIFICATION_INSUFFICIENT = "verification_insufficient"


class ObservationAttemptOutcome(StrEnum):
    """Why an observation request did or did not produce a sample."""

    OBSERVED = "observed"
    MISSING = "missing"
    TIMEOUT = "timeout"
    PERCEPTION_FAILURE = "perception_failure"


@dataclass(frozen=True)
class ObservationSample:
    """An observation plus the action it was produced in response to."""

    action_id: str
    observation: Observation

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise PostActionError("sample action_id must be a non-empty string")
        if not isinstance(self.observation, Observation):
            raise PostActionError("sample observation must be an Observation")


@dataclass(frozen=True)
class ObservationAttempt:
    """One observation request's typed result."""

    outcome: ObservationAttemptOutcome
    sample: ObservationSample | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ObservationAttemptOutcome):
            raise PostActionError("attempt outcome must be an ObservationAttemptOutcome")
        if self.outcome is ObservationAttemptOutcome.OBSERVED:
            if self.sample is None:
                raise PostActionError("an observed attempt must carry a sample")
        elif self.sample is not None:
            raise PostActionError("only an observed attempt may carry a sample")

    @classmethod
    def observed(cls, sample: ObservationSample) -> ObservationAttempt:
        return cls(ObservationAttemptOutcome.OBSERVED, sample=sample)

    @classmethod
    def missing(cls, detail: str = "") -> ObservationAttempt:
        return cls(ObservationAttemptOutcome.MISSING, detail=detail)

    @classmethod
    def timeout(cls, detail: str = "") -> ObservationAttempt:
        return cls(ObservationAttemptOutcome.TIMEOUT, detail=detail)

    @classmethod
    def perception_failure(cls, detail: str = "") -> ObservationAttempt:
        return cls(ObservationAttemptOutcome.PERCEPTION_FAILURE, detail=detail)

    @property
    def retryable(self) -> bool:
        """Transient outcomes may be retried; a correlated defect may not."""
        return self.outcome is not ObservationAttemptOutcome.OBSERVED


@runtime_checkable
class ObservationPort(Protocol):
    """The caller's perception boundary, asked for one fresh observation."""

    def observe(
        self,
        *,
        run_id: str,
        action_id: str,
        entity_id: str | None,
        not_before: datetime,
    ) -> ObservationAttempt:
        """Return an observation produced for this action, or a typed failure.

        ``not_before`` is the instant the action completed. An observation at or
        before it is not fresh evidence for that action.
        """


@runtime_checkable
class PostActionVerifier(Protocol):
    """The World Model boundary: reduce accepted observations, then verify.

    Two methods on purpose. The loop calls them in order and a fake records the
    order, so "reduce then verify" is observable rather than implied.
    """

    def reduce(self, observations: Sequence[Observation], *, run_id: str) -> object:
        """Reduce accepted observations into the caller's canonical state."""

    def verify(self, state: object, *, run_id: str, action_id: str) -> VerificationResult:
        """Verify the claim for one action against the reduced state."""


@dataclass(frozen=True)
class PostActionPolicy:
    """Explicit bounded retry/stop policy for insufficient evidence."""

    max_observation_attempts: int = 2

    def __post_init__(self) -> None:
        value = self.max_observation_attempts
        if isinstance(value, bool) or not isinstance(value, int):
            raise PostActionError("max_observation_attempts must be an integer")
        if not 1 <= value <= MAX_OBSERVATION_ATTEMPTS:
            raise PostActionError(f"max_observation_attempts must be between 1 and {MAX_OBSERVATION_ATTEMPTS}")


@dataclass(frozen=True)
class StepGateReport:
    """Immutable gate evidence for one step."""

    step_id: str
    action_id: str
    action_type: ActionType
    outcome: GateOutcome
    reason_code: GateReasonCode
    execution: StepExecutionRecord | None = None
    observations: tuple[Observation, ...] = ()
    verification: VerificationResult | None = None
    attempts: int = 0
    details: tuple[str, ...] = ()

    @property
    def confirmed(self) -> bool:
        return self.outcome is GateOutcome.CONFIRMED

    @property
    def allows_dependents(self) -> bool:
        """Only a confirmed action releases the steps that depend on it."""
        return self.outcome is GateOutcome.CONFIRMED

    @property
    def loop_version(self) -> str:
        return POST_ACTION_LOOP_VERSION


@dataclass(frozen=True)
class PostActionReport:
    """Immutable run-level report over every step the loop considered."""

    run_id: str
    task_id: str | None
    terminal: GateOutcome
    reason_code: GateReasonCode
    steps: tuple[StepGateReport, ...]
    details: tuple[str, ...] = ()

    @property
    def may_advance(self) -> bool:
        """True only when every considered step confirmed."""
        return self.terminal is GateOutcome.CONFIRMED

    def step(self, step_id: str) -> StepGateReport | None:
        return next((item for item in self.steps if item.step_id == step_id), None)


def _parse_instant(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PostActionError(f"{label} must be a non-empty RFC3339 timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise PostActionError(f"{label} is not an RFC3339 timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class PostActionLoop:
    """Dispatch, observe, reduce and verify one graph, fail-closed.

    Dispatch is delegated to the injected :class:`ExecutionController`, so policy
    preflight, per-action revalidation, duplicate suppression and STOP queuing are
    not reimplemented here. The loop adds the requirement the controller does not
    own: a physical action's result is only ever *verifiable* after a fresh
    observation of the affected entity.
    """

    def __init__(
        self,
        *,
        controller: ExecutionController,
        observer: ObservationPort,
        verifier: PostActionVerifier,
        policy: PostActionPolicy | None = None,
    ) -> None:
        if not isinstance(controller, ExecutionController):
            raise PostActionError("controller must be an ExecutionController")
        if not isinstance(observer, ObservationPort):
            raise PostActionError("observer must implement ObservationPort")
        if not isinstance(verifier, PostActionVerifier):
            raise PostActionError("verifier must implement PostActionVerifier")
        resolved_policy = policy if policy is not None else PostActionPolicy()
        if not isinstance(resolved_policy, PostActionPolicy):
            raise PostActionError("policy must be a PostActionPolicy")
        self._controller = controller
        self._observer = observer
        self._verifier = verifier
        self._policy = resolved_policy
        self._gate_concluded: set[str] = set()
        self._cancelled = False
        self._cancel_reason: str = ""
        self._restarted_runs: set[str] = set()

    def request_stop(self, action: SemanticAction) -> StopRequestStatus:
        """Delegate STOP queuing. A STOP never waits for perception."""
        return self._controller.request_stop(action)

    def cancel(self, *, reason: str) -> None:
        """Mark the loop cancelled; every later step fails closed as cancelled."""
        if not isinstance(reason, str) or not reason.strip():
            raise PostActionError("a cancellation requires a reason")
        self._cancelled = True
        self._cancel_reason = reason

    def note_restart(self, *, run_id: str) -> None:
        """Record a process restart that invalidates a run's correlation."""
        if not isinstance(run_id, str) or not run_id.strip():
            raise PostActionError("a restart note requires a run_id")
        self._restarted_runs.add(run_id)

    def run(
        self,
        graph: TaskGraph,
        *,
        run_id: str,
        confirmed_action_ids: frozenset[str] = frozenset(),
    ) -> PostActionReport:
        """Gate every step of one graph in order, honouring ``depends_on``."""
        if not isinstance(graph, TaskGraph):
            raise PostActionError("graph must be a TaskGraph")
        if not isinstance(run_id, str) or not run_id.strip():
            raise PostActionError("run_id must be a non-empty string")

        reports: list[StepGateReport] = []
        confirmed_step_ids: set[str] = set()
        blocked_step_ids: set[str] = set()
        terminal = GateOutcome.CONFIRMED
        terminal_reason = GateReasonCode.POST_ACTION_CONFIRMED
        details: tuple[str, ...] = ()

        # A dependency is evaluated before the run-level stop rule, so a step
        # held back by an unconfirmed dependency is reported as such rather than
        # as generic "not executed".
        stopped = False
        for step in graph.steps:
            blocked_by = tuple(
                dependency
                for dependency in step.depends_on
                if dependency in blocked_step_ids or dependency not in confirmed_step_ids
            )
            if blocked_by:
                reports.append(
                    self._skipped(
                        step,
                        GateReasonCode.DEPENDENCY_BLOCKED,
                        f"unconfirmed dependencies: {', '.join(blocked_by)}",
                    )
                )
                blocked_step_ids.add(step.step_id)
                if not stopped:
                    terminal = GateOutcome.INSUFFICIENT_EVIDENCE
                    terminal_reason = GateReasonCode.DEPENDENCY_BLOCKED
                continue
            if stopped:
                reports.append(self._skipped(step, GateReasonCode.NOT_EXECUTED, "run had already stopped"))
                continue

            step_report = self.run_step(
                graph,
                step,
                run_id=run_id,
                confirmed_action_ids=confirmed_action_ids,
            )
            reports.append(step_report)

            if step_report.outcome in (GateOutcome.CONFIRMED, GateOutcome.NOT_APPLICABLE):
                confirmed_step_ids.add(step.step_id)
                continue

            blocked_step_ids.add(step.step_id)
            stopped = True
            terminal_reason = step_report.reason_code
            details = step_report.details
            if step_report.outcome is GateOutcome.REFUTED:
                terminal = GateOutcome.REFUTED
            elif step_report.outcome is GateOutcome.STOPPED:
                terminal = GateOutcome.STOPPED
            else:
                terminal = GateOutcome.INSUFFICIENT_EVIDENCE

        if not reports:
            terminal = GateOutcome.NOT_APPLICABLE
            terminal_reason = GateReasonCode.NOT_EXECUTED
            details = ("graph had no steps",)

        return PostActionReport(
            run_id=run_id,
            task_id=graph.task_id if isinstance(graph.task_id, str) else None,
            terminal=terminal,
            reason_code=terminal_reason,
            steps=tuple(reports),
            details=details,
        )

    def run_step(
        self,
        graph: TaskGraph,
        step: TaskStep,
        *,
        run_id: str,
        confirmed_action_ids: frozenset[str] = frozenset(),
    ) -> StepGateReport:
        """Gate exactly one step: dispatch, observe, reduce, verify."""
        if not isinstance(step, TaskStep):
            raise PostActionError("step must be a TaskStep")
        action = step.action
        action_id = action.action_id

        if self._cancelled:
            return self._skipped(step, GateReasonCode.CANCELLED, self._cancel_reason)
        if run_id in self._restarted_runs:
            return self._skipped(
                step,
                GateReasonCode.RESTART_INVALIDATED_RUN,
                "run correlation was invalidated by a process restart",
            )
        if action_id in self._gate_concluded:
            return self._skipped(
                step,
                GateReasonCode.DUPLICATE_DELIVERY,
                f"action_id already gated: {action_id}",
            )

        execution, controller_reason = self._dispatch(graph, step, confirmed_action_ids)
        self._gate_concluded.add(action_id)

        if execution is None:
            # A queued STOP preempts the pending physical action at the dispatch
            # boundary, so the controller records the stop instead of this step.
            # That is a stop, not a missing execution.
            if controller_reason is ExecutionReasonCode.STOP_DISPATCHED:
                return StepGateReport(
                    step_id=step.step_id,
                    action_id=action_id,
                    action_type=action.action_type,
                    outcome=GateOutcome.STOPPED,
                    reason_code=GateReasonCode.STOP_DISPATCHED,
                    details=("a queued STOP preempted this action",),
                )
            return StepGateReport(
                step_id=step.step_id,
                action_id=action_id,
                action_type=action.action_type,
                outcome=GateOutcome.INSUFFICIENT_EVIDENCE,
                reason_code=GateReasonCode.ACTION_NOT_COMPLETED,
                details=(f"controller did not record this action ({controller_reason.value})",),
            )

        terminal_state = execution.transitions[-1] if execution.transitions else None
        if action.action_type is ActionType.STOP or controller_reason is ExecutionReasonCode.STOP_DISPATCHED:
            # A stop is dispatched and acknowledged; it never waits for perception.
            return StepGateReport(
                step_id=step.step_id,
                action_id=action_id,
                action_type=action.action_type,
                outcome=GateOutcome.STOPPED,
                reason_code=GateReasonCode.STOP_DISPATCHED,
                execution=execution,
            )
        if controller_reason is ExecutionReasonCode.DUPLICATE_ACTION:
            return StepGateReport(
                step_id=step.step_id,
                action_id=action_id,
                action_type=action.action_type,
                outcome=GateOutcome.INSUFFICIENT_EVIDENCE,
                reason_code=GateReasonCode.DUPLICATE_DELIVERY,
                execution=execution,
                details=("controller refused a duplicate dispatch",),
            )
        if terminal_state is ExecutionState.STOPPED or controller_reason is ExecutionReasonCode.ACTION_STOPPED:
            return StepGateReport(
                step_id=step.step_id,
                action_id=action_id,
                action_type=action.action_type,
                outcome=GateOutcome.STOPPED,
                reason_code=GateReasonCode.ACTION_STOPPED,
                execution=execution,
            )
        if terminal_state is not ExecutionState.SUCCEEDED:
            return StepGateReport(
                step_id=step.step_id,
                action_id=action_id,
                action_type=action.action_type,
                outcome=GateOutcome.INSUFFICIENT_EVIDENCE,
                reason_code=GateReasonCode.ACTION_NOT_COMPLETED,
                execution=execution,
                details=(
                    f"execution ended in {terminal_state.value if terminal_state else 'unknown'}"
                    f" ({controller_reason.value})",
                ),
            )
        if action.action_type not in STATE_CHANGING_ACTIONS:
            return StepGateReport(
                step_id=step.step_id,
                action_id=action_id,
                action_type=action.action_type,
                outcome=GateOutcome.NOT_APPLICABLE,
                reason_code=GateReasonCode.NOT_A_STATE_CHANGING_ACTION,
                execution=execution,
            )

        return self._observe_reduce_verify(step, execution, run_id=run_id)

    def _observe_reduce_verify(
        self,
        step: TaskStep,
        execution: StepExecutionRecord,
        *,
        run_id: str,
    ) -> StepGateReport:
        result = execution.result
        not_before = self._completion_instant(result)

        accepted: list[Observation] = []
        attempts = 0
        failure: tuple[GateReasonCode, str] | None = None

        while attempts < self._policy.max_observation_attempts:
            attempts += 1
            attempt = self._observer.observe(
                run_id=run_id,
                action_id=step.action.action_id,
                entity_id=self._expected_entity(step),
                not_before=not_before,
            )
            if attempt.outcome is ObservationAttemptOutcome.MISSING:
                failure = (GateReasonCode.OBSERVATION_MISSING, attempt.detail)
                continue
            if attempt.outcome is ObservationAttemptOutcome.TIMEOUT:
                failure = (GateReasonCode.OBSERVATION_TIMEOUT, attempt.detail)
                continue
            if attempt.outcome is ObservationAttemptOutcome.PERCEPTION_FAILURE:
                failure = (GateReasonCode.PERCEPTION_FAILURE, attempt.detail)
                continue

            sample = attempt.sample
            if sample is None:  # pragma: no cover - constructor guarantees a sample
                failure = (GateReasonCode.OBSERVATION_MISSING, "attempt carried no sample")
                continue
            defect = self._correlation_defect(step, sample, run_id=run_id, not_before=not_before)
            if defect is not None:
                # A mis-correlated observation is a defect, not a transient miss:
                # retrying could not make it belong to this action.
                return StepGateReport(
                    step_id=step.step_id,
                    action_id=step.action.action_id,
                    action_type=step.action.action_type,
                    outcome=GateOutcome.INSUFFICIENT_EVIDENCE,
                    reason_code=defect[0],
                    execution=execution,
                    observations=tuple(accepted),
                    attempts=attempts,
                    details=(defect[1],),
                )

            accepted.append(sample.observation)
            failure = None
            verification, verifier_error = self._verify(accepted, run_id=run_id, step=step)
            if verifier_error is not None:
                # A verifier failure is deterministic, not a transient miss:
                # re-observing cannot fix it, and reporting it as
                # "insufficient evidence" would hide a broken verifier.
                return StepGateReport(
                    step_id=step.step_id,
                    action_id=step.action.action_id,
                    action_type=step.action.action_type,
                    outcome=GateOutcome.INSUFFICIENT_EVIDENCE,
                    reason_code=GateReasonCode.VERIFIER_FAILED,
                    execution=execution,
                    observations=tuple(accepted),
                    attempts=attempts,
                    details=(verifier_error,),
                )
            if verification is None:  # pragma: no cover - verifier_error narrows this
                return StepGateReport(
                    step_id=step.step_id,
                    action_id=step.action.action_id,
                    action_type=step.action.action_type,
                    outcome=GateOutcome.INSUFFICIENT_EVIDENCE,
                    reason_code=GateReasonCode.VERIFIER_FAILED,
                    execution=execution,
                    observations=tuple(accepted),
                    attempts=attempts,
                    details=("verifier returned no result",),
                )

            if verification.status is VerificationStatus.CONFIRMED:
                return StepGateReport(
                    step_id=step.step_id,
                    action_id=step.action.action_id,
                    action_type=step.action.action_type,
                    outcome=GateOutcome.CONFIRMED,
                    reason_code=GateReasonCode.POST_ACTION_CONFIRMED,
                    execution=execution,
                    observations=tuple(accepted),
                    verification=verification,
                    attempts=attempts,
                )
            if verification.status is VerificationStatus.REFUTED:
                return StepGateReport(
                    step_id=step.step_id,
                    action_id=step.action.action_id,
                    action_type=step.action.action_type,
                    outcome=GateOutcome.REFUTED,
                    reason_code=GateReasonCode.VERIFICATION_REFUTED,
                    execution=execution,
                    observations=tuple(accepted),
                    verification=verification,
                    attempts=attempts,
                )
            # insufficient_evidence: re-observe while the bounded budget allows.
            failure = (
                GateReasonCode.VERIFICATION_INSUFFICIENT,
                f"verification returned {verification.reason}",
            )

        reason, detail = (
            failure
            if failure is not None
            else (
                GateReasonCode.VERIFICATION_INSUFFICIENT,
                "observation budget exhausted",
            )
        )
        return StepGateReport(
            step_id=step.step_id,
            action_id=step.action.action_id,
            action_type=step.action.action_type,
            outcome=GateOutcome.INSUFFICIENT_EVIDENCE,
            reason_code=reason,
            execution=execution,
            observations=tuple(accepted),
            attempts=attempts,
            details=(detail,) if detail else (),
        )

    def _verify(
        self,
        observations: Sequence[Observation],
        *,
        run_id: str,
        step: TaskStep,
    ) -> tuple[VerificationResult | None, str | None]:
        """Reduce then verify. Returns ``(result, None)`` or ``(None, reason)``."""
        try:
            state = self._verifier.reduce(tuple(observations), run_id=run_id)
        except Exception as error:  # noqa: BLE001 - injected port must fail closed
            return None, f"reduce failed: {type(error).__name__}"
        try:
            verification = self._verifier.verify(state, run_id=run_id, action_id=step.action.action_id)
        except Exception as error:  # noqa: BLE001 - injected port must fail closed
            return None, f"verify failed: {type(error).__name__}"
        if not isinstance(verification, VerificationResult):
            return None, f"verifier returned {type(verification).__name__}"
        return verification, None

    def _correlation_defect(
        self,
        step: TaskStep,
        sample: ObservationSample,
        *,
        run_id: str,
        not_before: datetime | None,
    ) -> tuple[GateReasonCode, str] | None:
        observation = sample.observation
        if sample.action_id != step.action.action_id:
            return (
                GateReasonCode.OBSERVATION_WRONG_ACTION,
                f"observation answers {sample.action_id}, not {step.action.action_id}",
            )
        if observation.run_id != run_id:
            return (
                GateReasonCode.OBSERVATION_WRONG_RUN,
                f"observation run_id {observation.run_id} != {run_id}",
            )
        expected_entity = self._expected_entity(step)
        if expected_entity is not None and observation.entity_id != expected_entity:
            return (
                GateReasonCode.OBSERVATION_WRONG_ENTITY,
                f"observation entity_id {observation.entity_id} != {expected_entity}",
            )
        if not_before is None:
            return (
                GateReasonCode.OBSERVATION_UNREADABLE_TIME,
                "execution result carried no usable completion time",
            )
        try:
            observed_at = _parse_instant(observation.observed_at, "observation.observed_at")
        except PostActionError as error:
            return (GateReasonCode.OBSERVATION_UNREADABLE_TIME, str(error))
        if not_before.tzinfo != observed_at.tzinfo and observed_at.utcoffset() != not_before.utcoffset():
            return (
                GateReasonCode.OBSERVATION_CLOCK_MISMATCH,
                "observation and action completion are not on the same clock",
            )
        if observed_at <= not_before:
            return (
                GateReasonCode.OBSERVATION_STALE,
                f"observation at {observation.observed_at} is not newer than {not_before.isoformat()}",
            )
        return None

    def _completion_instant(self, result: object) -> datetime | None:
        ended_at = getattr(result, "ended_at", None)
        if ended_at is None:
            return None
        try:
            return _parse_instant(ended_at, "result.ended_at")
        except PostActionError:
            return None

    @staticmethod
    def _expected_entity(step: TaskStep) -> str | None:
        target = step.action.target_id
        return target if isinstance(target, str) and target.strip() else None

    def _dispatch(
        self,
        graph: TaskGraph,
        step: TaskStep,
        confirmed_action_ids: frozenset[str],
    ) -> tuple[StepExecutionRecord | None, ExecutionReasonCode]:
        # Dispatch the step in isolation. Its ``depends_on`` is this loop's
        # ordering concern, and the controller's structure check would report a
        # dependency that is legitimately absent from a one-step graph.
        isolated = TaskStep(step_id=step.step_id, action=step.action, depends_on=[])
        single = TaskGraph(
            task_id=graph.task_id,
            goal=graph.goal,
            steps=[isolated],
            planner=graph.planner,
            model_route=graph.model_route,
        )
        report = self._controller.execute(single, confirmed_action_ids=confirmed_action_ids)
        records = [record for record in report.records if record.action_id == step.action.action_id]
        return (records[0] if records else None), report.reason_code

    @staticmethod
    def _skipped(step: TaskStep, reason: GateReasonCode, detail: str) -> StepGateReport:
        return StepGateReport(
            step_id=step.step_id,
            action_id=step.action.action_id,
            action_type=step.action.action_type,
            outcome=GateOutcome.INSUFFICIENT_EVIDENCE,
            reason_code=reason,
            details=(detail,) if detail else (),
        )


def build_post_action_loop(
    *,
    controller: ExecutionController,
    observer: ObservationPort,
    verifier: PostActionVerifier,
    policy: PostActionPolicy | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PostActionLoop:
    """Build a loop. ``clock`` is accepted so a caller can state its time source."""
    if clock is not None and not callable(clock):
        raise PostActionError("clock must be callable or None")
    return PostActionLoop(controller=controller, observer=observer, verifier=verifier, policy=policy)


__all__ = [
    "MAX_OBSERVATION_ATTEMPTS",
    "POST_ACTION_LOOP_VERSION",
    "STATE_CHANGING_ACTIONS",
    "GateOutcome",
    "GateReasonCode",
    "ObservationAttempt",
    "ObservationAttemptOutcome",
    "ObservationPort",
    "ObservationSample",
    "PostActionError",
    "PostActionLoop",
    "PostActionPolicy",
    "PostActionReport",
    "PostActionVerifier",
    "StepGateReport",
    "build_post_action_loop",
]
