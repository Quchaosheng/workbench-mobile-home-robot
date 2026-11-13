"""A support case must carry its evidence hold, or not exist (#590).

``hardware/support/README.md`` defines the severity model and names the case
records, but it had no path to the artifacts a run produces. Without one, an
incident's evidence sat under the ordinary 30-day camera retention, which is the
exact failure ``docs/deployment/data-governance.md``'s hold mechanism exists to
prevent.

These tests pin the four behaviours the issue names, and each one is written so it
fails if the behaviour is removed:

* an S0/S1 case opens its hold in the same operation, so a case cannot exist
  unbacked;
* the hold outranks retention, and ``deletion_decision`` names it as the reason;
* closing a case does not release the hold, and release is its own recorded act;
* a case against an unknown run is refused at creation, not discovered later.

``test_data_governance.py`` is untouched by this change, which is the evidence
that the policy table and its version did not move.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "application"))
sys.path.insert(0, str(ROOT / "hardware" / "support"))

import support_case as sc
from workbench.application.data_governance import HoldRegister, deletion_decision

OPENED = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
# Well past the 30-day camera retention and any other window in the table.
AFTER_RETENTION = OPENED + timedelta(days=400)


def open_case(severity: str = "S0", *, run_id: str = "run-1", holds: HoldRegister | None = None, **overrides):
    arguments = {
        "case_id": "CS-2026-001",
        "unit_serial": "UNIT-0001",
        "severity": severity,
        "run_id": run_id,
        "configuration_hash": "a" * 40,
        "opened_by": "operator",
        "opened_at": OPENED,
        "symptom": "uncontrolled motion during pick",
        "holds": holds if holds is not None else HoldRegister(),
        "known_runs": frozenset({run_id}),
    }
    arguments.update(overrides)
    return sc.open_case(**arguments)


class TestCaseCreation:
    def test_an_s0_case_opens_its_hold_in_the_same_operation(self) -> None:
        holds = HoldRegister()
        case = open_case("S0", holds=holds)

        assert case.holds_evidence is True
        assert case.hold_id == "hold-CS-2026-001"
        opened = {hold.hold_id for hold in holds.holds()}
        assert opened == {"hold-CS-2026-001", "hold-CS-2026-001-unit"}

    def test_the_hold_covers_the_run_and_the_unit_separately(self) -> None:
        """Two scopes, not one wildcard: a `*` hold would cover every other run."""
        holds = HoldRegister()
        case = open_case("S1", holds=holds)
        assert case.hold_id is not None

        covering_run = [hold.scope for hold in holds.covering("run-1", at=OPENED)]
        covering_unit = [hold.scope for hold in holds.covering("UNIT-0001", at=OPENED)]
        covering_other = [hold.scope for hold in holds.covering("run-2", at=OPENED)]

        assert covering_run == ["run-1"]
        assert covering_unit == ["UNIT-0001"]
        assert covering_other == []

    def test_an_s3_case_opens_no_hold_and_says_so(self) -> None:
        """A cosmetic defect must not hold evidence forever, and must not pretend to."""
        holds = HoldRegister()
        case = open_case("S3", holds=holds)

        assert case.holds_evidence is False
        assert case.hold_id is None
        assert holds.holds() == ()

    @pytest.mark.parametrize("severity", ["S2"])
    def test_s2_opens_no_hold(self, severity: str) -> None:
        holds = HoldRegister()
        assert open_case(severity, holds=holds).holds_evidence is False
        assert holds.holds() == ()

    def test_an_unknown_severity_is_refused(self) -> None:
        with pytest.raises(sc.SupportCaseError, match="unknown severity"):
            open_case("S9")

    def test_a_case_against_an_unknown_run_is_refused_at_creation(self) -> None:
        """The failure path: the run is checked before the case exists."""
        holds = HoldRegister()
        with pytest.raises(sc.SupportCaseError, match="not a known run"):
            open_case("S0", holds=holds, known_runs=frozenset({"run-2"}))

        assert holds.holds() == (), "a refused case must leave no hold behind"

    def test_a_refused_case_leaves_no_partial_hold(self) -> None:
        """A hold that cannot be opened must not leave half a case in the register."""
        holds = HoldRegister()
        # Occupy the second hold id so the unit-scope open fails after the first.
        holds.open(
            "hold-CS-2026-001-unit",
            kind=sc.HoldKind.INCIDENT,
            scope="UNIT-0001",
            opened_by="someone",
            reason="pre-existing",
            opened_at=OPENED,
        )

        with pytest.raises(sc.SupportCaseError, match="cannot open the evidence hold"):
            open_case("S0", holds=holds)

        # Only the pre-existing hold remains; the run-scope hold was rolled back.
        assert {hold.hold_id for hold in holds.holds()} == {"hold-CS-2026-001-unit"}

    @pytest.mark.parametrize(
        "field", ["case_id", "unit_serial", "run_id", "configuration_hash", "opened_by", "symptom"]
    )
    def test_every_identifier_must_be_present(self, field: str) -> None:
        with pytest.raises(sc.SupportCaseError, match=f"{field} must be a non-empty string"):
            open_case(**{field: "  "})


class TestHoldOutranksRetention:
    def test_the_hold_blocks_deletion_after_retention_expires(self) -> None:
        holds = HoldRegister()
        case = open_case("S0", holds=holds)

        decision = sc.case_evidence_is_deletable(
            case,
            data_class="run_event_log",
            scope="run-1",
            created_at=OPENED,
            at=AFTER_RETENTION,
            holds=holds,
        )

        assert decision.allowed is False
        assert "active hold protects this scope: hold-CS-2026-001" in decision.reasons
        assert decision.covering_holds == ("hold-CS-2026-001",)

    def test_without_the_hold_the_same_artifact_would_be_deletable(self) -> None:
        """The control: the hold is what changed the answer, not the data class."""
        decision = deletion_decision(
            "run_event_log",
            scope="run-1",
            created_at=OPENED,
            at=AFTER_RETENTION,
            holds=HoldRegister(),
        )
        assert decision.allowed is True

    def test_an_s3_case_does_not_extend_retention(self) -> None:
        holds = HoldRegister()
        case = open_case("S3", holds=holds)
        decision = sc.case_evidence_is_deletable(
            case,
            data_class="run_event_log",
            scope="run-1",
            created_at=OPENED,
            at=AFTER_RETENTION,
            holds=holds,
        )
        assert decision.allowed is True

    def test_hardware_record_is_never_deleted_by_a_lifetime_rule(self) -> None:
        """The table already says this; the case must not weaken it."""
        holds = HoldRegister()
        case = open_case("S0", holds=holds)
        decision = sc.case_evidence_is_deletable(
            case,
            data_class="hardware_record",
            scope="UNIT-0001",
            created_at=OPENED,
            at=AFTER_RETENTION,
            holds=holds,
        )
        assert decision.allowed is False


class TestCloseAndRelease:
    def test_closing_a_case_does_not_release_the_hold(self) -> None:
        holds = HoldRegister()
        case = open_case("S0", holds=holds)

        closed = sc.close_case(
            case, closed_by="investigator", closed_at=OPENED + timedelta(days=2), reason="cause found"
        )

        assert closed.closed_at is not None
        assert {hold.hold_id for hold in holds.holds()} == {"hold-CS-2026-001", "hold-CS-2026-001-unit"}

    def test_release_is_a_separate_recorded_action(self) -> None:
        holds = HoldRegister()
        case = sc.close_case(
            open_case("S0", holds=holds),
            closed_by="investigator",
            closed_at=OPENED + timedelta(days=2),
            reason="cause found",
        )

        released = sc.release_case_hold(
            case,
            holds=holds,
            released_by="data-owner",
            reason="retention satisfied and the report is published",
            released_at=OPENED + timedelta(days=90),
        )

        assert len(released.releases) == 2
        assert {item.released_by for item in released.releases} == {"data-owner"}
        assert all(item.reason for item in released.releases)
        assert holds.holds() == ()

    def test_a_case_that_opened_no_hold_cannot_be_released(self) -> None:
        holds = HoldRegister()
        case = open_case("S3", holds=holds)
        with pytest.raises(sc.SupportCaseError, match="opened no hold"):
            sc.release_case_hold(case, holds=holds, released_by="x", reason="y", released_at=OPENED)

    def test_a_case_cannot_close_twice(self) -> None:
        holds = HoldRegister()
        case = sc.close_case(open_case("S0", holds=holds), closed_by="a", closed_at=OPENED, reason="r")
        with pytest.raises(sc.SupportCaseError, match="already closed"):
            sc.close_case(case, closed_by="b", closed_at=OPENED, reason="r")

    def test_a_case_cannot_close_before_it_opened(self) -> None:
        holds = HoldRegister()
        with pytest.raises(sc.SupportCaseError, match="cannot close before it opened"):
            sc.close_case(open_case("S0", holds=holds), closed_by="a", closed_at=OPENED - timedelta(days=1), reason="r")


class TestEvidenceReferences:
    def test_the_case_names_the_run_and_the_configuration_hash(self) -> None:
        """Identifiers the runtime already uses, not copied file paths."""
        case = open_case("S0")
        assert case.run_id == "run-1"
        assert case.configuration_hash == "a" * 40

    def test_the_held_classes_are_the_three_the_issue_names(self) -> None:
        case = open_case("S0")
        assert set(case.evidence_classes()) == {
            "run_event_log",
            "hardware_record",
            "release_evidence",
        }

    def test_camera_evidence_is_not_held(self) -> None:
        """Holding every frame of every S0 case would cover more than the investigation."""
        case = open_case("S0")
        assert "camera_evidence" not in {item.value for item in case.evidence_classes()}


class TestGovernanceTableUnchanged:
    def test_the_policy_table_version_did_not_move(self) -> None:
        from workbench.application import data_governance

        assert data_governance.GOVERNANCE_RULES_VERSION == "data-governance-v1"

    def test_the_case_module_does_not_define_its_own_retention(self) -> None:
        """One rule about expiry: the governance table's, not a second one here."""
        source = (ROOT / "hardware" / "support" / "support_case.py").read_text(encoding="utf-8")
        # Look for an assignment, not a mention: the module docstring names the
        # version on purpose, to say it is untouched.
        assert "retention_days =" not in source
        assert "retention_days:" not in source
        assert "GOVERNANCE_RULES_VERSION =" not in source
