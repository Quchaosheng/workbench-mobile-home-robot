"""Bounded, evidence-aware recovery policies shared by every scenario (#305).

A failed verification used to leave the caller with a free-form
``recovery_hint`` string and no shared rule for what to do next. Each scenario
grew its own retry loop, none of them were bounded, and none of them had to show
new evidence before trying again - which is how a retry loop turns a refuted
claim into an unearned success.

This module owns the shared rule and nothing else:

* the legal transitions among ``retry_observation``, ``retry_action``,
  ``ask_confirm``, ``safe_stop`` and ``abort``;
* a finite attempt ceiling and a finite recovery budget, both validated at parse
  time so a malformed policy fails before anything is dispatched;
* the evidence gate, which refuses an action retry unless a *new* and *fresh*
  observation arrived after the failed attempt;
* an auditable decision carrying the attempt number and the reason, so retry
  counts are visible in replay.

It deliberately owns none of the layers it talks to. Dispatch stays with
:class:`~workbench_agent_runtime.execution_controller.ExecutionController`,
verification stays with the World Model, and ``safe_stop`` stays with the
trusted runtime: this module can *request* a stop and can never record one as
executed. A scenario that declares no policy keeps exactly its previous
behaviour, because a disabled policy returns no decision rather than a default
one.

No manifest, fixture, seed or scene hash changes. This module imports no
simulator, starts no ROS, Gazebo or hardware, and produces no physical evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from workbench_contracts import RecoveryHint, VerificationStatus

# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #

# Every diagnostic this module can emit. A caller switches on these, so each one
# names one cause and one remedy.
RECOVERY_POLICY_MALFORMED = "RECOVERY_POLICY_MALFORMED"
RECOVERY_POLICY_UNKNOWN_ACTION = "RECOVERY_POLICY_UNKNOWN_ACTION"
RECOVERY_POLICY_EMPTY_ACTIONS = "RECOVERY_POLICY_EMPTY_ACTIONS"
RECOVERY_POLICY_INVALID_BUDGET = "RECOVERY_POLICY_INVALID_BUDGET"
RECOVERY_POLICY_ILLEGAL_TRANSITION = "RECOVERY_POLICY_ILLEGAL_TRANSITION"
RECOVERY_POLICY_BUDGET_EXHAUSTED = "RECOVERY_POLICY_BUDGET_EXHAUSTED"
RECOVERY_POLICY_EVIDENCE_NOT_FRESH = "RECOVERY_POLICY_EVIDENCE_NOT_FRESH"
RECOVERY_POLICY_RUNTIME_OWNED_ACTION = "RECOVERY_POLICY_RUNTIME_OWNED_ACTION"
RECOVERY_POLICY_TERMINAL = "RECOVERY_POLICY_TERMINAL"

EMITTED_CODES = (
    RECOVERY_POLICY_MALFORMED,
    RECOVERY_POLICY_UNKNOWN_ACTION,
    RECOVERY_POLICY_EMPTY_ACTIONS,
    RECOVERY_POLICY_INVALID_BUDGET,
    RECOVERY_POLICY_ILLEGAL_TRANSITION,
    RECOVERY_POLICY_BUDGET_EXHAUSTED,
    RECOVERY_POLICY_EVIDENCE_NOT_FRESH,
    RECOVERY_POLICY_RUNTIME_OWNED_ACTION,
    RECOVERY_POLICY_TERMINAL,
)

# Bounds are fixed here rather than per scenario. A retry ceiling of one is a
# legitimate "never retry"; there is no legitimate reason for a scenario to
# declare two hundred attempts, because that is an unbounded loop wearing a
# number.
MIN_MAX_ATTEMPTS = 1
MAX_MAX_ATTEMPTS = 10
MIN_MAX_TICKS = 1
MAX_MAX_TICKS = 100

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_TICKS = 30

# The only authority that may record a runtime-owned action as executed. This is
# a declared boundary, not a security mechanism: it makes scenario code that
# tries to acknowledge its own stop fail loudly instead of silently.
RUNTIME_AUTHORITY = "trusted-runtime"

POLICY_VERSION = "recovery-policy-v1"


class RecoveryPolicyError(ValueError):
    """A recovery policy is malformed, out of bounds, or misused."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #


class RecoveryAction(StrEnum):
    """The closed set of recovery actions a scenario may declare or receive."""

    RETRY_OBSERVATION = "retry_observation"
    RETRY_ACTION = "retry_action"
    ASK_CONFIRM = "ask_confirm"
    SAFE_STOP = "safe_stop"
    ABORT = "abort"


# The registry manifests predate this module and spell the first action
# ``re_observe``. Accepting the alias here rather than rewriting the manifests
# keeps the change additive: no committed manifest changes.
MANIFEST_ALIASES: dict[str, str] = {
    "re_observe": RecoveryAction.RETRY_OBSERVATION.value,
    "reobserve": RecoveryAction.RETRY_OBSERVATION.value,
}

# ``RecoveryHint`` is the verification-side vocabulary. It is narrower than
# ``RecoveryAction`` - it has no ``safe_stop``, because the World Model does not
# own the stop boundary.
HINT_TO_ACTION: dict[RecoveryHint, RecoveryAction] = {
    RecoveryHint.RE_OBSERVE: RecoveryAction.RETRY_OBSERVATION,
    RecoveryHint.RETRY_ACTION: RecoveryAction.RETRY_ACTION,
    RecoveryHint.ASK_CONFIRM: RecoveryAction.ASK_CONFIRM,
    RecoveryHint.ABORT: RecoveryAction.ABORT,
}

# Preference order for a policy that declares several actions. Cheapest and
# least invasive first: look again, try again, ask a human, stop, give up.
ACTION_PREFERENCE: tuple[RecoveryAction, ...] = (
    RecoveryAction.RETRY_OBSERVATION,
    RecoveryAction.RETRY_ACTION,
    RecoveryAction.ASK_CONFIRM,
    RecoveryAction.SAFE_STOP,
    RecoveryAction.ABORT,
)

# Actions the trusted runtime executes. Scenario code may request them and may
# never record them as done.
RUNTIME_OWNED_ACTIONS: frozenset[RecoveryAction] = frozenset({RecoveryAction.SAFE_STOP})

# A verification whose reason names an evidence problem. Only these make an
# action retry evidence-dependent: an actuator that reported "the grasp slipped"
# does not need a new frame to be worth trying again, but a claim refused
# because nothing fresh was seen must not be retried on the same stale sighting.
EVIDENCE_REASON_CODES: frozenset[str] = frozenset(
    {
        "stale_observation",
        "evidence_missing",
        "target_not_observed",
        "confidence_below_threshold",
        "conflicting_observations",
    }
)

# Actions that end the attempt. ``safe_stop`` is terminal until the runtime
# acknowledges it; ``abort`` is terminal outright.
TERMINAL_ACTIONS: frozenset[RecoveryAction] = frozenset({RecoveryAction.SAFE_STOP, RecoveryAction.ABORT})


class RecoveryState(StrEnum):
    """Where one task's recovery has got to."""

    IDLE = "idle"
    OBSERVING = "observing"
    ACTING = "acting"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    STOPPING = "stopping"
    ABORTED = "aborted"
    COMPLETE = "complete"


_STATE_FOR_ACTION: dict[RecoveryAction, RecoveryState] = {
    RecoveryAction.RETRY_OBSERVATION: RecoveryState.OBSERVING,
    RecoveryAction.RETRY_ACTION: RecoveryState.ACTING,
    RecoveryAction.ASK_CONFIRM: RecoveryState.AWAITING_CONFIRMATION,
    RecoveryAction.SAFE_STOP: RecoveryState.STOPPING,
    RecoveryAction.ABORT: RecoveryState.ABORTED,
}

# The legal transition table. Every entry is the complete set of successors, so
# an edge that is absent is illegal by construction rather than by omission.
_DEFAULT_SUCCESSORS = frozenset(
    {
        RecoveryState.OBSERVING,
        RecoveryState.ACTING,
        RecoveryState.AWAITING_CONFIRMATION,
        RecoveryState.COMPLETE,
        RecoveryState.STOPPING,
        RecoveryState.ABORTED,
    }
)

LEGAL_TRANSITIONS: dict[RecoveryState, frozenset[RecoveryState]] = {
    RecoveryState.IDLE: _DEFAULT_SUCCESSORS,
    RecoveryState.OBSERVING: _DEFAULT_SUCCESSORS,
    RecoveryState.ACTING: _DEFAULT_SUCCESSORS,
    # A confirmation is answered by a new observation or action, a stop, an
    # abort, or a completion. It cannot silently re-enter the confirmation
    # state without a new decision.
    RecoveryState.AWAITING_CONFIRMATION: frozenset(
        {
            RecoveryState.OBSERVING,
            RecoveryState.ACTING,
            RecoveryState.COMPLETE,
            RecoveryState.STOPPING,
            RecoveryState.ABORTED,
        }
    ),
    # A stop is terminal until the trusted runtime acknowledges it. Recovery
    # cannot walk itself back out of a stop.
    RecoveryState.STOPPING: frozenset(),
    RecoveryState.ABORTED: frozenset(),
    # A confirmed completion is terminal for a run, but a later task in the same
    # ledger legitimately starts a new attempt, so it may return to IDLE.
    RecoveryState.COMPLETE: frozenset({RecoveryState.IDLE}),
}


def transition_is_legal(current: RecoveryState, following: RecoveryState) -> bool:
    """Whether ``current -> following`` is a declared edge."""

    return following in LEGAL_TRANSITIONS.get(current, frozenset())


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RecoveryPolicy:
    """One validated, bounded recovery policy.

    ``enabled`` is False for a scenario that declares nothing, and a disabled
    policy returns no decision at all - it never falls back to a default action.
    """

    actions: frozenset[RecoveryAction] = frozenset()
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_recovery_ticks: int = DEFAULT_MAX_TICKS
    requires_fresh_evidence: bool = True
    enabled: bool = False
    policy_version: str = POLICY_VERSION

    @property
    def ordered_actions(self) -> tuple[RecoveryAction, ...]:
        return tuple(action for action in ACTION_PREFERENCE if action in self.actions)

    @property
    def runtime_owned_actions(self) -> frozenset[RecoveryAction]:
        return self.actions & RUNTIME_OWNED_ACTIONS

    @classmethod
    def disabled(cls) -> RecoveryPolicy:
        """The policy of a scenario that declares none: no recovery, ever."""

        return cls()

    @classmethod
    def parse(cls, payload: object) -> RecoveryPolicy:
        """Validate a manifest ``recovery_policy`` object, or fail closed."""

        if payload is None:
            return cls.disabled()
        if not isinstance(payload, Mapping):
            raise RecoveryPolicyError(RECOVERY_POLICY_MALFORMED, "recovery_policy must be an object")

        unknown = set(payload) - {
            "allowed",
            "max_attempts",
            "requires_fresh_evidence",
            "max_recovery_ticks",
            "allow_safe_stop",
        }
        if unknown:
            raise RecoveryPolicyError(
                RECOVERY_POLICY_MALFORMED,
                f"unknown recovery_policy key(s): {', '.join(sorted(str(key) for key in unknown))}",
            )

        raw_actions = payload.get("allowed", ())
        if isinstance(raw_actions, (str, bytes)) or not isinstance(raw_actions, Iterable):
            raise RecoveryPolicyError(RECOVERY_POLICY_MALFORMED, "recovery_policy.allowed must be a list")
        actions: set[RecoveryAction] = set()
        for entry in raw_actions:
            if not isinstance(entry, str):
                raise RecoveryPolicyError(
                    RECOVERY_POLICY_UNKNOWN_ACTION,
                    f"recovery action must be a string, got {type(entry).__name__}",
                )
            normalized = MANIFEST_ALIASES.get(entry, entry)
            try:
                actions.add(RecoveryAction(normalized))
            except ValueError as error:
                raise RecoveryPolicyError(
                    RECOVERY_POLICY_UNKNOWN_ACTION, f"{entry!r} is not a recovery action"
                ) from error
        if not actions:
            raise RecoveryPolicyError(
                RECOVERY_POLICY_EMPTY_ACTIONS, "a declared policy must allow at least one recovery action"
            )

        if actions & RUNTIME_OWNED_ACTIONS:
            if payload.get("allow_safe_stop", True) is not True:
                raise RecoveryPolicyError(
                    RECOVERY_POLICY_RUNTIME_OWNED_ACTION,
                    "safe_stop is runtime-owned; allow_safe_stop must be true to request it",
                )

        max_attempts = _bounded_int(
            payload.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
            field_name="max_attempts",
            minimum=MIN_MAX_ATTEMPTS,
            maximum=MAX_MAX_ATTEMPTS,
        )
        max_ticks = _bounded_int(
            payload.get("max_recovery_ticks", DEFAULT_MAX_TICKS),
            field_name="max_recovery_ticks",
            minimum=MIN_MAX_TICKS,
            maximum=MAX_MAX_TICKS,
        )

        requires_fresh = payload.get("requires_fresh_evidence", True)
        if not isinstance(requires_fresh, bool):
            raise RecoveryPolicyError(RECOVERY_POLICY_MALFORMED, "requires_fresh_evidence must be a boolean")

        return cls(
            actions=frozenset(actions),
            max_attempts=max_attempts,
            max_recovery_ticks=max_ticks,
            requires_fresh_evidence=requires_fresh,
            enabled=True,
        )


def _bounded_int(value: object, *, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecoveryPolicyError(
            RECOVERY_POLICY_INVALID_BUDGET, f"{field_name} must be an integer, got {type(value).__name__}"
        )
    if not minimum <= value <= maximum:
        raise RecoveryPolicyError(
            RECOVERY_POLICY_INVALID_BUDGET,
            f"{field_name}={value} is outside the permitted range {minimum}..{maximum}",
        )
    return value


# --------------------------------------------------------------------------- #
# Evidence and decisions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceStamp:
    """What the ledger knows about the newest observation for one task.

    The ledger never reads perception; it is handed a stamp and trusts exactly
    the three facts that decide an evidence-dependent retry: whether the source
    declared the sample fresh, whether its provenance validated, and when it was
    observed. ``observed_at`` is a wall-clock string in the contract's format, so
    it is comparable with ``VerificationResult.verified_at``, and ``sequence_no``
    orders it against the failed attempt without trusting the clock alone.
    """

    observed_at: str
    fresh: bool
    provenance_valid: bool
    sequence_no: int
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryDecision:
    """One auditable recovery step. Not a completion claim."""

    recovery_id: str
    task_id: str
    action: RecoveryAction
    state: RecoveryState
    attempt: int
    max_attempts: int
    ticks: int
    max_recovery_ticks: int
    reason_code: str
    reason: str
    policy_version: str = POLICY_VERSION
    terminal: bool = False
    runtime_owned: bool = False
    evidence_refs: tuple[str, ...] = ()

    @property
    def event_type(self) -> str:
        """``recovery_complete`` for a terminal step, ``recovery_started`` otherwise."""

        return "recovery_complete" if self.terminal else "recovery_started"

    def as_payload(self) -> dict[str, object]:
        """A plain JSON payload for the world-model validator."""

        payload: dict[str, object] = {
            "recovery_id": self.recovery_id,
            "task_id": self.task_id,
            "action": self.action.value,
            "state": self.state.value,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "ticks": self.ticks,
            "max_recovery_ticks": self.max_recovery_ticks,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "runtime_owned": self.runtime_owned,
        }
        if self.terminal:
            payload["outcome"] = _OUTCOMES.get(self.action.value, "recovered")
        if self.evidence_refs:
            payload["evidence_refs"] = list(self.evidence_refs)
        return payload


_OUTCOMES: dict[str, str] = {
    RecoveryAction.ABORT.value: "aborted",
    RecoveryAction.SAFE_STOP.value: "stopped",
}


class RecoveryLedger:
    """Track one task's bounded recovery, deterministically.

    The ledger is pure and monotonic: equal inputs produce equal decisions, the
    attempt and tick counters only ever increase, and a terminal state has no
    successors, so calling it again fails instead of silently retrying.
    """

    def __init__(
        self,
        *,
        policy: RecoveryPolicy,
        recovery_id: str,
        task_id: str,
    ) -> None:
        if not isinstance(policy, RecoveryPolicy):
            raise TypeError("policy must be a RecoveryPolicy")
        self._policy = policy
        self._recovery_id = _non_blank(recovery_id, "recovery_id")
        self._task_id = _non_blank(task_id, "task_id")
        self._state = RecoveryState.IDLE
        self._attempt = 0
        self._ticks = 0
        self._evidence: EvidenceStamp | None = None
        self._failed_at = ""
        self._failed_sequence = -1
        self._failed_evidence: set[str] = set()
        self._failed_verification_id: str | None = None
        self._completed_by: str | None = None

    # -- read-only views -------------------------------------------------- #

    @property
    def policy(self) -> RecoveryPolicy:
        return self._policy

    @property
    def state(self) -> RecoveryState:
        return self._state

    @property
    def attempt(self) -> int:
        return self._attempt

    @property
    def ticks(self) -> int:
        return self._ticks

    @property
    def enabled(self) -> bool:
        return self._policy.enabled

    @property
    def terminal(self) -> bool:
        return self._state in {RecoveryState.STOPPING, RecoveryState.ABORTED}

    @property
    def completed_by(self) -> str | None:
        return self._completed_by

    @property
    def evidence(self) -> EvidenceStamp | None:
        return self._evidence

    # -- inputs ----------------------------------------------------------- #

    def record_observation(self, stamp: EvidenceStamp) -> None:
        """Record the newest observation. Does not itself advance the state."""

        if not isinstance(stamp, EvidenceStamp):
            raise TypeError("stamp must be an EvidenceStamp")
        if self._evidence is not None and stamp.sequence_no < self._evidence.sequence_no:
            # An out-of-order arrival is dropped rather than allowed to rewind
            # freshness. Replay must not depend on delivery order.
            return
        self._evidence = stamp

    def acknowledge_runtime_action(self, action: RecoveryAction, *, authority: str) -> None:
        """Record that the trusted runtime executed a runtime-owned action.

        Scenario code has no authority here, and trying to acknowledge its own
        stop is an error rather than a silent success.
        """

        if not isinstance(action, RecoveryAction):
            raise RecoveryPolicyError(RECOVERY_POLICY_MALFORMED, "action must be a RecoveryAction")
        if action in RUNTIME_OWNED_ACTIONS and authority != RUNTIME_AUTHORITY:
            raise RecoveryPolicyError(
                RECOVERY_POLICY_RUNTIME_OWNED_ACTION,
                f"{action.value} is runtime-owned; only {RUNTIME_AUTHORITY!r} may record it as executed",
            )
        if action in TERMINAL_ACTIONS:
            following = _STATE_FOR_ACTION[action]
            if self._state is not following:
                self._advance(following)

    def record_verification(self, result: object) -> RecoveryDecision | None:
        """Turn one verification into the next bounded recovery step.

        Returns ``None`` when the ledger is disabled or the verification asked
        for no recovery at all, which is how a scenario that declares no policy
        keeps exactly its previous behaviour.
        """

        status = getattr(result, "status", None)
        verification_id = getattr(result, "verification_id", None)
        evidence_refs = tuple(getattr(result, "evidence_refs", ()) or ())

        if _status_value(status) == VerificationStatus.CONFIRMED.value:
            self._complete(verification_id, evidence_refs)
            return None

        if not self._policy.enabled:
            return None

        if self.terminal:
            raise RecoveryPolicyError(
                RECOVERY_POLICY_TERMINAL, f"recovery is already terminal in state {self._state.value}"
            )

        self._attempt += 1
        self._ticks += 1
        self._failed_verification_id = verification_id if isinstance(verification_id, str) else None
        self._failed_at = str(getattr(result, "verified_at", "") or "")
        self._failed_evidence.update(evidence_refs)

        if self._attempt > self._policy.max_attempts or self._ticks > self._policy.max_recovery_ticks:
            return self._decide(
                RecoveryAction.ABORT,
                code=RECOVERY_POLICY_BUDGET_EXHAUSTED,
                reason=(
                    f"recovery budget exhausted at attempt {self._attempt} of {self._policy.max_attempts} "
                    f"and tick {self._ticks} of {self._policy.max_recovery_ticks}"
                ),
            )

        candidate = _hint_to_action(getattr(result, "recovery_hint", None))
        if candidate is None:
            return None

        if candidate not in self._policy.actions:
            return self._decide(
                RecoveryAction.ABORT,
                code=RECOVERY_POLICY_UNKNOWN_ACTION,
                reason=f"the policy does not allow {candidate.value}",
            )

        evidence_dependent = _reason_value(getattr(result, "reason_code", None)) in EVIDENCE_REASON_CODES
        if candidate is RecoveryAction.RETRY_ACTION and self._policy.requires_fresh_evidence and evidence_dependent:
            if not self._evidence_is_new():
                if RecoveryAction.RETRY_OBSERVATION in self._policy.actions:
                    return self._decide(
                        RecoveryAction.RETRY_OBSERVATION,
                        code=RECOVERY_POLICY_EVIDENCE_NOT_FRESH,
                        reason=(
                            "an action retry was refused because no fresh provenance-valid observation "
                            "arrived after the failed attempt; observing again first"
                        ),
                    )
                return self._decide(
                    RecoveryAction.ABORT,
                    code=RECOVERY_POLICY_EVIDENCE_NOT_FRESH,
                    reason=(
                        "an action retry needs fresh provenance-valid evidence and the policy allows "
                        "no observation retry"
                    ),
                )

        return self._decide(
            candidate,
            code="recovery_selected",
            reason=f"the verification requested {candidate.value}",
        )

    # -- internals -------------------------------------------------------- #

    def _evidence_is_new(self) -> bool:
        """Whether a fresh, provenance-valid observation postdates the last failure.

        "New" is measured by observation sequence against the watermark taken at
        the previous failed attempt, not by wall clock: a sequential loop
        verifies *after* the observation it used, so comparing the newest
        observation against the newest failure timestamp would refuse every
        action retry and the policy would collapse to observation-only. The
        watermark advances after a decision, so each retry_action consumes one
        fresh sample and the loop stays finite. A sample the source did not
        declare fresh, or whose provenance did not validate, never qualifies.
        """

        stamp = self._evidence
        if stamp is None or not (stamp.fresh and stamp.provenance_valid):
            return False
        return stamp.sequence_no > self._failed_sequence

    def _complete(self, verification_id: object, evidence_refs: tuple[str, ...]) -> None:
        if self._state is RecoveryState.COMPLETE:
            return
        if self._failed_verification_id is not None and not (set(evidence_refs) - self._failed_evidence):
            raise RecoveryPolicyError(
                RECOVERY_POLICY_EVIDENCE_NOT_FRESH,
                "a confirmed verification after a failed attempt must cite at least one evidence "
                "reference the failed attempt did not already carry",
            )
        self._advance(RecoveryState.COMPLETE)
        self._completed_by = verification_id if isinstance(verification_id, str) else None

    def _advance(self, following: RecoveryState) -> None:
        if not transition_is_legal(self._state, following):
            raise RecoveryPolicyError(
                RECOVERY_POLICY_ILLEGAL_TRANSITION,
                f"{self._state.value} -> {following.value} is not a legal recovery transition",
            )
        self._state = following

    def _decide(self, action: RecoveryAction, *, code: str, reason: str) -> RecoveryDecision:
        self._advance(_STATE_FOR_ACTION[action])
        # The decision has now consumed everything the ledger knew. The next
        # evidence-dependent retry must bring a strictly newer sample.
        if self._evidence is not None:
            self._failed_sequence = self._evidence.sequence_no
        evidence_refs = self._evidence.evidence_refs if self._evidence is not None else ()
        return RecoveryDecision(
            recovery_id=self._recovery_id,
            task_id=self._task_id,
            action=action,
            state=self._state,
            attempt=self._attempt,
            max_attempts=self._policy.max_attempts,
            ticks=self._ticks,
            max_recovery_ticks=self._policy.max_recovery_ticks,
            reason_code=code,
            reason=reason,
            terminal=action in TERMINAL_ACTIONS,
            runtime_owned=action in RUNTIME_OWNED_ACTIONS,
            evidence_refs=evidence_refs,
        )


def _status_value(status: object) -> str:
    return status.value if isinstance(status, StrEnum) else str(status)


def _reason_value(reason: object) -> str:
    if reason is None:
        return ""
    return reason.value if isinstance(reason, StrEnum) else str(reason)


def _hint_to_action(hint: object) -> RecoveryAction | None:
    if hint is None:
        return None
    if isinstance(hint, RecoveryAction):
        return hint
    if isinstance(hint, RecoveryHint):
        return HINT_TO_ACTION.get(hint)
    if isinstance(hint, str):
        try:
            return RecoveryAction(hint)
        except ValueError:
            try:
                return HINT_TO_ACTION.get(RecoveryHint(hint))
            except ValueError:
                return None
    return None


def _non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecoveryPolicyError(RECOVERY_POLICY_MALFORMED, f"{field_name} must be a non-blank string")
    return value
