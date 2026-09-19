"""A typed ``WorldEvent`` adapter over the Kernel event store (Issue #161).

This module used to be a second SQLite implementation. It owned its own
``world_events`` table, its own connection lifecycle, its own schema validation
and its own conflict handling, while ``workbench.kernel.event_store`` owned a
different table, different lifecycle rules, checkpoints, integrity checking and
backup/restore. Two implementations of one responsibility meant two answers to
"what does an exact retry do", and a fix in one left the other wrong.

It is now an adapter. All storage, transaction and conflict handling belongs to
:class:`workbench.kernel.event_store.EventStore`, and this module only:

* validates and normalizes a ``WorldEvent`` through the shared payload boundary,
  so an event is checked before it can reach a database;
* renders an event to canonical JSON, so two structurally equal events compare
  equal regardless of key order;
* translates the Kernel store's ``EventStoreError`` into the typed
  ``EventStoreIntegrityError`` that World Model callers already catch.

The public surface is unchanged: ``append``, ``append_allocated``, ``get_event``,
``list_run`` and ``close``. ``connection`` is gone, because handing out a raw
connection is what allowed the store's own invariants to be bypassed; the one
in-repo caller that used it now asks for ``list_run``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from workbench_contracts import WorldEvent, WorldEventType

from .event_payloads import normalize_world_event

# The Kernel store lives in the ``libs/kernel`` source tree, which is on the path
# in every runtime entry point but not necessarily when a service module is
# imported directly in a focused test. The import is explicit rather than
# defensive: a missing Kernel store is a broken checkout, not a fallback case,
# because falling back would reintroduce the second implementation this adapter
# exists to remove.
_KERNEL_ROOT = Path(__file__).resolve().parents[3] / "libs" / "kernel"
if _KERNEL_ROOT.is_dir() and str(_KERNEL_ROOT) not in sys.path:
    sys.path.append(str(_KERNEL_ROOT))

from workbench.kernel.event_store import (
    RECOVERY_INSTRUCTION,
    SCHEMA_VERSION,
    EventStore,
    EventStoreError,
)

# The World Model event contract numbers its first event ``1``. The Kernel
# store's own log format numbers from ``0``, so the starting number is passed in
# rather than assumed; silently renumbering an existing run's first event would
# rewrite what the run recorded.
FIRST_SEQUENCE_NO = 1

_T = TypeVar("_T")


class EventStoreIntegrityError(RuntimeError):
    """The requested append conflicts with the persisted event stream."""


class EventStoreMigrationRequiredError(EventStoreIntegrityError):
    """The database schema cannot be upgraded safely by this store."""


class SQLiteEventStore:
    """Persist ``WorldEvent`` rows through one SQLite implementation."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        try:
            self._store = EventStore(self.database_path, backend="sqlite", multi_run=True)
        except EventStoreError as error:
            raise self._integrity_error(error) from error

    @staticmethod
    def _integrity_error(error: EventStoreError) -> EventStoreIntegrityError:
        """Translate a Kernel refusal into this module's typed error.

        A layout or version refusal becomes a migration error, because the
        caller's remedy is an operator action rather than a code change. The
        detection is on the recorded schema version and the recovery instruction
        rather than on the word "table", so an ordinary append conflict that
        happens to mention a table is not misreported as a migration problem.
        """

        message = str(error)
        if RECOVERY_INSTRUCTION in message or "schema_version" in message:
            # The old diagnostic promised a "backup" and a "rebuild"; those two
            # words are kept here so an operator following the earlier runbook
            # still recognizes the message.
            return EventStoreMigrationRequiredError(
                f"{message}; back up the file first, then rebuild it (schema_version={SCHEMA_VERSION})"
            )
        return EventStoreIntegrityError(message)

    def _read(self, operation: Callable[[], _T]) -> _T:
        """Run one Kernel read, publishing only this module's typed errors.

        The adapter exists so World Model callers have one error vocabulary. A
        Kernel ``EventStoreError`` escaping from a read - a closed store, or a row
        the Kernel refuses - would be a second vocabulary leaking through the
        boundary that is supposed to remove it, so every call is translated.
        """

        try:
            return operation()
        except EventStoreError as error:
            raise self._integrity_error(error) from error

    @staticmethod
    def _canonical_event_json(event: WorldEvent) -> str:
        """Serialize an event so structurally equal events compare equal.

        ``mode="python"`` is used deliberately: it preserves the already-validated
        field values without a second JSON round trip, and the fixed separators
        and sorted keys make the digest of an event depend on its values rather
        than on the order its fields happened to be written in.
        """

        return json.dumps(
            event.model_dump(mode="python"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def _parse_event_json(cls, event_json: str) -> WorldEvent:
        """Read one stored event back through the shared payload boundary.

        Stored bytes are not trusted just because this store wrote them: a row
        edited underneath the store is refused rather than returned.
        """

        return normalize_world_event(WorldEvent.model_validate_json(event_json))

    def append(self, event: WorldEvent) -> None:
        """Append one event, idempotently when the retry is exact."""

        event = normalize_world_event(event)
        event_json = self._canonical_event_json(event)
        try:
            stored = self._store.get_event(event.event_id)
        except EventStoreError as error:
            raise self._integrity_error(error) from error
        if stored is not None:
            if json.dumps(stored, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True) == (
                event_json
            ):
                # An exact retry is a no-op rather than an error, so a caller that
                # cannot tell whether its last write landed can safely repeat it.
                return
            raise EventStoreIntegrityError(
                f"event_id {event.event_id!r} already exists with different canonical event content"
            )
        try:
            self._store.append(json.loads(event_json))
        except EventStoreError as error:
            raise self._integrity_error(error) from error

    def append_allocated(
        self,
        *,
        event_id: str,
        run_id: str,
        event_type: WorldEventType,
        occurred_at: str,
        payload: dict[str, Any],
        evidence_refs: list[str] | None = None,
    ) -> WorldEvent:
        """Atomically allocate the next per-run sequence and append an event.

        Exact retries reuse the persisted sequence and event. Reusing an
        ``event_id`` with different canonical content fails closed.

        The sequence is allocated and inserted in one transaction inside the
        Kernel store, so two writers racing for the same run produce one winner
        rather than two events claiming one sequence number.
        """

        references = list(evidence_refs or [])
        # The candidate is validated before it can reach an allocation, so an
        # invalid payload never consumes a sequence number.
        preflight = normalize_world_event(
            WorldEvent(
                event_id=event_id,
                run_id=run_id,
                sequence_no=FIRST_SEQUENCE_NO,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload,
                evidence_refs=references,
            )
        )
        try:
            existing = self._store.raw_event_row(event_id)
        except EventStoreError as error:
            raise self._integrity_error(error) from error
        if existing is not None:
            record = self._parse_stored_row(existing, expected_event_id=event_id)
            if record.run_id == run_id and self._canonical_event_json(record) != self._canonical_event_json(preflight):
                raise EventStoreIntegrityError(
                    f"event_id {event_id!r} already exists with different canonical event content"
                )
            if record.run_id != run_id:
                raise EventStoreIntegrityError(
                    f"event_id {event_id!r} already exists in run {record.run_id!r}, not {run_id!r}"
                )
            return record

        def build(sequence_no: int) -> dict[str, Any]:
            event = WorldEvent(
                event_id=event_id,
                run_id=run_id,
                sequence_no=sequence_no,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload,
                evidence_refs=references,
            )
            return json.loads(self._canonical_event_json(normalize_world_event(event)))

        try:
            written = self._store.append_allocated(run_id, build, first=FIRST_SEQUENCE_NO)
        except EventStoreError as error:
            raise self._integrity_error(error) from error
        return self._parse_event_json(json.dumps(written))

    @classmethod
    def _parse_stored_row(
        cls,
        row: tuple[str, int, str],
        *,
        expected_event_id: str | None = None,
    ) -> WorldEvent:
        """Validate one stored row, reporting a contract violation before an index one.

        The order matters. A row whose JSON violates the event contract is a
        contract error and is reported as the pydantic error that describes it. A
        row whose JSON is valid but whose indexed columns disagree is a different
        failure - the row was edited underneath the store - and is reported as an
        integrity error. Checking the index first would report the second as the
        first and hide the field that was actually wrong.
        """

        run_id, sequence_no, event_json = row
        event = cls._parse_event_json(event_json)
        expected_id = expected_event_id if expected_event_id is not None else event.event_id
        if (event.event_id, event.run_id, event.sequence_no) != (expected_id, run_id, sequence_no):
            raise EventStoreIntegrityError(
                f"stored row for event_id {expected_id!r} has indexed columns that disagree with its own event; "
                "the row was modified outside this store"
            )
        return event

    def get_event(self, event_id: str) -> WorldEvent | None:
        """Return one stored event by ``event_id``, or ``None``."""

        row = self._read(lambda: self._store.raw_event_row(event_id))
        return None if row is None else self._parse_stored_row(row, expected_event_id=event_id)

    def list_run(self, run_id: str) -> list[WorldEvent]:
        """Return one run's events in ``sequence_no`` order."""

        return [self._parse_stored_row(row) for row in self._read(lambda: self._store.raw_run_rows(run_id))]

    def run_ids(self) -> list[str]:
        """Every run id the database holds, ordered deterministically."""

        return self._read(self._store.run_ids)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> SQLiteEventStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
