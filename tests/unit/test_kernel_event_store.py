import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "kernel"))

from workbench.kernel.event_store import EventStore, EventStoreError, migrate_jsonl


def event(event_id: int, *, run_id: str = "run-1", sequence_no: int | None = None) -> dict:
    return {
        "event_id": f"event-{event_id}",
        "run_id": run_id,
        "sequence_no": event_id if sequence_no is None else sequence_no,
        "event_type": "observation",
        "occurred_at": "2026-08-13T00:00:00Z",
        "payload": {"index": event_id},
        "evidence_refs": [],
    }


def test_checkpoint_counts_persisted_events_after_restart(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    first = EventStore(log)
    for event_id in range(3):
        first.append(event(event_id))

    reopened = EventStore(log)
    reopened.append(event(3))
    checkpoint = reopened.create_checkpoint()

    assert checkpoint == 4
    assert reopened.replay(from_checkpoint=3) == [event(3)]
    assert reopened.replay(from_checkpoint=checkpoint) == []


def test_replay_rejects_invalid_checkpoints(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.jsonl")
    store.append(event(0))

    for checkpoint in (-1, True, 2):
        with pytest.raises(ValueError, match="checkpoint must be between"):
            store.replay(from_checkpoint=checkpoint)


def test_corrupt_or_non_object_events_fail_integrity_check(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    store = EventStore(log)

    log.write_text(f"{json.dumps(event(0))}\n{{bad-json}}\n", encoding="utf-8")
    assert not store.verify_integrity()
    with pytest.raises(EventStoreError, match="invalid event JSON"):
        store.create_checkpoint()

    log.write_text(f'{json.dumps(event(0))}\n["not-an-object"]\n', encoding="utf-8")
    assert not store.verify_integrity()


def test_append_rejects_non_object_before_writing(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    store = EventStore(log)

    with pytest.raises(TypeError, match="event must be an object"):
        store.append(["not-an-object"])
    assert not log.exists()


@pytest.mark.parametrize(
    "bad_event, message",
    [
        ({"garbage": True}, "missing fields"),
        (event(0, sequence_no=1), "contiguous"),
        ({**event(0), "event_type": "unknown"}, "unknown event_type"),
        ({**event(0), "payload": []}, "payload must be an object"),
        ({**event(0), "evidence_refs": [1]}, "string list"),
    ],
)
def test_append_rejects_invalid_event_contract(tmp_path: Path, bad_event: dict, message: str) -> None:
    log = tmp_path / "events.jsonl"
    store = EventStore(log)
    with pytest.raises(EventStoreError, match=message):
        store.append(bad_event)
    assert not log.exists()


def test_append_rejects_duplicate_identity_and_mixed_run_before_writing(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    store = EventStore(log)
    store.append(event(0))
    before = log.read_bytes()

    with pytest.raises(EventStoreError, match="duplicate event_id"):
        store.append({**event(1), "event_id": "event-0"})
    assert log.read_bytes() == before

    with pytest.raises(EventStoreError, match="mixes run_id"):
        store.append(event(1, run_id="run-2"))
    assert log.read_bytes() == before


def test_append_reloads_jsonl_after_external_change(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    store = EventStore(log)
    store.append(event(0))
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event(1), separators=(",", ":")) + "\n")

    store.append(event(2))

    assert store.replay() == [event(0), event(1), event(2)]


def test_integrity_checks_contract_and_strict_json(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    store = EventStore(log)
    log.write_text('{"garbage":true}\n', encoding="utf-8")
    assert not store.verify_integrity()
    with pytest.raises(EventStoreError, match="missing fields"):
        store.replay()

    with pytest.raises(EventStoreError, match="strict JSON"):
        EventStore(tmp_path / "nan.jsonl").append({**event(0), "payload": {"confidence": float("nan")}})


def test_legacy_object_mode_is_explicit(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "legacy.jsonl", legacy_objects=True)
    store.append({"id": 0})
    assert store.verify_integrity()
    assert store.replay() == [{"id": 0}]


def test_sqlite_restart_checkpoint_and_unknown_fields(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    first = EventStore(database)
    first.append({**event(0), "extension": {"source": "bench"}})
    first.append(event(1))
    checkpoint = first.create_checkpoint()
    first.close()

    reopened = EventStore(database)
    assert reopened.checkpoints == [2]
    assert reopened.replay(from_checkpoint=checkpoint) == []
    assert reopened.replay()[0]["extension"] == {"source": "bench"}
    assert reopened.verify_integrity()
    reopened.close()


def test_jsonl_migration_and_verified_restore(tmp_path: Path) -> None:
    source = tmp_path / "events.jsonl"
    legacy = EventStore(source)
    legacy.append(event(0))
    legacy.append(event(1))
    database = tmp_path / "events.sqlite3"
    migrated = migrate_jsonl(source, database)
    assert migrated.replay() == [event(0), event(1)]
    snapshot = tmp_path / "events.snapshot.sqlite3"
    manifest = migrated.backup(snapshot)
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    migrated.close()
    restored = EventStore.restore(snapshot, tmp_path / "restored.sqlite3")
    assert manifest.exists()
    assert metadata["database"] == "sqlite"
    assert metadata["sqlite_version"]
    assert metadata["created_at"].endswith("+00:00")
    assert restored.replay() == [event(0), event(1)]
    restored.close()

    snapshot.write_bytes(snapshot.read_bytes() + b"tamper")
    with pytest.raises(EventStoreError, match="checksum"):
        EventStore.restore(snapshot, tmp_path / "rejected.sqlite3")


def test_sqlite_batch_is_atomic_on_contract_failure(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    with pytest.raises(EventStoreError, match="contiguous"):
        store.append_many([event(0), event(2)])
    assert store.replay() == []
    store.close()


def test_multi_run_database_holds_two_runs_each_starting_at_zero(tmp_path: Path) -> None:
    """Issue #161: one database must hold several runs, each numbered per run.

    This is the acceptance the schema bump exists for. Under the version 1 layout
    ``sequence_no`` was globally unique, so a second run could not be written at
    all; the assertion is therefore about two runs coexisting with identical
    sequence numbers rather than about a count.
    """

    database = tmp_path / "events.sqlite3"
    store = EventStore(database, backend="sqlite", multi_run=True)
    store.append(event(0, run_id="run-a", sequence_no=0))
    store.append(event(1, run_id="run-a", sequence_no=1))
    store.append(event(2, run_id="run-b", sequence_no=0))
    assert store.run_ids() == ["run-a", "run-b"]
    assert [item["sequence_no"] for item in store.list_run("run-a")] == [0, 1]
    assert [item["sequence_no"] for item in store.list_run("run-b")] == [0]
    store.close()

    reopened = EventStore(database, backend="sqlite", multi_run=True)
    assert [item["sequence_no"] for item in reopened.list_run("run-a")] == [0, 1]
    assert [item["sequence_no"] for item in reopened.list_run("run-b")] == [0]
    assert reopened.multi_run
    reopened.close()


def test_multi_run_allocation_is_per_run_and_survives_reopen(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    store = EventStore(database, backend="sqlite", multi_run=True)
    first = store.append_allocated("run-a", lambda sequence_no: event(0, run_id="run-a", sequence_no=sequence_no))
    second = store.append_allocated("run-b", lambda sequence_no: event(1, run_id="run-b", sequence_no=sequence_no))
    third = store.append_allocated("run-a", lambda sequence_no: event(2, run_id="run-a", sequence_no=sequence_no))

    assert [first["sequence_no"], second["sequence_no"], third["sequence_no"]] == [0, 0, 1]
    store.close()

    reopened = EventStore(database, backend="sqlite", multi_run=True)
    subsequent = reopened.append_allocated(
        "run-a", lambda sequence_no: event(3, run_id="run-a", sequence_no=sequence_no)
    )
    assert subsequent["sequence_no"] == 2
    reopened.close()


def test_per_run_checkpoint_survives_close_and_reopen(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    store = EventStore(database, backend="sqlite", multi_run=True)
    store.append(event(0, run_id="run-a", sequence_no=0))
    store.append(event(1, run_id="run-a", sequence_no=1))
    store.append(event(2, run_id="run-b", sequence_no=0))
    checkpoint = store.create_run_checkpoint("run-a")

    assert checkpoint == 2
    assert store.replay_run("run-a", from_checkpoint=checkpoint) == []
    assert store.run_checkpoints("run-a") == [2]
    store.close()

    reopened = EventStore(database, backend="sqlite", multi_run=True)
    assert reopened.run_checkpoints("run-a") == [2]
    # A checkpoint belongs to the run that recorded it, so the other run's
    # position is unaffected and remains unrecorded.
    assert reopened.run_checkpoints("run-b") == []
    assert [item["event_id"] for item in reopened.replay_run("run-a", from_checkpoint=1)] == ["event-1"]
    assert [item["event_id"] for item in reopened.replay_run("run-b")] == ["event-2"]
    reopened.close()


def test_multi_run_rejects_duplicate_per_run_sequence_before_writing(tmp_path: Path) -> None:
    """The declared contract and the enforced contract must be the same.

    ``(run_id, sequence_no)`` uniqueness is enforced by a SQLite index on the
    database backend and by validation here, so a batch that reuses one run's
    sequence number is refused identically on JSONL, which has no index at all.
    """

    for suffix in ("events.jsonl", "events.sqlite3"):
        store = EventStore(tmp_path / suffix, multi_run=True)
        try:
            with pytest.raises(EventStoreError, match=r"run_id.*sequence_no"):
                store.append_many([event(0, run_id="run-a", sequence_no=0), event(1, run_id="run-a", sequence_no=0)])
            assert store.replay() == []
        finally:
            store.close()


def test_append_after_close_is_a_typed_refusal(tmp_path: Path) -> None:
    """A closed store must not turn into an ``AttributeError`` on ``None``."""

    database = tmp_path / "events.sqlite3"
    store = EventStore(database, backend="sqlite", multi_run=True)
    store.close()

    for operation in (
        lambda: store.append(event(0)),
        lambda: store.append_allocated("run-a", lambda sequence_no: event(0, sequence_no=sequence_no)),
        lambda: store.list_run("run-a"),
        lambda: store.create_run_checkpoint("run-a"),
        lambda: store.run_checkpoints("run-a"),
        lambda: store.run_ids(),
    ):
        with pytest.raises(EventStoreError, match="no open SQLite connection"):
            operation()


def test_closed_store_refusal_does_not_rely_on_assertions(tmp_path: Path) -> None:
    """The refusal above must survive ``python -O``, which strips ``assert``.

    The check on the connection used to be an ``assert``, so under ``-O`` the
    guarantee silently disappeared and the store raised ``AttributeError:
    'NoneType' object has no attribute 'execute'`` instead. Running the same
    scenario in a stripped-assertion interpreter is what proves the refusal is a
    real branch rather than an artifact of assertions being enabled.
    """

    probe = tmp_path / "probe.py"
    probe.write_text(
        f"""
import sys
sys.path.insert(0, {str(ROOT / "libs" / "kernel")!r})
from workbench.kernel.event_store import EventStore, EventStoreError

store = EventStore({str(tmp_path / "probe.sqlite3")!r}, backend="sqlite", multi_run=True)
store.close()
try:
    store.list_run("run-a")
except EventStoreError as error:
    print("refused:", error)
else:
    print("no refusal")
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-O", str(probe)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("refused: no open SQLite connection"), result.stdout + result.stderr


def test_refuses_a_globally_unique_sequence_layout(tmp_path: Path) -> None:
    """The pre-#161 layout is refused by name, not read as an empty database."""

    import sqlite3

    database = tmp_path / "legacy-layout.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL UNIQUE,
            event_json TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(EventStoreError, match=r"globally unique.*backup.*rebuild"):
        EventStore(database, backend="sqlite", multi_run=True)


def test_per_run_checkpoints_work_on_the_jsonl_backend_too(tmp_path: Path) -> None:
    """A public method must not crash on one backend and work on the other.

    Per-run checkpoints were initialized only in the sqlite branch, so the jsonl
    backend answered a public call with a bare ``AttributeError``. The backend is
    a storage detail; the same call has to say something meaningful on both.
    """

    store = EventStore(tmp_path / "events.jsonl", backend="jsonl", multi_run=True)
    store.append(event(0, run_id="run-a", sequence_no=0))
    store.append(event(1, run_id="run-a", sequence_no=1))

    assert store.create_run_checkpoint("run-a") == 2
    assert store.run_checkpoints("run-a") == [2]
    # A run that never recorded a checkpoint reports none rather than raising.
    assert store.run_checkpoints("run-b") == []
    assert store.replay_run("run-a", from_checkpoint=2) == []
    store.close()


def test_checkpoints_table_without_run_id_is_refused(tmp_path: Path) -> None:
    """A pre-#161 checkpoints table cannot record which run it belongs to.

    The failure must be this store's typed error with the recovery instruction,
    not a raw ``sqlite3.OperationalError`` about a missing column escaping from
    the constructor while the first query runs.
    """

    import sqlite3

    database = tmp_path / "legacy-checkpoints.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE event_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO event_store_meta VALUES ('schema_version', '2')")
    connection.execute(
        """
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            UNIQUE(run_id, sequence_no)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE checkpoints (
            checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_count INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(EventStoreError, match=r"run_id column.*backup.*rebuild"):
        EventStore(database, backend="sqlite", multi_run=True)
