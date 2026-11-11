"""Issue #411: one bounded "what if" that never edits the run it asks about."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Appended, not prepended: `workbench` is a regular package under several of these
# trees, so taking precedence would decide which copy every later import sees.
for entry in (
    ROOT / "tools" / "scripts",
    ROOT / "libs" / "kernel",
    ROOT / "libs" / "application",
    ROOT / "libs" / "contracts",
    ROOT / "services" / "world_model",
    ROOT / "libs" / "hardware",
):
    if str(entry) not in sys.path:
        sys.path.append(str(entry))

import counterfactual_replay as cf

TRANSFORMS = cf.TRANSFORMS
CounterfactualError = cf.CounterfactualError


def apply_transform(*args: object, **kwargs: object) -> list:
    return cf.apply_transform(*args, **kwargs)


def replay_counterfactual(*args: object, **kwargs: object) -> dict:
    return cf.replay_counterfactual(*args, **kwargs)


ACCEPTED = {
    "event_id": "evt-0",
    "run_id": "run-1",
    "sequence_no": 0,
    "event_type": "task_accepted",
    "occurred_at": "2026-08-22T00:00:00Z",
    "payload": {"task_id": "task-1", "goal": "Place the red block in the tray"},
    "evidence_refs": [],
}
OBSERVED = {
    "event_id": "evt-1",
    "run_id": "run-1",
    "sequence_no": 1,
    "event_type": "observation",
    "occurred_at": "2026-08-22T00:00:01Z",
    "payload": {
        "observation_id": "obs-1",
        "entity_id": "red_block",
        "location": "on:table",
        "confidence": 0.9,
        "observed_at": "2026-08-22T00:00:01Z",
    },
    "evidence_refs": ["frame-0"],
}


def bundle() -> dict:
    return {"run_id": "run-1", "events": [copy.deepcopy(ACCEPTED), copy.deepcopy(OBSERVED)]}


def test_dropping_an_event_reports_what_changed() -> None:
    report = replay_counterfactual(bundle(), transform="drop-event", event_id="evt-1")
    assert report["applied"] is True
    assert report["events_before"] == 2
    assert report["events_after"] == 1
    assert "red_block" in report["changed_entities"]


def test_the_report_labels_itself_a_counterfactual_not_a_replay() -> None:
    report = replay_counterfactual(bundle(), transform="drop-event", event_id="evt-1")
    assert report["label"] == "counterfactual"
    assert report["observed_run"] is False


def test_the_input_bundle_is_never_modified() -> None:
    original = bundle()
    before = json.dumps(original, sort_keys=True)
    replay_counterfactual(original, transform="drop-event", event_id="evt-1")
    assert json.dumps(original, sort_keys=True) == before


def test_replace_field_changes_one_value() -> None:
    events = bundle()["events"]
    changed = apply_transform(
        events, transform="replace-field", event_id="evt-1", field="payload.location", value="in:tray"
    )
    assert changed[1]["payload"]["location"] == "in:tray"
    # And the original is untouched.
    assert events[1]["payload"]["location"] == "on:table"


def test_an_unknown_transform_is_refused() -> None:
    with pytest.raises(CounterfactualError, match="unknown transform"):
        replay_counterfactual(bundle(), transform="rewrite-history", event_id="evt-1")


def test_an_unknown_event_id_is_refused() -> None:
    with pytest.raises(CounterfactualError, match="no event in the run"):
        replay_counterfactual(bundle(), transform="drop-event", event_id="evt-9")


def test_missing_field_on_replace_is_refused() -> None:
    with pytest.raises(CounterfactualError, match="no field"):
        replay_counterfactual(bundle(), transform="replace-field", event_id="evt-1", field="payload.nothere", value=1)


def test_an_empty_bundle_is_refused() -> None:
    with pytest.raises(CounterfactualError, match="non-empty events"):
        replay_counterfactual({"run_id": "run-1", "events": []}, transform="drop-event", event_id="x")


def test_the_transform_set_is_closed() -> None:
    assert TRANSFORMS == ("drop-event", "replace-field")
