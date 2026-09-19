"""Append-only event storage with a strict JSONL compatibility path and SQLite backend.

Two durability rules decide the shape of this module:

* Damage fails closed.  A store that cannot prove its own contents readable
  refuses to append, to checkpoint or to restore, rather than continuing from a
  guess.
* Recovery never invents evidence.  A recovered store is a new file built from
  the bytes that were still complete; the damaged original is left untouched and
  the discarded fragment is recorded by hash so the loss stays auditable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EVENT_TYPES = {
    "observation",
    "action_request",
    "action_result",
    "verification",
    "fault",
    "emotion",
    "task_accepted",
    "task_terminal",
    "task_start",
    "tool_call",
    "policy_violation",
    "recovery_started",
    "recovery_complete",
}
REQUIRED_EVENT_FIELDS = {"event_id", "run_id", "sequence_no", "event_type", "occurred_at", "payload"}
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
# Version 2 scopes sequence uniqueness to a run. Version 1 declared
# ``sequence_no INTEGER NOT NULL UNIQUE`` globally, so a second run could not
# start at zero and one database could hold only one run. That is the accidental
# global uniqueness Issue #161 removes: a log of several runs is now the normal
# case rather than an unsupported one.
SCHEMA_VERSION = 2
SNAPSHOT_FORMAT = "workbench-event-store-snapshot"
SNAPSHOT_FORMAT_VERSION = 2

# The instruction an operator gets when a database cannot be read under the
# current schema. It names the recovery rather than only the failure, because a
# bare "schema mismatch" invites deleting the file, which loses evidence.
RECOVERY_INSTRUCTION = (
    "take a backup of the file first, then rebuild the database from its source log with "
    "kernel.event_store.migrate_jsonl(source, destination), or rebuild it by restoring a verified snapshot with "
    "EventStore.restore(snapshot, destination); do not delete the file"
)


class EventStoreError(ValueError):
    """Raised when persisted evidence cannot be written or replayed safely."""


class EventStore:
    """Persist one contiguous event run.

    SQLite is the default for database-looking paths (``.sqlite3``, ``.sqlite``
    and ``.db``). ``.jsonl`` remains an explicit compatibility format so old
    logs can be inspected and migrated without silently changing their meaning.
    """

    def __init__(
        self,
        log_file: Path,
        *,
        legacy_objects: bool = False,
        backend: str | None = None,
        multi_run: bool = False,
    ):
        self.log_file = Path(log_file)
        self.legacy_objects = legacy_objects
        # ``multi_run`` selects the validation contract, not the storage engine:
        # one database may hold several runs, each keeping its own sequence
        # numbers. The default stays single-run and contiguous exactly as before,
        # so every existing caller keeps its guarantee.
        self.multi_run = multi_run
        self.backend = backend or ("sqlite" if self.log_file.suffix.lower() in SQLITE_SUFFIXES else "jsonl")
        if self.backend not in {"jsonl", "sqlite"}:
            raise ValueError("backend must be 'jsonl' or 'sqlite'")
        if self.backend == "sqlite" and legacy_objects:
            raise ValueError("legacy_objects is only supported by the JSONL compatibility backend")
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self.checkpoints: list[int] = []
        # Per-run checkpoints are initialized for both backends. The sqlite branch
        # below replaces this with what the database already records; the jsonl
        # branch keeps the empty mapping as the in-memory record. Initializing it
        # only in the sqlite branch left the jsonl backend raising AttributeError
        # from a public method, which is a crash rather than a refusal.
        self._run_checkpoints: dict[str, list[int]] = {}
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._events_signature: tuple[int, int, int] | None = None
        if self.backend == "sqlite":
            self._connection = sqlite3.connect(self.log_file)
            self._connection.execute("PRAGMA foreign_keys = ON")
            # The compatibility check runs before any CREATE TABLE, because a
            # legacy database is identified by the tables it already has; once
            # the current tables exist, every layout looks current.
            self._refuse_incompatible_layout()
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    UNIQUE(run_id, sequence_no)
                );
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_count INTEGER NOT NULL,
                    run_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO event_store_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._connection.commit()
            rows = self._connection.execute(
                "SELECT event_count, run_id FROM checkpoints ORDER BY checkpoint_id"
            ).fetchall()
            self.checkpoints = [row[0] for row in rows if row[1] is None]
            for event_count, run_id in rows:
                if run_id is not None:
                    self._run_checkpoints.setdefault(run_id, []).append(event_count)

    def _refuse_incompatible_layout(self) -> None:
        """Refuse a database whose stored tables cannot be read under this schema.

        Two layouts are refused by name rather than opened and read as empty:

        * a ``world_events`` table, which is the World Model's own former
          implementation. Opening it here would show zero events for a file that
          holds a run, which is the silent-empty-database failure this issue
          exists to prevent;
        * an ``events`` table whose ``sequence_no`` is globally unique, which is
          the pre-#161 layout that cannot hold two runs;
        * a ``checkpoints`` table with no ``run_id`` column, which cannot record
          which run a checkpoint belongs to;
        * a database that records a ``schema_version`` this build does not read.

        Each message names the recovery rather than only the failure. The columns
        are checked here rather than left to the first query, because a missing
        column would otherwise surface as a raw ``sqlite3.OperationalError`` from
        the constructor, which is neither this store's error type nor an
        instruction the operator can act on.
        """

        self._require_connection()
        tables = {
            row[0] for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "world_events" in tables and "events" not in tables:
            raise EventStoreError(
                "this database holds the former World Model 'world_events' table, which this build does not read; "
                f"{RECOVERY_INSTRUCTION}"
            )
        if "event_store_meta" in tables:
            # The recorded version is checked before any table is created and
            # before the constructor returns, so an older database is refused
            # where the operator can see it. Leaving the check to the first read
            # would let construction succeed and fail later, which reads as a
            # working store that happens to be empty.
            recorded = self._connection.execute(
                "SELECT value FROM event_store_meta WHERE key = 'schema_version'"
            ).fetchone()
            if recorded is not None and recorded != (str(SCHEMA_VERSION),):
                raise EventStoreError(
                    f"event database schema_version is {recorded[0]!r} but this build reads {SCHEMA_VERSION}; "
                    f"{RECOVERY_INSTRUCTION}"
                )
        if "events" not in tables:
            return
        columns = {row[1]: row for row in self._connection.execute("PRAGMA table_info(events)").fetchall()}
        expected = {"event_id", "run_id", "sequence_no", "event_json"}
        if not expected <= set(columns):
            raise EventStoreError(
                f"the stored 'events' table is missing {sorted(expected - set(columns))}; {RECOVERY_INSTRUCTION}"
            )
        globally_unique = False
        for index in self._connection.execute("PRAGMA index_list(events)").fetchall():
            if index[2] != 1:
                continue
            indexed = [
                row[0]
                for row in self._connection.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],)
                ).fetchall()
            ]
            if indexed == ["sequence_no"]:
                globally_unique = True
                break
        if globally_unique:
            raise EventStoreError(
                f"the stored 'events' table declares sequence_no globally unique, which cannot hold more than "
                f"one run; {RECOVERY_INSTRUCTION}"
            )
        if "checkpoints" in tables:
            checkpoint_columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(checkpoints)").fetchall()
            }
            if "run_id" not in checkpoint_columns:
                raise EventStoreError(
                    "the stored 'checkpoints' table has no run_id column, so it cannot record which run a "
                    f"checkpoint belongs to; {RECOVERY_INSTRUCTION}"
                )

    def _jsonl_signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self.log_file.stat()
        except FileNotFoundError:
            return None
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _validate_events(self, events: list[dict[str, Any]]) -> None:
        if self.legacy_objects:
            return
        expected_run_id: str | None = None
        event_ids: set[str] = set()
        per_run_sequences: set[tuple[str, int]] = set()
        for index, event in enumerate(events):
            missing = REQUIRED_EVENT_FIELDS - set(event)
            if missing:
                raise EventStoreError(f"event at index {index} is missing fields: {sorted(missing)}")
            event_id = event["event_id"]
            run_id = event["run_id"]
            sequence_no = event["sequence_no"]
            event_type = event["event_type"]
            occurred_at = event["occurred_at"]
            payload = event["payload"]
            evidence_refs = event.get("evidence_refs", [])
            if not isinstance(event_id, str) or not event_id:
                raise EventStoreError(f"event at index {index} has an invalid event_id")
            if event_id in event_ids:
                raise EventStoreError(f"event log has duplicate event_id {event_id!r}")
            event_ids.add(event_id)
            if not isinstance(run_id, str) or not run_id:
                raise EventStoreError(f"event at index {index} has an invalid run_id")
            if expected_run_id is None:
                expected_run_id = run_id
            elif run_id != expected_run_id and not self.multi_run:
                raise EventStoreError(f"event log mixes run_id {expected_run_id!r} and {run_id!r}")
            if type(sequence_no) is not int:
                raise EventStoreError(f"event at index {index} has a non-integer sequence_no {sequence_no!r}")
            if self.multi_run:
                # The multi-run contract is the one the World Model store already
                # had: an event_id is unique, ``(run_id, sequence_no)`` is unique,
                # and sequence numbers are per run. Contiguity is deliberately
                # *not* required here, because a caller that persists an event at
                # sequence 4 has an event at 4, and forcing it to 0 would rewrite
                # evidence. The single-run contract below is unchanged and is
                # still the strict one.
                #
                # Uniqueness *is* enforced here rather than only by the SQLite
                # index, because the JSONL compatibility backend has no index: a
                # batch that reused one run's sequence number would otherwise be
                # accepted on JSONL and refused on SQLite, which is two answers to
                # one question. The declared contract and the enforced contract
                # are the same on both backends.
                if (run_id, sequence_no) in per_run_sequences:
                    raise EventStoreError(
                        f"run_id {run_id!r} already contains sequence_no {sequence_no!r} at index {index}"
                    )
                per_run_sequences.add((run_id, sequence_no))
            elif sequence_no != index:
                raise EventStoreError(
                    f"event sequence_no must be contiguous from zero; index {index} has {sequence_no!r}"
                )
            if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
                raise EventStoreError(f"event at index {index} has an unknown event_type")
            if not isinstance(occurred_at, str) or not occurred_at:
                raise EventStoreError(f"event at index {index} has an invalid occurred_at")
            if not isinstance(payload, dict):
                raise EventStoreError(f"event at index {index} payload must be an object")
            if not isinstance(evidence_refs, list) or any(not isinstance(ref, str) for ref in evidence_refs):
                raise EventStoreError(f"event at index {index} evidence_refs must be a string list")

    def _read_jsonl(self) -> list[dict[str, Any]]:
        if not self.log_file.exists():
            return []
        events = []
        try:
            with self.log_file.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise EventStoreError(f"invalid event JSON at line {line_number}") from exc
                    if not isinstance(event, dict):
                        raise EventStoreError(f"event at line {line_number} must be an object")
                    events.append(event)
        except (OSError, UnicodeError) as exc:
            raise EventStoreError(f"event log is unavailable or not UTF-8: {self.log_file}") from exc
        self._validate_events(events)
        return events

    def _read_sqlite(self) -> list[dict[str, Any]]:
        self._require_connection()
        try:
            version = self._connection.execute(
                "SELECT value FROM event_store_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version != (str(SCHEMA_VERSION),):
                found = version[0] if version else "unset"
                raise EventStoreError(
                    f"event database schema_version is {found!r} but this build reads {SCHEMA_VERSION}; "
                    f"{RECOVERY_INSTRUCTION}"
                )
            rows = self._connection.execute(
                "SELECT event_id, run_id, sequence_no, event_json FROM events ORDER BY run_id, sequence_no"
            ).fetchall()
            events = []
            for index, (event_id, run_id, sequence_no, encoded) in enumerate(rows):
                try:
                    event = json.loads(encoded)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise EventStoreError(f"invalid event JSON in SQLite row {index}") from exc
                if not isinstance(event, dict):
                    raise EventStoreError(f"event in SQLite row {index} must be an object")
                if (event.get("event_id"), event.get("run_id"), event.get("sequence_no")) != (
                    event_id,
                    run_id,
                    sequence_no,
                ):
                    raise EventStoreError(f"indexed fields disagree with SQLite row {index}")
                events.append(event)
            self._validate_events(events)
            return events
        except sqlite3.DatabaseError as exc:
            raise EventStoreError(f"event database is unavailable or corrupt: {self.log_file}") from exc

    def _read_events(self) -> list[dict[str, Any]]:
        return self._read_jsonl() if self.backend == "jsonl" else self._read_sqlite()

    def append(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            raise TypeError("event must be an object")
        self.append_many([event])

    def append_many(self, new_events: list[dict[str, Any]]) -> None:
        """Append a validated batch in one transaction."""
        if any(not isinstance(event, dict) for event in new_events):
            raise TypeError("event must be an object")
        try:
            serialized = [
                json.dumps(event, allow_nan=False, ensure_ascii=False, separators=(",", ":")) for event in new_events
            ]
        except (TypeError, ValueError) as exc:
            raise EventStoreError("event must contain strict JSON values") from exc
        persisted_events = [json.loads(encoded) for encoded in serialized]
        with self._lock:
            if self.backend == "jsonl" and self._events_signature == self._jsonl_signature():
                events = self.events
            else:
                events = self._read_events()
            self._validate_events([*events, *persisted_events])
            if self.backend == "jsonl":
                try:
                    with self.log_file.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.writelines(f"{encoded}\n" for encoded in serialized)
                except OSError as exc:
                    raise EventStoreError(f"event log could not be appended: {self.log_file}") from exc
            else:
                self._require_connection()
                try:
                    self._connection.executemany(
                        "INSERT INTO events(event_id, run_id, sequence_no, event_json) VALUES (?, ?, ?, ?)",
                        [
                            (event["event_id"], event["run_id"], event["sequence_no"], encoded)
                            for event, encoded in zip(persisted_events, serialized, strict=True)
                        ],
                    )
                    self._connection.commit()
                except sqlite3.IntegrityError as exc:
                    self._connection.rollback()
                    raise EventStoreError(self._conflict_message(persisted_events)) from exc
                except sqlite3.DatabaseError as exc:
                    self._connection.rollback()
                    raise EventStoreError(f"event database could not be appended: {self.log_file}") from exc
            self.events = [*events, *persisted_events]
            if self.backend == "jsonl":
                self._events_signature = self._jsonl_signature()

    def append_allocated(
        self,
        run_id: str,
        build: Callable[[int], dict[str, Any]],
        *,
        first: int = 0,
    ) -> dict[str, Any]:
        """Atomically allocate the next per-run sequence and append one event.

        ``build`` receives the allocated ``sequence_no`` and returns the complete
        event, which keeps this method free of any knowledge of what an event
        means: the caller owns the schema, the store owns the allocation and the
        transaction. Allocation and insert share one ``BEGIN IMMEDIATE``
        transaction, so two writers cannot be handed the same sequence number and
        only one of them can commit it.

        An exact retry is idempotent because the caller can look the event up by
        ``event_id`` first; a partial retry that reused an event_id with different
        content is refused by the primary key.
        """

        if self.backend != "sqlite":
            raise EventStoreError("append_allocated requires the sqlite backend")
        self._require_connection()
        with self._lock:
            try:
                # BEGIN IMMEDIATE takes the write lock before the maximum is
                # read, so a second writer cannot slip an insert between the read
                # and the append. ``append_many`` commits this same transaction.
                self._connection.execute("BEGIN IMMEDIATE")
                sequence_no = self.next_sequence_no(run_id, first=first)
                event = build(sequence_no)
                self.append_many([event])
                return event
            except sqlite3.DatabaseError as exc:
                self._connection.rollback()
                raise EventStoreError(
                    f"an event could not be appended to run {run_id!r} at the next free sequence; "
                    "a concurrent writer may hold the same sequence number"
                ) from exc
            except Exception:
                self._connection.rollback()
                raise

    def _conflict_message(self, attempted: list[dict[str, Any]]) -> str:
        """Name which stored event already claims the identity that was refused.

        The distinction matters to a caller: an exact retry is idempotent and is
        handled before this point, so reaching here means the content differs.
        Reporting only "integrity error" would leave the caller unable to tell a
        duplicate identity from a duplicate sequence number.
        """

        self._require_connection()
        for event in attempted:
            owner = self._connection.execute(
                "SELECT run_id, sequence_no, event_json FROM events WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if owner is not None:
                if owner[2] == json.dumps(event, allow_nan=False, ensure_ascii=False, separators=(",", ":")):
                    # Byte-identical content reached the insert, so the only way
                    # to be here is a second row in the same batch.
                    continue
                return (
                    f"event_id {event['event_id']!r} already exists with different canonical event content; "
                    "an exact retry is idempotent but a changed event is not"
                )
            sequence_owner = self._connection.execute(
                "SELECT event_id FROM events WHERE run_id = ? AND sequence_no = ?",
                (event["run_id"], event["sequence_no"]),
            ).fetchone()
            if sequence_owner is not None:
                return (
                    f"run_id {event['run_id']!r} already contains sequence_no {event['sequence_no']} "
                    f"for event_id {sequence_owner[0]!r}"
                )
        return f"event database could not be appended: {self.log_file}"

    def raw_event_row(self, event_id: str) -> tuple[str, int, str] | None:
        """One stored row as ``(run_id, sequence_no, event_json)``, unparsed.

        An adapter that owns a stricter event contract than this module needs the
        bytes plus the indexed columns separately, so it can report a contract
        violation as a contract violation and an index disagreement as an index
        disagreement. Parsing here would collapse the two into one error and lose
        which one actually happened.
        """

        self._require_connection()
        row = self._connection.execute(
            "SELECT run_id, sequence_no, event_json FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return None if row is None else (row[0], row[1], row[2])

    def raw_run_rows(self, run_id: str) -> list[tuple[str, int, str]]:
        """One run's stored rows in ``sequence_no`` order, unparsed."""

        self._require_connection()
        rows = self._connection.execute(
            "SELECT run_id, sequence_no, event_json FROM events WHERE run_id = ? ORDER BY sequence_no",
            (run_id,),
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Return one stored event by ``event_id``, or ``None``.

        The row's indexed columns are compared against the serialized event, so a
        row edited underneath the store is refused rather than returned as if it
        were the event that was written.
        """

        if self.backend != "sqlite":
            for event in self._read_events():
                if event["event_id"] == event_id:
                    return event
            return None
        self._require_connection()
        row = self._connection.execute(
            "SELECT run_id, sequence_no, event_json FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return self._decode_indexed_row(row, event_id=event_id)

    def list_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return one run's events in ``sequence_no`` order."""

        if self.backend != "sqlite":
            events = [event for event in self._read_events() if event["run_id"] == run_id]
            return sorted(events, key=lambda event: event["sequence_no"])
        self._require_connection()
        rows = self._connection.execute(
            "SELECT run_id, sequence_no, event_json FROM events WHERE run_id = ? ORDER BY sequence_no",
            (run_id,),
        ).fetchall()
        return [self._decode_indexed_row(row, run_id=run_id) for row in rows]

    def _decode_indexed_row(
        self,
        row: tuple[Any, ...],
        *,
        event_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        stored_run_id, sequence_no, encoded = row
        try:
            event = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise EventStoreError("stored event JSON is unreadable") from exc
        if not isinstance(event, dict):
            raise EventStoreError("stored event must be an object")
        expected_id = event_id if event_id is not None else event.get("event_id")
        expected_run = run_id if run_id is not None else stored_run_id
        if (event.get("event_id"), event.get("run_id"), event.get("sequence_no")) != (
            expected_id,
            expected_run,
            sequence_no,
        ):
            raise EventStoreError("indexed fields disagree with the stored event")
        return event

    def next_sequence_no(self, run_id: str, *, first: int = 0) -> int:
        """The next free per-run sequence number.

        ``first`` is the number an empty run starts at, and it is a parameter
        because the two callers documented different conventions: this module's own
        log format counts from zero, while the World Model event contract starts at
        one. Making the difference a named argument is honest about it; folding one
        into the other would silently renumber an existing run's first event.

        This only reports a candidate. The unique ``(run_id, sequence_no)`` index
        is what enforces it, so two writers that read the same maximum cannot both
        commit the same sequence.
        """

        self._require_connection()
        row = self._connection.execute("SELECT MAX(sequence_no) FROM events WHERE run_id = ?", (run_id,)).fetchone()
        maximum = row[0] if row is not None else None
        return first if maximum is None else int(maximum) + 1

    def run_ids(self) -> list[str]:
        """Every run id the database holds, ordered deterministically."""

        if self.backend != "sqlite":
            return sorted({event["run_id"] for event in self._read_events()})
        self._require_connection()
        return [row[0] for row in self._connection.execute("SELECT DISTINCT run_id FROM events ORDER BY run_id")]

    def create_run_checkpoint(self, run_id: str) -> int:
        """Record the current event count of one run and return it."""

        with self._lock:
            count = len(self.list_run(run_id))
            if self.backend == "sqlite":
                self._require_connection()
                try:
                    self._connection.execute(
                        "INSERT INTO checkpoints(event_count, run_id) VALUES (?, ?)", (count, run_id)
                    )
                    self._connection.commit()
                except sqlite3.DatabaseError as exc:
                    self._connection.rollback()
                    raise EventStoreError(f"run checkpoint could not be persisted: {run_id}") from exc
            self._run_checkpoints.setdefault(run_id, []).append(count)
            return count

    def run_checkpoints(self, run_id: str) -> list[int]:
        """The recorded checkpoint positions for one run, oldest first."""

        if self.backend != "sqlite":
            return list(self._run_checkpoints.get(run_id, []))
        self._require_connection()
        return [
            row[0]
            for row in self._connection.execute(
                "SELECT event_count FROM checkpoints WHERE run_id = ? ORDER BY checkpoint_id", (run_id,)
            ).fetchall()
        ]

    def replay_run(self, run_id: str, *, from_checkpoint: int | None = None) -> list[dict[str, Any]]:
        """Replay one run, optionally from a per-run checkpoint position."""

        events = self.list_run(run_id)
        start_index = 0 if from_checkpoint is None else from_checkpoint
        if type(start_index) is not int or not 0 <= start_index <= len(events):
            raise ValueError(f"checkpoint must be between 0 and {len(events)}")
        return events[start_index:]

    def create_checkpoint(self) -> int:
        with self._lock:
            self.events = self._read_events()
            checkpoint = len(self.events)
            if self.backend == "sqlite":
                self._require_connection()
                try:
                    self._connection.execute("INSERT INTO checkpoints(event_count) VALUES (?)", (checkpoint,))
                    self._connection.commit()
                except sqlite3.DatabaseError as exc:
                    self._connection.rollback()
                    raise EventStoreError("checkpoint could not be persisted") from exc
                self.checkpoints = [
                    row[0]
                    for row in self._connection.execute(
                        "SELECT event_count FROM checkpoints ORDER BY checkpoint_id"
                    ).fetchall()
                ]
            else:
                self.checkpoints.append(checkpoint)
            return checkpoint

    def replay(self, from_checkpoint: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self.events = self._read_events()
            start_index = 0 if from_checkpoint is None else from_checkpoint
            if type(start_index) is not int or not 0 <= start_index <= len(self.events):
                raise ValueError(f"checkpoint must be between 0 and {len(self.events)}")
            return self.events[start_index:]

    def verify_integrity(self) -> bool:
        with self._lock:
            try:
                if self.backend == "sqlite":
                    self._require_connection()
                    result = self._connection.execute("PRAGMA integrity_check").fetchone()
                    if result != ("ok",):
                        return False
                self._read_events()
            except (EventStoreError, sqlite3.DatabaseError):
                return False
            return True

    def backup(self, destination: Path) -> Path:
        """Create a checksummed snapshot and return its manifest path.

        Both backends are snapshot the same way, so an operator has one recovery
        procedure: a byte-identical copy plus a manifest naming the format
        version, the schema version, the event count and the content hash. The
        manifest is written to a temporary name and moved into place last, so a
        crash cannot leave a manifest describing a snapshot that never landed.
        """
        destination = Path(destination)
        if destination.resolve() == self.log_file.resolve():
            raise EventStoreError("snapshot destination must differ from the live store")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.backup-tmp")
        if temp.exists():
            temp.unlink()
        with self._lock:
            if self.backend == "sqlite":
                self._require_connection()
                self._connection.commit()
                target = sqlite3.connect(temp)
                try:
                    self._connection.backup(target)
                finally:
                    target.close()
                self._read_events()
            else:
                # A JSONL snapshot is a byte copy of validated content, never a
                # re-serialization: re-encoding could silently drop a field that
                # the current schema does not know about yet.
                self._read_events()
                if self.log_file.exists():
                    shutil.copy2(self.log_file, temp)
                else:
                    temp.write_bytes(b"")
        manifest = {
            "format": SNAPSHOT_FORMAT,
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "database": "sqlite" if self.backend == "sqlite" else "jsonl",
            "sqlite_version": sqlite3.sqlite_version if self.backend == "sqlite" else None,
            "schema_version": SCHEMA_VERSION,
            "event_count": len(self.events),
            "created_at": datetime.now(UTC).isoformat(),
            "sha256": _sha256(temp),
        }
        manifest_path = Path(f"{destination}.manifest.json")
        manifest_temp = Path(f"{temp}.manifest.json")
        manifest_temp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, destination)
        os.replace(manifest_temp, manifest_path)
        return manifest_path

    @classmethod
    def restore(cls, snapshot: Path, destination: Path) -> EventStore:
        """Verify a snapshot before atomically replacing a destination database."""
        snapshot = Path(snapshot)
        destination = Path(destination)
        if snapshot.resolve() == destination.resolve():
            raise EventStoreError("restore destination must differ from the snapshot")
        manifest_path = Path(f"{snapshot}.manifest.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EventStoreError("snapshot manifest is unavailable or malformed") from exc
        if manifest.get("format") != SNAPSHOT_FORMAT or manifest.get("schema_version") != SCHEMA_VERSION:
            raise EventStoreError("snapshot format or schema version mismatch")
        if manifest.get("sha256") != _sha256(snapshot):
            # The checksum is verified before the live store is touched, so a
            # tampered or truncated snapshot cannot replace good evidence.
            raise EventStoreError("snapshot checksum mismatch")
        backend = "sqlite" if manifest.get("database") == "sqlite" else "jsonl"
        probe = cls(snapshot, backend=backend)
        try:
            if not probe.verify_integrity():
                raise EventStoreError("snapshot failed integrity verification")
            if len(probe.replay()) != manifest.get("event_count"):
                raise EventStoreError("snapshot event count does not match its manifest")
        finally:
            probe.close()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.restore-tmp")
        shutil.copy2(snapshot, temp)
        os.replace(temp, destination)
        shutil.copy2(manifest_path, Path(f"{destination}.manifest.json"))
        return cls(destination, backend=backend)

    def _require_connection(self) -> sqlite3.Connection:
        """The open SQLite connection, or a typed refusal.

        This replaced a bare ``assert self._connection is not None``. An assert
        is stripped by ``python -O``, so the check that a store is still open
        disappeared in exactly the interpreter mode where a silent ``AttributeError``
        on ``None`` is hardest to diagnose. Raising keeps the failure the same in
        every mode and makes it catchable as the store's own error type.

        The message names both causes, because this store has two: it was closed,
        or it is using the ``jsonl`` compatibility backend, which has no connection
        at all.
        """

        if self._connection is None:
            raise EventStoreError("no open SQLite connection; this store is either closed or using the jsonl backend")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def migrate_jsonl(source: Path, destination: Path) -> EventStore:
    """Migrate a strict JSONL log into SQLite after validating every event."""
    source_store = EventStore(source, backend="jsonl")
    events = source_store.replay()
    destination = Path(destination)
    if destination.exists():
        raise EventStoreError(f"migration destination already exists: {destination}")
    temp = destination.with_name(f".{destination.name}.migration-tmp")
    if temp.exists():
        temp.unlink()
    target = EventStore(temp, backend="sqlite")
    try:
        target.append_many(events)
        target.close()
        os.replace(temp, destination)
    except Exception:
        target.close()
        raise
    return EventStore(destination, backend="sqlite")


def recover_torn_jsonl(source: Path, destination: Path) -> dict[str, Any]:
    """Rebuild a JSONL log that an interrupted append left with a torn last line.

    Recovery is deliberately narrow. It refuses whenever the damage cannot be
    proven to be a single incomplete final line, because anything else might be a
    lost event rather than a lost byte:

    * every complete line must still parse and satisfy the event contract;
    * the damaged tail must be the last line and must not end with a newline;
    * more than one damaged line is refused.

    The recovered log is a new file. The damaged original is left exactly as it
    was found, and the discarded fragment is written next to the destination as
    ``<destination>.discarded`` and identified by hash, so the loss is explicit
    and auditable instead of silently absorbed. Events are never synthesized, and
    a recovered log is never extended to reach an expected event count.

    A candidate that is still not a valid event log - because the surviving lines
    do not satisfy the contract - is refused rather than published. Recovery that
    produced an unreadable store would only move the corruption.
    """
    source = Path(source)
    destination = Path(destination)
    if source.resolve() == destination.resolve():
        raise EventStoreError("recovery destination must differ from the damaged log")
    if not source.exists():
        raise EventStoreError(f"damaged event log is unavailable: {source}")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise EventStoreError(f"damaged event log could not be read: {source}") from exc
    text = raw.decode("utf-8", errors="strict") if _is_utf8(raw) else None
    if text is None:
        raise EventStoreError("damaged event log is not valid UTF-8; recovery is not attempted")
    if not text or text.endswith("\n"):
        raise EventStoreError("event log has no incomplete trailing line; recovery is not required")

    last_newline = text.rfind("\n")
    complete_text = text[: last_newline + 1] if last_newline >= 0 else ""
    discarded = text[last_newline + 1 :]
    complete_lines = [line for line in complete_text.splitlines() if line.strip()]
    events = []
    for line_number, line in enumerate(complete_lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EventStoreError(f"line {line_number} is damaged before the final line; recovery is refused") from exc
        if not isinstance(event, dict):
            raise EventStoreError(f"line {line_number} is not an event object; recovery is refused")
        events.append(event)
    if any(not isinstance(event, dict) for event in events):  # pragma: no cover - guarded above
        raise EventStoreError("damaged event log contains a non-object event")

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.recovery-tmp")
        temp.write_text("".join(f"{json.dumps(event, separators=(',', ':'))}\n" for event in events), encoding="utf-8")
    except OSError as exc:
        # An unwritable or full destination is an operational failure, not a
        # corruption finding, and it must not be reported as one.
        raise EventStoreError(f"recovered event log could not be written: {destination}") from exc
    # A candidate that fails the contract must not be promoted, so it is read
    # back and verified before the destination is replaced.
    if not _is_readable(temp):
        temp.unlink(missing_ok=True)
        raise EventStoreError("recovered event log would not satisfy the event contract")
    try:
        os.replace(temp, destination)
        Path(f"{destination}.discarded").write_text(discarded, encoding="utf-8")
    except OSError as exc:
        raise EventStoreError(f"recovery output could not be published: {destination}") from exc
    discarded_path = Path(f"{destination}.discarded")
    return {
        "format": "workbench-event-store-recovery",
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "source": str(source),
        "destination": str(destination),
        "recovered_events": len(events),
        "discarded_bytes": len(discarded.encode("utf-8")),
        "discarded_sha256": hashlib.sha256(discarded.encode("utf-8")).hexdigest(),
        "discarded_path": str(discarded_path),
        "created_at": datetime.now(UTC).isoformat(),
    }


def audit_evidence_refs(store: EventStore, available: Iterable[str]) -> dict[str, Any]:
    """Report evidence references that resolve to nothing.

    This is an audit, not a repair: a missing reference is reported as missing and
    is never replaced by a placeholder, a synthetic reference, or a completion
    claim. Callers decide whether an incomplete run may be published; the store
    only states the fact.
    """
    known = set(available)
    referenced: list[str] = []
    missing: list[str] = []
    for event in store.replay():
        for reference in event.get("evidence_refs", []):
            referenced.append(reference)
            if reference not in known:
                missing.append(reference)
    return {
        "referenced_count": len(referenced),
        "unique_referenced_count": len(set(referenced)),
        "missing_count": len(set(missing)),
        "missing_refs": sorted(set(missing)),
        "complete": not missing,
    }


def _is_readable(path: Path) -> bool:
    """Report whether a candidate file is a readable, contract-valid event log."""
    probe = EventStore(path, backend="jsonl")
    try:
        return probe.verify_integrity()
    finally:
        probe.close()


def _is_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
