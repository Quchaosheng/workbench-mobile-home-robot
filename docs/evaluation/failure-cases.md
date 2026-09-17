# Scripted failure and recovery cases

These are deterministic interface fixtures, not Gazebo or hardware results. Their purpose is to keep failure semantics, evidence requirements and dashboard behavior reviewable before integration exists.

## Occluded camera: evidence is insufficient

Run: `run-uncertain`<br>
Evidence: `apps/dashboard/data/run-uncertain.jsonl`

The motion adapter reports `succeeded`, but the last observation confidence is `0.41`. The verifier returns `insufficient_evidence`, lists `fresh_camera_frame` and `target_confidence_above_0.80` as missing, and recommends `re_observe`. The dashboard renders `uncertain`, never `failed` or `pleased`.

## First grasp fails: result is refuted

Run: `run-recovery`, sequence `3-4`<br>
Evidence: `apps/dashboard/data/run-recovery.jsonl`

The first grasp loses contact. The action result is `failed`; the verifier separately returns `refuted` with both a camera-frame and motion-log reference. Dispatch state is not treated as physical completion.

## Recovery succeeds: history remains visible

Run: `run-recovery`, sequence `5-9`<br>
Evidence: `apps/dashboard/data/run-recovery.jsonl`

The second attempt produces a new action result and fresh observation. Only then does the verifier emit `confirmed`. Replay retains the earlier refuted conclusion, so an operator can inspect both attempts rather than seeing a rewritten success-only history.

Formal D7 review still needs 36 Gazebo runs with real logs and an independent false-completion audit. `tools/scripts/run_evaluation.py --runner external` is the integration boundary; `--runner scripted` always writes `release_eligible: false`. Eligibility itself is recomputed from the logs and a provenance record rather than read from a summary; see [release eligibility](release-eligibility.md).
