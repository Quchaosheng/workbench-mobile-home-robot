# Run evidence data governance

A verified run produces a lot of evidence: event logs, timing telemetry, camera
frames, model traces, prompts and hardware captures. Keeping all of it forever is
not a policy, and deleting it on the first convenient day is not a policy either.
This page states one lifecycle rule per artifact family and gives the operator the
four answers a deletion or export tool has to have: how long may this be kept, who
may see it, what may be exported, and what must not be deleted while an
investigation is open.

The rules are data, not prose. They live in
`workbench.application.data_governance` under the version
`data-governance-v1`, and a deletion tool, an export tool and the test suite all
consult the same table. Nothing on this page requires physical hardware.

## Data classes

Every artifact the runtime produces belongs to exactly one data class. Each class
declares a retention period, an owner, an access level and whether deletion and
holds apply.

| Data class | Retention | Owner | Access level | Deletable |
|---|---|---|---|---|
| `run_event_log` | 90 days | Integration | operator | yes, while no hold applies |
| `task_timing` | 180 days | Integration | operator | yes, while no hold applies |
| `camera_evidence` | 30 days | Perception | restricted | yes, while no hold applies |
| `model_trace` | 90 days | Runtime | operator | yes, while no hold applies |
| `model_prompt` | 30 days | Runtime | restricted | yes, while no hold applies |
| `hardware_record` | no automatic expiry | Hardware Owner | restricted | no |
| `release_evidence` | no automatic expiry | Product Owner | operator | no |
| `incident_record` | no automatic expiry | Security Owner | restricted | no |

Retention is measured from the artifact's creation time. A class with no
automatic expiry is kept until a human decides otherwise, which is the honest
answer for an audit trail: nobody should be able to make an incident disappear by
waiting.

`AccessLevel` ranks the three levels from widest to narrowest: `public`,
`operator` and `restricted`. No data class is public. A public dashboard is served
from an export projection, never from the raw artifact.

## Retention and deletion

`deletion_decision` is the single verdict a deletion tool must respect. It refuses
a deletion and returns the reasons whenever any of the following is true:

- the class is not deletable at all;
- an active hold covers the scope;
- the retention period is still active.

Deleting a run event log that is ten days old is refused with
`retention period is still active until ...`. Deleting the same log after 120 days
is allowed, because no rule protects it any more.

`retention_summary` reports the same information for every class in one pass, so
an operator dashboard can show owner, access level, expiry time, whether the class
is expired and whether it is deletable, without re-deriving any of it.

## Holds outrank everything

A hold is a named, explicit reason that covered evidence must not be deleted.
There are two kinds, `release` and `incident`, and both protect their scope the
same way.

An active hold **outranks** retention. A run past its retention period still cannot
be deleted while an incident or release hold covers it, and a re-opened hold id is
refused rather than merged. Holds apply to the whole scope, or to `*` when a
release freeze covers everything.

Holds never expire implicitly. A hold may carry an `expires_at`, which is
evaluated against a caller-supplied time so a test can move time forward without
sleeping and an operator can see exactly when it stops applying; otherwise it
stays open until `release` closes it explicitly. Audit and safety records - the
`hardware_record`, `release_evidence` and `incident_record` classes - are never
deleted by an automated lifetime rule, whether or not a hold is open.

> An expired retention period is not permission to delete. A hold is a promise
> that the evidence is still needed.

## Access and export

Export authorization is a check, not a filter. `authorize_export` raises rather
than returning a subset the caller did not ask for: a requester who may not export
`camera_evidence` or `model_prompt` must fix the request, not receive redacted data
they did not expect.

`export_projection` produces the publishable form of a run and a report of what it
withheld.

- A **public** projection keeps correlation fields and references - `run_id`,
  `event_id`, `sequence_no`, `occurred_at`, `event_type` and `evidence_refs` - and
  replaces everything else, including `payload`, with an evidence-redaction
  marker. It reports every withheld field by name, because a silently omitted
  field looks identical to a field that was never there.
- A **restricted** projection returns the record in full, but still scrubs
  credentials and secrets through
  `workbench.application.redaction`: authorization is not a reason to copy a
  secret into an artifact.

Every projection carries `content_sha256`, so provenance and integrity travel with
the published data even when the payload does not. The projection never mutates
the source record, and projecting the same run twice produces the same bytes.

The boundary rules that decide what may leave the process are defined in
[security hardening](../security/hardening.md) and applied at the point of writing.
How a damaged or missing store is recovered is defined in
[event evidence recovery](event-evidence-recovery.md).

## Lifecycle logging

Deletion and restore are logged and authorized. `LifecycleLog` records each action
with its data class, scope, actor, time, authorized flag and detail. A refused
action is recorded too: "we tried to delete this and were stopped" is the finding
an investigator needs, and a log that only holds successes cannot show it. An
action with no named actor or no scope is refused, and the log stays empty.

## Failure handling

| Symptom | Action |
|---|---|
| Deletion refused with an active hold | Keep the artifact. Resolve or release the hold, then decide again. Do not delete around the hold. |
| Deletion refused with active retention | Wait for the retention period to end, or escalate to the class owner. Do not disable the rule. |
| Export refused for access | Request the required access level. Do not retry with a public projection and treat it as the export. |
| A field is missing from a public projection | Check `withheld_fields`; the field was withheld on purpose and named. Do not reconstruct it by hand. |
| A record must be removed immediately | Open the decision through the class owner and the security owner first. The repository defines no emergency deletion path. |

## What the tests prove

`tests/unit/test_data_governance.py` covers retention expiry, an active hold that
outranks expiry, release and incident holds, hold scope and wildcard holds,
duplicate and malformed holds, refused public export of restricted classes, a
public projection that withholds the payload while keeping references and the
content hash, a restricted projection that still scrubs secrets, a determinism
check, and a lifecycle log that records both authorized and refused actions.
