# World model algorithms

How the world model decides what is true, and when it decides it cannot tell.

This is the technical reference. Task breakdown and staffing live outside the
repository.

---

## Why this module carries the project's claim

The project asserts one thing: *a command having been sent is not the same as
the goal having been reached.*

Every other module can report "my part finished." This one has to report
"the goal was reached, here is the evidence" — or "I cannot tell, here is what
is missing."

The hard part is not the code. It is making **"I cannot tell" a supported
conclusion rather than an excuse**. That needs real algorithms: multi-hypothesis
tracking, conflict handling, belief decay, deterministic reconstruction.

---

## 1. Deterministic state reconstruction

The same event stream, replayed twice, must produce the same `state_hash`.
Three ways to get this wrong:

```python
# Wrong: dict ordering is not stable across runs
h = hashlib.sha256(json.dumps(state).encode())

# Wrong: float mantissa noise. 0.1 + 0.2 can differ across platforms
h = hash((entity.x, entity.y, entity.z))

# Wrong: a timestamp inside the hash
state["updated_at"] = datetime.now()
```

Correct:

```python
def state_hash(s: WorldState) -> str:
    canonical = {
        "run_id": s.run_id,
        "entities": sorted((e.id, quantize(e.pose), confidence_bucket(e.confidence)) for e in s.entities),
        "relations": sorted(s.relations),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def quantize(p: Pose, eps: float = 1e-6) -> tuple:
    """Pose quantisation.

    eps is NOT final. It must come from the measured pose jitter of a
    stationary object on real hardware (see section 6). Until then this is an
    estimate, and it is a config value rather than a literal for that reason.
    """
    return tuple(round(v / eps) * eps for v in (p.x, p.y, p.z, *p.quat))


def confidence_bucket(c: float, width: float = 0.05) -> int:
    """0.9001 and 0.9002 must not produce different hashes."""
    return int(c / width)
```

`confidence_bucket` is the one people skip. Raw confidence is a float; feeding
it in unbucketed makes the hash change every frame.

### Snapshots

A ten-minute task produces tens of thousands of events. Full replay is too slow
for a UI that replays on every request.

```
Snapshot every N events (start with N=500).
Replay request -> nearest snapshot -> apply the delta.

Required invariant:
    hash(restore(snapshot) + replay(delta)) == hash(replay(everything))
```

That assertion is the whole point. A snapshot that disagrees with full replay is
manufacturing state.

### Public snapshot projection and canonical bytes

The verifier continues to use the World Model's internal reducer WorldState.
create_world_state_snapshot() is a separate public projection over the same
validated, sequence-ordered stream; it does not replace reduce_events() or
expose verifier-only entity_attributes.

Before replay, the projection folds exact duplicate events and rejects mixed
runs, duplicate sequence ownership, malformed Observation entity_type values,
and conflicting types for one entity_id. No event is applied until this full
preflight succeeds.

Snapshot entities are ordered by entity_id. Relations are ordered by
(subject_id, predicate, object_id). Canonical in:<entity_id> and
on:<entity_id> locations become inside and on_top_of; any other non-empty
location fails projection instead of being guessed or omitted.
Relation evidence contains only observations supporting the current location:
same-location observations accumulate evidence, a location change replaces it,
and observations without a location never enter the relation evidence set.

Spatial verifiers use the same dedicated entity_location_evidence_refs as the
snapshot relation, never the generic entity_evidence_refs as a fallback. This
applies to both confirmation and refutation, including an unexpected extra
entity in a managed container. Missing location support means
insufficient_evidence / evidence_missing. Attribute and identity checks keep
their generic observation evidence; compound parcel success requires both
attribute evidence and independent location evidence.

Relation object IDs containing whitespace (including tabs, newlines and Unicode
whitespace) fail snapshot projection. IDs are not silently trimmed or
normalized; in:tray is canonical, in: tray is not. This is a World Model
projection rule, not a change to the public JSON Schema or a database migration.

canonical_world_state_bytes() serialises this exact hash material:

    {
      "run_id": "...",
      "sequence_no": 0,
      "entities": [],
      "relations": []
    }

It uses UTF-8 JSON, sorted object keys, compact separators, finite JSON numbers,
and deterministic collection order. state_hash is the lowercase SHA-256
hex digest of those bytes. state_hash, reduced_at, and clock_id are excluded
from the hash; schema-visible entity and relation fields are included.
reduced_at is the occurred_at value at the highest accepted sequence boundary,
never the greatest timestamp. An empty stream has no truthful reduced_at
boundary, so public snapshot creation fails closed while the existing
empty-stream reducer behavior remains unchanged.

### Causal order, not timestamp order

An action result can carry an *earlier* timestamp than the observation that
triggered it — different nodes, different clock offsets.

```python
events.sort(key=lambda e: e.timestamp)  # wrong
events.sort(key=lambda e: e.sequence_no)  # right
```

`sequence_no` establishes total order. Timestamps are diagnostic only and never
participate in a transition decision. This is a reducer invariant and needs a
test: build a stream whose timestamps run backwards while `sequence_no` runs
forwards, then assert the state is still correct.

| Field | Clock | Reason |
|---|---|---|
| `sequence_no` | none (monotonic int) | total order |
| `monotonic_ns` | monotonic | intervals, timeouts. NTP steps don't affect it |
| `wall_clock` | wall clock | human-readable logs, cross-machine alignment |
| anything in `state_hash` | **excluded** | time never enters the hash |

Timeout logic must use monotonic. With wall clock, a single NTP correction can
fire or suppress a watchdog.

---

## 2. Multi-hypothesis tracking

### Why a single estimate is not enough

A hand occludes the module. The last direct observation put it at A. After the
hand moves, it may already have been pushed to B.

A single-estimate model reports "at A" — stale information presented as fact.
That is where false completions come from.

A multi-hypothesis model keeps:

```
module_red: {
    pose_A:  0.60,   # last direct observation
    pose_B:  0.30,   # implied by the hand's trajectory
    unknown: 0.10
}
```

No hypothesis clears the threshold, so verification returns
`insufficient_evidence` and the planner re-observes.

**Without multi-hypothesis, `insufficient_evidence` can only fire on "never
observed at all"** — which misses the common case, "observed but not sure."

### Data association

Three identically coloured modules; a new frame shows three red blocks. Which is
which?

```python
from scipy.optimize import linear_sum_assignment


def associate(observations, tracks, w1=1.0, w2=0.5, w3=0.2):
    C = np.zeros((len(observations), len(tracks)))
    for i, obs in enumerate(observations):
        for j, tr in enumerate(tracks):
            C[i][j] = (
                w1 * pose_distance(obs.pose, tr.predicted_pose)
                + w2 * (1.0 - appearance_similarity(obs, tr))
                + w3 * time_penalty(obs.stamp, tr.last_seen)
            )

    row, col = linear_sum_assignment(C)

    matched, new = [], []
    for i, j in zip(row, col):
        if C[i][j] < COST_THRESHOLD:
            matched.append((observations[i], tracks[j]))
        else:
            new.append(observations[i])  # too expensive: new instance
    lost = [t for k, t in enumerate(tracks) if k not in col]
    return matched, new, lost
```

The algorithm is one library call. **The work is calibrating w1/w2/w3 and
`COST_THRESHOLD`** so they behave under occlusion, lighting change, fast motion,
and near-identical appearance. Use the annotated ground-truth set as the
validation set.

Picking those weights by intuition is the usual way this task fails.

### Pruning

Hypotheses multiply per frame per entity, so they need a ceiling.

```
Cap: <=5 hypotheses per entity.

Prune in order:
  1. drop anything below 0.05 confidence
  2. merge hypotheses closer together than the quantisation step
     (they are the same hypothesis)
  3. still over the cap -> keep the top 5, fold the rest into `unknown`
```

Step 3 must **add the pruned mass to `unknown`**, not discard it. Discarding
makes the total stop summing to 1, and then the verifier's threshold comparison
means nothing.

---

## 3. Belief decay

Decay too fast and a stationary object drops to unknown, so the system
re-observes forever and the task never finishes. Decay too slow and a moved
object's stale pose is treated as fact, which produces false completions.

Half-life has to vary by object kind:

```python
HALF_LIFE_S = {
    "tray": 600.0,  # fixture. Position does not change unless moved
    "table": 600.0,
    "module": 15.0,  # can be moved by the arm or a person
    "gripper_tip": 0.5,  # always moving; last frame is already stale
}


def decay(conf: float, elapsed_s: float, kind: str) -> float:
    hl = HALF_LIFE_S[kind]
    return conf * (0.5 ** (elapsed_s / hl))
```

That table is itself the deliverable — one global constant will not do.

The 0.5 s on `gripper_tip` matters most: give it the tray's half-life and the
verifier will use a several-second-old tip pose to decide whether something is
currently being held.

---

## 4. Conflicting evidence

Two observations put the same object in different places.

| Strategy | Fits | Fails when |
|---|---|---|
| Newest wins | the object really is moving | a false detection overwrites good data |
| Highest confidence wins | sensors differ in quality | a confident stale reading beats a fresh uncertain one |
| Primary sensor wins | there is a clear primary | the primary fails and there is no fallback |

### The choice here: do not resolve

```python
def resolve(obs_a, obs_b) -> Belief:
    if pose_distance(obs_a.pose, obs_b.pose) > CONFLICT_THRESHOLD:
        return Belief(
            status="conflicting_observations",
            hypotheses=[
                Hypothesis(obs_a.pose, obs_a.confidence, obs_a.evidence_refs),
                Hypothesis(obs_b.pose, obs_b.confidence, obs_b.evidence_refs),
            ],
            recovery_hint="reobserve",
        )
    return merge(obs_a, obs_b)  # merge only when they agree
```

Same reasoning as three-valued verification: **the system does not guess.**
Forcing a resolution *is* a guess, and a wrong one leaves no trace — the log
shows a confident conclusion with no sign it was picked from two contradictory
readings.

This is written down with its reasoning because the first instinct of whoever
maintains this next will be to add a rule that auto-selects one. That hides
uncertainty the system already knew about.

---

## 5. Verification

### Containment is three-valued, not boolean

Is a module whose centre sits on the tray rim "inside"?

```python
def containment(module_aabb, cavity_aabb) -> VerificationStatus:
    inter = intersect_volume(module_aabb, cavity_aabb)
    ratio = inter / volume(module_aabb)

    if ratio >= 0.95:
        return CONFIRMED  # fully contained
    if ratio <= 0.01:
        return REFUTED  # fully separate
    return INSUFFICIENT_EVIDENCE  # partial: caught on the rim
```

The middle band is the point. A boolean test forces "wedged on the tray edge"
into either success or failure, when what it actually means is *placed badly,
retry* — `recovery_hint = retry_action`.

This is also why the tray must be modelled as five parts with a real interior
cavity rather than a flat plate. A plate has no `cavity_aabb` and this test has
nothing to work with.

### Threshold calibration does not optimise accuracy

Take the annotated ground-truth frames, plot ROC, and **pick the threshold where
false positives are zero.**

False positive = reported complete but was not = false completion = release
blocker.

The cost is more false negatives (actually complete, reported uncertain). **That
trade direction is the project's core claim** and must not be reversed to make
a completion-rate number look better.

```
Objective:   FP = 0, minimise FN subject to that
Not:         maximise (TP + TN) / total
```

### Failure taxonomy

Ten `reason_code` values, each mapping to a recovery action:

| `reason_code` | Meaning | `recovery_hint` |
|---|---|---|
| `target_not_observed` | never observed | `reobserve` |
| `target_lost` | was observed, now gone | `reobserve` |
| `belief_stale` | observation expired | `reobserve` |
| `confidence_below_threshold` | observed, not confident enough | `reobserve` |
| `conflicting_observations` | observations disagree | `reobserve` |
| `partial_containment` | caught on a boundary | `retry_action` |
| `precondition_unmet` | action precondition false | `replan` |
| `action_reported_failure` | the action layer reported failure | `retry_action` |
| `geometry_mismatch` | pose far from prediction | `replan` |
| `timeout_no_evidence` | timed out with no new evidence | `ask_confirm` |

The first five all map to `reobserve` but keep distinct codes. That is not
redundancy — they are counted separately in the metrics report, which is how you
learn where the system actually stalls.

---

## 6. Real-hardware noise sets the quantisation step

A real camera watching a stationary object reports a different pose every frame.
Measure the jitter before fixing the quantisation step in section 1.

```
1. Fix a module. Do not move it.
2. Capture 1000 frames.
3. Compute the per-axis standard deviation of the pose.
4. Quantisation step = 3σ (covers 99.7% of the jitter).
```

Then backfill `eps` in `quantize()` and **re-run the hash-consistency tests** —
the parameter changed, so that evidence has to be regenerated.

This dependency crosses from early simulation work into real-hardware work and
is the easiest link in the module to lose. The `quantize()` docstring says so
for that reason.

---

## Invariants

1. **Only this module decides task completion.** No other module may assert
   "the task succeeded."
2. **Missing evidence is never filled with a default.** Missing is missing;
   record a `reason_code`.
3. **Oracle fields never reach the reducer or the verifier.** Evaluation ground
   truth stays in the evaluation module. Using oracle data to compute a
   perception score voids the whole run.

---

## Metrics

| Metric | Target |
|---|---|
| `state_hash` consistency | 100% |
| **False completion** | **0** (release blocker) |
| Verification carries evidence refs | 100% |
| Fixed-task replay success | ≥95% |
| Event store throughput | >1000 events/s |
| Hypotheses per entity | ≤5 |
| Data association accuracy (validation set) | ≥90% |
