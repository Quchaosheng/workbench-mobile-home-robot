# Event evidence backup, restore and recovery

World Model event data is the evidence behind every verified task. This page
defines one backup format, one restore procedure and one recovery procedure, and
states the targets the procedures are measured against. The rules here are
exercised by `tests/unit/test_kernel_event_store.py` against temporary stores; no
step below requires physical hardware.

The storage implementation is `workbench.kernel.event_store.EventStore`. It has a
SQLite backend (`.db`, `.sqlite`, `.sqlite3`) and a strict JSONL compatibility
backend for legacy logs.

## Service targets

| Target | Value | Meaning |
|---|---|---|
| RPO | 0 events for SQLite; 1 event for the JSONL compatibility backend | SQLite snapshots are taken with the online backup API inside a committed transaction, so a snapshot never loses an accepted event. A JSONL append is a single line, so an interrupted append can lose only the in-flight event, which recovery discards explicitly rather than guessing. |
| RTO | under 5 minutes for a store under 1 GiB | Restore is a verified copy plus a reopen; no replay, migration, or rebuild is required. |
| Verification | before every restore | Checksum and integrity are verified before the live store is replaced. |
| Retention | operator-owned, minimum 30 days for release evidence | The repository stores no raw evidence and defines no deletion policy; the deployment owns both. |

A restore that cannot verify its checksum is refused. It is never a partial
restore and never a "best effort" restore.

## Snapshot format

`EventStore.backup(destination)` writes two files:

- `<destination>` - a byte-identical copy of the store;
- `<destination>.manifest.json` - the manifest below.

```json
{
  "format": "workbench-event-store-snapshot",
  "format_version": 2,
  "database": "sqlite",
  "sqlite_version": "3.x.y",
  "schema_version": 1,
  "event_count": 42,
  "created_at": "2026-09-17T00:00:00+00:00",
  "sha256": "<hash of the snapshot bytes>"
}
```

`format` names the artifact, `format_version` names the manifest contract,
`schema_version` names the event-store schema, and `sha256` binds the manifest to
the exact bytes. A restore accepts a snapshot only when all four agree and the
snapshot still passes `PRAGMA integrity_check` with the documented event count.
The manifest is moved into place after the snapshot, so a crash cannot leave a
manifest describing a snapshot that never landed.

## Operating procedures

Back up a live SQLite store:

```python
from pathlib import Path
from workbench.kernel.event_store import EventStore

store = EventStore(Path("runs/events.sqlite3"))
manifest = store.backup(Path("backups/2026-09-17.sqlite3"))
store.close()
```

Restore into a new location, then swap the path only after the restore returns:

```python
from pathlib import Path
from workbench.kernel.event_store import EventStore

restored = EventStore.restore(Path("backups/2026-09-17.sqlite3"), Path("runs/events.restored.sqlite3"))
assert restored.verify_integrity()
restored.close()
```

The destination must differ from the snapshot, so a restore cannot consume its
own source. An existing destination is replaced only after the snapshot has been
verified, which is what keeps a failed restore from destroying good evidence.

### Interrupted append and torn JSONL

A crash during an append can leave one incomplete final line. `recover_torn_jsonl`
rebuilds such a log:

```python
from pathlib import Path
from workbench.kernel.event_store import recover_torn_jsonl

report = recover_torn_jsonl(Path("runs/events.jsonl"), Path("runs/events.recovered.jsonl"))
```

Recovery is deliberately narrow and refuses anything it cannot prove:

- every complete line must parse and satisfy the event contract;
- the damaged fragment must be the last line and must not end with a newline;
- a damaged line anywhere else, a non-UTF-8 file, a clean file or a missing file
  is refused;
- the recovered candidate is read back and verified before it is published.

A refused recovery never leaves a partial destination behind. The damaged
original is left exactly as found, and the discarded fragment is written to
`<destination>.discarded` and reported by byte count and SHA-256, so the loss is
auditable instead of silently absorbed. The report names the recovered event
count; recovery never synthesizes an event to reach an expected count and never
marks an unverified task complete.

### Disk full

An append that cannot be written raises `EventStoreError` and adds no event. An
unwritable or full recovery destination raises `EventStoreError` as well, rather
than being reported as corruption. Both cases leave the store readable at its
previous state.

### Missing evidence

Evidence references are resolved against the operator-owned store.
`audit_evidence_refs` reports which references resolve to nothing:

```python
from workbench.kernel.event_store import EventStore, audit_evidence_refs

audit = audit_evidence_refs(EventStore(Path("runs/events.sqlite3")), available_reference_ids)
```

This is an audit, not a repair. A missing reference is reported as missing; it is
never replaced by a placeholder, a fabricated reference, or a completion claim.
Raw evidence lives by reference in the operator-owned store described in
[security hardening](../security/hardening.md), not in the event log.

## Failure handling

| Symptom | Action |
|---|---|
| Restore refuses with a checksum mismatch | Treat the snapshot as tampered or truncated. Restore a different snapshot; never override the check. |
| Restore refuses with a schema or format mismatch | Use a snapshot taken from a compatible schema version. Do not edit the manifest. |
| Recovery refuses a damaged line before the tail | Treat every event after the damage as unproven. Restore the most recent verified snapshot instead. |
| Integrity check fails on the live store | Restore the most recent verified snapshot, then re-run the scenario. Do not append to a store that cannot be read. |
| References are missing | Report the incomplete run as incomplete evidence. Do not reconstruct the reference by hand. |

## What the tests prove

`tests/unit/test_kernel_event_store.py` covers the snapshot manifest fields, a
restore that is replay-equivalent to its source, a refused tampered snapshot, a
refused manifest mismatch, an interrupted append, a refused mid-file corruption,
a refused clean-log recovery, a refused unwritable destination, and an evidence
audit that reports a missing reference without inventing one.
