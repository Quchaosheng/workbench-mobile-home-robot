"""Contract tests for the Issue #310 Definition-of-Ready and Exit Gate.

The gate is a promise about evidence, so its own promises are tested. Every rule
in ``tools/qa/governance/ready-gate-v1.json`` has an accepted case and a rejected
case here; the rejected case for a forbidden claim is read from the rule's own
``sample_violation`` so a rule cannot be added without one. The tests also pin the
committed templates, the gate definition and the detector together, and prove the
detector cannot run the commands it reads.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts")]

import check_ready_gate as gate

FIXTURES = ROOT / "tests/fixtures/ready-gate"
DEFINITION = json.loads((ROOT / "tools/qa/governance/ready-gate-v1.json").read_text(encoding="utf-8"))


def _definition() -> dict[str, Any]:
    """Return the committed gate definition as the detector loads it."""

    return json.loads((ROOT / "tools/qa/governance/ready-gate-v1.json").read_text(encoding="utf-8"))


def _complete_body() -> str:
    return (FIXTURES / "complete.md").read_text(encoding="utf-8")


def _pull_request_verdict(body: str) -> gate.Verdict:
    return gate.check_body(body, _definition(), source="<test>")


def _issue_verdict(body: str) -> gate.Verdict:
    return gate.check_body(body, _definition(), source="<test>", kind=gate.ISSUE)


# --------------------------------------------------------------------------- #
# The gate definition itself
# --------------------------------------------------------------------------- #


def test_the_committed_definition_validates_against_its_schema() -> None:
    """The detector refuses an invalid definition, so the committed one must pass."""

    definition = gate._load_gate()

    assert definition["gate_version"] == "workbench-ready-gate-v1"


def test_every_requirement_kind_is_one_the_detector_can_evaluate() -> None:
    definition = _definition()

    for key in ("required_sections", "required_checklist", "required_issue_sections"):
        for entry in definition[key]:
            for requirement in entry.get("requires", []):
                assert requirement in gate.EVIDENCE_MARKERS, f"{key}: {requirement}"
                assert callable(gate.EVIDENCE_MARKERS[requirement].search)


def test_a_rule_whose_sample_does_not_match_is_incomplete_not_a_pass() -> None:
    """A dead rule must break the gate, not silently stop protecting anything."""

    definition = _definition()
    definition["forbidden_claims"][0]["sample_violation"] = "nothing forbidden here"

    with pytest.raises(gate.GateError, match="does not match its own sample"):
        gate._validate_definition(definition)


def test_a_rule_naming_an_unknown_requirement_is_incomplete() -> None:
    definition = _definition()
    definition["required_checklist"][0]["requires"] = ["vibes"]

    with pytest.raises(gate.GateError, match="unknown requirement kind"):
        gate._validate_definition(definition)


# --------------------------------------------------------------------------- #
# Accepted case
# --------------------------------------------------------------------------- #


def test_the_complete_fixture_is_ready_with_exit_code_zero() -> None:
    verdict = _pull_request_verdict(_complete_body())

    assert verdict.status == "READY"
    assert verdict.exit_code == gate.READY


def test_a_complete_issue_body_is_ready() -> None:
    verdict = _issue_verdict((FIXTURES / "complete-issue.md").read_text(encoding="utf-8"))

    assert verdict.status == "READY"
    assert verdict.exit_code == gate.READY


# --------------------------------------------------------------------------- #
# Required sections: one rejected case per rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entry", DEFINITION["required_sections"], ids=lambda entry: entry["heading"])
def test_a_missing_section_blocks_and_names_itself(entry: dict[str, Any]) -> None:
    body = _complete_body().replace(f"## {entry['heading']}", "## Renamed section", 1)

    verdict = _pull_request_verdict(body)

    assert verdict.status == "BLOCKED"
    assert verdict.exit_code == gate.BLOCKED
    assert any(f"missing section: '{entry['heading']}'" in detail for detail in verdict.details)


def _blank_section(body: str, heading: str) -> str:
    """Remove the text under one heading, leaving the heading in place."""

    sections = gate.parse_sections(body)
    content = sections[heading.casefold()].body
    assert content, f"the fixture's '{heading}' section is already empty"
    return body.replace(content, "", 1)


@pytest.mark.parametrize("entry", DEFINITION["required_sections"], ids=lambda entry: entry["heading"])
def test_an_empty_section_blocks_and_names_itself(entry: dict[str, Any]) -> None:
    body = _blank_section(_complete_body(), entry["heading"])

    verdict = _pull_request_verdict(body)

    assert verdict.status == "BLOCKED"
    assert any(f"section '{entry['heading']}' is empty" in detail for detail in verdict.details)


def test_a_section_present_only_as_template_guidance_is_treated_as_empty() -> None:
    """A comment and a quoted example must not satisfy a section."""

    body = _complete_body().replace(
        "## Risks and rollback\n\nRisk: a template edit drifts from the gate definition. "
        "Rollback: revert this commit. The detector is additive and no runtime path depends on it.",
        "## Risks and rollback\n\n<!-- explain the risk here -->\n\n> Risk: <what could break>",
    )

    verdict = _pull_request_verdict(body)

    assert verdict.status == "BLOCKED"
    assert any("section 'Risks and rollback' is empty" in detail for detail in verdict.details)


def test_a_section_without_its_required_evidence_blocks() -> None:
    """The 'Tests and commands' section must carry a command, not a claim."""

    body = _complete_body().replace(
        "- `python3 -m pytest tests/unit/test_ready_gate.py -v` -- PASS, exit code 0.\n"
        "- `python3 tools/scripts/check_ready_gate.py --body-file tests/fixtures/ready-gate/complete.md` "
        "-- PASS, exit code 0.",
        "I ran the tests and they were fine.",
    )

    verdict = _pull_request_verdict(body)

    assert verdict.status == "BLOCKED"
    assert any("has no command" in detail for detail in verdict.details)


def test_the_interfaces_section_must_name_a_path() -> None:
    body = _complete_body().replace(
        "None. No public interface, contract model, firmware boundary or workflow file changed. "
        "The new files are `tools/scripts/check_ready_gate.py` and `tools/qa/governance/ready-gate-v1.json`.",
        "None. Nothing public changed.",
    )

    verdict = _pull_request_verdict(body)

    assert verdict.status == "BLOCKED"
    assert any("has no path_reference" in detail for detail in verdict.details)


def test_the_related_issue_section_must_reference_an_issue_or_a_commit() -> None:
    body = _complete_body().replace(
        "Closes #310. The Task Packet is `docs/task_packets/issue-310-definition-of-ready.json` "
        "and it describes commit `a1b2c3d`.",
        "It closes the relevant issue.",
    )

    verdict = _pull_request_verdict(body)

    assert verdict.status == "BLOCKED"
    assert any("has no commit_reference" in detail for detail in verdict.details)


# --------------------------------------------------------------------------- #
# Required checklist: one rejected case per rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entry", DEFINITION["required_checklist"], ids=lambda entry: entry["item"])
def test_a_missing_checklist_item_blocks_and_names_itself(entry: dict[str, Any]) -> None:
    body = _complete_body().replace(f"- [x] {entry['item']}\n", "", 1)

    verdict = _pull_request_verdict(body)

    assert verdict.status == "BLOCKED"
    assert any(f"missing checklist item: '{entry['item']}'" in detail for detail in verdict.details)


def test_an_unticked_checkbox_blocks_and_names_itself() -> None:
    """This is the case the mutation probe neuters and requires to be caught."""

    body = (FIXTURES / "unticked-checkbox.md").read_text(encoding="utf-8")

    verdict = _pull_request_verdict(body)

    assert verdict.status == "BLOCKED"
    assert verdict.exit_code == gate.BLOCKED
    assert any(
        "unticked checklist item: 'I covered normal and failure behaviour.'" in detail for detail in verdict.details
    )


def test_an_uppercase_tick_is_accepted() -> None:
    body = _complete_body().replace(
        "- [x] I covered normal and failure behaviour.",
        "- [X] I covered normal and failure behaviour.",
    )

    assert _pull_request_verdict(body).status == "READY"


def test_an_item_ticked_without_its_required_command_blocks() -> None:
    """A ticked box is not evidence; the checklist entry decides what it needs."""

    body = (FIXTURES / "missing-evidence.md").read_text(encoding="utf-8")

    verdict = _pull_request_verdict(body)

    assert verdict.status == "BLOCKED"
    assert any("claims completion without a command" in detail for detail in verdict.details)


# --------------------------------------------------------------------------- #
# Authority the checklist withholds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("claim", DEFINITION["forbidden_claims"], ids=lambda claim: claim["pattern"][:28])
def test_every_forbidden_claim_rule_rejects_its_own_sample(claim: dict[str, Any]) -> None:
    """Each rule is exercised by the literal the rule itself declares."""

    body = _complete_body() + f"\n\n{claim['sample_violation']}\n"

    verdict = _pull_request_verdict(body)

    assert verdict.status == "BLOCKED"
    assert verdict.exit_code == gate.BLOCKED
    assert any(claim["reason"] in detail for detail in verdict.details)


def test_the_overclaimed_fixture_is_blocked() -> None:
    verdict = _pull_request_verdict((FIXTURES / "overclaimed-authority.md").read_text(encoding="utf-8"))

    assert verdict.status == "BLOCKED"
    assert any("never authorises an automatic merge" in detail for detail in verdict.details)


def test_a_forbidden_claim_in_an_issue_body_is_blocked_too() -> None:
    body = (FIXTURES / "complete-issue.md").read_text(encoding="utf-8") + "\n\nThe change is approved for release.\n"

    verdict = _issue_verdict(body)

    assert verdict.status == "BLOCKED"
    assert any("release authority" in detail for detail in verdict.details)


def test_a_finding_does_not_echo_the_offending_sentence() -> None:
    """The gate reads untrusted text and must not republish a claim as its own."""

    verdict = _pull_request_verdict((FIXTURES / "overclaimed-authority.md").read_text(encoding="utf-8"))

    assert not any("merges automatically" in detail for detail in verdict.details)
    assert any("line " in detail for detail in verdict.details)


# --------------------------------------------------------------------------- #
# Definition of Ready (issue bodies)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entry", DEFINITION["required_issue_sections"], ids=lambda entry: entry["heading"])
def test_an_issue_missing_a_readiness_field_is_blocked(entry: dict[str, Any]) -> None:
    body = (FIXTURES / "complete-issue.md").read_text(encoding="utf-8").replace(f"## {entry['heading']}\n", "", 1)

    verdict = _issue_verdict(body)

    assert verdict.status == "BLOCKED"
    assert any(f"missing section: '{entry['heading']}'" in detail for detail in verdict.details)


def test_the_issue_mode_does_not_require_the_pull_request_checklist() -> None:
    """The two halves are separate: an issue body is not judged on Exit Gate boxes."""

    body = (FIXTURES / "complete-issue.md").read_text(encoding="utf-8")

    assert _issue_verdict(body).status == "READY"
    assert _pull_request_verdict(body).status == "BLOCKED"


def test_the_issue_mode_still_requires_a_rollback_or_failure_path() -> None:
    body = (
        (FIXTURES / "complete-issue.md")
        .read_text(encoding="utf-8")
        .replace(
            "- Rollback or disable path is documented.",
            "- It should be fine.",
        )
    )

    verdict = _issue_verdict(body)

    assert verdict.status == "BLOCKED"
    assert any("Definition of Ready" in detail for detail in verdict.details)


def test_an_unknown_body_kind_is_an_error_rather_than_a_pass() -> None:
    with pytest.raises(gate.GateError, match="unknown body kind"):
        gate.check_body(_complete_body(), _definition(), kind="release-note")


# --------------------------------------------------------------------------- #
# The third outcome
# --------------------------------------------------------------------------- #


def test_an_empty_stdin_body_is_incomplete_rather_than_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """INCOMPLETE must stay distinct from BLOCKED: the gate must not have guessed."""

    monkeypatch.setattr(sys, "stdin", io.StringIO("   \n\n"))

    assert gate.main(["--stdin"]) == gate.INCOMPLETE


def test_a_missing_body_file_is_incomplete(tmp_path: Path) -> None:
    assert gate.main(["--body-file", str(tmp_path / "absent.md")]) == gate.INCOMPLETE


def test_an_empty_body_file_is_incomplete(tmp_path: Path) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("\n\n", encoding="utf-8")

    assert gate.main(["--body-file", str(empty)]) == gate.INCOMPLETE


def test_an_unreadable_gate_definition_is_incomplete(tmp_path: Path) -> None:
    broken = tmp_path / "gate.json"
    broken.write_text("{ not json", encoding="utf-8")

    assert gate.main(["--body-file", str(FIXTURES / "complete.md"), "--gate", str(broken)]) == gate.INCOMPLETE


def test_every_incomplete_outcome_is_distinct_from_ready(tmp_path: Path) -> None:
    """The three exit codes are the contract; nothing may collapse into 0."""

    assert {gate.READY, gate.BLOCKED, gate.INCOMPLETE} == {0, 1, 2}
    assert gate.BLOCKED != gate.READY
    assert gate.INCOMPLETE != gate.READY

    for argv in (
        ["--body-file", str(tmp_path / "absent.md")],
        ["--body-file", str(tmp_path / "absent.json"), "--gate", str(tmp_path / "absent.json")],
    ):
        assert gate.main(argv) == gate.INCOMPLETE


# --------------------------------------------------------------------------- #
# The detector reports, it does not run
# --------------------------------------------------------------------------- #


def test_the_detector_never_executes_the_commands_it_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """This is the promise that makes it safe to run on an untrusted pull request."""

    def _refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the readiness detector must never execute anything")

    monkeypatch.setattr(subprocess, "run", _refuse)
    monkeypatch.setattr(subprocess, "Popen", _refuse)
    monkeypatch.setattr(subprocess, "check_output", _refuse)
    monkeypatch.setattr("os.system", _refuse)

    body = _complete_body() + "\n\n- `rm -rf /` -- PASS\n- `git push origin HEAD:main` -- PASS\n"
    assert _pull_request_verdict(body).status == "READY"


def test_the_detector_does_not_import_subprocess_for_its_own_work() -> None:
    source = (ROOT / "tools/scripts/check_ready_gate.py").read_text(encoding="utf-8")

    assert "subprocess" not in source
    assert "os.system" not in source


def test_the_archive_records_findings_but_not_the_body(tmp_path: Path) -> None:
    archive = tmp_path / "summary.json"
    body = (FIXTURES / "overclaimed-authority.md").read_text(encoding="utf-8")

    verdict = _pull_request_verdict(body)
    gate._write_archive(archive, gate.PULL_REQUEST, verdict)
    payload = json.loads(archive.read_text(encoding="utf-8"))

    assert payload["status"] == "BLOCKED"
    assert payload["exit_code"] == gate.BLOCKED
    assert payload["kind"] == "pull-request"
    assert payload["findings"] == verdict.details
    assert "merges automatically" not in archive.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The templates and the gate cannot drift apart
# --------------------------------------------------------------------------- #


def test_the_committed_templates_agree_with_the_gate() -> None:
    assert gate.check_templates_agree_with_the_gate(_definition()) == []


def test_a_pull_request_template_missing_a_section_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stripped = tmp_path / "template.md"
    stripped.write_text(
        (ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8").replace("## Evidence", "## Proof", 1),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "TEMPLATE", stripped)

    problems = gate.check_templates_agree_with_the_gate(_definition())

    assert any("missing the 'Evidence' section" in problem for problem in problems)


def test_the_issue_template_is_read_from_the_gate_not_hard_coded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = _definition()
    extra = tmp_path / "missing-owner.md"
    extra.write_text(
        (ROOT / definition["issue_template"]).read_text(encoding="utf-8").replace("## Owner\n", "", 1),
        encoding="utf-8",
    )
    monkeypatch.setitem(definition, "issue_template", str(extra))

    problems = gate.check_templates_agree_with_the_gate(definition)

    assert any("issue template is missing the 'Owner' section" in problem for problem in problems)


def test_a_template_that_does_not_name_the_detector_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    silent = tmp_path / "silent.md"
    silent.write_text(
        (ROOT / ".github/pull_request_template.md")
        .read_text(encoding="utf-8")
        .replace("check_ready_gate.py", "the gate"),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "TEMPLATE", silent)

    problems = gate.check_templates_agree_with_the_gate(_definition())

    assert any("does not name tools/scripts/check_ready_gate.py" in problem for problem in problems)


def test_the_gate_definition_cannot_claim_authority_the_repository_withholds() -> None:
    definition = _definition()

    assert "merge authority" in definition["authority"]["does_not_grant"]
    assert "release authority" in definition["authority"]["does_not_grant"]
    assert "physical validation authority" in definition["authority"]["does_not_grant"]
    assert all("merge" not in grant for grant in definition["authority"]["grants"])


# --------------------------------------------------------------------------- #
# The fixtures themselves
# --------------------------------------------------------------------------- #


def test_every_named_fixture_gets_the_verdict_its_name_promises() -> None:
    expected = {
        "complete.md": ("READY", gate.READY),
        "missing-evidence.md": ("BLOCKED", gate.BLOCKED),
        "unticked-checkbox.md": ("BLOCKED", gate.BLOCKED),
        "overclaimed-authority.md": ("BLOCKED", gate.BLOCKED),
    }

    for name, (status, exit_code) in expected.items():
        verdict = _pull_request_verdict((FIXTURES / name).read_text(encoding="utf-8"))
        assert (verdict.status, verdict.exit_code) == (status, exit_code), name


PULL_REQUEST_TEMPLATE = ROOT / ".github/pull_request_template.md"
ISSUE_TEMPLATE = ROOT / ".github/ISSUE_TEMPLATE/feature.md"

FIXTURE_NAMES = (
    "complete.md",
    "complete-issue.md",
    "missing-evidence.md",
    "unticked-checkbox.md",
    "overclaimed-authority.md",
)


def test_an_untouched_pull_request_template_is_not_ready() -> None:
    """A blank form must not read as READY just because it contains the headings."""

    verdict = _pull_request_verdict(PULL_REQUEST_TEMPLATE.read_text(encoding="utf-8"))

    assert verdict.status == "BLOCKED"
    assert verdict.exit_code == gate.BLOCKED
    assert len(verdict.details) >= len(DEFINITION["required_checklist"])


def test_an_untouched_issue_form_is_not_ready() -> None:
    verdict = _issue_verdict(ISSUE_TEMPLATE.read_text(encoding="utf-8"))

    assert verdict.status == "BLOCKED"
    assert any("section 'Owner' is empty" in detail for detail in verdict.details)


def test_filler_does_not_satisfy_the_rollback_requirement() -> None:
    """A readiness section that names no outcome is a missing answer, not an answer."""

    for filler in ("- None.", "- N/A.", "- It should be fine."):
        body = (
            (FIXTURES / "complete-issue.md")
            .read_text(encoding="utf-8")
            .replace(
                "- Rollback or disable path is documented.",
                filler,
            )
        )
        verdict = _issue_verdict(body)

        assert verdict.status == "BLOCKED", filler
        assert any("Definition of Ready" in detail for detail in verdict.details), filler


def test_the_fixtures_are_read_from_the_repository_not_generated_at_test_time() -> None:
    committed = sorted(path.name for path in FIXTURES.glob("*.md"))

    assert committed == sorted(FIXTURE_NAMES)
    for name in FIXTURE_NAMES:
        assert (FIXTURES / name).read_text(encoding="utf-8").strip(), name
