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

import itertools
import json
import os
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

import readiness_report as rr_module
from readiness_report import (
    AUTHORITY,
    EVIDENCE_CLASSES,
    GENERATED_BY,
    LEFT_UNCHANGED,
    PUBLISHED,
    README_BEGIN,
    README_END,
    REPORT_VERSION,
    GeneratedArtifactSetError,
    apply_readme_block,
    build_report,
    find_staging_residue,
    markdown_page,
    readme_block,
    render_outputs,
    stabilize_volatile,
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
        locks_dir=tmp_path / "locks",
    )
    assert json.loads(report_path.read_text(encoding="utf-8"))["report_version"] == REPORT_VERSION
    assert page_path.read_text(encoding="utf-8") == markdown_page(report)
    assert README_BEGIN in readme_path.read_text(encoding="utf-8")
    assert README_BEGIN in zh_path.read_text(encoding="utf-8")


# --- the four artifacts publish as one set (Issue #465) ----------------------


def _outputs(tmp_path: Path) -> dict:
    """A complete pre-run artifact set to fail against."""

    readme_path = tmp_path / "README.md"
    readme_path.write_text("# Title\n\nintroduction\n", encoding="utf-8")
    zh_path = tmp_path / "README.zh-CN.md"
    zh_path.write_text("# 标题\n", encoding="utf-8")
    return {
        "report_path": tmp_path / "docs" / "report.json",
        "page_path": tmp_path / "docs" / "page.md",
        "readme_path": readme_path,
        "readme_zh_path": zh_path,
        "locks_dir": tmp_path / "locks",
    }


def _snapshot(paths: list[Path]) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in paths}


def test_a_successful_run_publishes_every_target_as_one_set(tmp_path: Path):
    outputs = _outputs(tmp_path)
    report = build_report()
    outcomes = write_outputs(report, **outputs)

    assert {outcome.action for outcome in outcomes} == {PUBLISHED}
    assert {outcome.target for outcome in outcomes} == {
        outputs["report_path"],
        outputs["page_path"],
        outputs["readme_path"],
        outputs["readme_zh_path"],
    }
    assert (
        json.loads(outputs["report_path"].read_text(encoding="utf-8"))["configuration_hash"]
        == report["configuration_hash"]
    )
    assert outputs["page_path"].read_text(encoding="utf-8") == markdown_page(report)
    expected_readme = apply_readme_block("# Title\n\nintroduction\n", readme_block(report))
    assert outputs["readme_path"].read_text(encoding="utf-8") == expected_readme
    assert find_staging_residue("readiness", [tmp_path, tmp_path / "docs"]) == []


def test_a_mid_write_failure_leaves_every_target_byte_identical(tmp_path: Path):
    """The Issue #465 regression: die after the first write, keep the whole set."""

    outputs = _outputs(tmp_path)
    write_outputs(build_report(), **outputs)
    targets = [outputs["report_path"], outputs["page_path"], outputs["readme_path"], outputs["readme_zh_path"]]
    before = _snapshot(targets)

    changed = build_report()
    changed["limitations"] = ["this generation must never reach the disk"]

    def die_after_first_write(written: int) -> None:
        assert written == 1
        raise OSError("injected failure after the first staged write")

    with pytest.raises(GeneratedArtifactSetError) as failure:
        write_outputs(changed, on_staged=die_after_first_write, **outputs)

    assert failure.value.artifact_set == "readiness"
    assert "injected failure after the first staged write" in failure.value.reason
    assert _snapshot(targets) == before
    assert find_staging_residue("readiness", [tmp_path, tmp_path / "docs"]) == []


def test_a_failure_before_anything_is_written_leaves_no_target(tmp_path: Path):
    outputs = _outputs(tmp_path)
    before = _snapshot([outputs["readme_path"], outputs["readme_zh_path"]])

    def die_immediately(written: int) -> None:
        raise OSError(f"injected failure after {written} staged write(s)")

    with pytest.raises(GeneratedArtifactSetError):
        write_outputs(build_report(), on_staged=die_immediately, **outputs)

    assert not outputs["report_path"].exists()
    assert not outputs["page_path"].exists()
    assert _snapshot([outputs["readme_path"], outputs["readme_zh_path"]]) == before


def test_the_cli_publishes_the_set_in_one_step(tmp_path: Path):
    outputs = _outputs(tmp_path)
    outputs["report_path"].parent.mkdir(parents=True)
    result = _run(
        [
            str(GENERATOR),
            "--output",
            str(outputs["report_path"]),
            "--page",
            str(outputs["page_path"]),
            "--readme",
            str(outputs["readme_path"]),
            "--readme-zh",
            str(outputs["readme_zh_path"]),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "4 of 4 artifact(s) published as one set" in result.stdout


def test_a_failure_during_the_renames_restores_the_targets_already_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Renaming is the last step; a failure there still leaves the set intact."""

    outputs = _outputs(tmp_path)
    write_outputs(build_report(), **outputs)
    targets = [outputs["report_path"], outputs["page_path"], outputs["readme_path"], outputs["readme_zh_path"]]
    before = _snapshot(targets)

    changed = build_report()
    changed["limitations"] = ["this generation must not survive a failed rename"]
    real_replace = os.replace
    calls = {"count": 0}

    def fail_second_rename(source, destination):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected failure during publication")
        return real_replace(source, destination)

    monkeypatch.setattr(rr_module.os, "replace", fail_second_rename)
    with pytest.raises(GeneratedArtifactSetError) as failure:
        write_outputs(changed, **outputs)

    assert "injected failure during publication" in failure.value.reason
    assert _snapshot(targets) == before
    assert find_staging_residue("readiness", [tmp_path, tmp_path / "docs"]) == []


def test_a_staging_failure_publishes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The disable path: refuse generation, never fall back to sequential writes."""

    outputs = _outputs(tmp_path)
    before = _snapshot([outputs["readme_path"], outputs["readme_zh_path"]])

    def refuse(*_args, **_kwargs):
        raise OSError("the staging area is not writable")

    monkeypatch.setattr(rr_module.tempfile, "mkdtemp", refuse)
    with pytest.raises(GeneratedArtifactSetError) as failure:
        write_outputs(build_report(), **outputs)

    assert "cannot create a staging directory" in failure.value.reason
    assert not outputs["report_path"].exists()
    assert not outputs["page_path"].exists()
    assert _snapshot([outputs["readme_path"], outputs["readme_zh_path"]]) == before


def test_the_gate_reports_incomplete_rather_than_a_torn_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A named failure keeps every target byte-identical, so the gate still passes."""

    outputs = _outputs(tmp_path)
    write_outputs(build_report(), **outputs)
    targets = [outputs["report_path"], outputs["page_path"], outputs["readme_path"], outputs["readme_zh_path"]]
    before = _snapshot(targets)

    def die(written: int) -> None:
        raise OSError(f"injected failure after {written} staged write(s)")

    with pytest.raises(GeneratedArtifactSetError):
        write_outputs(build_report(), on_staged=die, **outputs)

    assert _snapshot(targets) == before


def test_a_successful_run_produces_the_bytes_the_sequential_writer_produced(tmp_path: Path):
    """Issue #465: only publication is atomic; the content must not change.

    The pre-change writer spliced the same blocks into the same files with
    sequential writes. Replaying that splice here proves the atomic path changed
    *when* the bytes land, not *which* bytes they are.
    """

    outputs = _outputs(tmp_path)
    report = build_report()
    before = outputs["readme_path"].read_text(encoding="utf-8")
    before_zh = outputs["readme_zh_path"].read_text(encoding="utf-8")

    write_outputs(report, **outputs)

    assert outputs["report_path"].read_text(encoding="utf-8") == json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    assert outputs["page_path"].read_text(encoding="utf-8") == markdown_page(report)
    assert outputs["readme_path"].read_text(encoding="utf-8") == apply_readme_block(before, readme_block(report))
    assert outputs["readme_zh_path"].read_text(encoding="utf-8") == apply_readme_block(
        before_zh, readme_block(report, language="zh")
    )


def test_a_generation_with_no_change_does_not_rewrite_tracked_artifacts(tmp_path: Path):
    """Regenerating an up-to-date checkout must not dirty the evidence files."""

    outputs = _outputs(tmp_path)
    first = write_outputs(build_report(), **outputs)
    targets = [outcome.target for outcome in first]
    before = _snapshot(targets)

    second = write_outputs(build_report(), **outputs)

    assert {outcome.action for outcome in second} == {LEFT_UNCHANGED}
    assert _snapshot(targets) == before


def test_a_changed_field_republishes_the_set_with_fresh_stamps(tmp_path: Path):
    outputs = _outputs(tmp_path)
    write_outputs(build_report(), **outputs)
    changed = build_report(generated_at="2026-01-01T00:00:00+00:00", source_commit="0" * 40)
    changed["limitations"] = ["a changed input"]

    outcomes = write_outputs(changed, **outputs)

    published = json.loads(outputs["report_path"].read_text(encoding="utf-8"))
    assert published["limitations"] == ["a changed input"]
    assert published["generated_at"] == "2026-01-01T00:00:00+00:00"
    # The page carries the revision, so it moves with the report; the README
    # blocks state no stamps, so their bytes do not change.
    by_target = {outcome.target: outcome.action for outcome in outcomes}
    assert by_target[outputs["report_path"]] == PUBLISHED
    assert by_target[outputs["page_path"]] == PUBLISHED
    assert by_target[outputs["readme_path"]] == LEFT_UNCHANGED


def test_stabilize_volatile_keeps_the_stamps_only_when_nothing_else_changed():
    committed = {"generated_at": "old", "source_commit": "old", "summary": {"a": 1}}
    same = {"generated_at": "new", "source_commit": "new", "summary": {"a": 1}}
    assert stabilize_volatile(same, committed) == committed
    changed = {"generated_at": "new", "source_commit": "new", "summary": {"a": 2}}
    assert stabilize_volatile(changed, committed) == changed
    assert stabilize_volatile(changed, None) == changed


def test_render_outputs_reads_the_readme_and_never_writes_it(tmp_path: Path):
    outputs = _outputs(tmp_path)
    before = _snapshot([outputs["readme_path"], outputs["readme_zh_path"]])
    artifacts = render_outputs(build_report(), **{k: v for k, v in outputs.items() if k != "locks_dir"})
    assert len(artifacts) == 4
    assert _snapshot([outputs["readme_path"], outputs["readme_zh_path"]]) == before
    assert not outputs["report_path"].exists()
    assert not outputs["page_path"].exists()


def test_concurrent_generation_of_the_same_targets_is_serialised(tmp_path: Path):
    """Issue #465: no two generations may publish the same set at once.

    Three processes generate the same four targets. Each records when it entered
    and left the publish step; if the lock works those intervals never overlap,
    so no set can be interleaved with another.
    """

    outputs = _outputs(tmp_path)
    outputs["report_path"].parent.mkdir(parents=True)
    log = tmp_path / "publish.log"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys, time\n"
        f"sys.path[:0] = [{str(ROOT / 'tools/scripts')!r}, {str(ROOT / 'libs/kernel')!r}, "
        f"{str(ROOT / 'libs/contracts')!r}, {str(ROOT / 'services/world_model')!r}]\n"
        "from pathlib import Path\n"
        "import readiness_report as rr\n"
        "log, output_dir, readme, readme_zh, locks = (Path(value) for value in sys.argv[1:6])\n"
        "original = rr.publish_artifact_set\n"
        "def traced(name, artifacts, *, on_staged=None):\n"
        "    started = time.monotonic_ns()\n"
        "    time.sleep(0.05)\n"
        "    outcomes = original(name, artifacts, on_staged=on_staged)\n"
        "    with log.open('a', encoding='utf-8') as stream:\n"
        "        stream.write(f'{started} {time.monotonic_ns()}\\n')\n"
        "    return outcomes\n"
        "rr.publish_artifact_set = traced\n"
        "rr.write_outputs(rr.build_report(), report_path=output_dir / 'report.json',\n"
        "    page_path=output_dir / 'page.md', readme_path=readme, readme_zh_path=readme_zh,\n"
        "    locks_dir=locks)\n",
        encoding="utf-8",
    )
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(worker),
                str(log),
                str(outputs["report_path"].parent),
                str(outputs["readme_path"]),
                str(outputs["readme_zh_path"]),
                str(outputs["locks_dir"]),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    for process in processes:
        _, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr

    intervals = sorted(tuple(int(value) for value in line.split()) for line in log.read_text().splitlines())
    assert len(intervals) == 3
    for (_, previous_end), (next_start, _) in itertools.pairwise(intervals):
        assert previous_end <= next_start, f"two generations published at once: {intervals}"

    assert (outputs["readme_path"].read_text(encoding="utf-8")).count(README_BEGIN) == 1
    assert json.loads(outputs["report_path"].read_text(encoding="utf-8"))["report_version"] == REPORT_VERSION
    assert find_staging_residue("readiness", [tmp_path, outputs["report_path"].parent]) == []


def test_the_gate_refuses_a_checkout_left_with_staging_residue(tmp_path: Path):
    residue = tmp_path / ".readiness.abc123.staging"
    residue.mkdir()
    (residue / "00-report.json").write_text("{}", encoding="utf-8")
    result = _run([str(GATE), "--report", str(COMMITTED), "--page", str(tmp_path / "page.md")])
    assert result.returncode == 1
    assert "staging residue" in result.stderr


def test_generator_writes_nothing_outside_its_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A dry run must not touch the tree, so the gate can regenerate freely."""

    before = {path: path.stat().st_mtime_ns for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.parts}
    build_report()
    after = {path: path.stat().st_mtime_ns for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.parts}
    assert before == after
