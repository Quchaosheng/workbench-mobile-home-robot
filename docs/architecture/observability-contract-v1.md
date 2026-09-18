# Observability contract v1

Issue #76 defines one contract for the three properties an operator needs from a
telemetry line: which run, action and attempt produced it, which distinct
failure it counts, and whether a bounded writer changed it before it was
written.

It does not define robot health. Current state remains
`workbench.application.monitoring` (Issue #170 and #171) and alert meaning
remains `workbench.application.alerts`. This contract governs the *records* those
surfaces and their callers emit.

## Where the rules live

| Concern | Owner | Rule |
| --- | --- | --- |
| What may leave the process | `workbench.application.redaction` | Format and key-name rules, versioned as `redaction-rules-v1` |
| Correlation, bounds, failure classes | `workbench.application.observability` | `observability-v1` schema, `FailureClass`, `bounded_payload`, `JsonlSink` |
| Record emission | `workbench_backend.logging.StructuredLogger` | Adds the correlation envelope, then scrubs |
| State meaning | `services/world_model/` | Whether a task is complete |

The modules are separate on purpose. Redaction answers "may this text leave?"
The observability contract answers "is this line joinable, bounded and honest
about what it dropped?" A line can satisfy either question and fail the other.

## The correlation envelope

A record carries `run_id`, `component` and `schema_version` always, and
`action_id`, `attempt_id` and `monotonic_ns` when the caller has them.

An `attempt_id` without an `action_id` is refused rather than written: a retry
that cannot name the action it retried is not a trace, and storing it would
produce a record that looks correlated and is not. A missing `run_id` or
`component` is likewise refused, never defaulted to an empty string.

Only the identifiers the redactor already freezes are copied verbatim.
`component`, `attempt_id` and every other field are still scanned as untrusted
text, because a credential placed in a field the code declared "safe" would
bypass the redactor.

## Failure classes

Five failures must be countable separately, because they need different
responses:

| Class | Distinguishing contract values |
| --- | --- |
| `timeout` | `ActionOutcome.TIMEOUT`, `ack_timeout`, `stop_timeout` |
| `rejection` | `DeviceState.REJECTED`, `stop_rejected`, `malformed_frame`, `duplicate_frame` |
| `transport_loss` | `DispatchState.SEND_FAILED`, `link_lost`, `watchdog_expired` |
| `stale_evidence` | `stale_observation`, `target_not_observed`, `conflicting_observations`, `evidence_missing`, `confidence_below_threshold` |
| `verification_failure` | `VerificationStatus.REFUTED`, `INSUFFICIENT_EVIDENCE`, `ActionOutcome.FAILED` |

Precedence is a contract, not a preference: the class that explains the others
wins. A lost link is *why* the timeout happened, and a stale observation is
*why* verification failed. Without that order a single incident would be counted
three times and the class an operator must act on would be ambiguous.

`None` means "these values describe no failure". An unrecognized enum member
raises instead of returning `None`, because a renamed status that silently
stops being counted is exactly the defect this contract exists to prevent.
Cancels and deliberate safe stops are not failures.

`ActionOutcome.FAILED` maps to `verification_failure` because the more specific
classes already claim the cases the evidence can distinguish.

## Bounds

`bounded_payload` projects a value into the record budget and reports what it
withheld, by path, in `PayloadReport`. Silent truncation looks identical to
absence, so the omission travels in the record under `truncation`:

- at most `MAX_DETAIL_KEYS` (32) mapping keys per level,
- at most `MAX_LIST_ITEMS` (64) items per list,
- at most `MAX_DETAIL_DEPTH` (4) levels of nesting,
- at most `MAX_STRING_BYTES` (2048) bytes per string,
- at most `MAX_RECORD_BYTES` (16 KiB) per record.

A projection that still exceeds the record budget raises. A value that cannot
be written as finite JSON (`NaN`, `Infinity`, a set) raises. A line a strict
parser cannot finish is worse than a visible failure, and writing it anyway
would hide the defect that produced it.

## Tracing and counting

`trace_fields` joins one correlation record to its retries, MCU frame ids,
evidence references and final verification verdict. It reads the record
structurally and raises when a field is missing, so a trace can never silently
omit what it was asked to correlate. One frame id is kept per attempt: a
repeated id is a real duplicate-frame observation, not noise to deduplicate.

`FailureCounter` stores one bounded series per `(class, component, run_id)` so a
metric is attributable to the run that produced it, and
`FailureCounter.as_document` publishes it under `observability-v1`.
`failure_projection` builds the versioned metric record for one classified
failure. `observation_failures` recomputes counts from recorded fields rather
than trusting a producer, so a wrong count in an artifact is detectable.

## Durable output

`JsonlSink` appends records under an exclusive advisory lock
(`workbench_task_utils.exclusive_file_lock`) and rotates only between complete
lines while holding it. A reader therefore never observes a half-written line
and no two writers interleave one. Rotated files are complete JSONL documents
and the number kept is bounded.

The sink scrubs every record again at the write. It is the last point where
untrusted text becomes a file on disk, so a caller that forgot to redact must
not be able to leak by forgetting. It also refuses to write a record that would
not parse back, instead of writing a partial one.

## Verifying locally

```bash
python -m pytest tests/unit/test_observability_contract.py -v
python -m pytest tests/unit -k log -v
make test
make context-check
```

The health and alert surfaces keep their own issue-scoped suites:
`tests/unit/test_monitoring.py`, `tests/unit/test_robot_alerts.py` and
`tests/unit/test_backend_health.py`.
