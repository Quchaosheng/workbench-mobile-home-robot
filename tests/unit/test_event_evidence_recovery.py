"""Issue #90: backup, restore and corruption recovery must fail closed.

Every test drives the real store against a temporary directory. The assertions
are about what survives: a refused operation must leave the live store readable
at its previous state, and a recovery must never invent an event.
"""

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "kernel"))

from workbench.kernel.event_store import (
    SCHEMA_VERSION,
    SNAPSHOT_FORMAT,
    SNAPSHOT_FORMAT_VERSION,
    EventStore,
    EventStoreError,
    audit_evidence_refs,
    migrate_jsonl,
    recover_torn_jsonl,
)


def event(index: int, *, run_id: str = "run-1", refs: list[str] | None = None) -> dict:
    return {
        "event_id": f"event-{index}",
        "run_id": run_id,
        "sequence_no": index,
        "event_type": "observation",
        "occurred_at": "2026-08-13T00:00:00Z",
        "payload": {"index": index},
        "evidence_refs": refs if refs is not None else [],
    }


def populated(database: Path, count: int = 3) -> EventStore:
    store = EventStore(database, backend="sqlite")
    for index in range(count):
        store.append(event(index, refs=[f"frame-{index}"]))
    return store


class TestSnapshotMetadata:
    def test_manifest_records_format_schema_count_and_hash(self, tmp_path: Path) -> None:
        store = populated(tmp_path / "events.sqlite3")
        manifest_path = store.backup(tmp_path / "snapshot.sqlite3")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        store.close()

        assert manifest["format"] == SNAPSHOT_FORMAT
        assert manifest["format_version"] == SNAPSHOT_FORMAT_VERSION
        assert manifest["database"] == "sqlite"
        assert manifest["schema_version"] == SCHEMA_VERSION
        assert manifest["event_count"] == 3
        assert manifest["sqlite_version"]
        assert manifest["created_at"].endswith("+00:00")
        assert len(manifest["sha256"]) == 64
        assert manifest_path.exists()

    def test_backup_refuses_to_overwrite_its_own_source(self, tmp_path: Path) -> None:
        database = tmp_path / "events.sqlite3"
        store = populated(database)
        with pytest.raises(EventStoreError, match="must differ"):
            store.backup(database)
        store.close()
        assert EventStore(database, backend="sqlite").verify_integrity()

    def test_jsonl_snapshot_is_a_byte_copy_with_a_manifest(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        source = EventStore(log, backend="jsonl")
        source.append(event(0))
        source.append(event(1))
        manifest_path = source.backup(tmp_path / "events.snapshot.jsonl")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source.close()

        assert manifest["database"] == "jsonl"
        assert manifest["event_count"] == 2
        # A byte copy, not a re-serialization, so unknown fields survive intact.
        assert (tmp_path / "events.snapshot.jsonl").read_bytes() == log.read_bytes()


class TestRestore:
    def test_restore_is_replay_equivalent_to_its_source(self, tmp_path: Path) -> None:
        store = populated(tmp_path / "events.sqlite3")
        expected = store.replay()
        snapshot = tmp_path / "snapshot.sqlite3"
        store.backup(snapshot)
        store.close()

        restored = EventStore.restore(snapshot, tmp_path / "restored.sqlite3")
        assert restored.replay() == expected
        assert restored.verify_integrity()
        restored.close()

    def test_tampered_snapshot_is_refused_and_live_data_survives(self, tmp_path: Path) -> None:
        live = tmp_path / "events.sqlite3"
        store = populated(live)
        snapshot = tmp_path / "snapshot.sqlite3"
        store.backup(snapshot)
        expected = store.replay()
        store.close()

        snapshot.write_bytes(snapshot.read_bytes() + b"tamper")
        with pytest.raises(EventStoreError, match="checksum"):
            EventStore.restore(snapshot, live)

        survived = EventStore(live, backend="sqlite")
        assert survived.replay() == expected
        assert survived.verify_integrity()
        survived.close()

    def test_manifest_event_count_mismatch_is_refused(self, tmp_path: Path) -> None:
        store = populated(tmp_path / "events.sqlite3")
        snapshot = tmp_path / "snapshot.sqlite3"
        manifest_path = store.backup(snapshot)
        store.close()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["event_count"] = 99
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        with pytest.raises(EventStoreError, match="event count"):
            EventStore.restore(snapshot, tmp_path / "out.sqlite3")

    def test_missing_or_malformed_manifest_is_refused(self, tmp_path: Path) -> None:
        store = populated(tmp_path / "events.sqlite3")
        snapshot = tmp_path / "snapshot.sqlite3"
        manifest_path = store.backup(snapshot)
        store.close()

        manifest_path.unlink()
        with pytest.raises(EventStoreError, match="manifest is unavailable or malformed"):
            EventStore.restore(snapshot, tmp_path / "out.sqlite3")

        manifest_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(EventStoreError, match="manifest is unavailable or malformed"):
            EventStore.restore(snapshot, tmp_path / "out2.sqlite3")

    def test_unknown_snapshot_format_is_refused(self, tmp_path: Path) -> None:
        store = populated(tmp_path / "events.sqlite3")
        snapshot = tmp_path / "snapshot.sqlite3"
        manifest_path = store.backup(snapshot)
        store.close()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["format"] = "something-else"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        with pytest.raises(EventStoreError, match="format or schema version mismatch"):
            EventStore.restore(snapshot, tmp_path / "out.sqlite3")

    def test_jsonl_restore_round_trips(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        source = EventStore(log, backend="jsonl")
        source.append(event(0, refs=["frame-0"]))
        source.append(event(1, refs=["frame-1"]))
        snapshot = tmp_path / "snapshot.jsonl"
        source.backup(snapshot)
        source.close()

        restored = EventStore.restore(snapshot, tmp_path / "restored.jsonl")
        assert [item["event_id"] for item in restored.replay()] == ["event-0", "event-1"]
        assert restored.verify_integrity()
        restored.close()


class TestInterruptedAppend:
    def test_torn_final_line_is_recovered_without_inventing_events(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        store = EventStore(log, backend="jsonl")
        store.append(event(0, refs=["frame-0"]))
        store.append(event(1, refs=["frame-1"]))
        original = log.read_bytes()
        with log.open("a", encoding="utf-8") as handle:
            handle.write('{"event_id": "event-2", "run_id": "run-1", "sequence_index": 2')

        destination = tmp_path / "recovered.jsonl"
        report = recover_torn_jsonl(log, destination)

        assert report["format"] == "workbench-event-store-recovery"
        assert report["recovered_events"] == 2
        assert report["discarded_bytes"] > 0
        assert len(report["discarded_sha256"]) == 64
        recovered = EventStore(destination, backend="jsonl")
        events = recovered.replay()
        assert [item["event_id"] for item in recovered.replay()] == ["event-0", "event-1"]
        assert all(item["event_id"] != "event-2" for item in events)
        recovered.close()

        # The damaged original is left exactly as found for the investigation.
        assert log.read_bytes() == original + b'{"event_id": "event-2", "run_id": "run-1", "sequence_index": 2'
        discarded = Path(report["discarded_path"])
        assert discarded.read_text(encoding="utf-8").startswith('{"event_id": "event-2"')

    def test_damage_before_the_final_line_is_refused(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(
            json.dumps(event(0)) + "\n" + "{not json}\n" + '{"event_id": "event-2"',
            encoding="utf-8",
        )
        destination = tmp_path / "recovered.jsonl"
        with pytest.raises(EventStoreError, match="damaged before the final line"):
            recover_torn_jsonl(log, destination)
        assert not destination.exists()
        assert not list(tmp_path.glob("*.recovery-tmp"))

    def test_clean_or_missing_log_is_not_recovered(self, tmp_path: Path) -> None:
        clean = tmp_path / "clean.jsonl"
        store = EventStore(clean, backend="jsonl")
        store.append(event(0))
        store.close()

        with pytest.raises(EventStoreError, match="recovery is not required"):
            recover_torn_jsonl(clean, tmp_path / "out.jsonl")
        with pytest.raises(EventStoreError, match="is unavailable"):
            recover_torn_jsonl(tmp_path / "absent.jsonl", tmp_path / "out2.jsonl")

    def test_non_utf8_log_is_refused(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_bytes(json.dumps(event(0)).encode() + b"\n\xff\xfe")
        with pytest.raises(EventStoreError, match="not valid UTF-8"):
            recover_torn_jsonl(log, tmp_path / "out.jsonl")

    def test_recovery_refuses_a_candidate_that_breaks_the_event_contract(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(
            json.dumps({**event(0), "event_type": "not_a_real_event"}) + "\n" + '{"event_id": "event-1"',
            encoding="utf-8",
        )
        destination = tmp_path / "recovered.jsonl"
        with pytest.raises(EventStoreError, match="would not satisfy the event contract"):
            recover_torn_jsonl(log, destination)
        assert not destination.exists()
        assert not list(tmp_path.glob("*.recovery-tmp"))

    def test_recovery_refuses_a_sequence_gap(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(
            json.dumps(event(0)) + "\n" + json.dumps(event(5)) + "\n" + '{"event_id": "event-6"',
            encoding="utf-8",
        )
        with pytest.raises(EventStoreError, match="would not satisfy the event contract"):
            recover_torn_jsonl(log, tmp_path / "recovered.jsonl")

    def test_recovery_refuses_its_own_source(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        store = EventStore(log, backend="jsonl")
        store.append(event(0))
        with log.open("a", encoding="utf-8") as handle:
            handle.write("{torn")
        with pytest.raises(EventStoreError, match="must differ"):
            recover_torn_jsonl(log, log)

    def test_unwritable_destination_is_an_operational_error(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(json.dumps(event(0)) + "\n" + '{"event_id": "event-1"', encoding="utf-8")
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        try:
            with pytest.raises(EventStoreError, match="could not be written"):
                recover_torn_jsonl(log, readonly / "out.jsonl")
        finally:
            readonly.chmod(0o700)

    def test_append_after_damage_fails_closed_and_changes_nothing(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        store = EventStore(log, backend="jsonl")
        store.append(event(0))
        store.close()
        with log.open("a", encoding="utf-8") as handle:
            handle.write('{"event_id": "event-1"')
        damaged = log.read_bytes()

        reopened = EventStore(log, backend="jsonl")
        assert not reopened.verify_integrity()
        with pytest.raises(EventStoreError):
            reopened.append(event(1))
        # A refused append must not append to a file it cannot read.
        assert log.read_bytes() == damaged
        reopened.close()

    def test_sqlite_store_survives_jsonl_recovery_work(self, tmp_path: Path) -> None:
        """Recovery is a JSONL-only path; a SQLite store must be untouched."""
        database = tmp_path / "events.sqlite3"
        store = populated(database, count=2)
        expected = store.replay()
        store.close()

        log = tmp_path / "events.jsonl"
        damaged = EventStore(log, backend="jsonl")
        damaged.append(event(0))
        with log.open("a", encoding="utf-8") as handle:
            handle.write('{"event_id": "event-1"')
        recover_torn_jsonl(log, tmp_path / "recovered.jsonl")

        reopened = EventStore(database, backend="sqlite")
        assert reopened.replay() == expected
        assert reopened.verify_integrity()
        reopened.close()


class TestEvidenceReferences:
    def test_missing_reference_is_reported_and_never_invented(self, tmp_path: Path) -> None:
        store = populated(tmp_path / "events.sqlite3", count=2)
        audit = audit_evidence_refs(store, ["frame-0"])
        store.close()

        assert audit["referenced_count"] == 2
        assert audit["missing_count"] == 1
        assert audit["missing_refs"] == ["frame-1"]
        assert audit["complete"] is False

    def test_complete_references_report_as_complete(self, tmp_path: Path) -> None:
        store = populated(tmp_path / "events.sqlite3", count=2)
        audit = audit_evidence_refs(store, ["frame-0", "frame-1"])
        store.close()

        assert audit["missing_refs"] == []
        assert audit["complete"] is True
        assert audit["unique_referenced_count"] == 2

    def test_audit_does_not_modify_the_store(self, tmp_path: Path) -> None:
        database = tmp_path / "events.sqlite3"
        store = populated(database, count=2)
        before = store.replay()
        audit_evidence_refs(store, [])
        assert store.replay() == before
        store.close()


class TestRunbookTargets:
    @staticmethod
    def _runbook() -> str:
        """Return the runbook with prose wrapped into single spaces.

        Assertions are about the documented commitments, not about where a line
        break happens to fall, so the text is normalized first.
        """
        path = ROOT / "docs" / "deployment" / "event-evidence-recovery.md"
        return " ".join(path.read_text(encoding="utf-8").split())

    def test_runbook_documents_format_version_targets_and_procedures(self) -> None:
        runbook = self._runbook()
        for expected in (
            SNAPSHOT_FORMAT,
            str(SNAPSHOT_FORMAT_VERSION),
            str(SCHEMA_VERSION),
            "RPO",
            "RTO",
            "sha256",
            "recover_torn_jsonl",
            "EventStore.restore",
            "audit_evidence_refs",
            "Minimum 30 days for release evidence",
        ):
            assert expected.casefold() in runbook.casefold(), f"runbook does not document {expected}"

    def test_runbook_states_that_recovery_never_invents_evidence(self) -> None:
        runbook = self._runbook()
        assert "never synthesizes an event" in runbook
        assert "never marks an unverified task complete" in runbook
        assert "never replaced by a placeholder" in runbook

    def test_runbook_links_to_the_raw_evidence_policy(self) -> None:
        assert "../security/hardening.md" in self._runbook()

    def test_migration_then_backup_then_restore_round_trips(self, tmp_path: Path) -> None:
        """The documented JSONL-to-SQLite move keeps the same replay."""
        legacy = tmp_path / "events.jsonl"
        source = EventStore(legacy, backend="jsonl")
        source.append(event(0, refs=["frame-0"]))
        source.append(event(1, refs=["frame-1"]))
        expected = source.replay()
        source.close()

        migrated = migrate_jsonl(legacy, tmp_path / "events.sqlite3")
        assert migrated.replay() == expected
        snapshot = tmp_path / "snapshot.sqlite3"
        migrated.backup(snapshot)
        migrated.close()

        restored = EventStore.restore(snapshot, tmp_path / "restored.sqlite3")
        assert restored.replay() == expected
        restored.close()


class TestServiceTargets:
    def test_sqlite_snapshot_keeps_every_committed_event(self, tmp_path: Path) -> None:
        """RPO 0 for SQLite: a snapshot taken after a commit loses no event."""
        store = populated(tmp_path / "events.sqlite3", count=50)
        snapshot = tmp_path / "snapshot.sqlite3"
        manifest_path = store.backup(snapshot)
        expected = store.replay()
        store.close()
        assert json.loads(manifest_path.read_text(encoding="utf-8"))["event_count"] == len(expected)

        restored = EventStore.restore(snapshot, tmp_path / "restored.sqlite3")
        assert restored.replay() == expected
        restored.close()

    def test_restore_completes_inside_the_documented_rto(self, tmp_path: Path) -> None:
        """RTO under 5 minutes: restore is a verified copy, not a replay."""
        store = populated(tmp_path / "events.sqlite3", count=200)
        snapshot = tmp_path / "snapshot.sqlite3"
        store.backup(snapshot)
        store.close()

        started = time.monotonic()
        restored = EventStore.restore(snapshot, tmp_path / "restored.sqlite3")
        elapsed = time.monotonic() - started
        restored.close()

        assert elapsed < 300, f"restore took {elapsed:.3f}s, above the documented RTO"
