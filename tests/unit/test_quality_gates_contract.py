"""Contract tests for the Issue #89 property and mutation gates.

The gate is evidence, so its own promises are tested: the registry covers every
declared boundary, the mutation literals still occur exactly once, the quarantine
registry is honest, the workflow runs the same commands a developer runs, and the
gate never reports PASS for a run it could not complete.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts")]

import quality_gates

REGISTRY = ROOT / "tools/qa/mutations-v1.json"
QUARANTINE = ROOT / "tools/qa/quarantine-v1.json"
WORKFLOW = ROOT / ".github/workflows/quality-gates.yml"


def registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def test_the_registry_covers_every_declared_boundary() -> None:
    payload = registry()
    boundaries = {entry["boundary"] for entry in payload["mutations"]}

    assert quality_gates.REQUIRED_BOUNDARIES <= boundaries


def test_every_registered_literal_still_occurs_exactly_once() -> None:
    """A stale literal must fail here, not silently become an INCOMPLETE probe."""

    for entry in registry()["mutations"]:
        source = (ROOT / entry["file"]).read_text(encoding="utf-8")
        assert source.count(entry["literal"]) == 1, entry["id"]
        assert entry["replacement"] != entry["literal"], entry["id"]
        assert entry["tests"], entry["id"]


def test_every_registered_probe_names_tests_that_exist() -> None:
    for entry in registry()["mutations"]:
        for test in entry["tests"]:
            assert (ROOT / test).is_file(), f"{entry['id']}: {test}"


def test_the_registry_never_names_a_path_agents_md_protects() -> None:
    payload = registry()
    assert payload["excluded_paths"], "the registry must declare its exclusions"

    for entry in payload["mutations"]:
        assert quality_gates._protected_path_violation(payload, entry["file"]) is None, entry["id"]


def test_a_protected_path_is_still_refused_by_the_guard() -> None:
    payload = registry()

    assert quality_gates._protected_path_violation(payload, "firmware/mcu/main.c") is not None
    assert quality_gates._protected_path_violation(payload, "robot/control/motion.py") is not None
    assert quality_gates._protected_path_violation(payload, "libs/kernel/workbench/kernel/event_store.py") is None


def test_the_quarantine_registry_is_valid_and_current() -> None:
    payload = json.loads(QUARANTINE.read_text(encoding="utf-8"))

    assert payload["entries"] == [], "an entry needs an owner, a reason and a future expiry; none are needed today"
    assert "reported, never retried" in payload["purpose"]


def test_the_property_suites_are_registered_by_their_logical_names() -> None:
    for path, suite in quality_gates.PROPERTY_SUITES.items():
        assert (ROOT / path).is_file(), path
        source = (ROOT / path).read_text(encoding="utf-8")
        assert f'SUITE = "{suite}"' in source, path


def test_the_archive_check_refuses_a_shrunken_corpus() -> None:
    good = {
        "suites": [
            {
                "suite": suite,
                "generator_version": quality_gates.GENERATOR_VERSION,
                "case_schema_version": quality_gates.CASE_SCHEMA_VERSION,
                "seeds": [1],
                "case_count": quality_gates.MINIMUM_CASES_PER_SUITE,
                "corpus_digest": "a" * 64,
                "failures": [],
            }
            for suite in quality_gates.PROPERTY_SUITES.values()
        ]
    }
    assert quality_gates._verify_archive(good, frozenset(quality_gates.PROPERTY_SUITES.values())) == []

    shrunken = json.loads(json.dumps(good))
    shrunken["suites"][0]["case_count"] = quality_gates.MINIMUM_CASES_PER_SUITE - 1
    problems = quality_gates._verify_archive(shrunken, frozenset(quality_gates.PROPERTY_SUITES.values()))
    assert any("below the committed minimum" in problem for problem in problems)

    missing = json.loads(json.dumps(good))
    missing["suites"] = missing["suites"][:-1]
    problems = quality_gates._verify_archive(missing, frozenset(quality_gates.PROPERTY_SUITES.values()))
    assert any("missing suites" in problem for problem in problems)

    recorded_failure = json.loads(json.dumps(good))
    recorded_failure["suites"][0]["failures"] = [{"case_id": "deadbeef"}]
    problems = quality_gates._verify_archive(recorded_failure, frozenset(quality_gates.PROPERTY_SUITES.values()))
    assert any("failing cases" in problem for problem in problems)


def test_the_failure_parsers_read_real_pytest_output() -> None:
    output = (
        "FAILED tests/property/test_a.py::test_one - assert False\n"
        "FAILED tests/property/test_a.py::test_one - assert False\n"
        "1 failed, 4 passed\n"
    )

    assert quality_gates._parse_failures(output) == ["tests/property/test_a.py::test_one"]
    counts = quality_gates._parse_counts(output)
    assert counts["failed"] == 1
    assert counts["passed"] == 4


def test_the_timeout_is_reported_as_incomplete_rather_than_a_pass(tmp_path: Path) -> None:
    """An exhausted budget must not be able to read as green."""

    result = quality_gates.run_property_gate(archive=tmp_path / "summary.json", time_budget_s=-1.0)

    assert result.status == "INCOMPLETE"
    assert result.exit_code == quality_gates.INCOMPLETE
    assert result.exit_code != quality_gates.PASS


def test_a_missing_registry_is_incomplete_rather_than_a_pass(tmp_path: Path) -> None:
    result = quality_gates.run_mutation_gate(
        registry_path=tmp_path / "absent.json",
        quarantine_path=QUARANTINE,
        archive=None,
        time_budget_s=30.0,
    )

    assert result.status == "INCOMPLETE"
    assert result.exit_code == quality_gates.INCOMPLETE


def test_an_expired_quarantine_entry_is_rejected(tmp_path: Path) -> None:
    expired = tmp_path / "quarantine.json"
    expired.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "test": "tests/property/test_property_event_ordering.py::test_a_foreign_run_id_is_rejected",
                        "owner": "Quchaosheng",
                        "reason": "synthetic expired entry",
                        "expires": "2020-01-01",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    active, problems = quality_gates._load_quarantine(expired)

    assert active == {}
    assert any("expired" in problem for problem in problems)


def test_a_quarantine_entry_without_an_owner_or_reason_is_rejected(tmp_path: Path) -> None:
    for entry in (
        {"test": "t", "owner": "  ", "reason": "x", "expires": "2099-01-01"},
        {"test": "t", "owner": "Quchaosheng", "reason": "", "expires": "2099-01-01"},
        {"test": "t", "owner": "Quchaosheng", "reason": "x", "expires": "not-a-date"},
        {"owner": "Quchaosheng", "reason": "x", "expires": "2099-01-01"},
    ):
        path = tmp_path / "quarantine.json"
        path.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
        active, problems = quality_gates._load_quarantine(path)
        assert active == {}
        assert problems


def test_the_gate_audits_quarantine_entries_against_the_sandbox(tmp_path: Path) -> None:
    """A quarantined test that now passes must be reported as stale."""

    problems = quality_gates._check_quarantine_entries(
        {"tests/property/test_property_event_ordering.py::test_a_foreign_run_id_is_rejected": {"owner": "Quchaosheng"}},
        cwd=ROOT,
        env=dict(__import__("os").environ),
        timeout_s=120.0,
    )

    assert any("currently passing" in problem for problem in problems)


def test_a_quarantined_test_that_does_not_exist_is_reported(tmp_path: Path) -> None:
    problems = quality_gates._check_quarantine_entries(
        {"tests/property/test_property_event_ordering.py::test_does_not_exist": {"owner": "Quchaosheng"}},
        cwd=ROOT,
        env=dict(__import__("os").environ),
        timeout_s=120.0,
    )

    assert problems


def test_the_makefile_targets_run_the_same_commands_as_the_workflow() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "property-gate:" in makefile
    assert "mutation-gate:" in makefile
    assert "quality-gates: property-gate mutation-gate" in makefile
    assert "make property-gate" in workflow
    assert "make mutation-gate" in workflow


def test_the_workflow_is_scheduled_pinned_and_read_only() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    header = workflow.split("jobs:", maxsplit=1)[0]

    assert "schedule:" in header
    assert "workflow_dispatch:" in header
    assert "permissions:\n  contents: read" in header
    assert "if-no-files-found: error" in workflow
    assert "NOT_EXECUTED" in workflow

    import re

    action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]
    assert action_lines
    assert all(re.search(r"@[0-9a-f]{40}\s+# v[0-9]", line) for line in action_lines)


def test_the_sandbox_guard_covers_every_module_the_probes_can_reach() -> None:
    """A module that escapes the copy would let a probe test unmutated code."""

    for module in (
        "workbench.kernel.event_store",
        "workbench.hardware.can_driver_safe",
        "workbench_world_model.reducer",
        "workbench_agent_runtime.policy_validator",
    ):
        assert module in quality_gates.SANDBOX_GUARD_MODULES

    for entry in registry()["mutations"]:
        assert any(entry["file"].startswith(prefix) for prefix in ("libs/", "services/")), entry["id"]


def test_the_gates_never_retry() -> None:
    """No retry loop may hide a flake; quarantine is the only escape hatch."""

    for script in (ROOT / "tools/scripts/quality_gates.py", ROOT / "tests/property/_generator.py"):
        source = script.read_text(encoding="utf-8")
        assert "pytest-rerunfailures" not in source
        assert "--reruns" not in source
        assert "flaky" not in source.casefold()


@pytest.mark.parametrize("protected", ["firmware/x.py", "robot/control/y.py"])
def test_protected_prefixes_are_named_in_the_registry_exclusions(protected: str) -> None:
    exclusions = registry()["excluded_paths"]
    assert any(protected.startswith(key.rstrip("*")) for key in exclusions)
