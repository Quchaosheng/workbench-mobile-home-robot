# Conflicting beliefs in the World Model

The contract promised `insufficient_evidence` when an entity was observed but is
no longer certain, and described bounded multi-hypothesis tracking. The reducer
collapsed each entity to one location, so a second contradictory observation
silently replaced the first and the verifier answered `refuted` as if nothing had
disagreed (issue #409).

## What changed

`WorldState` (internal reducer state) gains `entity_location_hypotheses`: a
bounded, ordered list of the locations an entity has actually been observed at,
newest first. `entity_locations` keeps its old meaning — the newest accepted
value — so a reader that does not know about hypotheses sees exactly what it saw
before.

The set is bounded to `MAX_LOCATION_HYPOTHESES` (2). Two is the smallest number
that can represent a conflict; keeping more would grow the state without telling
a reviewer anything extra. A list rather than a probability distribution: the
issue asks for a bounded set of incompatible beliefs **without inventing
calibrated probabilities**, and a number that looks calibrated but is not would
be worse than an honest set.

`verify_object_in_tray` now returns `insufficient_evidence` with the new
`EVIDENCE_CONFLICTING` reason code when the expected location is one of several
competing hypotheses. The claim names both readings, so a reviewer can see which
two samples disagree.

`EVIDENCE_CONFLICTING` is distinct from `EVIDENCE_MISSING`: one says two things
disagree, the other says nothing arrived.

## Migration note

Old snapshots cannot be relabelled silently, and this change does not try. A
snapshot written before this change has no `entity_location_hypotheses` field; it
loads as empty, which reads as "no recorded conflict" — the same conclusion the
old reducer reached, now stated rather than implied. Re-deriving a conflict from
an old snapshot would require replaying its source events, which is a separate
operation and is not performed on load.

## Out of scope

- Probabilities, confidence calibration or belief decay rates.
- Any change to the planner, Motion, MCU or control path.
- A schema-version bump of the public `WorldState` contract: the hypothesis set
  is internal reducer state, and the public projection is unchanged.
