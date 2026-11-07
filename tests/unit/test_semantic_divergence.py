"""Issue #413: compare two traces by step without merging their environment class."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools" / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "tools" / "scripts"))

import semantic_divergence as sd


def trace(environment: str, events: list[dict]) -> dict:
    return {"environment_class": environment, "events": events}


def event(event_id: str, sequence_no: int, **payload: object) -> dict:
    return {"event_id": event_id, "sequence_no": sequence_no, "payload": payload}


def test_identical_traces_diverged_nowhere() -> None:
    left = trace("SCRIPTED_FIXTURE", [event("e1", 0, x=1), event("e2", 1, y=2)])
    right = trace("SCRIPTED_FIXTURE", [event("e1", 0, x=1), event("e2", 1, y=2)])
    report = sd.compare_traces(left, right)
    assert report["divergent_step_count"] == 0
    assert report["steps_compared"] == 2
    assert report["comparable"] is True


def test_a_divergent_step_names_the_fields_that_differ() -> None:
    left = trace("SCRIPTED_FIXTURE", [event("e1", 0, x=1)])
    right = trace("SCRIPTED_FIXTURE", [event("e1", 0, x=2)])
    report = sd.compare_traces(left, right)
    assert report["divergent_step_count"] == 1
    step = report["divergent_steps"][0]
    assert step["step"] == "e1"
    assert step["fields"] == ["payload"]
    assert step["left"]["payload"] == {"x": 1}
    assert step["right"]["payload"] == {"x": 2}


def test_environment_classes_are_kept_apart() -> None:
    left = trace("SCRIPTED_FIXTURE", [event("e1", 0)])
    right = trace("GAZEBO", [event("e1", 0)])
    report = sd.compare_traces(left, right)
    assert report["comparable"] is False
    assert [family["environment_class"] for family in report["families"]] == ["SCRIPTED_FIXTURE", "GAZEBO"]


def test_a_trace_that_never_executed_is_not_reported_as_agreement() -> None:
    left = trace("PHYSICAL", [])
    right = trace("SCRIPTED_FIXTURE", [event("e1", 0)])
    report = sd.compare_traces(left, right)
    assert report["left_executed"] is False
    assert report["right_executed"] is True
    assert report["never_executed"] == ["PHYSICAL"]
    assert report["divergent_step_count"] == 0


def test_steps_present_in_only_one_trace_are_listed() -> None:
    left = trace("SCRIPTED_FIXTURE", [event("e1", 0), event("e2", 1)])
    right = trace("SCRIPTED_FIXTURE", [event("e1", 0), event("e3", 2)])
    report = sd.compare_traces(left, right)
    assert report["only_in_left"] == ["e2"]
    assert report["only_in_right"] == ["e3"]


def test_identity_fields_are_not_treated_as_divergence() -> None:
    left = trace(
        "SCRIPTED_FIXTURE", [{"event_id": "e1", "sequence_no": 0, "occurred_at": "2026-08-22T00:00:00Z", "payload": {}}]
    )
    right = trace(
        "SCRIPTED_FIXTURE", [{"event_id": "e1", "sequence_no": 0, "occurred_at": "2026-08-22T00:00:09Z", "payload": {}}]
    )
    report = sd.compare_traces(left, right)
    assert report["divergent_step_count"] == 0, "a timestamp is identity, not content"


def test_an_unlabelled_trace_is_refused() -> None:
    with pytest.raises(sd.DivergenceError, match="environment_class"):
        sd.compare_traces({"events": []}, trace("GAZEBO", []))


def test_the_report_is_bounded() -> None:
    many = [event(f"e{i}", i, x=i) for i in range(sd.MAX_REPORTED_STEPS + 10)]
    other = [event(f"e{i}", i, x=i + 1) for i in range(sd.MAX_REPORTED_STEPS + 10)]
    report = sd.compare_traces(trace("SCRIPTED_FIXTURE", many), trace("SCRIPTED_FIXTURE", other))
    assert report["divergent_step_count"] == len(many), report["divergent_step_count"]
    assert len(report["divergent_steps"]) == sd.MAX_REPORTED_STEPS
    assert report["truncated"] is True
