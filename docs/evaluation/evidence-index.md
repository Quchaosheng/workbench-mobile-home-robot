# Run evidence index

`collect_metrics.py` publishes aggregate numbers. A total alone is not evidence:
`run_count`, `vtcr` or `recovery_rate` without the run ids behind them cannot
answer which run supplied a recovery, and a hand-edited metrics JSON can point at
a different set of runs without anything noticing.

Every metrics record therefore carries a bounded per-run index, the run ids
behind each aggregate, and a digest a reviewer can recompute.

## What is published

| Field | Meaning |
|---|---|
| `runs[]` | one record per run, sorted by `run_id` |
| `metric_contributors` | per aggregate, the run ids that supplied its numerator |
| `metric_exclusions` | per aggregate, the reason and the run ids that could not contribute |
| `evidence_index_sha256` | digest over the aggregates, the contributor sets and the sorted run records |

Each `runs[]` record carries the per-run evidence rather than a copy of the run:

```json
{
  "run_id": "v0.2-A--normal-001",
  "source_class": "external",
  "task_family": "task-place-red-block",
  "event_log_sha256": "...",
  "manifest_sha256": "...",
  "final_outcome": "confirmed",
  "evidence_refs": ["frame://...", "motion-log://..."],
  "contributions": {
    "vtcr": {"numerator": 1, "denominator": 1},
    "recovery_rate": {"numerator": 1, "denominator": 1},
    "task_duration_p95_s": {"value": 52.0},
    "collision_count": {"count": 0}
  },
  "excluded": {"false_completion_count": "no human false-completion audit was supplied"}
}
```

`evidence_refs` is capped at eight references and holds reference strings only.
Frames, payloads and camera content stay in the run directory: the index names
evidence, it does not copy it.

`source_class` is `external`, `scripted` or `unknown`, taken from the per-event
`evaluation` metadata the log itself declares. The manifest and log hashes come
from the provenance entry when one exists, so the binding survives the metrics
file being separated from the run directory.

## Recomputing an aggregate

Each aggregate declares how it is rebuilt from the per-run contributions:

| Aggregation | Metrics | Rebuild |
|---|---|---|
| `sum` | `false_completion_count`, `collision_count`, `policy_violation_count` | add `count` |
| `ratio` | `vtcr`, `recovery_rate`, `evidence_coverage`, `state_hash_consistency`, `replay_success_rate`, `goal_condition_coverage`, `tool_call_validity`, `local_planning_coverage`, `observation_completeness`, `mean_observed_entities`, `complex_task_rate` | summed `numerator` / summed `denominator` |
| `sample` | `task_duration_p50_s`, `task_duration_p95_s` | the existing percentile over `value` |
| `distinct` | `task_family_count` | count distinct `value` |

```python
from collect_metrics import AGGREGATE_METRICS, aggregate_from_index, verify_evidence_index

verify_evidence_index(metrics)                      # refuses a stale or drifted index
aggregate_from_index("recovery_rate", metrics["runs"])   # the recomputation itself
```

`verify_evidence_index` is the gate: it raises when a published aggregate differs
from its per-run rebuild, when a contributor set names a run its records do not
support, or when the digest does not match the content. `collect()` calls it
before returning, so a metrics record that cannot explain its own totals is never
written.

## Zero-contributor states

An aggregate no run could evaluate is explicit, not invented. `false_completion_count`
is `null` without a human audit, `recovery_rate` is `null` without a recoverable
run, and a duration is `null` when no run measured one. Count metrics such as
`collision_count` publish `0` with an empty contributor set, which is a measured
zero rather than a missing measurement.

A run present in the directory is never silently dropped. When it cannot
contribute to a metric, `metric_exclusions[metric][reason]` names it.

## Digest

`evidence_index_sha256` is SHA-256 over the compact, sorted-key JSON of:

- `format` and `format_version`;
- every aggregate, excluding `runs`, `metric_contributors`, `metric_exclusions`,
  `evidence_index_sha256` and `run_dir`;
- `metric_contributors` and `metric_exclusions`;
- `runs[]`, sorted by `run_id`.

`run_dir` is excluded because it is a local path, not evidence: moving the same
run set to another machine must not invalidate a digest a reviewer already
checked. Reordering the input run files cannot change the digest; editing a run
id, a log hash, a manifest hash, a metric contribution or an aggregate does.

## Consumers

- `generate_report.py` refuses to render a report from a metrics file whose index
  does not rebuild, and shows a bounded run count plus the digest prefix per
  column instead of the run list. The full `runs[]` stays in the metrics JSON.
- `compare_evaluations.py` carries each version's digest, its VTCR contributor
  ids and a `vtcr_contributors` list, plus `evidence_indexes_match` and
  `every_version_has_an_evidence_index`. Its Wilson intervals and difference
  intervals are unchanged. `evidence_indexes_match` is `false` when the compared
  versions cover different run sets, which is the normal case for versions whose
  run ids carry the version label: it reports that the files are not the same
  evidence, not that either one is wrong.
- `check_evaluation_eligibility.py` reads the same logs through `load_runs` and
  the same eligibility predicate, so eligibility and the index cannot disagree.

Bounded means bounded: more than 256 runs in one collection is refused rather
than truncated, because a partial index cannot cover the run set it claims to
explain.

## Limits

- The index binds content, not authorship. `event_log_sha256` detects an edited
  log; it does not prove who produced it. That remains the provenance record's job.
- `manifest_sha256` is `null` when neither provenance nor the declared scenario
  resolves a manifest. It is reported as unbound, never borrowed from another run.
- The index does not change what any aggregate means. VTCR, false completion and
  recovery keep their definitions; only attribution is added.
