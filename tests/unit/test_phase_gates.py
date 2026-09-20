"""Issue #314: a machine-generated phase-gate status for incremental delivery.

The status is only worth publishing if a reviewer can see what it refuses, so
every test here drives a refusal as well as a success:

* each declared phase has exactly one row, and a phase with a gate that does not
  pass is never marked completed;
* a gate that answers INCOMPLETE holds the phase at INCOMPLETE rather than
  letting it pass, and a delivery probe that answers NOT_DELIVERED marks the
  phase BLOCKED;
* a phase that was never delivered reached no evidence class, so it cannot borrow
  the class a neighbouring phase earned;
* release eligibility follows the readiness report, and a software-only phase is
  never release eligible;
* two generations in one checkout agree except for the two fields that describe
  when and where the status was built, and the gate fails a hand-edited artifact.

The tests exercise the real generator and the real gate. They never write a run,
start a simulator or claim a physical validation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "tools/scripts"),
    str(ROOT / "libs/kernel"),
    str(ROOT / "libs/contracts"),
    str(ROOT / "services/world_model"),
    str(ROOT / "services/agent_runtime"),
    str(ROOT / "services/backend"),
]

import phase_gates as pg

GATE = ROOT / "tools/scripts/check_phase_gates.py"
GENERATOR = ROOT / "tools/scripts/phase_gates.py"
COMMITTED = ROOT / "docs/releases/phase-status-v1.json"
PAGE = ROOT / "docs/releases/phase-gates.md"
NOTES = ROOT / "docs/releases/release-notes.md"
README = ROOT / "README.md"
README_ZH = ROOT / "README.zh-CN.md"
READINESS = ROOT / "docs/evaluation/readiness-report-v1.json"


def _committed() -> dict:
    return json.loads(COMMITTED.read_text(encoding="utf-8"))


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def _block(text: str, begin: str, end: str) -> str:
    start = text.index(begin)
    stop = text.index(end)
    return text[start : stop + len(end)]


# --- the committed artifact is the one this checkout generates ----------------


def test_committed_status_matches_the_generator():
    committed = _committed()
    regenerated = pg.build_status()
    assert committed["status_version"] == pg.STATUS_VERSION
    assert committed["generated_by"] == pg.GENERATED_BY
    for key in ("configuration_hash", "summary", "phases", "readiness", "limitations"):
        assert committed[key] == regenerated[key], key


def test_two_generations_agree_except_for_where_and_when():
    first = pg.build_status()
    second = pg.build_status()
    volatile = {"generated_at", "source_commit"}
    assert {k: v for k, v in first.items() if k not in volatile} == {
        k: v for k, v in second.items() if k not in volatile
    }


def test_status_states_its_authority_and_its_edit_policy():
    committed = _committed()
    assert committed["authority"] == pg.AUTHORITY
    assert "release approval" in committed["authority"]["does_not_grant"]
    assert "a completed human sign-off" in committed["authority"]["does_not_grant"]
    assert "generated" in committed["edit_policy"]
    assert "hand-edit" in committed["edit_policy"]
    assert set(committed["generated_fields"]) >= {
        "generated_at",
        "configuration_hash",
        "phases[].gate_status",
        "phases[].completed",
        "summary",
    }


# --- every declared phase has exactly one row ---------------------------------


def test_every_declared_phase_has_exactly_one_row():
    committed = _committed()
    numbers = [row["phase"] for row in committed["phases"]]
    assert sorted(numbers) == sorted(phase["phase"] for phase in pg.PHASES)
    assert len(numbers) == len(set(numbers))


def test_a_row_records_the_fields_a_reviewer_needs():
    required = {
        "phase",
        "name",
        "issues",
        "entry_criteria",
        "exit_criteria",
        "required_evidence",
        "probes",
        "gate_status",
        "completed",
        "evidence_class",
        "release_eligible",
        "rollback",
        "blocked_on",
        "sign_off",
        "sign_off_outstanding",
    }
    for row in _committed()["phases"]:
        assert required <= set(row), row["phase"]
        assert row["probes"], row["phase"]
        assert row["sign_off"], row["phase"]


def test_every_real_probe_passes_in_this_checkout():
    for name in pg.PROBES:
        code, detail = pg.run_probe(name)
        assert code in (pg.PASS, pg.NOT_DELIVERED), (name, pg.VERDICTS[code], detail)


# --- a phase is complete only when it proves itself ---------------------------


def test_a_phase_with_a_delivered_probe_is_not_marked_complete():
    committed = _committed()
    for row in committed["phases"]:
        labels = [probe["gate_status"] for probe in row["probes"]]
        if all(label == "PASS" for label in labels):
            assert row["completed"] is True, row["phase"]
        else:
            assert row["completed"] is False, row["phase"]


def test_a_failing_gate_holds_its_phase_out_of_complete(monkeypatch):
    real = pg.run_probe

    def fake(name: str) -> tuple[int, str]:
        if name == "scenario_registry":
            return pg.FAIL, "the registry failed under test"
        return real(name)

    monkeypatch.setattr(pg, "run_probe", fake)
    rows = {row["phase"]: row for row in pg.build_status()["phases"]}
    assert rows[1]["gate_status"] == "FAIL"
    assert rows[1]["completed"] is False


def test_an_incomplete_gate_holds_its_phase_at_incomplete(monkeypatch):
    real = pg.run_probe

    def fake(name: str) -> tuple[int, str]:
        if name == "scenario_registry":
            return pg.INCOMPLETE, "the registry could not be read under test"
        return real(name)

    monkeypatch.setattr(pg, "run_probe", fake)
    rows = {row["phase"]: row for row in pg.build_status()["phases"]}
    assert rows[1]["gate_status"] == "INCOMPLETE"
    assert rows[1]["completed"] is False


def test_an_undeclared_probe_fails_rather_than_passing():
    code, detail = pg.run_probe("not_a_declared_probe")
    assert code == pg.FAIL
    assert "not implemented" in detail


# --- a phase that was never delivered reached nothing -------------------------


def test_the_undelivered_phase_is_blocked_and_reaches_no_evidence_class():
    rows = {row["phase"]: row for row in _committed()["phases"]}
    blocked = [row for row in rows.values() if row["gate_status"] == "BLOCKED"]
    assert blocked, "the status declares no blocked phase to check"
    for row in blocked:
        assert row["completed"] is False
        assert row["evidence_class"] == "none"
        assert row["release_eligible"] is False
        assert row["blocked_on"], row["phase"]


def test_a_not_delivered_probe_blocks_only_its_own_phase(monkeypatch):
    rows = {row["phase"]: row for row in pg.build_status()["phases"]}
    assert rows[3]["gate_status"] == "BLOCKED"
    assert rows[1]["completed"] is True
    assert rows[2]["completed"] is True
    assert rows[4]["completed"] is True


# --- software readiness is not a physical capability --------------------------


def test_every_phase_is_recorded_as_not_release_eligible_today():
    for row in pg.build_status()["phases"]:
        assert row["release_eligible"] is False, row["phase"]


def test_release_eligibility_follows_the_readiness_class(monkeypatch):
    physical = {
        "available": True,
        "path": "docs/evaluation/readiness-report-v1.json",
        "registered_count": 1,
        "classes": {"software": True, "scripted_fixture": True, "gazebo": True, "physical": True},
        "release_eligible": True,
    }
    monkeypatch.setattr(pg, "_readiness_summary", lambda: physical)
    rows = {row["phase"]: row for row in pg.build_status()["phases"]}
    assert rows[1]["evidence_class"] == "physical"
    assert rows[1]["release_eligible"] is True
    assert rows[3]["release_eligible"] is False
    assert rows[3]["evidence_class"] == "none"


def test_a_scripted_only_readiness_never_makes_a_phase_release_eligible(monkeypatch):
    scripted = {
        "available": True,
        "path": "docs/evaluation/readiness-report-v1.json",
        "registered_count": 1,
        "classes": {"software": True, "scripted_fixture": True, "gazebo": False, "physical": False},
        "release_eligible": False,
    }
    monkeypatch.setattr(pg, "_readiness_summary", lambda: scripted)
    for row in pg.build_status()["phases"]:
        assert row["evidence_class"] in {"scripted_fixture", "none"}
        assert row["release_eligible"] is False


def test_a_missing_readiness_report_is_reported_not_claimed(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "DEFAULT_READINESS_PATH", tmp_path / "absent.json")
    summary = pg._readiness_summary()
    assert summary["available"] is False
    assert summary["release_eligible"] is False


# --- a sign-off is never implied by a passing gate ----------------------------


def test_each_phase_lists_the_sign_off_it_still_needs():
    committed = _committed()
    assert committed["summary"]["sign_off_outstanding_count"] == sum(
        len(row["sign_off_outstanding"]) for row in committed["phases"]
    )
    for row in committed["phases"]:
        expected = [entry["role"] for entry in row["sign_off"] if entry["status"] != "approved"]
        assert row["sign_off_outstanding"] == expected


# --- generated artifacts agree with the artifact ------------------------------


def test_the_page_notes_and_readmes_are_what_this_status_generates():
    committed = _committed()
    assert PAGE.read_text(encoding="utf-8") == pg.markdown_page(committed)
    notes = NOTES.read_text(encoding="utf-8")
    assert _block(notes, pg.NOTES_BEGIN, pg.NOTES_END) == pg.release_notes_block(committed).strip("\n")
    readme = README.read_text(encoding="utf-8")
    assert _block(readme, pg.README_BEGIN, pg.README_END) == pg.readme_block(committed).strip("\n")
    readme_zh = README_ZH.read_text(encoding="utf-8")
    assert _block(readme_zh, pg.README_BEGIN, pg.README_END) == pg.readme_block(committed, language="zh").strip("\n")


def test_applying_a_block_twice_is_idempotent():
    block = pg.readme_block(_committed())
    text = "prefix\n"
    once = pg.apply_block(text, block)
    twice = pg.apply_block(once, block)
    assert once == twice


# --- the gate itself -----------------------------------------------------------


def test_the_gate_passes_on_the_committed_artifact():
    result = _run([str(GATE)])
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout


def test_the_gate_refuses_a_hand_edited_artifact(tmp_path):
    tampered = json.loads(COMMITTED.read_text(encoding="utf-8"))
    tampered["phases"][0]["gate_status"] = "COMPLETE"
    tampered["phases"][0]["completed"] = True
    tampered["phases"][0]["evidence_class"] = "physical"
    tampered["phases"][0]["release_eligible"] = True
    path = tmp_path / "phase-status-v1.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    result = _run([str(GATE), "--status", str(path)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "PHASE_STATUS_STALE" in result.stderr


def test_the_gate_reports_incomplete_when_the_artifact_is_missing(tmp_path):
    result = _run([str(GATE), "--status", str(tmp_path / "absent.json")])
    assert result.returncode == 2, result.stdout + result.stderr
    assert "INCOMPLETE" in result.stderr


def test_the_gate_refuses_an_artifact_that_overclaims_a_blocked_phase(tmp_path):
    tampered = json.loads(COMMITTED.read_text(encoding="utf-8"))
    for row in tampered["phases"]:
        row["completed"] = True
        row["gate_status"] = "COMPLETE"
        row["evidence_class"] = "scripted_fixture"
    path = tmp_path / "phase-status-v1.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    result = _run([str(GATE), "--status", str(path)])
    assert result.returncode == 1
    assert "PHASE_STATUS_STALE" in result.stderr


def test_the_generator_reports_a_broken_probe_as_incomplete(monkeypatch):
    def raising() -> tuple[int, str]:
        raise RuntimeError("the input is unreadable under test")

    monkeypatch.setitem(pg.PROBES, "scenario_registry", raising)
    code, detail = pg.run_probe("scenario_registry")
    assert code == pg.INCOMPLETE
    assert "could not read its input" in detail


@pytest.mark.parametrize("name", sorted(pg.PROBES))
def test_each_probe_returns_a_declared_verdict(name):
    code, detail = pg.run_probe(name)
    assert code in pg.VERDICTS
    assert isinstance(detail, str) and detail
