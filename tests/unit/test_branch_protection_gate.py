"""Issue #224: the main-branch safety matrix must be declared and checkable.

The observed gap was that ``main`` required only ``foundation-checks`` while the
container, kernel, MCU, CodeQL and dependency-review jobs could fail or be stale,
and the release workflow accepted any ``v*`` tag. Declaring the matrix is not
enough on its own, so these tests also pin the exit-code contract that keeps
"could not observe" from being reported as "passed".
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "scripts"))

from check_branch_protection import (
    FAIL,
    INCOMPLETE,
    PASS,
    DeclarationError,
    compare,
    load_declaration,
)
from check_branch_protection import (
    main as protection_main,
)
from check_release_commit import (
    FAIL as RELEASE_FAIL,
)
from check_release_commit import (
    INCOMPLETE as RELEASE_INCOMPLETE,
)
from check_release_commit import (
    PASS as RELEASE_PASS,
)
from check_release_commit import (
    failing_required_checks,
    load_required_checks,
)
from check_release_commit import (
    main as release_main,
)

DECLARATION = ROOT / ".github" / "rulesets" / "main-protection.json"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-image.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SECURITY_WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"
CONTAINER_WORKFLOW = ROOT / ".github" / "workflows" / "container-full-stack.yml"


def live_protection(checks: list[str], *, strict: bool = True, enforce_admins: bool = True) -> dict:
    return {
        "required_status_checks": {"strict": strict, "contexts": checks},
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "dismiss_stale_reviews": True,
        },
        "required_conversation_resolution": {"enabled": True},
        "enforce_admins": {"enabled": enforce_admins},
    }


class DeclarationTests(unittest.TestCase):
    def test_declaration_is_valid_and_lists_the_whole_safety_matrix(self) -> None:
        declaration = load_declaration(DECLARATION)
        self.assertEqual(declaration["target_branch"], "main")
        self.assertTrue(declaration["strict_required_status_checks"])
        self.assertFalse(declaration["administrator_bypass"]["allowed"])
        self.assertEqual(declaration["required_approving_review_count"], 1)

        required = declaration["required_status_checks"]
        # The five families Issue #224 names must all be declared.
        for name in (
            "foundation-checks",
            "static-and-cpu-image",
            "kernel-module",
            "mcu-qemu",
            "codeql-python",
            "dependency-review",
        ):
            with self.subTest(name=name):
                self.assertIn(name, required)

    def test_declared_checks_all_exist_as_real_jobs(self) -> None:
        declaration = load_declaration(DECLARATION)
        jobs: set[str] = set()
        for path in (CI_WORKFLOW, SECURITY_WORKFLOW, CONTAINER_WORKFLOW):
            import re

            jobs.update(re.findall(r"^  ([a-z0-9_-]+):$", path.read_text(encoding="utf-8"), re.M))
        for name in declaration["required_status_checks"]:
            with self.subTest(name=name):
                self.assertIn(name, jobs, "a required check must name a job that exists")

    def test_declaration_rejects_a_duplicate_or_empty_matrix(self) -> None:
        for payload in (
            {"required_status_checks": []},
            {"required_status_checks": ["a", "a"]},
            {"required_status_checks": ["  "]},
        ):
            with self.subTest(payload=payload), self.assertRaises(DeclarationError):
                path = ROOT / "tests" / "fixtures" / "tmp-declaration.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                try:
                    load_declaration(path)
                finally:
                    path.unlink(missing_ok=True)


class ProtectionComparisonTests(unittest.TestCase):
    def test_matching_configuration_passes(self) -> None:
        declaration = load_declaration(DECLARATION)
        self.assertEqual(compare(declaration, live_protection(declaration["required_status_checks"])), ())

    def test_the_observed_legacy_configuration_fails(self) -> None:
        # This is the exact payload quoted in Issue #224's evidence section.
        legacy = {
            "required_status_checks": {"strict": False, "contexts": ["foundation-checks"]},
            "enforce_admins": {"enabled": False},
        }
        reasons = compare(load_declaration(DECLARATION), legacy)
        joined = " | ".join(reasons)
        self.assertIn("does not require", joined)
        self.assertIn("strict", joined)
        self.assertIn("administrator bypass", joined)

    def test_each_weakening_is_reported_individually(self) -> None:
        declaration = load_declaration(DECLARATION)
        required = declaration["required_status_checks"]
        cases = {
            "missing check": live_protection(required[1:]),
            "not strict": live_protection(required, strict=False),
            "admin bypass": live_protection(required, enforce_admins=False),
            "no checks at all": {"required_status_checks": None},
            "no pull request": {"required_status_checks": {"strict": True, "contexts": required}},
        }
        for label, live in cases.items():
            with self.subTest(label=label):
                self.assertTrue(compare(declaration, live), f"{label} must be refused")

    def test_check_run_objects_are_read_as_well_as_contexts(self) -> None:
        declaration = load_declaration(DECLARATION)
        live = live_protection([])
        live["required_status_checks"] = {
            "strict": True,
            "contexts": [],
            "checks": [{"context": name} for name in declaration["required_status_checks"]],
        }
        self.assertEqual(compare(declaration, live), ())


class ProtectionExitCodeTests(unittest.TestCase):
    def test_pass_fail_and_incomplete_are_distinct(self) -> None:
        declaration = load_declaration(DECLARATION)
        good = ROOT / "tests" / "fixtures" / "tmp-live-good.json"
        bad = ROOT / "tests" / "fixtures" / "tmp-live-bad.json"
        good.write_text(json.dumps(live_protection(declaration["required_status_checks"])), encoding="utf-8")
        bad.write_text(
            json.dumps({"required_status_checks": {"strict": False, "contexts": ["foundation-checks"]}}),
            encoding="utf-8",
        )
        try:
            self.assertEqual(protection_main(["--live", str(good)]), PASS)
            self.assertEqual(protection_main(["--live", str(bad)]), FAIL)
        finally:
            good.unlink(missing_ok=True)
            bad.unlink(missing_ok=True)

    def test_unobservable_protection_is_incomplete_not_pass(self) -> None:
        # No token and no recorded payload: the gate must refuse to guess.
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(protection_main(["--repository", "owner/repo"]), INCOMPLETE)

    def test_missing_recording_is_incomplete(self) -> None:
        self.assertEqual(protection_main(["--live", str(ROOT / "does-not-exist.json")]), INCOMPLETE)

    def test_malformed_declaration_is_a_failure(self) -> None:
        bad = ROOT / "tests" / "fixtures" / "tmp-decl-bad.json"
        bad.write_text("{not json", encoding="utf-8")
        try:
            self.assertEqual(protection_main(["--declaration", str(bad), "--live", str(DECLARATION)]), FAIL)
        finally:
            bad.unlink(missing_ok=True)


class ReleaseSafetyMatrixTests(unittest.TestCase):
    def test_required_checks_are_read_from_the_declaration(self) -> None:
        required = load_required_checks(DECLARATION)
        self.assertIn("kernel-module", required)
        self.assertIn("dependency-review", required)

    def test_only_success_may_publish(self) -> None:
        required = ("a", "b", "c", "d")
        runs = [
            {"name": "a", "status": "completed", "conclusion": "success"},
            {"name": "b", "status": "completed", "conclusion": "skipped"},
            {"name": "c", "status": "completed", "conclusion": "neutral"},
            {"name": "d", "status": "in_progress", "conclusion": None},
        ]
        not_passing, missing = failing_required_checks(required, runs)
        self.assertEqual(missing, [])
        self.assertEqual(len(not_passing), 3)
        self.assertTrue(any("skipped" in item for item in not_passing))
        self.assertTrue(any("neutral" in item for item in not_passing))
        self.assertTrue(any("in_progress" in item for item in not_passing))

    def test_a_required_check_with_no_run_is_reported_missing(self) -> None:
        not_passing, missing = failing_required_checks(
            ("a", "b"), [{"name": "a", "status": "completed", "conclusion": "success"}]
        )
        self.assertEqual(not_passing, [])
        self.assertEqual(missing, ["b"])

    def test_the_latest_run_for_a_name_wins(self) -> None:
        runs = [
            {"name": "a", "status": "completed", "conclusion": "failure"},
            {"name": "a", "status": "completed", "conclusion": "success"},
        ]
        not_passing, missing = failing_required_checks(("a",), runs)
        self.assertEqual((not_passing, missing), ([], []))

    def test_offline_pass_and_failure_exit_codes(self) -> None:
        good = ROOT / "tests" / "fixtures" / "tmp-checks-good.json"
        bad = ROOT / "tests" / "fixtures" / "tmp-checks-bad.json"
        required = load_required_checks(DECLARATION)
        good.write_text(
            json.dumps({"check_runs": [{"name": n, "status": "completed", "conclusion": "success"} for n in required]}),
            encoding="utf-8",
        )
        bad.write_text(
            json.dumps({"check_runs": [{"name": required[0], "status": "completed", "conclusion": "failure"}]}),
            encoding="utf-8",
        )
        try:
            self.assertEqual(release_main(["--check-runs", str(good), "--commit", "c" * 40]), RELEASE_PASS)
            self.assertEqual(release_main(["--check-runs", str(bad), "--commit", "c" * 40]), RELEASE_FAIL)
        finally:
            good.unlink(missing_ok=True)
            bad.unlink(missing_ok=True)

    def test_unobservable_release_inputs_are_incomplete(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(release_main([]), RELEASE_INCOMPLETE)


class ReleaseWorkflowWiringTests(unittest.TestCase):
    def test_release_workflow_gates_on_the_safety_matrix_before_building(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("actions: read", workflow)
        self.assertIn("check_release_commit.py", workflow)
        self.assertIn("--protected-branch main", workflow)
        # The matrix gate must run before the image is built.
        self.assertLess(workflow.index("check_release_commit.py"), workflow.index("docker/build-push-action@"))

    def test_release_workflow_runs_the_task_packet_and_contract_gate_before_building(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("check_task_packet.py --all", workflow)
        self.assertIn("make contract", workflow)
        self.assertIn("make scenario-check", workflow)
        self.assertIn("make context-check", workflow)
        # A malformed packet or a drifted contract must refuse the release
        # before an image is built, not after it is pushed.
        self.assertLess(workflow.index("check_task_packet.py --all"), workflow.index("docker/build-push-action@"))

    def test_release_workflow_still_verifies_provenance_before_attestation(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        # The manifest is written after publishing because it records the
        # registry digest, so the gate that protects promotion is the
        # verification that precedes the attestation.
        self.assertLess(
            workflow.index("--verify release-manifest.json"), workflow.index("actions/attest-build-provenance@")
        )
        self.assertLess(workflow.index("--require-promotion"), workflow.index("workbench-1-release-provenance"))


if __name__ == "__main__":
    unittest.main()
