# Parcel batch budget v1

Issue #440 bounds the parcel policy planner. This page records the ceiling, the
arithmetic behind it, and what a refusal means. It is a planning input limit, not
a robot capability claim and not execution or verifier authority.

## Why a ceiling exists

`build_policy_routed_parcel_plan()` accepted any number of unique parcel IDs.
Each parcel emits an observation, a grasp and a route step, and every grasp waits
on the whole scan while every route waits on its own grasp. Both the step count
and the dependency fan-out therefore grow with the batch, so an accepted batch
directly sized the in-memory and serialized `TaskGraph`.

Measured on this machine before the limit, with `identity` attributes and no
capacity snapshot:

| Parcels | Steps | Dependency links | JSON bytes |
| --- | --- | --- | --- |
| 100 | 300 | 10,199 | 0.28 MiB |
| 250 | 750 | 62,999 | 1.5 MiB |
| 1,000 | 3,000 | 1,001,999 | 21.7 MiB |

## The committed ceiling

| Constant | Value |
| --- | --- |
| `MAX_PARCEL_BATCH_PARCELS` | 64 |
| `MAX_PARCEL_BATCH_STEPS` | 192 |
| `MAX_PARCEL_BATCH_DEPENDENCIES` | 4,223 |

For `n` parcels the planner emits `3n` steps and `n*n + 2n - 1` dependency links:
`n` observations, then `n` grasps that each depend on all `n` observations plus
the previous route step, then `n` routes that depend on their own grasp. At the
ceiling that is 192 steps and 4,223 links, which serializes to 137 KiB.

64 is a reviewed workstation batch, not a measured throughput limit: it is eight
times the largest committed parcel scenario, whose evaluation profile configures
four pickup and four quarantine slots. It is deliberately not derived from this
benchmark host.

## Refusal

A batch above the ceiling raises `ParcelBatchLimitExceeded` with code
`parcel_batch_too_large`, raised before any `TaskStep` is constructed:

```text
parcel_batch_too_large: parcel batch has 1000 parcels, above the 64 parcel limit (192 steps and 4223 dependency links)
```

`build_parcel_sorting_plan()` enforces the same budget, so the limit cannot be
bypassed by calling the step producer directly. Below the limit the plan shape is
unchanged: the planner still emits `parcel-policy-v3` with the same step order,
routing decisions, capacity projections and dependency structure.

A limit that is unset must not mean unbounded. Removing either check is a
regression in graph size, not a configuration choice.
