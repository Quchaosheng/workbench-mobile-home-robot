"""Explicit real-Gazebo motion experiment; never substitutes simulated fixtures.

Run with the project container's current-source overlay and sim_control launch.
The seed controls target generation, not Gazebo's scheduling or physics noise.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import random
import time
from datetime import UTC, datetime
from pathlib import Path

from workbench_motion.arm_config import load_arm_config
from workbench_motion.gazebo_adapter import GazeboAdapter
from workbench_motion.joint_limits import build_preflight_context
from workbench_motion.motion_metrics import MotionJournal, evaluate_samples
from workbench_motion.motion_safety import load_motion_config, point_to_point
from workbench_motion.motion_types import CommandMode, ControllerLifecycle, ReceiptStatus, RobotCommand
from workbench_motion.trajectory_executor import TrajectoryExecutor, digest

ACTIVE = {ControllerLifecycle.EXECUTING, ControllerLifecycle.STOPPING}


def target_for_seed(anchor: tuple[float, ...], seed: int) -> tuple[float, ...]:
    """Small shoulder-pan excursion with deterministic command generation."""
    rng = random.Random(seed)
    offset = rng.choice((-1, 1)) * rng.uniform(0.03, 0.06)
    return (anchor[0] + offset, *anchor[1:])


def persist_result(
    directory: Path,
    run_id: str,
    request: str,
    *,
    completed: bool,
    stopped: bool,
    dispatched: bool,
    started_at: str,
    ended_at: str,
) -> dict:
    # Reuse the existing semantic event boundary; samples are artifact refs.
    from workbench_contracts import ActionOutcome, ActionResult, ClockId, DeviceState, DispatchState
    from workbench_world_model.event_store import SQLiteEventStore
    from workbench_world_model.motion_evidence_adapter import MotionEvidenceAdapter
    from workbench_world_model.reducer import reduce_events

    from workbench_motion.evidence import ExecutionEvent

    result = ActionResult(
        result_id=f"{request}-result",
        action_id=request,
        run_id=run_id,
        outcome=ActionOutcome.SAFE_STOP if stopped else ActionOutcome.COMPLETED if completed else ActionOutcome.FAILED,
        dispatch_state=DispatchState.SENT if dispatched else DispatchState.NOT_SENT,
        device_state=DeviceState.STOPPED
        if stopped
        else DeviceState.CONFIRMED
        if completed
        else DeviceState.UNCONFIRMED,
        started_at=started_at,
        ended_at=ended_at,
        clock_id=ClockId.WALL,
        evidence_refs=[f"motion-journal:{directory.name}/motion.jsonl#{request}"],
    )
    store = SQLiteEventStore(directory / "events.sqlite")
    try:
        reference = MotionEvidenceAdapter(store).append(
            ExecutionEvent(
                event_type="action_result", run_id=run_id, action_id=request, payload=result.model_dump(mode="json")
            )
        )
    finally:
        store.close()
    reopened = SQLiteEventStore(directory / "events.sqlite")
    try:
        events = reopened.list_run(run_id)
        resolved = MotionEvidenceAdapter(reopened).resolve(reference)
        state = reduce_events(run_id, events)
        if resolved is None or state.entity_locations or state.entity_evidence_refs:
            raise RuntimeError("motion replay unexpectedly created semantic facts or lost evidence")
        return {
            "reference": reference,
            "reopened": True,
            "events": len(events),
            "entity_locations": state.entity_locations,
            "verified_success": None,
            "verification_status": "insufficient_independent_world_observation",
        }
    finally:
        reopened.close()


def feedback_loss_confirmed(events, request):
    injected = False
    for event in events:
        if event.get("request_id") != request:
            continue
        if event["event"] == "fault_injection" and event.get("kind") == "withhold_observation":
            injected = True
        if event["event"] == "fault" and injected:
            return event.get("reason") == "stale_state"
    return False


def measured_bounds(metric, limits):
    """Report measured derivative excursions independently of execution success."""
    if not metric.get("valid") or metric.get("jerk_estimate_max_rad_s3") is None:
        return {"status": "insufficient_samples"}
    exceeded = []
    for index, name in enumerate(metric["joint_names"]):
        if metric["acceleration_estimate_max_rad_s2"][index] > limits[name].max_acceleration:
            exceeded.append(f"{name}:acceleration")
        if metric["jerk_estimate_max_rad_s3"][index] > limits[name].max_jerk:
            exceeded.append(f"{name}:jerk")
    return {
        "status": "exceeded" if exceeded else "within_observed_bounds",
        "exceeded": exceeded,
        "continuous_physical_bound_proven": False,
    }


class FeedbackFaultView:
    """Labeled observation withholding over a live engine, for watchdog tests."""

    def __init__(self, backend):
        self.backend = backend
        self.withhold = False

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def read_state(self):
        return None if self.withhold else self.backend.read_state()


def wait_state(backend, timeout_s=10):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        backend.spin()
        state = backend.read_state()
        if state and state.accelerations is not None and 0 <= backend.now_s() - state.observed_at_s <= 0.2:
            return state
    raise TimeoutError("fresh q/v/acceleration feedback unavailable")


def wait_ready(backend, timeout_s=10):
    """Allow bounded DDS discovery before the first command; never bypass gates."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        backend.spin()
        if backend.readiness():
            return
    raise TimeoutError("Gazebo execution readiness unavailable")


def runtime_identity():
    path = os.environ.get("WORKBENCH_MOTION_RUNTIME_MANIFEST")
    if not path:
        raise RuntimeError("source the verified motion runtime setup.bash before benchmarking")
    raw = Path(path).read_bytes()
    manifest = json.loads(raw)
    if (
        manifest.get("schema_version") != "motion-runtime-v1"
        or manifest.get("regression_exit_codes") != {"baseline": 1, "patched": 0}
        or manifest.get("simulation_clock") != "supplied_update_time"
    ):
        raise ValueError("unverified motion runtime manifest")
    return {"manifest_sha256": hashlib.sha256(raw).hexdigest(), **manifest}


def run_trial(backend, context, limits, policy, journal, *, request, target, mode, config_current):
    transport = FeedbackFaultView(backend)
    controller = TrajectoryExecutor(
        backend.robot_id, transport, context, limits, policy, emit=journal.append, configuration_current=config_current
    )
    state = wait_state(backend)
    start_event = len(journal.events)
    start_wall = time.monotonic()
    start_sim = backend.now_s()
    started_at = datetime.now(UTC).isoformat()
    sent_before = backend.send_count
    intervention = False
    completed = False
    try:
        trajectory = point_to_point(state, target, context, limits)
        receipt = controller.submit_trajectory(request, trajectory, now_s=backend.now_s())
        duplicate_before = backend.send_count
        duplicate = controller.submit_trajectory(request, trajectory, now_s=backend.now_s())
        duplicate_rejected = duplicate.reason.value == "duplicate_request" and backend.send_count == duplicate_before
        if receipt.status is ReceiptStatus.ACCEPTED and not duplicate_rejected:
            raise RuntimeError("duplicate request was not rejected with zero dispatch")
        deadline = time.monotonic() + 30
        while controller.lifecycle in ACTIVE and time.monotonic() < deadline:
            backend.spin()
            controller.poll(now_s=backend.now_s())
            elapsed, duration = controller.execution_progress(now_s=backend.now_s())
            if (
                not intervention
                and mode != "nominal"
                and elapsed >= duration * 0.35
                and controller.lifecycle is ControllerLifecycle.EXECUTING
            ):
                intervention = True
                if mode == "feedback_loss":
                    journal.append(
                        {
                            "event": "fault_injection",
                            "kind": "withhold_observation",
                            "request_id": request,
                            "sim_time_s": backend.now_s(),
                            "engine": "real_gazebo",
                        }
                    )
                    transport.withhold = True
                else:
                    method = controller.stop if mode == "stop" else controller.hold
                    receipt = method(request + "-" + mode, now_s=backend.now_s())
        if controller.lifecycle in ACTIVE:
            raise TimeoutError("trial wall timeout")
        terminal = controller.lifecycle
        stopped = terminal in {ControllerLifecycle.STOPPED, ControllerLifecycle.HOLDING}
        completed = terminal is ControllerLifecycle.READY and receipt.status is ReceiptStatus.ACCEPTED
        observed_fault = (
            mode == "feedback_loss"
            and intervention
            and terminal is ControllerLifecycle.FAULTED
            and feedback_loss_confirmed(journal.events[start_event:], request)
        )
        reset_ok = None
        if stopped or observed_fault:
            transport.withhold = False
            # Observe actual rest after the fault/cancel; reset is never inferred
            # from cancellation or timeout. This loop includes a measured dwell.
            stationary_since = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                backend.spin()
                state = backend.read_state()
                if state and max(map(abs, state.velocities)) <= policy.stopped_velocity_rad_s:
                    if stationary_since is None:
                        stationary_since = state.observed_at_s
                    if state.observed_at_s - stationary_since >= policy.stopped_dwell_s:
                        reset_ok = (
                            controller.reset(request + "-reset", now_s=backend.now_s()).status is ReceiptStatus.ACCEPTED
                        )
                        break
                else:
                    stationary_since = None
        rows = journal.events[start_event:]
        segments = sorted({r["request_id"] for r in rows if r["event"] == "sample"})
        metrics = {name: evaluate_samples([r for r in rows if r.get("request_id") == name]) for name in segments}
        # Include the sample pair across preemption; separate request segments
        # alone could omit a discontinuity exactly at the replacement boundary.
        metrics[request + ":whole_trial"] = evaluate_samples(rows)
        evidence = persist_result(
            journal.path.parent,
            journal.run_id,
            request,
            completed=completed,
            stopped=stopped,
            dispatched=backend.send_count > sent_before,
            started_at=started_at,
            ended_at=datetime.now(UTC).isoformat(),
        )
        passed = completed if mode == "nominal" else (stopped or observed_fault) and reset_ok is True
        return {
            "request_id": request,
            "mode": mode,
            "status": "PASS" if passed else "FAIL",
            "terminal_lifecycle": terminal.value,
            "reset_accepted": reset_ok,
            "dispatch_count": backend.send_count - sent_before,
            "metrics": metrics,
            "duplicate_rejected_without_dispatch": duplicate_rejected,
            "measured_derivative_bounds": {name: measured_bounds(metric, limits) for name, metric in metrics.items()},
            "evidence": evidence,
            "real_time_factor": (backend.now_s() - start_sim) / (time.monotonic() - start_wall),
        }
    except Exception as error:  # noqa: BLE001 - persist failures as well as successful trials
        cleanup_error = None
        try:
            controller.abort(f"experiment failed: {error}")
        except Exception as cleanup:  # noqa: BLE001 - preserve both original and containment failures
            cleanup_error = str(cleanup)
        evidence = persist_result(
            journal.path.parent,
            journal.run_id,
            request,
            completed=False,
            stopped=False,
            dispatched=backend.send_count > sent_before,
            started_at=started_at,
            ended_at=datetime.now(UTC).isoformat(),
        )
        return {
            "request_id": request,
            "mode": mode,
            "status": "FAIL",
            "error": str(error),
            "cleanup_error": cleanup_error,
            "terminal_lifecycle": controller.lifecycle.value,
            "dispatch_count": backend.send_count - sent_before,
            "metrics": {},
            "evidence": evidence,
        }
    finally:
        journal.release_events()


def rejection_probes(backend, context, limits, policy, journal):
    state = wait_state(backend)
    controller = TrajectoryExecutor(backend.robot_id, backend, context, limits, policy, emit=journal.append)
    results = {}
    cases = {
        "velocity": (CommandMode.VELOCITY, state.positions, backend.now_s()),
        "excessive": (
            CommandMode.POSITION,
            (limits[state.joint_names[0]].max_position + 1, *state.positions[1:]),
            backend.now_s(),
        ),
        "stale": (CommandMode.POSITION, state.positions, backend.now_s() - 2),
    }
    for name, (mode, values, issued) in cases.items():
        before = backend.send_count
        receipt = controller.submit_command(
            RobotCommand(name, backend.robot_id, state.joint_names, mode, values, issued, "ros_sim"),
            now_s=backend.now_s(),
        )
        results[name] = {
            "rejected": receipt.status is ReceiptStatus.REJECTED,
            "dispatch_count": backend.send_count - before,
            "reason": receipt.reason.value,
        }
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new experiment directory (never overwritten)")
    parser.add_argument("--seeds", type=int, nargs="+", default=[0, 7, 42])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--image-id", required=True, help="immutable Docker image ID recorded by operator")
    parser.add_argument("--source-revision", required=True, help="Git revision plus dirty state")
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["nominal", "stop", "hold", "feedback_loss"],
        default=["nominal", "stop", "hold", "feedback_loss"],
    )
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    # Check all evidence dependencies before creating a ROS client or moving.
    try:
        importlib.import_module("workbench_contracts")
        importlib.import_module("workbench_world_model.motion_evidence_adapter")
    except ImportError as error:
        raise RuntimeError(
            "motion_benchmark requires the project Python environment; use "
            "python3 -m workbench_motion.motion_benchmark or ros2 run --prefix python3"
        ) from error
    import rclpy
    from ament_index_python.packages import get_package_share_directory

    args.output.mkdir(parents=True, exist_ok=False)
    journal = MotionJournal(args.output / "motion.jsonl", args.output.name)
    report = {
        "schema_version": "motion-benchmark-v1",
        "engine": "real_gazebo",
        "status": "FAIL",
        "trials": [],
        "status_scope": "execution_lifecycle_and_rejection_gates; measured_derivative_bounds_reported_separately",
    }
    backend = None
    rclpy.init(args=[])
    try:
        share = Path(get_package_share_directory("workbench_motion"))
        context = build_preflight_context()
        config_path = share / "config" / "motion_control.yaml"
        config_bytes = config_path.read_bytes()
        policy, limits = load_motion_config(config_path, context)
        backend = GazeboAdapter(load_arm_config(), max_velocity=max(v.max_velocity for v in limits.values()))
        wait_state(backend)
        wait_ready(backend)
        report["identity"] = {
            "image_id_operator_supplied": args.image_id,
            "source_revision_operator_supplied": args.source_revision,
            "loaded_module_directory": str(Path(__file__).resolve().parent),
            "module_sha256": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")
            },
            "configuration_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "robot_description_sha256": digest(backend.probe.robot_description()),
            "context_sha256": context.context_sha256,
            "seed_scope": "command_generation_only",
            "physics_reset_between_trials": False,
            "motion_runtime": runtime_identity(),
        }
        journal.append({"event": "run_identity", **report["identity"]})
        report["rejection_probes"] = rejection_probes(backend, context, limits, policy, journal)
        anchor = wait_state(backend).positions
        zero = run_trial(
            backend,
            context,
            limits,
            policy,
            journal,
            request="zero-motion",
            target=anchor,
            mode="nominal",
            config_current=lambda: config_path.read_bytes() == config_bytes,
        )
        report["trials"].append(zero)
        if zero["status"] != "PASS":
            raise RuntimeError("zero-motion trial failed")
        for seed in args.seeds:
            target = target_for_seed(anchor, seed)
            for repeat in range(args.repeats):
                for mode in args.modes:
                    request = f"seed-{seed}-repeat-{repeat}-{mode}"
                    journal.append(
                        {"event": "trial", "request_id": request, "seed": seed, "repeat": repeat, "target": target}
                    )
                    trial = run_trial(
                        backend,
                        context,
                        limits,
                        policy,
                        journal,
                        request=request,
                        target=target,
                        mode=mode,
                        config_current=lambda: config_path.read_bytes() == config_bytes,
                    )
                    report["trials"].append(trial)
                    if trial["status"] != "PASS":
                        raise RuntimeError(f"trial failed: {request}")
                    # Return to the measured initial anchor through the same gate.
                    returned = run_trial(
                        backend,
                        context,
                        limits,
                        policy,
                        journal,
                        request=request + "-return",
                        target=anchor,
                        mode="nominal",
                        config_current=lambda: config_path.read_bytes() == config_bytes,
                    )
                    report["trials"].append(returned)
                    if returned["status"] != "PASS":
                        raise RuntimeError(f"return failed: {request}")
        report["status"] = (
            "PASS"
            if all(v["rejected"] and v["dispatch_count"] == 0 for v in report["rejection_probes"].values())
            else "FAIL"
        )
    except Exception as error:  # noqa: BLE001 - retain failed real-run evidence and nonzero exit
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if backend:
            try:
                backend.close()
            except Exception as error:  # noqa: BLE001 - cleanup uncertainty must survive in the report
                report["status"] = "FAIL"
                report["cleanup_error"] = str(error)
                report["device_state"] = "unconfirmed"
        journal.close()
        (args.output / "summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        rclpy.shutdown()
    print(json.dumps({"status": report["status"], "output": str(args.output), "error": report.get("error")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
