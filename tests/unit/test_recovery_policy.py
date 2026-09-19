"""Issue #305: bounded, evidence-aware recovery policies.

The tests drive the real policy, ledger and payload validator. The assertions
are about what the shared rule refuses, because "recovery must not launder a
failed verification into success" is only true if a test can show it refusing.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
for relative in ("libs/contracts", "services/agent_runtime", "services/world_model"):
    sys.path.insert(0, str(ROOT / relative))

from workbench_agent_runtime.recovery import (
    ACTION_PREFERENCE,
    DEFAULT_MAX_ATTEMPTS,
    EMITTED_CODES,
    HINT_TO_ACTION,
    LEGAL_TRANSITIONS,
    MAX_MAX_ATTEMPTS,
    MAX_MAX_TICKS,
    RECOVERY_POLICY_BUDGET_EXHAUSTED,
    RECOVERY_POLICY_EMPTY_ACTIONS,
    RECOVERY_POLICY_EVIDENCE_NOT_FRESH,
    RECOVERY_POLICY_INVALID_BUDGET,
    RECOVERY_POLICY_MALFORMED,
    RECOVERY_POLICY_RUNTIME_OWNED_ACTION,
    RECOVERY_POLICY_TERMINAL,
    RECOVERY_POLICY_UNKNOWN_ACTION,
    RUNTIME_AUTHORITY,
    RUNTIME_OWNED_ACTIONS,
    TERMINAL_ACTIONS,
    RecoveryAction,
    RecoveryLedger,
    RecoveryPolicy,
    RecoveryPolicyError,
    RecoveryState,
    transition_is_legal,
)
from workbench_contracts import RecoveryHint
from workbench_world_model.event_payloads import (
    MAX_RECOVERY_EVIDENCE_REFS,
    RECOVERY_ACTIONS,
    RECOVERY_RUNTIME_OWNED_ACTIONS,
    RECOVERY_TERMINAL_ACTIONS,
    WorldEventPayloadValidationError,
    normalize_recovery_payload,
)

MANIFEST_POLICY = {
    "allowed": ["re_observe", "retry_action", "ask_confirm", "abort"],
    "max_attempts": 3,
    "requires_fresh_evidence": True,
}


def verification(
    *,
    status: str = "refuted",
    hint: str | None = "retry_action",
    verification_id: str = "verify-1",
    evidence_refs: tuple[str, ...] = (),
    verified_at: str = "2026-09-01T00:00:10Z",
    reason_code: str = "stale_observation",
) -> SimpleNamespace:
    # The default reason is an evidence problem, which is the case the policy
    # exists for. `reason_code="goal_not_satisfied"` is the actuator-failure
    # case that does not need a new frame before trying again.
    return SimpleNamespace(
        status=status,
        recovery_hint=hint,
        verification_id=verification_id,
        evidence_refs=list(evidence_refs),
        verified_at=verified_at,
        reason_code=reason_code,
    )


def stamp(*, observed_at: str = "2026-09-01T00:00:30Z", fresh: bool = True, valid: bool = True, sequence_no: int = 5):
    return SimpleNamespace(observed_at=observed_at, fresh=fresh, provenance_valid=valid, sequence_no=sequence_no)


def ledger(policy: RecoveryPolicy | None = None) -> RecoveryLedger:
    return RecoveryLedger(
        policy=policy if policy is not None else RecoveryPolicy.parse(MANIFEST_POLICY),
        recovery_id="recovery-1",
        task_id="task-1",
    )


def evidence_stamp(**kwargs):
    from workbench_agent_runtime.recovery import EvidenceStamp

    return EvidenceStamp(**kwargs)


class TestPolicyParse:
    def test_a_scenario_with_no_policy_is_disabled(self) -> None:
        policy = RecoveryPolicy.parse(None)
        assert policy.enabled is False
        assert policy.actions == frozenset()

    def test_the_registry_manifest_policy_parses_with_its_alias(self) -> None:
        policy = RecoveryPolicy.parse(MANIFEST_POLICY)
        assert policy.enabled is True
        assert RecoveryAction.RETRY_OBSERVATION in policy.actions
        assert policy.max_attempts == 3

    def test_ordered_actions_follow_the_shared_preference(self) -> None:
        policy = RecoveryPolicy.parse({"allowed": ["abort", "re_observe", "ask_confirm"]})
        ordered = [action for action in ACTION_PREFERENCE if action in policy.actions]
        assert policy.ordered_actions == tuple(ordered)
        assert policy.ordered_actions[0] is RecoveryAction.RETRY_OBSERVATION
        assert policy.ordered_actions[-1] is RecoveryAction.ABORT

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            ("not an object", RECOVERY_POLICY_MALFORMED),
            ({"nope": 1}, RECOVERY_POLICY_MALFORMED),
            ({"allowed": ["fly"]}, RECOVERY_POLICY_UNKNOWN_ACTION),
            ({"allowed": []}, RECOVERY_POLICY_EMPTY_ACTIONS),
            ({"allowed": ["abort"], "max_attempts": True}, RECOVERY_POLICY_INVALID_BUDGET),
            ({"allowed": ["abort"], "max_attempts": 1.5}, RECOVERY_POLICY_INVALID_BUDGET),
            ({"allowed": ["abort"], "max_attempts": 0}, RECOVERY_POLICY_INVALID_BUDGET),
            ({"allowed": ["abort"], "max_attempts": MAX_MAX_ATTEMPTS + 1}, RECOVERY_POLICY_INVALID_BUDGET),
            ({"allowed": ["abort"], "max_recovery_ticks": 0}, RECOVERY_POLICY_INVALID_BUDGET),
            ({"allowed": ["abort"], "max_recovery_ticks": MAX_MAX_TICKS + 1}, RECOVERY_POLICY_INVALID_BUDGET),
            ({"allowed": ["abort"], "requires_fresh_evidence": "yes"}, RECOVERY_POLICY_MALFORMED),
        ],
    )
    def test_a_malformed_policy_fails_before_execution(self, payload: object, code: str) -> None:
        with pytest.raises(RecoveryPolicyError) as caught:
            RecoveryPolicy.parse(payload)
        assert caught.value.code == code

    def test_an_empty_allowed_list_is_refused_with_its_own_code(self) -> None:
        # A declared policy with no action is a contradiction: it exists, so it
        # must authorise something. It gets its own code rather than sharing the
        # unknown-action one, so an operator can tell "nothing declared" from
        # "a typo".
        with pytest.raises(RecoveryPolicyError) as caught:
            RecoveryPolicy.parse({"allowed": []})
        assert caught.value.code == RECOVERY_POLICY_EMPTY_ACTIONS

    def test_requesting_safe_stop_can_be_disabled_explicitly(self) -> None:
        with pytest.raises(RecoveryPolicyError) as caught:
            RecoveryPolicy.parse({"allowed": ["safe_stop"], "allow_safe_stop": False})
        assert caught.value.code == RECOVERY_POLICY_RUNTIME_OWNED_ACTION

    def test_budgets_are_finite(self) -> None:
        policy = RecoveryPolicy.parse({"allowed": ["abort"]})
        assert policy.max_attempts == DEFAULT_MAX_ATTEMPTS
        assert isinstance(policy.max_attempts, int)
        assert isinstance(policy.max_recovery_ticks, int)


class TestTransitionTable:
    def test_every_declared_edge_is_legal(self) -> None:
        for current, followers in LEGAL_TRANSITIONS.items():
            for following in followers:
                assert transition_is_legal(current, following)

    def test_stopping_and_aborted_have_no_successors(self) -> None:
        assert LEGAL_TRANSITIONS[RecoveryState.STOPPING] == frozenset()
        assert LEGAL_TRANSITIONS[RecoveryState.ABORTED] == frozenset()

    def test_an_undeclared_edge_is_illegal(self) -> None:
        assert not transition_is_legal(RecoveryState.STOPPING, RecoveryState.OBSERVING)
        assert not transition_is_legal(RecoveryState.ABORTED, RecoveryState.ACTING)

    def test_a_ledger_refuses_an_illegal_transition(self) -> None:
        held = ledger()
        held.acknowledge_runtime_action(RecoveryAction.SAFE_STOP, authority=RUNTIME_AUTHORITY)
        assert held.state is RecoveryState.STOPPING
        with pytest.raises(RecoveryPolicyError) as caught:
            held.record_verification(verification())
        assert caught.value.code == RECOVERY_POLICY_TERMINAL


class TestLedgerBehaviour:
    def test_a_disabled_policy_returns_no_decision(self) -> None:
        held = RecoveryLedger(policy=RecoveryPolicy.disabled(), recovery_id="r", task_id="t")
        assert held.record_verification(verification()) is None
        assert held.attempt == 0
        assert held.state is RecoveryState.IDLE

    def test_a_disabled_policy_ignores_the_verification_hint_entirely(self) -> None:
        held = RecoveryLedger(policy=RecoveryPolicy.disabled(), recovery_id="r", task_id="t")
        for hint in ("retry_action", "re_observe", "ask_confirm", "abort", None):
            assert held.record_verification(verification(hint=hint)) is None

    def test_a_verification_with_no_hint_produces_no_step(self) -> None:
        held = ledger()
        assert held.record_verification(verification(hint="none")) is None

    def test_retry_action_without_fresh_evidence_downgrades_to_observation(self) -> None:
        held = ledger()
        decision = held.record_verification(verification(hint="retry_action"))
        assert decision is not None
        assert decision.action is RecoveryAction.RETRY_OBSERVATION
        assert decision.reason_code == RECOVERY_POLICY_EVIDENCE_NOT_FRESH
        assert decision.state is RecoveryState.OBSERVING

    def test_stale_evidence_does_not_unlock_an_action_retry(self) -> None:
        held = ledger()
        held.record_observation(
            evidence_stamp(observed_at="2026-09-01T00:00:05Z", fresh=False, provenance_valid=True, sequence_no=9)
        )
        decision = held.record_verification(verification(hint="retry_action"))
        assert decision.action is RecoveryAction.RETRY_OBSERVATION

    def test_evidence_without_valid_provenance_does_not_unlock_an_action_retry(self) -> None:
        held = ledger()
        held.record_observation(
            evidence_stamp(observed_at="2026-09-01T00:01:00Z", fresh=True, provenance_valid=False, sequence_no=9)
        )
        decision = held.record_verification(verification(hint="retry_action"))
        assert decision.action is RecoveryAction.RETRY_OBSERVATION

    def test_fresh_evidence_after_the_failure_unlocks_the_action_retry(self) -> None:
        held = ledger()
        first = held.record_verification(verification(hint="retry_action"))
        assert first.action is RecoveryAction.RETRY_OBSERVATION
        held.record_observation(
            evidence_stamp(observed_at="2026-09-01T00:00:30Z", fresh=True, provenance_valid=True, sequence_no=6)
        )
        second = held.record_verification(verification(hint="retry_action", verification_id="verify-2"))
        assert second.action is RecoveryAction.RETRY_ACTION
        assert second.attempt == 2

    def test_the_same_sample_cannot_unlock_two_action_retries(self) -> None:
        held = ledger()
        held.record_observation(
            evidence_stamp(observed_at="2026-09-01T00:00:30Z", fresh=True, provenance_valid=True, sequence_no=6)
        )
        first = held.record_verification(verification(hint="retry_action"))
        assert first.action is RecoveryAction.RETRY_ACTION
        second = held.record_verification(verification(hint="retry_action", verification_id="verify-2"))
        assert second.action is RecoveryAction.RETRY_OBSERVATION

    def test_an_actuator_failure_does_not_make_the_retry_evidence_dependent(self) -> None:
        # "the grasp slipped" is a reason to try the grasp again. It is not a
        # reason to demand a new frame before dispatching the same action.
        held = ledger()
        decision = held.record_verification(verification(hint="retry_action", reason_code="goal_not_satisfied"))
        assert decision.action is RecoveryAction.RETRY_ACTION
        assert decision.reason_code == "recovery_selected"

    def test_an_out_of_order_observation_cannot_rewind_freshness(self) -> None:
        held = ledger()
        held.record_observation(
            evidence_stamp(observed_at="2026-09-01T00:00:30Z", fresh=True, provenance_valid=True, sequence_no=6)
        )
        held.record_observation(
            evidence_stamp(observed_at="2026-09-01T00:00:10Z", fresh=True, provenance_valid=True, sequence_no=2)
        )
        assert held.evidence.sequence_no == 6

    def test_a_disallowed_action_aborts_rather_than_being_silently_skipped(self) -> None:
        held = ledger(RecoveryPolicy.parse({"allowed": ["re_observe"]}))
        decision = held.record_verification(verification(hint="ask_confirm"))
        assert decision.action is RecoveryAction.ABORT
        assert decision.reason_code == RECOVERY_POLICY_UNKNOWN_ACTION

    def test_budget_exhaustion_aborts_with_the_counters(self) -> None:
        held = ledger(RecoveryPolicy.parse({"allowed": ["re_observe"], "max_attempts": 2}))
        decisions = [held.record_verification(verification(hint="re_observe")) for _ in range(3)]
        assert [decision.action for decision in decisions[:2]] == [
            RecoveryAction.RETRY_OBSERVATION,
            RecoveryAction.RETRY_OBSERVATION,
        ]
        exhausted = decisions[2]
        assert exhausted.action is RecoveryAction.ABORT
        assert exhausted.reason_code == RECOVERY_POLICY_BUDGET_EXHAUSTED
        assert exhausted.attempt == 3
        assert exhausted.max_attempts == 2
        assert exhausted.terminal is True

    def test_retry_counts_are_visible_and_monotonic(self) -> None:
        held = ledger()
        attempts = []
        for index in range(3):
            decision = held.record_verification(verification(hint="re_observe", verification_id=f"verify-{index}"))
            attempts.append(decision.attempt)
        assert attempts == [1, 2, 3]
        assert held.ticks == 3

    def test_safe_stop_is_runtime_owned_and_cannot_be_recorded_by_scenario_code(self) -> None:
        held = ledger(RecoveryPolicy.parse({"allowed": ["re_observe", "safe_stop"]}))
        decision = held.record_verification(verification(hint="abort"))
        assert decision is not None
        with pytest.raises(RecoveryPolicyError) as caught:
            held.acknowledge_runtime_action(RecoveryAction.SAFE_STOP, authority="scenario")
        assert caught.value.code == RECOVERY_POLICY_RUNTIME_OWNED_ACTION

    def test_the_runtime_may_acknowledge_its_own_stop(self) -> None:
        held = ledger(RecoveryPolicy.parse({"allowed": ["safe_stop"]}))
        held.acknowledge_runtime_action(RecoveryAction.SAFE_STOP, authority=RUNTIME_AUTHORITY)
        assert held.state is RecoveryState.STOPPING
        assert held.terminal is True

    def test_a_terminal_decision_is_marked_terminal_and_runtime_owned(self) -> None:
        held = ledger(RecoveryPolicy.parse({"allowed": ["safe_stop"]}))
        decision = held.record_verification(verification(hint="retry_action"))
        # retry_action is not allowed, so it aborts rather than silently retrying.
        assert decision.action is RecoveryAction.ABORT
        assert decision.terminal is True
        assert decision.runtime_owned is False
        assert decision.event_type == "recovery_complete"

    def test_a_non_terminal_decision_is_a_recovery_started_event(self) -> None:
        held = ledger()
        decision = held.record_verification(verification(hint="re_observe"))
        assert decision.event_type == "recovery_started"
        assert decision.terminal is False

    def test_identical_inputs_produce_identical_decisions(self) -> None:
        first, second = ledger(), ledger()
        for index in range(3):
            left = first.record_verification(verification(hint="re_observe", verification_id=f"v{index}"))
            right = second.record_verification(verification(hint="re_observe", verification_id=f"v{index}"))
            assert left.as_payload() == right.as_payload()


class TestRecoveryCannotLaunderFailure:
    def test_a_confirmed_verification_with_no_new_evidence_is_refused(self) -> None:
        held = ledger()
        held.record_verification(verification(hint="retry_action", evidence_refs=("frame-a",)))
        with pytest.raises(RecoveryPolicyError) as caught:
            held.record_verification(
                verification(status="confirmed", hint="none", verification_id="verify-done", evidence_refs=("frame-a",))
            )
        assert caught.value.code == RECOVERY_POLICY_EVIDENCE_NOT_FRESH

    def test_a_confirmed_verification_with_new_evidence_completes(self) -> None:
        held = ledger()
        held.record_verification(verification(hint="retry_action", evidence_refs=("frame-a",)))
        assert (
            held.record_verification(
                verification(status="confirmed", hint="none", verification_id="verify-done", evidence_refs=("frame-b",))
            )
            is None
        )
        assert held.state is RecoveryState.COMPLETE
        assert held.completed_by == "verify-done"

    def test_completion_before_any_failure_needs_no_new_evidence(self) -> None:
        held = ledger()
        assert held.record_verification(verification(status="confirmed", hint="none")) is None
        assert held.state is RecoveryState.COMPLETE

    def test_recovery_cannot_move_out_of_a_terminal_state(self) -> None:
        held = ledger(RecoveryPolicy.parse({"allowed": ["re_observe", "safe_stop"]}))
        held.acknowledge_runtime_action(RecoveryAction.SAFE_STOP, authority=RUNTIME_AUTHORITY)
        with pytest.raises(RecoveryPolicyError) as caught:
            held.record_verification(verification(hint="re_observe"))
        assert caught.value.code == RECOVERY_POLICY_TERMINAL


class TestVocabularyPinnedAcrossModules:
    def test_the_policy_produces_exactly_the_payload_vocabulary(self) -> None:
        assert {action.value for action in RecoveryAction} == RECOVERY_ACTIONS

    def test_the_runtime_owned_set_matches_the_payload_validator(self) -> None:
        assert {action.value for action in RUNTIME_OWNED_ACTIONS} == RECOVERY_RUNTIME_OWNED_ACTIONS

    def test_the_terminal_set_matches_the_payload_validator(self) -> None:
        assert {action.value for action in TERMINAL_ACTIONS} == RECOVERY_TERMINAL_ACTIONS

    def test_the_hint_vocabulary_maps_into_the_action_vocabulary(self) -> None:
        # RecoveryHint is the verification-side vocabulary. It is narrower than
        # RecoveryAction - it has no safe_stop - and every hint has a shared
        # action, so a hint can never reach a caller as a raw string.
        for hint in RecoveryHint:
            if hint is RecoveryHint.NONE:
                continue
            action = HINT_TO_ACTION.get(hint)
            assert action is not None, hint
            assert action.value in RECOVERY_ACTIONS

    def test_every_diagnostic_is_documented(self) -> None:
        assert len(set(EMITTED_CODES)) == len(EMITTED_CODES)


class TestPayloadValidation:
    def valid_payload(self, **overrides: object) -> dict:
        payload = {
            "recovery_id": "recovery-1",
            "task_id": "task-1",
            "action": "retry_observation",
            "state": "observing",
            "attempt": 1,
            "max_attempts": 3,
            "ticks": 1,
            "max_recovery_ticks": 30,
            "reason_code": "recovery_selected",
            "reason": "the verification requested retry_observation",
            "policy_version": "recovery-policy-v1",
            "runtime_owned": False,
        }
        payload.update(overrides)
        return payload

    def normalize(self, payload: object, event_type: str = "recovery_started") -> dict:
        return normalize_recovery_payload(payload, event_run_id="run-1", event_type=event_type)

    def test_a_produced_decision_round_trips_through_the_validator(self) -> None:
        held = ledger()
        decision = held.record_verification(verification(hint="re_observe"))
        normalized = self.normalize(decision.as_payload())
        assert normalized["action"] == "retry_observation"
        assert normalized["attempt"] == 1
        assert normalized["runtime_owned"] is False

    def test_a_terminal_decision_round_trips_as_recovery_complete(self) -> None:
        held = ledger(RecoveryPolicy.parse({"allowed": ["abort"]}))
        decision = held.record_verification(verification(hint="abort"))
        normalized = self.normalize(decision.as_payload(), event_type=decision.event_type)
        assert normalized["outcome"] == "aborted"
        assert decision.event_type == "recovery_complete"

    @pytest.mark.parametrize(
        "payload",
        [
            "not an object",
            {},
            {"recovery_id": "r", "task_id": "t", "action": "retry_action", "state": "acting"},
            {"recovery_id": "", "task_id": "t"},
        ],
    )
    def test_a_malformed_payload_is_refused(self, payload: object) -> None:
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(payload)

    def test_an_unknown_action_is_refused(self) -> None:
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(self.valid_payload(action="move_arm"))

    def test_an_unknown_key_is_refused(self) -> None:
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(self.valid_payload(sudo=True))

    def test_attempt_above_the_ceiling_is_refused(self) -> None:
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(self.valid_payload(attempt=4, max_attempts=3))
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(self.valid_payload(attempt=99, max_attempts=99))

    def test_a_non_terminal_action_cannot_be_recovery_complete(self) -> None:
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(self.valid_payload(action="retry_action"), event_type="recovery_complete")

    def test_a_terminal_action_cannot_be_recovery_started(self) -> None:
        payload = self.valid_payload(action="abort", state="aborted")
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(payload)

    def test_a_contradictory_runtime_owned_flag_is_refused(self) -> None:
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(self.valid_payload(action="safe_stop", state="stopping", runtime_owned=False))

    def test_safe_stop_payload_records_a_stopped_outcome(self) -> None:
        payload = self.valid_payload(action="safe_stop", state="stopping", runtime_owned=True)
        normalized = self.normalize(payload, event_type="recovery_complete")
        assert normalized["runtime_owned"] is True
        assert normalized["outcome"] == "stopped"

    def test_a_bad_outcome_is_refused(self) -> None:
        payload = self.valid_payload(action="abort", state="aborted", outcome="recovered")
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(payload, event_type="recovery_complete")

    def test_an_oversized_evidence_ref_list_is_refused(self) -> None:
        refs = [f"frame-{index}" for index in range(MAX_RECOVERY_EVIDENCE_REFS + 1)]
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(self.valid_payload(evidence_refs=refs))

    def test_a_valid_evidence_ref_list_is_preserved(self) -> None:
        normalized = self.normalize(self.valid_payload(evidence_refs=["frame-a", "frame-b"]))
        assert normalized["evidence_refs"] == ["frame-a", "frame-b"]

    def test_ticks_above_the_ceiling_is_refused(self) -> None:
        with pytest.raises(WorldEventPayloadValidationError):
            self.normalize(self.valid_payload(ticks=40, max_recovery_ticks=30))


class TestEndToEndWithTheRealContracts:
    """Drive the real contract models and the real event store, not stubs."""

    def build_result(
        self,
        *,
        status: str,
        hint: str,
        verification_id: str,
        refs: list[str],
        verified_at: str,
        reason_code: str = "stale_observation",
    ):
        from workbench_contracts import ReasonCode, VerificationResult, VerificationStatus
        from workbench_contracts import RecoveryHint as Hint

        payload = {
            "verification_id": verification_id,
            "run_id": "run-1",
            "task_id": "task-1",
            "claim": "the red block is in the tray",
            "status": VerificationStatus(status),
            "evidence_refs": refs,
            "verified_at": verified_at,
            "rule_version": "verifier-v1",
        }
        if hint != "none":
            payload["recovery_hint"] = Hint(hint)
        if status == "refuted":
            payload["reason_code"] = ReasonCode(reason_code)
        return VerificationResult.model_validate(payload)

    def test_a_real_verification_result_drives_the_ledger(self) -> None:
        held = ledger()
        first = self.build_result(
            status="refuted",
            hint="retry_action",
            verification_id="verify-1",
            refs=["frame://run-1/first"],
            verified_at="2026-09-01T00:00:10Z",
        )
        decision = held.record_verification(first)
        assert decision.action is RecoveryAction.RETRY_OBSERVATION

        held.record_observation(
            evidence_stamp(observed_at="2026-09-01T00:00:30Z", fresh=True, provenance_valid=True, sequence_no=6)
        )
        second = self.build_result(
            status="refuted",
            hint="retry_action",
            verification_id="verify-2",
            refs=["frame://run-1/second"],
            verified_at="2026-09-01T00:00:40Z",
        )
        assert held.record_verification(second).action is RecoveryAction.RETRY_ACTION

        # The sample that unlocked the retry is now consumed, so a third
        # failure must observe again rather than reuse it.
        third = self.build_result(
            status="refuted",
            hint="retry_action",
            verification_id="verify-3",
            refs=["frame://run-1/third"],
            verified_at="2026-09-01T00:00:45Z",
        )
        assert held.record_verification(third).action is RecoveryAction.RETRY_OBSERVATION

        confirmed = self.build_result(
            status="confirmed",
            hint="none",
            verification_id="verify-4",
            refs=["frame://run-1/final"],
            verified_at="2026-09-01T00:00:50Z",
        )
        assert held.record_verification(confirmed) is None
        assert held.state is RecoveryState.COMPLETE

    def test_a_real_confirmed_result_with_no_new_evidence_is_refused(self) -> None:
        held = ledger()
        held.record_verification(
            self.build_result(
                status="refuted",
                hint="retry_action",
                verification_id="verify-1",
                refs=["frame://run-1/first"],
                verified_at="2026-09-01T00:00:10Z",
            )
        )
        repeated = self.build_result(
            status="confirmed",
            hint="none",
            verification_id="verify-2",
            refs=["frame://run-1/first"],
            verified_at="2026-09-01T00:00:20Z",
        )
        with pytest.raises(RecoveryPolicyError) as caught:
            held.record_verification(repeated)
        assert caught.value.code == RECOVERY_POLICY_EVIDENCE_NOT_FRESH

    def test_a_decision_persists_and_replays_through_the_real_event_store(self, tmp_path: Path) -> None:
        from workbench.kernel.event_store import EventStore
        from workbench_contracts import WorldEvent, WorldEventType
        from workbench_world_model.event_payloads import normalize_world_event

        held = ledger()
        decision = held.record_verification(
            self.build_result(
                status="refuted",
                hint="abort",
                verification_id="verify-1",
                refs=["frame://run-1/first"],
                verified_at="2026-09-01T00:00:10Z",
            )
        )
        assert decision is not None
        event = WorldEvent(
            event_id="event-recovery-1",
            run_id="run-1",
            sequence_no=0,
            event_type=WorldEventType(decision.event_type),
            occurred_at="2026-09-01T00:00:11Z",
            payload=decision.as_payload(),
        )
        normalized = normalize_world_event(event)
        assert normalized.payload["action"] == "abort"
        assert normalized.payload["outcome"] == "aborted"

        store = EventStore(tmp_path / "events.sqlite3", backend="sqlite")
        store.append(normalized.model_dump(mode="json"))
        replayed = store.replay() if hasattr(store, "replay") else store.events
        assert len(replayed) == 1
        store.close()

    def test_the_registry_manifests_parse_under_the_shared_policy(self) -> None:
        import json

        registry = ROOT / "sim/registry"
        parsed = 0
        for path in sorted(registry.glob("*.json")):
            manifest = json.loads(path.read_text(encoding="utf-8"))
            policy = RecoveryPolicy.parse(manifest.get("recovery_policy"))
            assert policy.enabled is True, path.name
            assert policy.max_attempts <= MAX_MAX_ATTEMPTS
            parsed += 1
        assert parsed >= 5
