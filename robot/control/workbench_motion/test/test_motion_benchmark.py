import pytest
from workbench_motion.motion_benchmark import FeedbackFaultView, persist_result, target_for_seed


def test_command_seed_reproducibility_and_small_single_joint_excursion():
    anchor = (0, -1, 1, 0, 0, 0)
    for seed in (0, 7, 42):
        target = target_for_seed(anchor, seed)
        assert target == target_for_seed(anchor, seed)
        assert target[1:] == anchor[1:]
        assert 0.03 <= abs(target[0]) <= 0.06
    assert target_for_seed(anchor, 0) != target_for_seed(anchor, 7)


def test_persisted_motion_completion_reopens_without_world_facts(tmp_path):
    pytest.importorskip(
        "workbench_contracts", reason="semantic persistence requires the project container dependencies"
    )
    result = persist_result(
        tmp_path,
        "run",
        "motion",
        completed=True,
        stopped=False,
        dispatched=True,
        started_at="2026-09-14T00:00:00Z",
        ended_at="2026-09-14T00:00:02Z",
    )
    assert result["reopened"]
    assert result["entity_locations"] == {}
    assert result["verified_success"] is None
    assert result["verification_status"] == "insufficient_independent_world_observation"


def test_fault_view_withholds_only_observation_and_preserves_transport():
    class Backend:
        send_count = 5

        def read_state(self):
            return "observation"

    view = FeedbackFaultView(Backend())
    assert view.read_state() == "observation"
    view.withhold = True
    assert view.read_state() is None
    assert view.send_count == 5


def test_measured_derivative_exceedance_cannot_be_hidden_by_execution_success():
    from test_motion_safety import LIMITS, NAMES
    from workbench_motion.motion_benchmark import measured_bounds

    metric = {
        "valid": True,
        "joint_names": NAMES,
        "acceleration_estimate_max_rad_s2": [1, 0],
        "jerk_estimate_max_rad_s3": [3, 0],
    }
    result = measured_bounds(metric, LIMITS)
    assert result["status"] == "exceeded"
    assert result["exceeded"] == ["j1:acceleration", "j1:jerk"]
    assert result["continuous_physical_bound_proven"] is False


def test_unrelated_fault_after_injection_is_not_feedback_loss_success():
    from workbench_motion.motion_benchmark import feedback_loss_confirmed

    events = [
        {"event": "fault_injection", "kind": "withhold_observation", "request_id": "one"},
        {"event": "fault", "reason": "backend_failure", "request_id": "one"},
    ]
    assert not feedback_loss_confirmed(events, "one")
    events[1]["reason"] = "stale_state"
    assert feedback_loss_confirmed(events, "one")
    assert not feedback_loss_confirmed(list(reversed(events)), "one")


@pytest.mark.parametrize("case", ["transport_exception", "wall_timeout"])
def test_trial_failure_is_persisted_and_returned_without_claiming_stopped(tmp_path, monkeypatch, case):
    import workbench_motion.motion_benchmark as module
    from test_motion_safety import CONTEXT, LIMITS, state
    from test_trajectory_executor import Transport
    from workbench_motion.motion_metrics import MotionJournal
    from workbench_motion.motion_safety import MotionPolicy

    class Backend(Transport):
        robot_id = "test"

        @property
        def send_count(self):
            return len(self.sent)

        def now_s(self):
            return 1

        def spin(self):
            raise RuntimeError("engine transport failed")

    backend = Backend()
    monkeypatch.setattr(module, "wait_state", lambda _: state())
    persisted = []

    def persist(*args, **kwargs):
        persisted.append(kwargs)
        return {"reference": "test-port-evidence"}

    monkeypatch.setattr(module, "persist_result", persist)
    if case == "wall_timeout":
        import itertools

        ticks = itertools.count(0, 100)
        monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    journal = MotionJournal(tmp_path / "motion.jsonl", "run")
    result = module.run_trial(
        backend,
        CONTEXT,
        LIMITS,
        MotionPolicy(),
        journal,
        request="one",
        target=(0.1, 0),
        mode="nominal",
        config_current=lambda: True,
    )
    journal.close()
    assert result["status"] == "FAIL"
    assert result["terminal_lifecycle"] == "faulted"
    assert persisted[0]["dispatched"] is True
    assert persisted[0]["completed"] is False
    assert persisted[0]["stopped"] is False
    assert backend.cancels == ["1"]
    assert "wall timeout" in result["error"] if case == "wall_timeout" else "engine transport" in result["error"]


def test_missing_evidence_dependencies_fail_before_runtime_or_output_creation(tmp_path, monkeypatch):
    import workbench_motion.motion_benchmark as module

    def missing(_):
        raise ImportError("contracts missing")

    monkeypatch.setattr(module.importlib, "import_module", missing)
    with pytest.raises(RuntimeError, match="project Python environment"):
        module.main(["--output", str(tmp_path / "run"), "--image-id", "test-image", "--source-revision", "test-source"])
    assert not (tmp_path / "run").exists()


def test_readiness_waits_for_discovery_but_never_accepts_permanently_unready_backend(monkeypatch):
    import itertools
    from types import SimpleNamespace

    import workbench_motion.motion_benchmark as module

    states = iter([False, False, True])
    backend = SimpleNamespace(spin=lambda: None, readiness=lambda: next(states))
    module.wait_ready(backend)
    ticks = itertools.count()
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    backend.readiness = lambda: False
    with pytest.raises(TimeoutError, match="readiness"):
        module.wait_ready(backend, timeout_s=3)


def test_benchmark_refuses_unverified_runtime_before_motion(tmp_path, monkeypatch):
    import json

    from workbench_motion.motion_benchmark import runtime_identity

    monkeypatch.delenv("WORKBENCH_MOTION_RUNTIME_MANIFEST", raising=False)
    with pytest.raises(RuntimeError, match="verified motion runtime"):
        runtime_identity()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": "motion-runtime-v1"}))
    monkeypatch.setenv("WORKBENCH_MOTION_RUNTIME_MANIFEST", str(manifest))
    with pytest.raises(ValueError, match="unverified"):
        runtime_identity()
