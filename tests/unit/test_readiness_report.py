"""Issue #307: one truthful multi-scenario software readiness report.

The report is only worth publishing if a reviewer can see what it refuses, so
every test here drives a refusal as well as a success:

* the report is derived from the live registry, so a row that is missing, extra
  or hand-edited is a failure rather than a stale table;
* a row may never claim a stronger evidence class than its manifest supports, so
  a scripted fixture cannot be rendered as Gazebo or physical success;
* ``NOT_EXECUTED`` and ``BLOCKED`` are reported as such and never as a pass;
* two generations in one checkout agree except for the two fields that describe
  when and where the report was built.

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
]

from readiness_report import (
    AUTHORITY,
    EVIDENCE_CLASSES,
    GENERATED_BY,
    README_BEGIN,
    README_END,
    REPORT_VERSION,
    apply_readme_block,
    build_report,
    markdown_page,
    readme_block,
    write_outputs,
)

GATE = ROOT / "tools/scripts/check_readiness_report.py"
GENERATOR = ROOT / "tools/scripts/readiness_report.py"
COMMITTED = ROOT / "docs/evaluation/readiness-report-v1.json"
PAGE = ROOT / "docs/evaluation/multi-scenario-readiness.md"


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


# --- the committed artifact is the one this checkout generates ----------------


def test_committed_report_matches_the_generator():
    committed = _committed()
    regenerated = build_report()
    assert committed["report_version"] == REPORT_VERSION
    for key in ("configuration_hash", "summary", "scenarios", "declared_not_registered"):
        assert committed[key] == regenerated[key], key
    assert committed["generated_by"] == GENERATED_BY


def test_two_generations_agree_except_for_where_and_when():
    first = build_report()
    second = build_report()
    volatile = {"generated_at", "source_commit"}
    assert {k: v for k, v in first.items() if k not in volatile} == {
        k: v for k, v in second.items() if k not in volatile
    }


def test_report_states_its_authority_and_its_edit_policy():
    committed = _committed()
    assert committed["authority"] == AUTHORITY
    assert "release approval" in committed["authority"]["does_not_grant"]
    assert "generated" in committed["edit_policy"]
    assert "hand-edit" in committed["edit_policy"]
    assert set(committed["generated_fields"]) >= {"generated_at", "configuration_hash", "scenarios[].release_eligible"}


# --- every registered identity has exactly one row ----------------------------


def test_every_registered_identity_has_a_row():
    from workbench.kernel.scenario_registry import load_registry

    registry = load_registry(ROOT / "sim/registry", repo_root=ROOT)
    committed = _committed()
    identities = {row["identity"] for row in committed["scenarios"]}
    assert identities == {entry.identity for entry in registry.entries}


def test_a_row_records_the_fields_a_reviewer_needs():
    required = {
        "identity",
        "scenario_id",
        "scenario_version",
        "execution_status",
        "evidence_classes",
        "release_eligible",
        "software_readiness",
        "proved_dimensions",
        "missing_dimensions",
        "failure_coverage",
        "replay_hash",
        "verifier",
        "semantic_actions",
        "required_adapters",
        "recovery_policy",
        "owners",
        "known_limitations",
        "rendered_as_success",
    }
    for row in _committed()["scenarios"]:
        assert required <= set(row), row["identity"]
        assert set(row["evidence_classes"]) == set(EVIDENCE_CLASSES)
        assert row["failure_coverage"], row["identity"]


def test_a_row_links_the_test_that_proves_each_dimension():
    from workbench.kernel.scenario_conformance import REQUIRED_DIMENSIONS

    for row in _committed()["scenarios"]:
        covered = {case["dimension"] for case in row["failure_coverage"]}
        assert covered == set(REQUIRED_DIMENSIONS), row["identity"]
        for case in row["failure_coverage"]:
            path, _, function = case["test"].partition("::")
            assert (ROOT / path).is_file(), case["test"]
            assert f"def {function}(" in (ROOT / path).read_text(encoding="utf-8"), case["test"]


def test_a_scenario_that_proves_a_dimension_is_not_reported_ready():
    for row in _committed()["scenarios"]:
        ready = row["software_readiness"] == "ready"
        assert ready is (not row["missing_dimensions"]), row["identity"]


# --- the four evidence classes are independent axes ---------------------------


@pytest.mark.parametrize(
    ("execution_status", "expected"),
    [
        ("SCRIPTED_FIXTURE", {"software", "scripted_fixture"}),
        ("GAZEBO", {"software", "scripted_fixture", "gazebo"}),
        ("PHYSICAL", {"software", "scripted_fixture", "gazebo", "physical"}),
        ("NOT_EXECUTED", {"software"}),
        ("BLOCKED", {"software"}),
    ],
)
def test_evidence_classes_follow_the_manifest_status(execution_status: str, expected: set[str]):
    from readiness_report import _evidence_classes

    classes = _evidence_classes(execution_status)
    assert {name for name, value in classes.items() if value} == expected


def test_a_scripted_fixture_is_never_release_eligible_or_rendered_as_success():
    for row in _committed()["scenarios"]:
        if row["execution_status"] in {"SCRIPTED_FIXTURE", "NOT_EXECUTED", "BLOCKED"}:
            assert row["release_eligible"] is False, row["identity"]
            assert row["rendered_as_success"] is False, row["identity"]


def test_the_committed_checkout_claims_no_gazebo_or_physical_row():
    committed = _committed()
    assert committed["summary"]["gazebo_count"] == 0
    assert committed["summary"]["physical_count"] == 0
    assert committed["summary"]["release_eligible_count"] == 0
    assert any("scripted fixture" in limitation for limitation in committed["limitations"])


def test_a_blocked_family_is_published_rather_than_hidden():
    families = {row["task_family"] for row in _committed()["declared_not_registered"]}
    assert {"assembly", "cleaning"} <= families
    for row in _committed()["declared_not_registered"]:
        assert row["status"] in {"NOT_REGISTERED", "PLANNED", "BLOCKED"}


# --- the gate refuses a stale, edited or strengthened report ------------------


def _gate(report: Path, *, page: Path | None = None) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            str(GATE),
            "--report",
            str(report),
            "--page",
            str(page or PAGE),
        ]
    )


def test_gate_passes_on_the_committed_report():
    result = _run([str(GATE)])
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout


def test_gate_reports_incomplete_for_an_unreadable_report(tmp_path: Path):
    result = _gate(tmp_path / "absent.json")
    assert result.returncode == 2
    assert "INCOMPLETE" in result.stderr


def test_gate_fails_a_hand_edited_outcome(tmp_path: Path):
    edited = json.loads(COMMITTED.read_text(encoding="utf-8"))
    edited["scenarios"][0]["replay_hash"] = "0" * 64
    target = tmp_path / "edited.json"
    target.write_text(json.dumps(edited), encoding="utf-8")
    result = _gate(target)
    assert result.returncode == 1
    assert "differs from the report this checkout generates" in result.stderr


def test_gate_fails_a_missing_registered_row(tmp_path: Path):
    edited = json.loads(COMMITTED.read_text(encoding="utf-8"))
    dropped = edited["scenarios"][0]["identity"]
    edited["scenarios"] = [row for row in edited["scenarios"] if row["identity"] != dropped]
    target = tmp_path / "dropped.json"
    target.write_text(json.dumps(edited), encoding="utf-8")
    result = _gate(target)
    assert result.returncode == 1
    assert f"registered scenario {dropped} has no row" in result.stderr


def test_gate_fails_a_row_that_strengthens_its_evidence(tmp_path: Path):
    edited = json.loads(COMMITTED.read_text(encoding="utf-8"))
    row = edited["scenarios"][0]
    row["evidence_classes"]["physical"] = True
    row["release_eligible"] = True
    row["rendered_as_success"] = True
    target = tmp_path / "physical.json"
    target.write_text(json.dumps(edited), encoding="utf-8")
    result = _gate(target)
    assert result.returncode == 1
    assert "may not strengthen its evidence" in result.stderr


def test_gate_fails_a_not_executed_row_rendered_as_success(tmp_path: Path):
    edited = json.loads(COMMITTED.read_text(encoding="utf-8"))
    row = edited["scenarios"][0]
    row["execution_status"] = "NOT_EXECUTED"
    row["evidence_classes"] = {"software": True, "scripted_fixture": False, "gazebo": False, "physical": False}
    row["release_eligible"] = False
    row["rendered_as_success"] = True
    target = tmp_path / "notexecuted.json"
    target.write_text(json.dumps(edited), encoding="utf-8")
    result = _gate(target)
    assert result.returncode == 1
    assert "never a pass" in result.stderr or "must not be rendered as a pass" in result.stderr


def test_gate_fails_a_stale_markdown_page(tmp_path: Path):
    stale = tmp_path / "page.md"
    stale.write_text("# a page a human typed\n", encoding="utf-8")
    result = _gate(COMMITTED, page=stale)
    assert result.returncode == 1
    assert "is not the page this report generates" in result.stderr


def test_gate_json_summary_is_machine_readable():
    result = _run([str(GATE), "--json"])
    payload = json.loads(result.stdout)
    assert payload["exit_code"] == 0
    assert payload["findings"] == []
    assert payload["registered_identities"] == payload["rows"]


# --- the generated page and README block are generated, not typed --------------


def test_committed_page_is_the_generated_page():
    assert PAGE.read_text(encoding="utf-8") == markdown_page(_committed())


def test_generated_page_names_its_source_of_truth():
    page = PAGE.read_text(encoding="utf-8")
    assert "Generated from docs/evaluation/readiness-report-v1.json" in page
    assert "Do not edit by hand" in page


def test_readme_block_is_present_and_regenerated(tmp_path: Path):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert README_BEGIN in readme and README_END in readme
    assert readme_block(_committed()).strip("\n") in readme


def test_readme_block_is_replaced_rather_than_appended_twice(tmp_path: Path):
    once = apply_readme_block("intro\n", readme_block(_committed()))
    twice = apply_readme_block(once, readme_block(_committed()))
    assert once == twice
    assert twice.count(README_BEGIN) == 1


def test_a_readme_without_the_marker_gains_the_block():
    block = readme_block(_committed())
    updated = apply_readme_block("# Title\n", block)
    assert README_BEGIN in updated and README_END in updated


def test_chinese_readme_block_is_generated_from_the_same_rows():
    zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    block = readme_block(_committed(), language="zh")
    assert README_BEGIN in zh and README_END in zh
    assert block.strip("\n") in zh
    for row in _committed()["scenarios"]:
        if not row["release_eligible"]:
            assert f"`{row['identity']}`" in zh


def test_both_readmes_agree_on_every_release_verdict():
    committed = _committed()
    english = readme_block(committed)
    chinese = readme_block(committed, language="zh")
    for row in committed["scenarios"]:
        assert f"`{row['identity']}`" in english
        assert f"`{row['identity']}`" in chinese


def test_gate_fails_a_stale_chinese_readme(tmp_path: Path):
    zh = tmp_path / "README.zh-CN.md"
    zh.write_text("# 标题\n", encoding="utf-8")
    result = _run([str(GATE), "--report", str(COMMITTED), "--page", str(PAGE), "--readme-zh", str(zh)])
    assert result.returncode == 1
    assert "has no scenario-readiness generated block" in result.stderr


def test_write_outputs_is_the_only_writer(tmp_path: Path):
    report = build_report()
    report_path = tmp_path / "report.json"
    page_path = tmp_path / "page.md"
    readme_path = tmp_path / "README.md"
    readme_path.write_text("# Title\n", encoding="utf-8")
    zh_path = tmp_path / "README.zh-CN.md"
    zh_path.write_text("# 标题\n", encoding="utf-8")
    write_outputs(
        report,
        report_path=report_path,
        page_path=page_path,
        readme_path=readme_path,
        readme_zh_path=zh_path,
    )
    assert json.loads(report_path.read_text(encoding="utf-8"))["report_version"] == REPORT_VERSION
    assert page_path.read_text(encoding="utf-8") == markdown_page(report)
    assert README_BEGIN in readme_path.read_text(encoding="utf-8")
    assert README_BEGIN in zh_path.read_text(encoding="utf-8")


def test_generator_writes_nothing_outside_its_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A dry run must not touch the tree, so the gate can regenerate freely."""

    before = {path: path.stat().st_mtime_ns for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.parts}
    build_report()
    after = {path: path.stat().st_mtime_ns for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.parts}
    assert before == after
