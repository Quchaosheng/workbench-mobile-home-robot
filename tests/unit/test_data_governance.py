"""Issue #85: retention, holds, access and export for run evidence.

The tests use a fixed clock rather than the wall clock, so "the retention period
has expired" is a deterministic statement and never a flaky one.
"""

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "application"))

from workbench.application.data_governance import (
    DATA_CLASSES,
    GOVERNANCE_RULES_VERSION,
    PUBLIC_PROJECTION_VERSION,
    PUBLIC_RETAINED_FIELDS,
    AccessLevel,
    DataClass,
    GovernanceError,
    HoldKind,
    HoldRegister,
    LifecycleLog,
    authorize_export,
    deletion_decision,
    export_projection,
    policy_for,
    retention_summary,
)
from workbench.application.redaction import EVIDENCE_REDACTED, REDACTION_RULES_VERSION

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def run_evidence(**overrides: object) -> dict:
    record = {
        "run_id": "run-1",
        "event_id": "run-1-evt-000",
        "sequence_no": 0,
        "event_type": "observation",
        "occurred_at": "2026-09-17T12:00:00Z",
        "evidence_refs": ["frame-0000"],
        "payload": {
            "entity_id": "red_block",
            "camera_frame": "raw-frame-bytes",
            "prompt": "private operator instruction",
            "api_key": "sk-live-abcdefghijklmnopqrst",
            "confidence": 0.97,
        },
    }
    record.update(overrides)
    return record


class DataClassTests(unittest.TestCase):
    def test_every_class_declares_owner_access_and_retention(self) -> None:
        self.assertEqual(set(DATA_CLASSES), set(DataClass))
        for data_class, policy in DATA_CLASSES.items():
            with self.subTest(data_class=data_class):
                self.assertIs(policy.data_class, data_class)
                self.assertTrue(policy.owner.strip(), "every class needs an owner")
                self.assertIsInstance(policy.access_level, AccessLevel)
                self.assertIsInstance(policy.retention_days, int | None)
                if policy.retention_days is not None:
                    self.assertGreater(policy.retention_days, 0)
                self.assertTrue(policy.description.strip())

    def test_sensitive_classes_are_restricted_and_audit_classes_are_undeletable(self) -> None:
        for data_class in (DataClass.CAMERA_EVIDENCE, DataClass.MODEL_PROMPT):
            self.assertIs(policy_for(data_class).access_level, AccessLevel.RESTRICTED)
        for data_class in (
            DataClass.HARDWARE_RECORD,
            DataClass.RELEASE_EVIDENCE,
            DataClass.INCIDENT_RECORD,
        ):
            self.assertFalse(policy_for(data_class).deletable)
        # A restricted class must never be public, or "restricted" means nothing.
        self.assertFalse(any(policy.access_level is AccessLevel.PUBLIC for policy in DATA_CLASSES.values()))

    def test_unknown_data_class_is_refused(self) -> None:
        for value in ("not_a_class", "", None, 7):
            with self.subTest(value=value):
                with self.assertRaises(GovernanceError):
                    policy_for(value)  # type: ignore[arg-type]

    def test_retention_until_matches_the_declared_period(self) -> None:
        policy = policy_for(DataClass.CAMERA_EVIDENCE)
        created = NOW - timedelta(days=10)
        self.assertEqual(policy.retention_until(created), created + timedelta(days=policy.retention_days))
        self.assertIsNone(policy_for(DataClass.INCIDENT_RECORD).retention_until(created))


class RetentionTests(unittest.TestCase):
    def test_deletion_is_refused_while_retention_is_active(self) -> None:
        decision = deletion_decision(
            DataClass.RUN_EVENT_LOG,
            scope="run-1",
            created_at=NOW - timedelta(days=10),
            at=NOW,
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(any("retention period is still active" in reason for reason in decision.reasons))

    def test_deletion_is_allowed_once_retention_expires(self) -> None:
        decision = deletion_decision(
            DataClass.RUN_EVENT_LOG,
            scope="run-1",
            created_at=NOW - timedelta(days=120),
            at=NOW,
        )
        self.assertTrue(decision.allowed, decision.reasons)
        self.assertEqual(decision.reasons, ())

    def test_retention_summary_reports_owner_access_and_expiry(self) -> None:
        summary = retention_summary(created_at=NOW - timedelta(days=120), at=NOW)
        by_class = {row["data_class"]: row for row in summary}
        self.assertEqual(len(summary), len(DataClass))
        self.assertTrue(by_class[DataClass.RUN_EVENT_LOG.value]["expired"])
        # 120 days is inside the 180-day timing period but past the 30-day camera one.
        self.assertFalse(by_class[DataClass.TASK_TIMING.value]["expired"])
        self.assertTrue(by_class[DataClass.CAMERA_EVIDENCE.value]["expired"])
        self.assertEqual(by_class[DataClass.CAMERA_EVIDENCE.value]["owner"], "Perception")
        self.assertEqual(by_class[DataClass.CAMERA_EVIDENCE.value]["access_level"], "restricted")
        self.assertIsNone(by_class[DataClass.INCIDENT_RECORD.value]["retention_until"])
        self.assertFalse(by_class[DataClass.INCIDENT_RECORD.value]["deletable"])

    def test_expired_retention_does_not_make_a_retained_class_deletable(self) -> None:
        decision = deletion_decision(
            DataClass.RELEASE_EVIDENCE,
            scope="release-v0.2.0",
            created_at=NOW - timedelta(days=3650),
            at=NOW,
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(any("never deleted" in reason for reason in decision.reasons))


class HoldTests(unittest.TestCase):
    def test_active_hold_outranks_expired_retention(self) -> None:
        register = HoldRegister()
        register.open(
            "INC-1",
            kind=HoldKind.INCIDENT,
            scope="run-1",
            opened_by="security-owner",
            reason="investigating an unexpected stop",
            opened_at=NOW,
        )
        decision = deletion_decision(
            DataClass.RUN_EVENT_LOG,
            scope="run-1",
            created_at=NOW - timedelta(days=120),
            at=NOW,
            holds=register,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.covering_holds, ("INC-1",))
        self.assertTrue(any("active hold" in reason for reason in decision.reasons))

    def test_release_and_incident_holds_both_protect(self) -> None:
        register = HoldRegister()
        for hold_id, kind in (("REL-1", HoldKind.RELEASE), ("INC-1", HoldKind.INCIDENT)):
            register.open(
                hold_id,
                kind=kind,
                scope="run-1",
                opened_by="owner",
                reason="evidence retention",
                opened_at=NOW,
            )
        decision = deletion_decision(
            DataClass.TASK_TIMING,
            scope="run-1",
            created_at=NOW - timedelta(days=365),
            at=NOW,
            holds=register,
        )
        self.assertEqual(decision.covering_holds, ("INC-1", "REL-1"))
        self.assertFalse(decision.allowed)

    def test_hold_release_and_expiry_stop_protecting(self) -> None:
        register = HoldRegister()
        register.open(
            "INC-1",
            kind=HoldKind.INCIDENT,
            scope="run-1",
            opened_by="security-owner",
            reason="investigation",
            opened_at=NOW,
            expires_at=NOW + timedelta(days=7),
        )
        during = deletion_decision(
            DataClass.RUN_EVENT_LOG,
            scope="run-1",
            created_at=NOW - timedelta(days=120),
            at=NOW + timedelta(days=1),
            holds=register,
        )
        self.assertFalse(during.allowed)

        after_expiry = deletion_decision(
            DataClass.RUN_EVENT_LOG,
            scope="run-1",
            created_at=NOW - timedelta(days=120),
            at=NOW + timedelta(days=8),
            holds=register,
        )
        self.assertTrue(after_expiry.allowed, after_expiry.reasons)

        register.release("INC-1")
        self.assertEqual(register.holds(), ())

    def test_hold_scope_is_respected(self) -> None:
        register = HoldRegister()
        register.open(
            "INC-1",
            kind=HoldKind.INCIDENT,
            scope="run-1",
            opened_by="security-owner",
            reason="investigation",
            opened_at=NOW,
        )
        other = deletion_decision(
            DataClass.RUN_EVENT_LOG,
            scope="run-2",
            created_at=NOW - timedelta(days=120),
            at=NOW,
            holds=register,
        )
        self.assertTrue(other.allowed, other.reasons)
        self.assertEqual(other.covering_holds, ())

    def test_wildcard_hold_covers_every_scope(self) -> None:
        register = HoldRegister()
        register.open(
            "FREEZE",
            kind=HoldKind.RELEASE,
            scope="*",
            opened_by="product-owner",
            reason="release freeze",
            opened_at=NOW,
        )
        decision = deletion_decision(
            DataClass.CAMERA_EVIDENCE,
            scope="any-run",
            created_at=NOW - timedelta(days=365),
            at=NOW,
            holds=register,
        )
        self.assertFalse(decision.allowed)

    def test_duplicate_or_malformed_holds_are_refused(self) -> None:
        register = HoldRegister()
        register.open(
            "INC-1",
            kind=HoldKind.INCIDENT,
            scope="run-1",
            opened_by="security-owner",
            reason="investigation",
            opened_at=NOW,
        )
        with self.assertRaises(GovernanceError):
            register.open(
                "INC-1",
                kind=HoldKind.INCIDENT,
                scope="run-2",
                opened_by="security-owner",
                reason="duplicate",
                opened_at=NOW,
            )
        for field, value in (("scope", ""), ("opened_by", "  "), ("reason", "")):
            with self.subTest(field=field):
                with self.assertRaises(GovernanceError):
                    register.open(
                        "INC-2",
                        kind=HoldKind.INCIDENT,
                        scope=value if field == "scope" else "run-1",
                        opened_by=value if field == "opened_by" else "owner",
                        reason=value if field == "reason" else "reason",
                        opened_at=NOW,
                    )
        with self.assertRaises(GovernanceError):
            register.open(
                "INC-3",
                kind=HoldKind.INCIDENT,
                scope="run-1",
                opened_by="owner",
                reason="bad expiry",
                opened_at=NOW,
                expires_at=NOW - timedelta(seconds=1),
            )
        with self.assertRaises(GovernanceError):
            register.release("NOPE")


class ExportAccessTests(unittest.TestCase):
    def test_public_export_of_sensitive_evidence_is_refused(self) -> None:
        for data_class in (
            DataClass.CAMERA_EVIDENCE,
            DataClass.MODEL_PROMPT,
            DataClass.HARDWARE_RECORD,
            DataClass.INCIDENT_RECORD,
        ):
            with self.subTest(data_class=data_class):
                with self.assertRaises(GovernanceError):
                    authorize_export(data_class, requester_access=AccessLevel.PUBLIC)

    def test_operator_export_cannot_reach_restricted_classes(self) -> None:
        authorize_export(DataClass.RUN_EVENT_LOG, requester_access=AccessLevel.OPERATOR)
        with self.assertRaises(GovernanceError):
            authorize_export(DataClass.CAMERA_EVIDENCE, requester_access=AccessLevel.OPERATOR)
        authorize_export(DataClass.CAMERA_EVIDENCE, requester_access=AccessLevel.RESTRICTED)

    def test_export_refusal_names_both_access_levels(self) -> None:
        with self.assertRaises(GovernanceError) as caught:
            authorize_export(DataClass.MODEL_PROMPT, requester_access=AccessLevel.PUBLIC)
        message = str(caught.exception)
        self.assertIn("model_prompt", message)
        self.assertIn("restricted", message)
        self.assertIn("public", message)


class PublicProjectionTests(unittest.TestCase):
    def test_public_projection_hides_payload_and_keeps_references_and_hashes(self) -> None:
        record = run_evidence()
        projection = export_projection(record, requester_access=AccessLevel.PUBLIC)
        rendered = str(projection)

        self.assertEqual(projection["projection_version"], PUBLIC_PROJECTION_VERSION)
        self.assertEqual(projection["governance_rules"], GOVERNANCE_RULES_VERSION)
        self.assertEqual(projection["redaction_rules"], REDACTION_RULES_VERSION)
        self.assertTrue(projection["requires_authorization"])
        self.assertEqual(projection["required_access"], "restricted")
        self.assertEqual(len(projection["content_sha256"]), 64)

        # Correlation and references survive; restricted payload does not.
        self.assertEqual(projection["run"]["run_id"], "run-1")
        self.assertEqual(projection["run"]["sequence_no"], 0)
        self.assertEqual(projection["run"]["evidence_refs"], ["frame-0000"])
        self.assertEqual(projection["run"]["payload"], EVIDENCE_REDACTED)
        for secret in ("raw-frame-bytes", "private operator instruction", "sk-live-abcdefghijklmnopqrst"):
            self.assertNotIn(secret, rendered)
        self.assertIn("payload", projection["withheld_fields"])

    def test_withheld_fields_are_named_so_omission_is_not_silent(self) -> None:
        record = run_evidence()
        projection = export_projection(record, requester_access=AccessLevel.PUBLIC)
        for field in set(record) - PUBLIC_RETAINED_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, projection["withheld_fields"])

    def test_projection_never_mutates_the_source_record(self) -> None:
        record = run_evidence()
        original = repr(record)
        export_projection(record, requester_access=AccessLevel.PUBLIC)
        export_projection(record, requester_access=AccessLevel.RESTRICTED)
        self.assertEqual(repr(record), original)

    def test_restricted_projection_is_redacted_but_complete(self) -> None:
        projection = export_projection(run_evidence(), requester_access=AccessLevel.RESTRICTED)
        self.assertFalse(projection["requires_authorization"])
        self.assertEqual(projection["withheld_fields"], [])
        # Restricted access still scrubs credentials: authorization is not a
        # reason to copy a secret into an artifact.
        self.assertEqual(projection["run"]["payload"]["api_key"], "<redacted>")
        self.assertEqual(projection["run"]["payload"]["camera_frame"], EVIDENCE_REDACTED)
        self.assertEqual(projection["run"]["payload"]["confidence"], 0.97)

    def test_projection_of_the_same_run_is_deterministic(self) -> None:
        record = run_evidence()
        first = export_projection(record, requester_access=AccessLevel.PUBLIC)
        second = export_projection(record, requester_access=AccessLevel.PUBLIC)
        self.assertEqual(first, second)


class LifecycleLogTests(unittest.TestCase):
    def test_authorized_and_refused_actions_are_both_recorded(self) -> None:
        log = LifecycleLog()
        log.record(
            "delete",
            DataClass.RUN_EVENT_LOG,
            scope="run-1",
            actor="operator",
            at=NOW,
            authorized=False,
            detail="active hold INC-1",
        )
        log.record(
            "restore",
            DataClass.RUN_EVENT_LOG,
            scope="run-1",
            actor="operator",
            at=NOW,
            authorized=True,
            detail="restored from verified snapshot",
        )
        events = log.as_dicts()
        self.assertEqual([event["authorized"] for event in events], [False, True])
        self.assertEqual(events[0]["data_class"], "run_event_log")
        self.assertEqual(events[0]["governance_rules"], GOVERNANCE_RULES_VERSION)
        self.assertTrue(events[0]["at"].startswith("2026-09-17"))

    def test_an_unnamed_actor_or_scope_is_refused(self) -> None:
        log = LifecycleLog()
        for actor, scope in (("", "run-1"), ("  ", "run-1"), ("operator", ""), ("operator", " ")):
            with self.subTest(actor=actor, scope=scope):
                with self.assertRaises(GovernanceError):
                    log.record(
                        "delete",
                        DataClass.RUN_EVENT_LOG,
                        scope=scope,
                        actor=actor,
                        at=NOW,
                        authorized=True,
                        detail="detail",
                    )
        self.assertEqual(log.events(), ())

    def test_unknown_data_class_in_a_lifecycle_action_is_refused(self) -> None:
        with self.assertRaises(GovernanceError):
            LifecycleLog().record(
                "delete",
                "not_a_class",
                scope="run-1",
                actor="operator",
                at=NOW,
                authorized=True,
                detail="detail",
            )


class DocumentationTests(unittest.TestCase):
    def test_policy_page_documents_each_class_and_rule(self) -> None:
        page = " ".join((ROOT / "docs" / "deployment" / "data-governance.md").read_text(encoding="utf-8").split())
        for data_class in DataClass:
            with self.subTest(data_class=data_class):
                self.assertIn(data_class.value, page)
        for expected in (
            GOVERNANCE_RULES_VERSION,
            "incident",
            "release",
            "hold",
            "AccessLevel",
            "retention",
            "export",
        ):
            self.assertIn(expected.casefold(), page.casefold())

    def test_policy_page_states_that_holds_outrank_expiry(self) -> None:
        page = " ".join((ROOT / "docs" / "deployment" / "data-governance.md").read_text(encoding="utf-8").split())
        self.assertIn("outranks", page)
        self.assertIn("never deleted by an automated lifetime rule", page)

    def test_policy_page_links_to_the_redaction_and_recovery_boundaries(self) -> None:
        page = (ROOT / "docs" / "deployment" / "data-governance.md").read_text(encoding="utf-8")
        self.assertIn("../security/hardening.md", page)
        self.assertIn("event-evidence-recovery.md", page)


if __name__ == "__main__":
    unittest.main()
