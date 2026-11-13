# pick_place

Put object A into container B.

This is the v0.1 demo task. The verifier checks spatial containment:
is the object inside the tray bounding box, with sufficient confidence?

Verifier: `services/world_model/workbench_world_model/verifier.py::verify_object_in_tray`.
This directory carries the task's description only; the containment check lives in
the world model, which owns state meaning and verification (AGENTS.md).
Scenarios: `sim/scenarios/frozen/normal-*.json`, `occlusion-*.json`, etc.
