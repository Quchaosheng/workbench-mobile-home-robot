"""Issue #82: release eligibility must come from evidence, not from a claim.

The tests below are written as the bypasses that used to work: a hand-written
summary, an edited log, a swapped manifest, an unattributed audit, a scripted run
relabelled as external. Each one must now be refused, and each refusal must name
the input that disagreed.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "scripts"))

from collect_metrics import collect
from generate_report import release_reasons
from release_eligibility import (
    PROVENANCE_FORMAT,
    PROVENANCE_FORMAT_VERSION,
    REASON_AUDIT_INCOMPLETE,
    REASON_AUDIT_INVALID,
    REASON_AUDIT_MISSING,
    REASON_COMMIT_MISMATCH,
    REASON_FALSE_COMPLETION,
    REASON_LOG_HASH_MISMATCH,
    REASON_MANIFEST_HASH_MISMATCH,
    REASON_NO_PROVENANCE,
    REASON_SCRIPTED_RUNNER,
    REASON_UNKNOWN_COMMIT,
    build_provenance,
    discover_manifests,
    evaluate_eligibility,
    event_log_sha256,
    file_sha256,
    load_provenance,
)
from run_evaluation import scripted_events, write_jsonl

MANIFEST = json.loads((ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text(encoding="utf-8"))

# Sentinel: "leave this argument at the fixture default" is not the same
# request as "this input is absent", and both must be testable.
MISSING = object()


def external_events() -> list[dict]:
    """A run whose per-event metadata declares the external runner."""
    events = scripted_events("v-test", MANIFEST, "abc123", 1000)
    for event in events:
        event["evaluation"] = {**event["evaluation"], "runner": "external"}
    return events


class Fixture:
    """A temporary run directory with provenance, audit and logs on disk."""

    def __init__(self, root: Path, *, runner: str = "external") -> None:
        self.root = root
        self.run_dir = root / "v-test"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = root / "normal-001.json"
        self.manifest_path.write_text(json.dumps(MANIFEST), encoding="utf-8")
        events = scripted_events("v-test", MANIFEST, "abc123", 1000)
        if runner != "scripted":
            for event in events:
                event["evaluation"] = {**event["evaluation"], "runner": runner}
        self.events = events
        write_jsonl(self.run_dir / "normal-001.jsonl", events)

    @property
    def run_id(self) -> str:
        return str(self.events[0]["run_id"])

    def manifests(self) -> dict[str, Path]:
        return {"normal-001": self.manifest_path}

    def provenance(self, **overrides: object) -> dict:
        provenance = build_provenance(
            runner="external",
            commit="abc123",
            environment={"platform": "test", "python": "3.12", "machine": "test"},
            manifests=self.manifests(),
            logs={self.run_id: self.events},
        )
        provenance.update(overrides)
        return provenance

    def audit(self, **overrides: object) -> dict:
        audit = {
            "format": "workbench-false-completion-audit",
            "format_version": 1,
            "reviewed_by": "reviewer",
            "reviewed_at": "2026-09-17T00:00:00+00:00",
            "runs": {self.run_id: {"oracle_status": "confirmed"}},
        }
        audit.update(overrides)
        return audit

    def write_audit(self, **overrides: object) -> Path:
        path = self.root / "audit.json"
        path.write_text(json.dumps(self.audit(**overrides)), encoding="utf-8")
        return path

    def write_provenance(self, **overrides: object) -> Path:
        path = self.root / "provenance.json"
        path.write_text(json.dumps(self.provenance(**overrides), indent=2), encoding="utf-8")
        return path

    def verdict(self, provenance: object = MISSING, audit: object = MISSING) -> dict:
        """Evaluate with the fixture's own provenance and audit unless overridden.

        ``MISSING`` distinguishes "use the fixture default" from an explicit
        ``None``, which means "this input is absent" and must be tested as such.
        """
        return evaluate_eligibility(
            runs={self.run_id: self.events},
            provenance=self.provenance() if provenance is MISSING else provenance,
            manifests=self.manifests(),
            audit=self.audit() if audit is MISSING else audit,
        )


class ProvenanceRecordTests(unittest.TestCase):
    def test_record_identifies_format_version_runner_commit_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            provenance = fixture.provenance()
            self.assertEqual(provenance["format"], PROVENANCE_FORMAT)
            self.assertEqual(provenance["format_version"], PROVENANCE_FORMAT_VERSION)
            self.assertEqual(provenance["runner"], "external")
            self.assertEqual(provenance["commit"], "abc123")
            self.assertEqual(provenance["environment"]["python"], "3.12")
            entry = provenance["runs"][fixture.run_id]
            self.assertEqual(entry["event_log_sha256"], event_log_sha256(fixture.events))
            self.assertEqual(entry["manifest_sha256"], file_sha256(fixture.manifest_path))
            self.assertEqual(entry["scenario_id"], "normal-001")

    def test_unsupported_provenance_format_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provenance.json"
            for payload in (
                {"format": "something-else", "format_version": PROVENANCE_FORMAT_VERSION, "runs": {}},
                {"format": PROVENANCE_FORMAT, "format_version": 99, "runs": {}},
                {"format": PROVENANCE_FORMAT, "format_version": PROVENANCE_FORMAT_VERSION},
            ):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_provenance(path)

    def test_manifest_discovery_accepts_one_directory_or_many(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Fixture(root)
            self.assertIn("normal-001", discover_manifests(fixture.manifest_path.parent))
            self.assertIn("normal-001", discover_manifests([fixture.manifest_path.parent, root / "absent"]))


class EligibilityRefusalTests(unittest.TestCase):
    def test_matching_evidence_with_a_signed_audit_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verdict = Fixture(Path(directory)).verdict()
            self.assertTrue(verdict["eligible"], verdict["reasons"])
            self.assertEqual(verdict["reasons"], [])
            self.assertTrue(verdict["per_run"]["v-test--normal-001"]["eligible"])

    def test_missing_provenance_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verdict = Fixture(Path(directory)).verdict(provenance=None)
            self.assertFalse(verdict["eligible"])
            self.assertIn(REASON_NO_PROVENANCE, verdict["reasons"])

    def test_scripted_run_cannot_be_relabelled_external(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), runner="scripted")
            # Provenance is built from the same scripted logs, so the claim and the
            # evidence agree - the runner identity itself is what refuses it.
            verdict = fixture.verdict()
            self.assertFalse(verdict["eligible"])
            self.assertIn(REASON_SCRIPTED_RUNNER, verdict["reasons"])

    def test_edited_log_no_longer_matches_its_provenance_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            provenance = fixture.provenance()
            edited = json.loads(json.dumps(fixture.events))
            # A real content edit: the observation confidence changes, which is
            # exactly the kind of quiet rewording a copied log could introduce.
            edited[-1]["payload"] = {**edited[-1]["payload"], "edited_after_the_fact": True}
            self.assertNotEqual(event_log_sha256(edited), event_log_sha256(fixture.events))
            verdict = evaluate_eligibility(
                runs={fixture.run_id: edited},
                provenance=provenance,
                manifests=fixture.manifests(),
                audit=fixture.audit(),
            )
            self.assertFalse(verdict["eligible"])
            self.assertIn(REASON_LOG_HASH_MISMATCH, verdict["reasons"])

    def test_changed_manifest_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            provenance = fixture.provenance()
            fixture.manifest_path.write_text(
                json.dumps({**MANIFEST, "timeout_s": MANIFEST["timeout_s"] + 1}), encoding="utf-8"
            )
            verdict = evaluate_eligibility(
                runs={fixture.run_id: fixture.events},
                provenance=provenance,
                manifests=fixture.manifests(),
                audit=fixture.audit(),
            )
            self.assertFalse(verdict["eligible"])
            self.assertIn(REASON_MANIFEST_HASH_MISMATCH, verdict["reasons"])

    def test_commit_and_runner_disagreements_are_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            provenance = fixture.provenance()
            provenance["runs"][fixture.run_id]["commit"] = "other"
            provenance["runs"][fixture.run_id]["runner"] = "external-but-different"
            verdict = evaluate_eligibility(
                runs={fixture.run_id: fixture.events},
                provenance=provenance,
                manifests=fixture.manifests(),
                audit=fixture.audit(),
            )
            self.assertIn(REASON_COMMIT_MISMATCH, verdict["reasons"])
            self.assertIn("provenance runner disagrees with the event log", verdict["reasons"])

    def test_unknown_commit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            events = json.loads(json.dumps(fixture.events))
            for event in events:
                event["evaluation"]["commit"] = "unknown"
            verdict = evaluate_eligibility(
                runs={fixture.run_id: events},
                provenance=fixture.provenance(),
                manifests=fixture.manifests(),
                audit=fixture.audit(),
            )
            self.assertFalse(verdict["eligible"])
            self.assertIn(REASON_UNKNOWN_COMMIT, verdict["reasons"])


class AuditTests(unittest.TestCase):
    def test_missing_audit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verdict = Fixture(Path(directory)).verdict(audit=None)
            self.assertFalse(verdict["eligible"])
            self.assertIn(REASON_AUDIT_MISSING, verdict["reasons"])

    def test_audit_without_reviewer_or_timestamp_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            for label, audit in (
                ("no reviewer", fixture.audit(reviewed_by="")),
                ("no timestamp", fixture.audit(reviewed_at=None)),
                ("no oracle status", {**fixture.audit(), "runs": {fixture.run_id: {}}}),
                ("unknown oracle status", {**fixture.audit(), "runs": {fixture.run_id: {"oracle_status": "maybe"}}}),
            ):
                with self.subTest(case=label):
                    verdict = fixture.verdict(audit=audit)
                    self.assertFalse(verdict["eligible"])
                    self.assertIn(REASON_AUDIT_INVALID, verdict["reasons"])

    def test_audit_must_cover_every_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            verdict = fixture.verdict(audit=fixture.audit(runs={}))
            self.assertFalse(verdict["eligible"])
            self.assertIn(REASON_AUDIT_INCOMPLETE, verdict["reasons"])

    def test_disagreement_with_the_oracle_is_a_false_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            verdict = fixture.verdict(audit=fixture.audit(runs={fixture.run_id: {"oracle_status": "refuted"}}))
            self.assertFalse(verdict["eligible"])
            self.assertIn(REASON_FALSE_COMPLETION, verdict["reasons"])

    def test_audit_is_judged_even_when_provenance_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            verdict = fixture.verdict(provenance=None, audit=fixture.audit(reviewed_by=""))
            self.assertIn(REASON_AUDIT_INVALID, verdict["reasons"])


class CopiedSummaryTests(unittest.TestCase):
    """``collect_metrics.collect`` must not trust a summary file."""

    def _run_dir(self, directory: Path, *, runner: str) -> tuple[Path, Path]:
        fixture = Fixture(Path(directory), runner=runner)
        fixture.write_provenance()
        audit = fixture.write_audit()
        (fixture.run_dir.parent / "summary.json").write_text(
            json.dumps({"runner": "external", "release_eligible": True}), encoding="utf-8"
        )
        return fixture.run_dir, audit

    def test_hand_written_summary_cannot_publish_a_scripted_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, audit = self._run_dir(Path(directory), runner="scripted")
            metrics = collect(run_dir, audit)
            self.assertFalse(metrics["release_eligible"])
            self.assertEqual(metrics["runner"], "scripted")
            self.assertTrue(
                any("does not support" in reason or "scripted" in reason for reason in metrics["eligibility_reasons"])
            )

    def test_external_run_with_matching_provenance_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, audit = self._run_dir(Path(directory), runner="external")
            metrics = collect(run_dir, audit)
            self.assertTrue(metrics["release_eligible"], metrics["eligibility_reasons"])
            self.assertTrue(metrics["provenance_present"])
            self.assertEqual(metrics["runner"], "external")

    def test_edited_log_beside_valid_provenance_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, audit = self._run_dir(Path(directory), runner="external")
            assert collect(run_dir, audit)["release_eligible"] is True

            log = run_dir / "normal-001.jsonl"
            events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
            events[-1]["payload"] = {**events[-1]["payload"], "edited_after_the_fact": True}
            write_jsonl(log, events)

            metrics = collect(run_dir, audit)
            self.assertFalse(metrics["release_eligible"])
            self.assertIn(REASON_LOG_HASH_MISMATCH, metrics["eligibility_reasons"])

    def test_missing_provenance_record_is_refused_even_for_external_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), runner="external")
            audit = fixture.write_audit()
            (fixture.run_dir.parent / "summary.json").write_text(
                json.dumps({"runner": "external", "release_eligible": True}), encoding="utf-8"
            )
            metrics = collect(fixture.run_dir, audit)
            self.assertFalse(metrics["release_eligible"])
            self.assertFalse(metrics["provenance_present"])
            self.assertIn(REASON_NO_PROVENANCE, metrics["eligibility_reasons"])

    def test_unattributed_audit_is_reported_as_not_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.write_provenance()
            (fixture.run_dir.parent / "summary.json").write_text(
                json.dumps({"runner": "external", "release_eligible": True}), encoding="utf-8"
            )
            unattributed = fixture.write_audit(reviewed_by="", reviewed_at=None)
            metrics = collect(fixture.run_dir, unattributed)
            self.assertFalse(metrics["false_completion_reviewed"])
            self.assertIsNone(metrics["false_completion_reviewed_by"])

            attributed = fixture.write_audit()
            metrics = collect(fixture.run_dir, attributed)
            self.assertTrue(metrics["false_completion_reviewed"])
            self.assertEqual(metrics["false_completion_reviewed_by"], "reviewer")


class SharedPredicateTests(unittest.TestCase):
    def test_report_and_metrics_agree_on_the_same_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), runner="scripted")
            fixture.write_provenance()
            audit = fixture.write_audit()
            (fixture.run_dir.parent / "summary.json").write_text(
                json.dumps({"runner": "external", "release_eligible": True}), encoding="utf-8"
            )
            metrics = collect(fixture.run_dir, audit)
            reasons = release_reasons(metrics)
            self.assertFalse(metrics["release_eligible"])
            self.assertTrue(reasons)
            self.assertTrue(any("发布资格被拒" in reason for reason in reasons))

    def test_a_passing_metric_set_has_no_reasons(self) -> None:
        passing = {
            "release_eligible": True,
            "provenance_present": True,
            "false_completion_count": 0,
            "collision_count": 0,
            "policy_violation_count": 0,
            "vtcr": 0.9,
            "task_duration_p95_s": 100.0,
            "task_duration_p50_s": 80.0,
            "evidence_coverage": 1.0,
            "recovery_rate": 0.8,
            "state_hash_consistency": 1.0,
            "replay_success_rate": 1.0,
            "task_family_count": 5,
            "complex_task_rate": 0.6,
            "mean_observed_entities": 2.0,
            "goal_condition_coverage": 1.0,
        }
        self.assertEqual(release_reasons(passing), [])

    def test_missing_provenance_is_named_in_the_report(self) -> None:
        metrics = {"release_eligible": False, "provenance_present": False, "eligibility_reasons": []}
        reasons = release_reasons(metrics)
        self.assertTrue(any("provenance" in reason for reason in reasons))


if __name__ == "__main__":
    unittest.main()
