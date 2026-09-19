# Backend boundary (owned by World Model in the 9-person plan)

P0 is SQLite plus a thin FastAPI read API. Do not create a second WorldState here. The initial `SQLiteEventStore` lives in `services/world_model/` until this service needs to be split by evidence.

## Multi-scenario run projection (Issue #303)

`GET /api/v1/runs` carries the read-only scenario identity of every run:

| Field | Meaning |
| --- | --- |
| `scenario_id`, `scenario_version` | Resolved through the committed legacy `task_id` mapping in `libs/kernel/workbench/kernel/scenario_migration.py`. |
| `scenario_label` | The registry identity, for example `kit-three-parts@0.2`. |
| `scenario_source` | `registry` when the mapping resolved, `unresolved` otherwise. |

An unmapped `task_id` reports `scenario_source: "unresolved"` with a null
`scenario_id`. The projection never guesses an identity, because a guessed
scenario would attribute a run to a manifest whose verifier never ran against it.

Two axes answer two different questions and are deliberately distinct:

| Axis | Values | Question it answers |
| --- | --- | --- |
| `outcome` | `confirmed`, `refuted`, `insufficient_evidence`, `none`, `running` | What did the last verifier say? `none` means no verifier ran. |
| `evidence` | `confirmed`, `refuted`, `insufficient_evidence`, `failed`, `not_executed`, `running` | What does the run's evidence chain show? |

`failed` is a refutation the run ended on without recovering; `not_executed` is a
run that ended with no verification event at all. A run that recovered from a
refutation keeps `refuted` on the evidence axis while its `outcome` reports the
final `confirmed` verdict, so the recovery stays visible instead of being
erased by the verdict that superseded it.

### Bounded filters and pagination

Passing any filter, `page` or `page_size` switches the response from the
committed two-key envelope to the bounded one:

```
GET /api/v1/runs?scenario_id=sort-parcels&evidence=refuted&page=1&page_size=20
```

```json
{
  "runs": [{"run_id": "...", "scenario_id": "sort-parcels", "evidence": "refuted"}],
  "total": 1,
  "unfiltered_count": 4,
  "page": 1,
  "page_size": 20,
  "max_page_size": 200,
  "facets": {"scenario_id": ["..."], "scenario_version": ["0.2"], "outcome": ["..."], "evidence": ["..."]},
  "read_only": true
}
```

`total` and `unfiltered_count` travel together so a page of a filtered view
cannot read as the whole run set. `facets` bounds each list to 64 sorted values,
so the Dashboard can only offer a filter value the run set actually contains.

A filter value outside `facets` is refused with `400 unknown_filter_value`
rather than ignored. A silently ignored filter returns the whole set and is
indistinguishable from a filter that matched everything, which is how a typo
becomes a wrong conclusion. An unusable `page` or `page_size` answers
`400 invalid_filter`; `page_size` is capped at 200.

### Evidence timeline

`GET /api/v1/runs/{run_id}/timeline` returns the run's events in committed
`sequence_no` order, each with exactly one phase: `execution`, `observation`,
`verification`, `recovery` or `context`. Anything that is not evidence is
`context`, so a task-accepted or tool-call event is never rendered as
verification of anything. The projection is bounded by the same
`MAX_EVENTS_PER_RUN` ceiling as the event stream, and `POST`, `PUT`, `PATCH` and
`DELETE` on both routes still answer `405 read_only`.
