"""Issue #414: one run's model route, recorded rather than inferred.

The policy declares tiers, fallbacks and cost recording; nothing bound those to
a run, so a model response could be read as a plan routed under the committed
policy. These tests pin the record and the refusals that keep it honest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "agent_runtime"))

from workbench_agent_runtime.model_route_evidence import (
    ModelRouteEvidence,
    ModelRouteEvidenceError,
    summarise,
)


def record(**overrides: object) -> ModelRouteEvidence:
    fields = {
        "run_id": "run-1",
        "policy_snapshot": "sha256:" + "a" * 64,
        "selected_route": "tier-1-local",
        "local_evidence": "complete",
    }
    fields.update(overrides)
    return ModelRouteEvidence(**fields)  # type: ignore[arg-type]


def test_a_record_names_selection_fallback_cost_and_evidence() -> None:
    payload = record(fallback_reason="timeout", latency_ms=150.5, actual_cost_micros=3, shadow_cost_micros=9).as_dict()
    assert payload["selected_route"] == "tier-1-local"
    assert payload["fallback_reason"] == "timeout"
    assert payload["fell_back"] is True
    assert payload["actual_cost_micros"] == 3.0
    assert payload["shadow_cost_micros"] == 9.0
    assert payload["local_evidence"] == "complete"
    assert payload["version"] == "model-route-evidence-v1"


def test_no_fallback_is_stated_not_implied() -> None:
    payload = record().as_dict()
    assert payload["fallback_reason"] == "none"
    assert payload["fell_back"] is False


def test_an_unknown_fallback_reason_is_refused() -> None:
    with pytest.raises(ModelRouteEvidenceError, match="fallback_reason"):
        record(fallback_reason="because")


def test_evidence_state_outside_the_vocabulary_is_refused() -> None:
    with pytest.raises(ModelRouteEvidenceError, match="local_evidence"):
        record(local_evidence="probably-fine")


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), 1e12])
def test_a_cost_that_cannot_be_trusted_is_refused(bad: float) -> None:
    with pytest.raises(ModelRouteEvidenceError):
        record(actual_cost_micros=bad)


def test_a_negative_latency_is_refused() -> None:
    with pytest.raises(ModelRouteEvidenceError, match="latency_ms"):
        record(latency_ms=-1)


def test_an_unbounded_label_is_refused() -> None:
    with pytest.raises(ModelRouteEvidenceError, match="selected_route"):
        record(selected_route="x" * 200)


def test_a_summary_reports_what_the_records_say() -> None:
    records = [
        record(run_id="run-1"),
        record(run_id="run-2", fallback_reason="unavailable"),
        record(run_id="run-3", fallback_reason="unavailable"),
    ]
    summary = summarise(records)
    assert summary["record_count"] == 3
    assert summary["fallback_count"] == 2
    assert summary["fallback_reasons"] == {"unavailable": 2}
    assert summary["local_evidence_complete"] is True
    assert summary["unrecorded"] is False


def test_a_run_with_no_records_is_unrecorded_not_compliant() -> None:
    summary = summarise([])
    assert summary["record_count"] == 0
    assert summary["unrecorded"] is True
    assert summary["local_evidence_complete"] is False


def test_one_partial_record_makes_the_summary_incomplete() -> None:
    summary = summarise([record(run_id="run-1"), record(run_id="run-2", local_evidence="partial")])
    assert summary["local_evidence_complete"] is False
