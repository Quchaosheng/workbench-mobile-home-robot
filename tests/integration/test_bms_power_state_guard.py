"""Motion is refused or derated on the pack state the BMS actually reports (#588).

The pack state machine is declared in `hardware/power/bms-state-machine.csv` and
`hardware/power/bms-transitions.csv`, and `hardware/power/tools/
validate_bms_state_machine.py` already checks the two agree with each other. What
was missing is the reader: nothing in the runtime looked at the pack state before
dispatching a motion request, so a robot whose pack was `FAULT_LATCHED`, `SERVICE`,
`OFF`, `SELF_TEST` or `STANDBY` would still be asked to drive.

These tests drive the guard, not a simulator, and each one is written so it fails
if the behaviour is removed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "agent_runtime"))

from workbench_agent_runtime import power_guard as pg


class TestUnknownStateIsAFault:
    def test_an_undeclared_state_is_refused(self) -> None:
        """Mapping an unrecognised string onto a default is the one reading a guard must not invent."""
        with pytest.raises(pg.PowerGuardError, match="unknown_pack_state"):
            pg.guard_motion("TOTALLY_FINE")

    def test_the_refusal_names_the_known_states(self) -> None:
        """A refusal has to tell the caller what it could have said."""
        with pytest.raises(pg.PowerGuardError) as caught:
            pg.guard_motion("BOGUS")
        message = str(caught.value)
        assert "RUN" in message
        assert "FAULT_LATCHED" in message

    @pytest.mark.parametrize("value", [None, "", "   ", 7, object()])
    def test_a_non_string_or_blank_state_is_refused(self, value: object) -> None:
        with pytest.raises(pg.PowerGuardError, match="unknown_pack_state"):
            pg.guard_motion(value)


class TestStatesThatForbidMotion:
    @pytest.mark.parametrize("state", ["FAULT_LATCHED", "SERVICE", "OFF", "SELF_TEST", "STANDBY"])
    def test_the_five_states_the_issue_names_are_refused(self, state: str) -> None:
        decision = pg.guard_motion(state)

        assert decision.admitted is False
        assert decision.refusal is pg.MotionRefusal.STATE_FORBIDS_MOTION
        # The state is named in the reason, so a caller does not have to guess.
        assert state in decision.reason
        assert decision.metadata()["pack_state"] == state

    @pytest.mark.parametrize("state", ["PRECHARGE", "CHARGE"])
    def test_states_the_table_also_forbids_are_refused(self, state: str) -> None:
        """The CSV gives both states contactors that cannot drive the base."""
        decision = pg.guard_motion(state)
        assert decision.admitted is False
        assert state in decision.reason

    def test_a_refusal_is_a_decision_not_an_exception(self) -> None:
        """A forbidden state is a normal outcome with a reason; only an unknown one raises."""
        decision = pg.guard_motion("FAULT_LATCHED")
        assert decision.admitted is False
        assert decision.refusal is not None


class TestRunAndDerate:
    def test_run_is_admitted_with_the_configured_limit(self) -> None:
        decision = pg.guard_motion("RUN", configured_limit_fraction=1.0)

        assert decision.admitted is True
        assert decision.refusal is None
        assert decision.reduced_limit_fraction is None

    def test_derate_is_admitted_with_a_reduced_limit(self) -> None:
        decision = pg.guard_motion("DERATE", derate_limit_fraction=0.4)

        assert decision.admitted is True
        assert decision.reduced_limit_fraction == pytest.approx(0.4)

    def test_the_reduced_limit_is_visible_in_the_metadata(self) -> None:
        """A clipping that is applied silently is a claim the caller cannot see."""
        metadata = pg.guard_motion("DERATE", derate_limit_fraction=0.4).metadata()

        assert metadata["pack_state"] == "DERATE"
        assert metadata["pack_state_admitted"] is True
        assert metadata["pack_reduced_limit_fraction"] == pytest.approx(0.4)

    def test_the_derate_limit_never_exceeds_the_configured_limit(self) -> None:
        """A derate cannot raise a limit that was already set lower."""
        decision = pg.guard_motion(
            "DERATE",
            configured_limit_fraction=0.3,
            derate_limit_fraction=0.8,
        )
        assert decision.reduced_limit_fraction == pytest.approx(0.3)

    @pytest.mark.parametrize("value", [0.0, -0.1, 1.5])
    def test_a_nonsensical_limit_is_refused(self, value: float) -> None:
        with pytest.raises(pg.PowerGuardError, match="limit must be in"):
            pg.guard_motion("RUN", derate_limit_fraction=value)


class TestResetAuthority:
    def test_the_bms_may_clear_its_own_latch(self) -> None:
        decision = pg.guard_reset("DERATE")
        assert decision.admitted is True

    @pytest.mark.parametrize("state", ["FAULT_LATCHED", "SERVICE", "OFF", "SELF_TEST", "STANDBY", "PRECHARGE"])
    def test_the_runtime_may_not_clear_what_needs_a_human(self, state: str) -> None:
        """`operator_plus_service` needs two humans; the runtime is neither."""
        decision = pg.guard_reset(state)

        assert decision.admitted is False
        assert decision.refusal is pg.MotionRefusal.RESET_NOT_AUTHORIZED
        assert state in decision.reason

    def test_the_refusal_names_the_required_authority(self) -> None:
        decision = pg.guard_reset("FAULT_LATCHED")
        # The CSV row for FAULT_LATCHED says service_owner.
        assert "service_owner" in decision.reason


class TestMidMotion:
    def test_a_latch_mid_motion_is_a_stop_that_records_the_transition(self) -> None:
        decision = pg.guard_mid_motion("FAULT_LATCHED")

        assert decision.admitted is False
        assert decision.refusal is pg.MotionRefusal.LATCHED_MID_MOTION
        assert "FAULT_LATCHED" in decision.reason
        # The fault action from the CSV travels with the reason.
        assert "remain_deenergized" in decision.reason

    def test_run_permits_continued_motion(self) -> None:
        assert pg.guard_mid_motion("RUN").admitted is True

    def test_derate_permits_continued_motion(self) -> None:
        assert pg.guard_mid_motion("DERATE").admitted is True


class TestTheCsvIsTheSingleSource:
    def test_the_table_is_read_from_the_committed_csv(self) -> None:
        table = pg.load_state_table()

        assert "RUN" in table
        assert table["RUN"].permits_motion is True
        assert table["FAULT_LATCHED"].permits_motion is False
        # The reset authority comes from the file, not from this module.
        assert table["FAULT_LATCHED"].reset_authority

    def test_every_declared_state_has_a_guard(self) -> None:
        """A state added to the CSV without a guard fails here, so the two cannot drift."""
        cov = pg.coverage()

        assert cov.ungarded == ()
        assert set(cov.motion_permitted) == {"RUN", "DERATE"}
        # Every declared state reaches a decision rather than raising.
        for state in cov.declared:
            decision = pg.guard_motion(state)
            assert decision.state is not None
            assert decision.state.state == state

    @pytest.mark.parametrize("state", sorted(pg.coverage().declared))
    def test_admission_matches_the_coverage_report(self, state: str) -> None:
        """The report and the guard must not disagree about a state."""
        cov = pg.coverage()
        admitted = pg.guard_motion(state).admitted
        assert admitted is (state in cov.motion_permitted), state

    def test_the_guard_reads_the_csv_rather_than_a_copy(self) -> None:
        """A second list of states would drift; the module reads the file."""
        source = (ROOT / "services/agent_runtime/workbench_agent_runtime/power_guard.py").read_text(encoding="utf-8")
        assert "bms-state-machine.csv" in source
        assert "load_state_table" in source

    def test_the_committed_validator_still_passes(self) -> None:
        """The table the guard reads is the table the existing validator checks."""
        import subprocess

        result = subprocess.run(
            [sys.executable, "hardware/power/tools/validate_bms_state_machine.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
