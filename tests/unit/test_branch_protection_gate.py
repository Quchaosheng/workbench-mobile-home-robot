"""Issue #224: the main-branch safety matrix must be declared and checkable.

The observed gap was that ``main`` required only ``foundation-checks`` while the
container, kernel, MCU, CodeQL and dependency-review jobs could fail or be stale,
and the release workflow accepted any ``v*`` tag. Declaring the matrix is not
enough on its own, so these tests also pin the exit-code contract that keeps
"could not observe" from being reported as "passed".

Issue #432: a declared required check is only as good as the workflow that emits
it. ``static-and-cpu-image`` was declared required while its workflow carried a
``paths:`` trigger filter, so a documentation-only, tools-only, dashboard-only or
ruleset-only pull request created no workflow run at all and the required context
never reported. The tests below refuse a path-filtered required check, tell "no
run was created" apart from "the job ran and skipped", and exercise the
applicability decision and the verdict that replaced the trigger filter.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

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


def _workflow() -> dict:
    return yaml.safe_load(CONTAINER_WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # `on:` is parsed by YAML 1.1 as the boolean True, hence the fallback.
    triggers = workflow.get("on") or workflow.get(True)
    assert isinstance(triggers, dict), "the container workflow must declare triggers"
    return triggers


def _job(workflow: dict, name: str) -> dict:
    assert name in workflow["jobs"], f"{name} must be a job in the container workflow"
    return workflow["jobs"][name]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _image_path_patterns() -> list[str]:
    patterns = _job(_workflow(), "static-image-applicability")["env"]["IMAGE_PATH_PATTERNS"]
    return [line.strip() for line in patterns.splitlines() if line.strip()]


def _step_script(job_name: str, *, contains: str) -> str:
    """One inline `run:` script, exactly as GitHub would execute it."""

    for step in _job(_workflow(), job_name)["steps"]:
        if "run" in step and contains in step["run"]:
            return step["run"]
    raise AssertionError(f"{job_name} has no inline script containing {contains!r}")


class _ScriptCase(unittest.TestCase):
    """Run a workflow script in a throwaway repository the way CI runs it."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="issue432-"))
        self.addCleanup(shutil.rmtree, self.directory, True)

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", *arguments], cwd=self.directory, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def commit(self, files: dict[str, str], message: str) -> str:
        for name, content in files.items():
            path = self.directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def repository(self) -> str:
        self.git("init", "-q")
        self.git("config", "user.name", "Issue 432")
        self.git("config", "user.email", "issue-432@example.invalid")
        return self.commit({"README.md": "base\n", "docs/guide.md": "base\n"}, "base")

    def run_script(
        self, script: str, environment: dict[str, str], *, name: str = "script"
    ) -> subprocess.CompletedProcess:
        path = self.directory / f"{name}.sh"
        path.write_text(script, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "HOME": os.environ.get("HOME", str(self.directory)),
            **environment,
        }
        return subprocess.run(
            ["bash", str(path)], cwd=self.directory, env=env, capture_output=True, text=True, check=False
        )


class RequiredCheckTriggerPolicyTests(unittest.TestCase):
    """Issue #432: a declared required check may not be path-filtered away."""

    def test_no_required_check_workflow_filters_pull_requests_by_path(self) -> None:
        triggers = _triggers(_workflow())
        self.assertIn("pull_request", triggers)
        # A `paths:`/`paths-ignore:` key makes GitHub create no run at all when
        # nothing matches, so the declared required check reports no result.
        filtered = {
            str(event)
            for event, config in triggers.items()
            if isinstance(config, dict) and ("paths" in config or "paths-ignore" in config)
        }
        self.assertEqual(filtered, set())

    def test_the_required_check_name_is_owned_by_an_always_running_job(self) -> None:
        workflow = _workflow()
        required = load_declaration(DECLARATION)["required_status_checks"]
        self.assertIn("static-and-cpu-image", required)
        verdict = _job(workflow, "static-and-cpu-image")
        # `if: always()` is what makes the verdict run when the build is skipped,
        # which is the case the old trigger filter hid by creating no run at all.
        self.assertEqual(verdict.get("if"), "always()")
        self.assertIn("static-image-build", _needs(verdict))

    def test_the_expensive_image_job_still_runs_for_image_relevant_changes(self) -> None:
        workflow = _workflow()
        build = _job(workflow, "static-image-build")
        self.assertIn("static-image-applicability", _needs(build))
        self.assertIn("outputs.applicable", build.get("if", ""))
        # The build keeps every expensive step and the artifact it produces; the
        # verdict job only reports, so it may not own the scan either.
        build_yaml = yaml.safe_dump(build)
        verdict_yaml = yaml.safe_dump(_job(workflow, "static-and-cpu-image"))
        for marker in (
            "anchore/scan-action@",
            "docker/build-push-action@",
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, build_yaml)
                self.assertNotIn(marker, verdict_yaml)

    def test_the_supply_chain_scan_contract_is_unchanged(self) -> None:
        text = CONTAINER_WORKFLOW.read_text(encoding="utf-8")
        scan_block = text.split("anchore/scan-action@", 1)[1].split("- uses:", 1)[0]
        self.assertIn("fail-build: true", scan_block)
        self.assertIn("severity-cutoff: high", scan_block)
        self.assertIn("vex: docker/vex.json", scan_block)
        self.assertIn("output-file: workbench-1-grype.json", scan_block)
        self.assertNotIn("only-fixed:", scan_block)

    def test_a_skipped_or_absent_run_is_not_a_passing_required_check(self) -> None:
        # The release checker consumes the same matrix. A skipped job and a run
        # that was never created must both refuse the release, which is exactly
        # what a path-filtered required check used to look like.
        not_passing, missing = failing_required_checks(
            ("static-and-cpu-image",),
            [{"name": "static-and-cpu-image", "status": "completed", "conclusion": "skipped"}],
        )
        self.assertEqual(missing, [])
        self.assertTrue(not_passing)
        not_passing, missing = failing_required_checks(("static-and-cpu-image",), [])
        self.assertEqual(not_passing, [])
        self.assertEqual(missing, ["static-and-cpu-image"])

    def test_the_branch_protection_page_documents_trigger_and_matrix_together(self) -> None:
        page = (ROOT / "docs" / "security" / "branch-protection.md").read_text(encoding="utf-8")
        self.assertIn("static-and-cpu-image", page)
        self.assertIn("path filter", page.lower())
        self.assertIn("owner review", page.lower())


class ApplicabilityDecisionTests(_ScriptCase):
    """Issue #432: the decision replaces the filter, so it is actually executed."""

    def decide(self, environment: dict[str, str]) -> tuple[subprocess.CompletedProcess, str, str]:
        output = self.directory / "github-output.txt"
        output.unlink(missing_ok=True)
        result = self.run_script(
            _step_script("static-image-applicability", contains="IMAGE_PATH_PATTERNS"),
            {
                "GITHUB_OUTPUT": str(output),
                "IMAGE_PATH_PATTERNS": "\n".join(_image_path_patterns()),
                **environment,
            },
            name="decide",
        )
        recorded = output.read_text(encoding="utf-8") if output.exists() else ""
        values = dict(line.partition("=")[::2] for line in recorded.splitlines() if "=" in line)
        return result, values.get("applicable", ""), values.get("reason", "")

    def test_documentation_tools_dashboard_and_ruleset_changes_are_not_applicable(self) -> None:
        base = self.repository()
        for label, files in (
            ("documentation only", {"docs/security/branch-protection.md": "text\n", "README.md": "text\n"}),
            ("tools only", {"tools/scripts/quality_gates.py": "text\n"}),
            ("dashboard only", {"apps/dashboard/app.js": "text\n"}),
            ("ruleset only", {".github/rulesets/main-protection.json": "{}\n"}),
        ):
            with self.subTest(label=label):
                head = self.commit(files, label)
                result, applicable, reason = self.decide(
                    {"EVENT_NAME": "pull_request", "PULL_REQUEST_BASE": base, "PULL_REQUEST_HEAD": head}
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(applicable, "false", reason)
                self.assertIn("none of the", reason)

    def test_an_image_relevant_change_is_applicable(self) -> None:
        base = self.repository()
        head = self.commit({"libs/contracts/model.py": "changed\n"}, "library change")
        result, applicable, reason = self.decide(
            {"EVENT_NAME": "pull_request", "PULL_REQUEST_BASE": base, "PULL_REQUEST_HEAD": head}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(applicable, "true")
        self.assertIn("libs/contracts/model.py", reason)

    def test_an_unknown_base_builds_rather_than_claiming_not_applicable(self) -> None:
        self.repository()
        # A manual dispatch and a brand-new branch have nothing to compare, so the
        # run must build the image instead of reporting that it is irrelevant.
        for environment in (
            {"EVENT_NAME": "workflow_dispatch"},
            {"EVENT_NAME": "push", "PUSH_BEFORE": "0" * 40, "PUSH_AFTER": "a" * 40},
        ):
            with self.subTest(environment=environment):
                result, applicable, _ = self.decide(environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(applicable, "true")

    def test_an_empty_policy_fails_instead_of_reporting_not_applicable(self) -> None:
        self.repository()
        result = self.run_script(
            _step_script("static-image-applicability", contains="IMAGE_PATH_PATTERNS"),
            {"GITHUB_OUTPUT": str(self.directory / "empty.txt"), "IMAGE_PATH_PATTERNS": ""},
            name="empty-policy",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("IMAGE_PATH_PATTERNS is empty", result.stderr)

    def test_a_decision_that_cannot_be_recorded_is_a_failure(self) -> None:
        base = self.repository()
        head = self.commit({"docs/note.md": "text\n"}, "docs")
        result = self.run_script(
            _step_script("static-image-applicability", contains="IMAGE_PATH_PATTERNS"),
            {
                "IMAGE_PATH_PATTERNS": "\n".join(_image_path_patterns()),
                "EVENT_NAME": "pull_request",
                "PULL_REQUEST_BASE": base,
                "PULL_REQUEST_HEAD": head,
            },
            name="no-output",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GITHUB_OUTPUT", result.stderr)


class VerdictJobTests(_ScriptCase):
    """Issue #432: the always-running job must report a truthful conclusion."""

    def verdict(self, build_result: str, apply_result: str) -> subprocess.CompletedProcess:
        return self.run_script(
            _step_script("static-and-cpu-image", contains="case "),
            {
                "GITHUB_STEP_SUMMARY": str(self.directory / "summary.md"),
                "BUILD_RESULT": build_result,
                "APPLY_RESULT": apply_result,
                "APPLY_REASON": "none of the 15 image path patterns match the two changed paths",
            },
            name="verdict",
        )

    def test_a_real_build_passes_and_a_failing_build_fails(self) -> None:
        passed = self.verdict("success", "success")
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertIn("static-and-cpu-image: success", passed.stdout)
        failed = self.verdict("failure", "success")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("static-and-cpu-image: failure", failed.stdout)

    def test_a_not_applicable_change_passes_and_says_why(self) -> None:
        skipped = self.verdict("skipped", "success")
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertIn("static-and-cpu-image: not-applicable", skipped.stdout)
        self.assertIn("none of the 15 image path patterns", skipped.stdout)

    def test_a_skipped_build_without_a_decision_is_a_failure(self) -> None:
        for apply_result in ("failure", "cancelled", "skipped"):
            with self.subTest(apply_result=apply_result):
                result = self.verdict("skipped", apply_result)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("static-and-cpu-image: failure", result.stdout)

    def test_a_cancelled_or_unusable_build_is_not_a_pass(self) -> None:
        for build_result in ("cancelled", ""):
            with self.subTest(build_result=build_result):
                result = self.verdict(build_result, "success")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("static-and-cpu-image: success", result.stdout)


class WorkflowPinTests(unittest.TestCase):
    """Issue #404's pinning rule, applied to the workflow this change edits."""

    def test_every_action_reference_stays_pinned_to_a_commit_sha(self) -> None:
        import re

        workflow = CONTAINER_WORKFLOW.read_text(encoding="utf-8")
        action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]
        self.assertTrue(action_lines)
        self.assertTrue(all(re.search(r"@[0-9a-f]{40}\s+# v[0-9]", line) for line in action_lines), action_lines)


if __name__ == "__main__":
    unittest.main()
