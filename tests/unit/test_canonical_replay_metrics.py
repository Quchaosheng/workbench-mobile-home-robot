"""Issue #207: replay metrics must come from the canonical WorldState.

Each test below states the bypass or the semantic difference that the previous
event-envelope digest could not see: a confidence change, a delivery
permutation, a mixed run, two events claiming one sequence number, a reducer
rejection, and a metric computed from a log that never produced a WorldState.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "libs" / "contracts"),
    str(ROOT / "services" / "world_model"),
    str(ROOT / "tools" / "scripts"),
]

from _jsonio import load_jsonl
from collect_metrics import (
    AGGREGATE_METRICS,
    STATE_EVENT_TYPES,
    aggregate_from_index,
    canonical_replay,
    collect,
    evidence_index_sha256,
    load_runs,
    verify_evidence_index,
)
from run_evaluation import scripted_events, write_jsonl
from workbench_contracts import WorldEvent
from workbench_world_model import create_world_state_snapshot

SCENARIO = json.loads((ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text(encoding="utf-8"))


def observation(
    *,
    event_id: str,
    sequence_no: int,
    confidence: float = 0.9,
    location: str = "on:table",
    run_id: str = "run-canonical",
) -> dict[str, object]:
    """One contract-shaped observation event, with the semantic knob exposed."""
    return {
        "event_id": event_id,
        "run_id": run_id,
        "sequence_no": sequence_no,
        "event_type": "observation",
        "occurred_at": f"2026-08-30T00:00:{sequence_no:02d}Z",
        "payload": {
            "observation_id": f"{run_id}-obs-{sequence_no}",
            "run_id": run_id,
            "entity_id": "red_block",
            "entity_type": "block",
            "location": location,
            "confidence": confidence,
        },
        "evidence_refs": [f"frame://{run_id}/{event_id}"],
    }


def verification(*, event_id: str, sequence_no: int, run_id: str = "run-canonical") -> dict[str, object]:
    """A verified claim so the metric pipeline has a terminal result to read."""
    reference = f"frame://{run_id}/{event_id}"
    return {
        "event_id": event_id,
        "run_id": run_id,
        "sequence_no": sequence_no,
        "event_type": "verification",
        "occurred_at": f"2026-08-30T00:00:{sequence_no:02d}Z",
        "payload": {
            "verification_id": f"{run_id}-verify",
            "task_id": "task-place-red-block",
            "claim": "red_block in:table",
            "status": "confirmed",
            "required_conditions": ["red_block in:table"],
            "evaluated_conditions": ["red_block in:table"],
            "satisfied_conditions": ["red_block in:table"],
            "evidence_refs": [reference],
        },
        "evidence_refs": [reference],
    }


def write_run(path: Path, events: list[dict[str, object]]) -> None:
    path.write_text("".join(f"{json.dumps(event)}\n" for event in events), encoding="utf-8")


def typed_events(events: list[dict[str, object]]):
    return [
        WorldEvent.model_validate_json(json.dumps(event, separators=(",", ":"), sort_keys=True)) for event in events
    ]


class TestCanonicalStateHash:
    """``state_hash_consistency`` is a comparison of canonical WorldState hashes."""

    def test_state_hash_matches_the_reducer_not_an_envelope_digest(self) -> None:
        events = [observation(event_id="obs-0", sequence_no=0), observation(event_id="obs-1", sequence_no=1)]

        forward, permuted = canonical_replay("run-canonical", events)

        snapshot = create_world_state_snapshot("run-canonical", typed_events(events))
        assert forward == snapshot.state_hash
        assert forward == permuted
        assert len(forward) == 64

    def test_permuted_delivery_matches_but_reordering_is_still_a_real_replay(self) -> None:
        events = [
            observation(event_id="obs-0", sequence_no=0, confidence=0.91),
            observation(event_id="obs-7", sequence_no=7, confidence=0.42, location="in:tray"),
        ]

        forward, permuted = canonical_replay("run-canonical", events)

        assert forward == permuted
        # A stream the reducer rejects must still fail rather than hash to the same value.
        with pytest.raises(RuntimeError, match="canonical WorldState replay failed"):
            canonical_replay("run-canonical", [observation(event_id="obs-0", sequence_no=0, location="beside:table")])

    def test_semantically_different_observation_produces_a_different_state_hash(self) -> None:
        baseline = [observation(event_id="obs-0", sequence_no=0, confidence=0.9, location="on:table")]
        moved = [observation(event_id="obs-0", sequence_no=0, confidence=0.9, location="in:tray")]
        faded = [observation(event_id="obs-0", sequence_no=0, confidence=0.4, location="on:table")]

        baseline_hash, _ = canonical_replay("run-canonical", baseline)

        assert canonical_replay("run-canonical", moved)[0] != baseline_hash
        assert canonical_replay("run-canonical", faded)[0] != baseline_hash

    def test_collect_reports_100_percent_consistency_from_canonical_hashes(self, tmp_path: Path) -> None:
        write_run(tmp_path / "normal-001.jsonl", scripted_events("v-test", SCENARIO, "abc123", 1000))

        metrics = collect(tmp_path)

        assert metrics["state_hash_consistency"] == 1.0
        assert metrics["replay_success_rate"] == 1.0


class TestFailClosedReplay:
    """Every ambiguous or unreplayable stream stops collection."""

    def test_mixed_run_ids_are_refused(self, tmp_path: Path) -> None:
        write_run(
            tmp_path / "run.jsonl",
            [
                observation(event_id="obs-0", sequence_no=0),
                observation(event_id="obs-1", sequence_no=1, run_id="other-run"),
            ],
        )

        with pytest.raises(RuntimeError, match="run_id drift"):
            collect(tmp_path)

    def test_duplicate_sequence_ownership_is_refused(self) -> None:
        events = [
            observation(event_id="obs-0", sequence_no=3),
            observation(event_id="obs-1", sequence_no=3),
        ]

        with pytest.raises(RuntimeError, match="sequence_no 3 is shared"):
            canonical_replay("run-canonical", events)

    def test_non_contract_envelope_field_is_refused(self, tmp_path: Path) -> None:
        record = observation(event_id="obs-0", sequence_no=0)
        record["unreviewed_extra"] = True
        write_run(tmp_path / "run.jsonl", [record])

        with pytest.raises(RuntimeError, match="outside the WorldEvent contract"):
            collect(tmp_path)

    def test_event_type_outside_the_state_contract_is_not_replayed(self, tmp_path: Path) -> None:
        record = observation(event_id="obs-0", sequence_no=0)
        record["event_type"] = "task_graph"
        write_run(tmp_path / "run.jsonl", [record])

        with pytest.raises(RuntimeError, match="no state-affecting events"):
            collect(tmp_path)

    def test_reducer_rejection_fails_closed(self, tmp_path: Path) -> None:
        write_run(tmp_path / "run.jsonl", [observation(event_id="obs-0", sequence_no=0, location="beside:table")])

        with pytest.raises(RuntimeError, match="cannot be represented"):
            collect(tmp_path)

    def test_malformed_json_is_refused_before_replay(self, tmp_path: Path) -> None:
        (tmp_path / "bad.jsonl").write_text("{not-json}\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="unreadable JSONL"):
            collect(tmp_path)

    def test_duplicate_run_ownership_is_refused(self, tmp_path: Path) -> None:
        values = [observation(event_id="obs-0", sequence_no=0)]
        write_run(tmp_path / "first.jsonl", values)
        write_run(tmp_path / "second.jsonl", values)

        with pytest.raises(RuntimeError, match="duplicate run_id"):
            collect(tmp_path)


class TestReplaySuccessMetric:
    """``replay_success_rate`` counts canonical replays, not contiguous numbering."""

    def test_a_contiguous_log_that_the_reducer_rejects_never_becomes_a_success(self, tmp_path: Path) -> None:
        # Contiguous numbering and matching run ids alone used to be the whole
        # definition of a valid replay. The reducer now decides, so a numbered
        # stream it refuses still aborts collection.
        write_run(
            tmp_path / "run.jsonl",
            [observation(event_id="obs-0", sequence_no=0, location="beside:table")],
        )

        with pytest.raises(RuntimeError, match="cannot be represented"):
            collect(tmp_path)

    def test_success_is_derived_from_canonical_hashes_not_from_numbering(self, tmp_path: Path) -> None:
        events = [
            observation(event_id="obs-0", sequence_no=0),
            observation(event_id="obs-1", sequence_no=1, confidence=0.8),
            verification(event_id="evt-2", sequence_no=2),
        ]
        write_run(tmp_path / "run.jsonl", events)

        forward, permuted = canonical_replay("run-canonical", load_runs(tmp_path)["run-canonical"])
        metrics = collect(tmp_path)

        assert forward == permuted
        assert metrics["replay_success_rate"] == 1.0
        assert metrics["state_hash_consistency"] == 1.0

    def test_one_unreplayable_run_removes_no_metric_from_the_report(self, tmp_path: Path) -> None:
        write_run(
            tmp_path / "good.jsonl",
            scripted_events("v-test", SCENARIO, "abc123", 1000),
        )
        bad = observation(event_id="obs-0", sequence_no=0, run_id="run-broken", location="beside:table")
        write_run(tmp_path / "broken.jsonl", [bad])

        with pytest.raises(RuntimeError, match="canonical WorldState replay failed"):
            collect(tmp_path)

    def test_scripted_logs_replay_through_the_canonical_reducer(self) -> None:
        # The fixture producer must emit records the World Model can actually
        # replay; otherwise the metric would be measuring a log the reducer can
        # never consume.
        events = scripted_events("v-test", SCENARIO, "abc123", 1000)
        run_id = events[0]["run_id"]
        state_events = [event for event in events if event["event_type"] in STATE_EVENT_TYPES]

        forward, permuted = canonical_replay(run_id, events)
        typed = [
            WorldEvent.model_validate_json(
                json.dumps({k: v for k, v in event.items() if k != "evaluation"}, separators=(",", ":"))
            )
            for event in state_events
        ]

        assert len(state_events) < len(events)  # task_graph stays out of state
        assert create_world_state_snapshot(run_id, typed).state_hash == forward == permuted

    def test_metrics_still_ingest_the_producer_log_shape(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "v-test"
        version_dir.mkdir()
        write_jsonl(version_dir / "normal-001.jsonl", scripted_events("v-test", SCENARIO, "abc123", 1000))

        runs = load_runs(version_dir)
        metrics = collect(version_dir)

        assert len(runs) == 1
        assert metrics["run_count"] == 1
        assert metrics["replay_success_rate"] == 1.0
        assert load_jsonl(version_dir / "normal-001.jsonl")[0]["evaluation"]["runner"] == "scripted"


class TestEvidenceIndex:
    """``runs[]`` is the per-run evidence behind every published aggregate.

    Each test states the question a reviewer could not answer from a total: which
    run ids produced a numerator, whether reordering the inputs changes the
    digest, whether editing a run changes it, and whether a run that could not
    contribute is reported instead of dropped.
    """

    def _metrics(self, tmp_path: Path, manifests: list[dict], *, audit: Path | None = None) -> dict:
        for manifest in manifests:
            write_jsonl(
                tmp_path / f"{manifest['scenario_id']}.jsonl",
                scripted_events("v-test", manifest, "abc123", 1000),
            )
        return collect(tmp_path, audit)

    def _manifest(self, relative: str) -> dict:
        return json.loads((ROOT / "sim" / "scenarios" / relative).read_text(encoding="utf-8"))

    def test_every_aggregate_names_its_contributing_run_ids(self, tmp_path: Path) -> None:
        metrics = self._metrics(tmp_path, [self._manifest("frozen/grasp-failure-001.json")])

        for metric in AGGREGATE_METRICS:
            assert metric in metrics["metric_contributors"]
            assert aggregate_from_index(metric, metrics["runs"]) == metrics[metric]

    def test_a_nonzero_recovery_names_the_run_that_recovered_it(self, tmp_path: Path) -> None:
        metrics = self._metrics(tmp_path, [self._manifest("frozen/grasp-failure-001.json")])

        assert metrics["recovery_rate"] == 1.0
        run_id = metrics["metric_contributors"]["recovery_rate"][0]
        record = next(run for run in metrics["runs"] if run["run_id"] == run_id)
        assert record["contributions"]["recovery_rate"] == {"numerator": 1, "denominator": 1}
        assert record["event_log_sha256"] and record["manifest_sha256"]

    def test_reordering_the_input_run_files_keeps_the_digest(self, tmp_path: Path) -> None:
        manifests = [
            self._manifest("frozen/normal-001.json"),
            self._manifest("frozen/grasp-failure-001.json"),
        ]
        forward = tmp_path / "forward"
        reverse = tmp_path / "reverse"
        forward.mkdir()
        reverse.mkdir()

        first = self._metrics(forward, manifests)
        for manifest in reversed(manifests):
            write_jsonl(
                reverse / f"{manifest['scenario_id']}.jsonl",
                scripted_events("v-test", manifest, "abc123", 1000),
            )
        second = collect(reverse)

        assert [run["run_id"] for run in first["runs"]] == sorted(run["run_id"] for run in first["runs"])
        assert first["evidence_index_sha256"] == second["evidence_index_sha256"]

    def test_editing_an_indexed_field_changes_the_digest(self, tmp_path: Path) -> None:
        metrics = self._metrics(tmp_path, [self._manifest("frozen/normal-001.json")])
        baseline = metrics["evidence_index_sha256"]

        def digest_after(**edits: object) -> str:
            edited = copy.deepcopy(metrics)
            edited["runs"][0].update(edits)
            return evidence_index_sha256(edited)

        for field, value in (
            ("run_id", "v-test--renamed"),
            ("event_log_sha256", "0" * 64),
            ("manifest_sha256", "1" * 64),
        ):
            assert digest_after(**{field: value}) != baseline, f"editing {field} must change the digest"

        contribution = copy.deepcopy(metrics)
        contribution["runs"][0]["contributions"]["vtcr"] = {"numerator": 0, "denominator": 1}
        assert evidence_index_sha256(contribution) != baseline

        aggregate = copy.deepcopy(metrics)
        aggregate["vtcr"] = 0.5
        assert evidence_index_sha256(aggregate) != baseline

        # The run directory is a local label, not evidence: moving the run set to
        # another machine must not invalidate a digest a reviewer already checked.
        moved = {**metrics, "run_dir": "/somewhere/else"}
        assert evidence_index_sha256(moved) == baseline

    def test_a_run_that_cannot_contribute_is_reported_as_excluded(self, tmp_path: Path) -> None:
        metrics = self._metrics(tmp_path, [self._manifest("frozen/normal-001.json")])

        assert metrics["false_completion_count"] is None
        assert metrics["metric_contributors"]["false_completion_count"] == []
        excluded = metrics["metric_exclusions"]["false_completion_count"]
        assert excluded["no human false-completion audit was supplied"] == ["v-test--normal-001"]
        assert metrics["runs"][0]["excluded"]["false_completion_count"]

    def test_a_count_metric_names_only_the_runs_that_contributed(self, tmp_path: Path) -> None:
        clean = self._metrics(tmp_path, [self._manifest("frozen/normal-001.json")])

        # A zero count is a measured zero, and no run supplied a collision, so the
        # contributor set is explicit and empty rather than an unexplained total.
        assert clean["collision_count"] == 0
        assert clean["metric_contributors"]["collision_count"] == []
        assert clean["metric_contributors"]["vtcr"] == ["v-test--normal-001"]

    def test_a_drifted_index_is_refused_instead_of_published(self, tmp_path: Path) -> None:
        metrics = self._metrics(tmp_path, [self._manifest("frozen/normal-001.json")])

        tampered = copy.deepcopy(metrics)
        tampered["vtcr"] = 0.0
        with pytest.raises(RuntimeError, match="rebuilds to"):
            verify_evidence_index(tampered)

        stale = copy.deepcopy(metrics)
        stale["evidence_index_sha256"] = "0" * 64
        with pytest.raises(RuntimeError, match="digest does not match"):
            verify_evidence_index(stale)

        indexless = {key: value for key, value in metrics.items() if key != "runs"}
        with pytest.raises(RuntimeError, match="without a per-run evidence index"):
            verify_evidence_index(indexless)
