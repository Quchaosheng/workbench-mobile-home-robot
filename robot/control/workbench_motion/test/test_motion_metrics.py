import json

import pytest
from workbench_motion.motion_metrics import MotionJournal, evaluate_samples


def sample(t, q=0.0, v=0.0, desired=0.0):
    return {
        "event": "sample",
        "state": {"observed_at_s": t, "joint_names": ["j"], "positions": [q], "velocities": [v]},
        "desired": {"positions": [desired], "velocities": [0.0]},
    }


def test_metrics_use_observed_nonuniform_time_and_distinguish_estimators():
    result = evaluate_samples([sample(0, q=1), sample(1, q=1), sample(3, q=3)])
    assert result["rms_position_error_rad"][0] == pytest.approx((11 / 3) ** 0.5)
    assert result["max_position_error_rad"] == [3]
    assert result["feedback_hz"] == pytest.approx(2 / 3)
    assert result["settling_time_s"] is None
    assert "measured_velocity" in result["derivative_source"]


def test_metrics_require_dwell_and_handle_duplicate_identical_feedback():
    rows = [sample(0), sample(0.1), sample(0.1)]
    assert evaluate_samples(rows)["settling_time_s"] is None
    assert evaluate_samples([*rows, sample(0.3)])["settling_time_s"] == 0
    with pytest.raises(ValueError):
        evaluate_samples([*rows, sample(0.1, q=0.2)])


def test_invalid_data_and_insufficient_measurements_never_make_zero_error_success():
    assert not evaluate_samples([])["valid"]
    assert not evaluate_samples([sample(0)])["valid"]
    with pytest.raises(ValueError):
        evaluate_samples([sample(1), sample(0)])
    with pytest.raises(ValueError):
        evaluate_samples([sample(0), sample(1, q=float("nan"))])


def test_journal_is_exclusive_strict_json_and_replayable(tmp_path):
    path = tmp_path / "motion.jsonl"
    journal = MotionJournal(path, "run")
    journal.append({"event": "dispatch_intent", "request_id": "request"})
    journal.append(sample(1))
    with pytest.raises(ValueError):
        journal.append({"value": float("nan")})
    journal.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows == journal.events
    assert [row["sequence"] for row in rows] == [1, 2]
    with pytest.raises(FileExistsError):
        MotionJournal(path, "run")


def test_finalized_trial_can_release_memory_without_resetting_sequence(tmp_path):
    import json

    journal = MotionJournal(tmp_path / "motion.jsonl", "run")
    journal.append({"event": "trial"})
    journal.release_events()
    assert journal.events == []
    journal.append({"event": "trial"})
    journal.close()
    assert [json.loads(line)["sequence"] for line in (tmp_path / "motion.jsonl").read_text().splitlines()] == [1, 2]
