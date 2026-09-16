from dataclasses import replace

import pytest
from test_motion_safety import CONTEXT, LIMITS, state
from workbench_motion.motion_safety import MotionPolicy, point_to_point, sample_trajectory
from workbench_motion.motion_types import CommandMode, ControllerLifecycle, ReceiptStatus, RobotCommand
from workbench_motion.trajectory_executor import TrajectoryExecutor


class Transport:
    """Transport test double, not a simulator or physics success fixture."""

    def __init__(self):
        self.state = state()
        self.clock = 1.0
        self.scene = "scene-1"
        self.ready = True
        self.collision_free = True
        self.sent = []
        self.epochs = []
        self.cancels = []
        self.result = None
        self.send_error = None
        self.accepted = True
        self.on_collision = lambda: None

    def now_s(self):
        return self.clock

    def read_state(self):
        return self.state

    def readiness(self):
        return self.ready

    def scene_revision(self):
        return self.scene

    def check_path(self, trajectory, resolution):
        self.on_collision()
        return self.collision_free

    def send(self, trajectory, *, start_time_s=None):
        self.sent.append(trajectory)
        self.epochs.append(start_time_s)
        if self.send_error:
            raise self.send_error
        return str(len(self.sent)) if self.accepted else None

    def status(self, handle):
        return self.result

    def cancel(self, handle):
        self.cancels.append(handle)


def setup():
    io = Transport()
    events = []
    controller = TrajectoryExecutor("test", io, CONTEXT, LIMITS, MotionPolicy(), emit=events.append)
    return io, controller, events


def submit(controller, request="one"):
    trajectory = point_to_point(state(), (0.1, 0.0), CONTEXT, LIMITS)
    return controller.submit_trajectory(request, trajectory, now_s=1.0, wall_s=10.0)


def test_success_needs_stationary_feedback_and_dwell_not_only_action_result():
    io, controller, events = setup()
    assert submit(controller).status is ReceiptStatus.ACCEPTED
    io.result = "succeeded"
    duration = io.sent[0].snapshot.points[-1].time_from_start_ns / 1e9
    now = 1 + duration
    io.state = replace(state(q=(0.1, 0.0)), observed_at_s=now)
    controller.poll(now_s=now, wall_s=10.1)
    assert controller.lifecycle is ControllerLifecycle.EXECUTING
    io.state = replace(io.state, observed_at_s=now + 0.21)
    controller.poll(now_s=now + 0.21, wall_s=10.31)
    assert controller.lifecycle is ControllerLifecycle.READY
    assert events[-1]["device_state"] == "confirmed"


@pytest.mark.parametrize("case", ["stale", "future", "moving", "scene", "collision", "not_ready", "config"])
def test_rejected_start_has_zero_dispatch(case):
    io, controller, _events = setup()
    if case == "stale":
        io.state = replace(io.state, observed_at_s=0.0)
    if case == "future":
        io.state = replace(io.state, observed_at_s=2.0)
    if case == "moving":
        io.state = replace(io.state, velocities=(0.1, 0.0))
    if case == "scene":
        io.on_collision = lambda: setattr(io, "scene", "scene-2")
    if case == "collision":
        io.collision_free = False
    if case == "not_ready":
        io.ready = False
    if case == "config":
        controller.configuration_current = lambda: False
    assert submit(controller).status is ReceiptStatus.REJECTED
    assert not io.sent


def test_normal_commands_cannot_preempt_and_duplicates_stay_rejected_after_reset():
    io, controller, _events = setup()
    assert submit(controller).status is ReceiptStatus.ACCEPTED
    assert submit(controller, "two").status is ReceiptStatus.REJECTED
    assert len(io.sent) == 1
    assert submit(controller).status is ReceiptStatus.REJECTED


def test_stale_feedback_latches_fault_without_claiming_stopped():
    io, controller, events = setup()
    submit(controller)
    controller.poll(now_s=2.0, wall_s=11.0)
    assert controller.lifecycle is ControllerLifecycle.FAULTED
    assert io.cancels == ["1"]
    assert events[-1]["device_state"] == "unconfirmed"
    assert controller.reset("reset", now_s=2).status is ReceiptStatus.REJECTED


def test_paused_sim_clock_is_detected_by_wall_watchdog():
    _io, controller, _events = setup()
    submit(controller)
    controller.poll(now_s=1.0, wall_s=10.6)
    assert controller.lifecycle is ControllerLifecycle.FAULTED


def test_stop_preserves_reference_and_requires_stationary_confirmation():
    io, controller, events = setup()
    submit(controller)
    receipt = controller.stop("stop", now_s=1, wall_s=10)
    assert receipt.status is ReceiptStatus.ACCEPTED
    assert len(io.sent) == 2
    assert io.sent[-1].snapshot.points[0] == io.sent[0].snapshot.points[0]
    assert controller.lifecycle is ControllerLifecycle.STOPPING
    assert not any(e.get("device_state") == "stopped" for e in events)


def test_stop_with_velocity_discontinuity_faults_and_cannot_claim_safe_stop():
    io, controller, _events = setup()
    submit(controller)
    io.state = replace(state(v=(0.1, 0.0)), accelerations=None)
    assert controller.stop("stop", now_s=1, wall_s=10).status is ReceiptStatus.REJECTED
    assert controller.lifecycle is ControllerLifecycle.FAULTED
    assert len(io.sent) == 1


def test_backend_rejection_and_unknown_send_have_different_fault_evidence():
    io, controller, events = setup()
    io.accepted = False
    receipt = submit(controller)
    assert receipt.dispatch_attempted
    assert receipt.status is ReceiptStatus.REJECTED
    assert controller.lifecycle is ControllerLifecycle.FAULTED
    io, controller, events = setup()
    io.send_error = TimeoutError("lost acceptance")
    assert submit(controller).dispatch_attempted
    assert controller.lifecycle is ControllerLifecycle.FAULTED
    assert events[-1]["device_state"] == "unconfirmed"


def test_velocity_mode_is_explicitly_unsupported():
    io, controller, _events = setup()
    command = RobotCommand("velocity", "test", state().joint_names, CommandMode.VELOCITY, (0.1, 0), 1)
    receipt = controller.submit_command(command, now_s=1)
    assert receipt.status is ReceiptStatus.REJECTED
    assert receipt.reason.value == "unsupported_mode"
    assert not io.sent


def test_failed_pre_dispatch_journal_write_never_sends():
    io, controller, _events = setup()

    def fail(event):
        raise OSError("disk full")

    controller.emit = fail
    with pytest.raises(OSError):
        submit(controller)
    assert not io.sent


def test_command_expiring_during_collision_check_has_zero_dispatch():
    io, controller, _ = setup()
    io.now_s = lambda: io.state.observed_at_s
    io.on_collision = lambda: setattr(io, "state", replace(io.state, observed_at_s=1.6))
    command = RobotCommand("aged", "test", state().joint_names, CommandMode.POSITION, (0.1, 0), 1)
    receipt = controller.submit_command(command, now_s=1)
    assert receipt.reason.value == "stale_command"
    assert not io.sent


def test_rejected_stop_receipt_matches_latched_fault():
    io, controller, _ = setup()
    submit(controller)
    io.ready = False
    receipt = controller.stop("stop", now_s=1)
    assert receipt.lifecycle_after is controller.lifecycle is ControllerLifecycle.FAULTED
    assert not receipt.dispatch_attempted
    assert io.cancels == ["1"]


@pytest.mark.parametrize("failure", ["missing", "moving", "drift", "paused"])
def test_hold_confirmation_is_revoked_when_feedback_no_longer_proves_rest(failure):
    io, controller, _ = setup()
    assert controller.hold("hold", now_s=1, wall_s=10).status is ReceiptStatus.ACCEPTED
    duration = io.sent[0].snapshot.points[-1].time_from_start_ns / 1e9
    io.result = "succeeded"
    now = 1 + duration
    for dt in (0, 0.21):
        io.state = replace(state(), observed_at_s=now + dt)
        controller.poll(now_s=now + dt, wall_s=10.1 + dt)
    assert controller.lifecycle is ControllerLifecycle.HOLDING
    now += 0.22
    if failure == "missing":
        io.state = None
        now += 1
    elif failure == "moving":
        io.state = replace(io.state, observed_at_s=now, velocities=(0.02, 0))
    elif failure == "drift":
        io.state = replace(io.state, observed_at_s=now, positions=(0.03, 0))
    controller.poll(now_s=now, wall_s=12)
    assert controller.lifecycle is ControllerLifecycle.FAULTED


@pytest.mark.parametrize("event_name", ["sample", "dispatched"])
def test_journal_failure_after_send_faults_and_attempts_cancellation(event_name):
    io, controller, _ = setup()

    def fail(event):
        if event["event"] == event_name:
            raise OSError("disk full")

    controller.emit = fail
    if event_name == "dispatched":
        with pytest.raises(OSError):
            submit(controller)
    else:
        submit(controller)
        controller.poll(now_s=1, wall_s=10)
    assert controller.lifecycle is ControllerLifecycle.FAULTED
    assert io.cancels == ["1"]


@pytest.mark.parametrize("drift, accepted", [(0.003, True), (0.006, False)])
def test_fresh_start_uses_bounded_tolerance(drift, accepted):
    io, controller, _ = setup()
    io.on_collision = lambda: setattr(io, "state", state(q=(drift, 0)))
    receipt = submit(controller)
    assert (receipt.status is ReceiptStatus.ACCEPTED) is accepted
    assert len(io.sent) == int(accepted)


def test_scene_change_during_execution_faults_and_reset_requires_terminal_goal():
    io, controller, _ = setup()
    submit(controller)
    io.scene = "changed"
    controller.poll(now_s=1, wall_s=10)
    assert controller.lifecycle is ControllerLifecycle.FAULTED
    assert controller.reset("reset", now_s=1).status is ReceiptStatus.REJECTED
    io.result = "canceled"
    assert controller.reset("reset", now_s=1).status is ReceiptStatus.ACCEPTED


def test_public_progress_and_abort_retain_unconfirmed_fault_evidence():
    io, controller, events = setup()
    submit(controller)
    elapsed, duration = controller.execution_progress(now_s=1.1)
    assert elapsed == pytest.approx(0.1)
    assert duration > 0
    controller.abort("runner exception")
    assert controller.lifecycle is ControllerLifecycle.FAULTED
    assert io.cancels == ["1"]
    assert events[-1]["device_state"] == "unconfirmed"


def test_new_feedback_cannot_hide_a_missed_wall_watchdog_deadline():
    io, controller, _ = setup()
    submit(controller)
    io.state = replace(io.state, observed_at_s=1.1)
    controller.poll(now_s=1.1, wall_s=10.6)
    assert controller.lifecycle is ControllerLifecycle.FAULTED


def test_reset_rechecks_feedback_after_blocking_readiness():
    io, controller, _ = setup()
    submit(controller)
    controller.abort("test fault")
    io.result = "canceled"

    def readiness():
        io.state = state(v=(0.1, 0))
        return True

    io.readiness = readiness
    assert controller.reset("reset", now_s=1).status is ReceiptStatus.REJECTED
    assert controller.lifecycle is ControllerLifecycle.FAULTED


def test_stop_preserves_active_reference_prefix_despite_measured_tracking_lag():
    io, controller, events = setup()
    submit(controller)
    original = io.sent[0]
    elapsed = original.snapshot.points[-1].time_from_start_ns / 1e9 * 0.35
    desired = sample_trajectory(original, elapsed)
    io.clock = 1 + elapsed
    io.state = replace(
        state(),
        observed_at_s=io.clock,
        positions=(desired.positions[0] - 0.0012, 0),
        velocities=desired.velocities,
        accelerations=desired.accelerations,
    )
    assert controller.stop("stop", now_s=io.clock, wall_s=10.1).status is ReceiptStatus.ACCEPTED
    replacement = io.sent[-1]
    for t in (0, elapsed * 0.5, elapsed, elapsed + 0.01):
        before, after = sample_trajectory(original, t), sample_trajectory(replacement, t)
        assert after.positions == pytest.approx(before.positions, abs=1e-12)
        assert after.velocities == pytest.approx(before.velocities, abs=1e-12)
        assert after.accelerations == pytest.approx(before.accelerations, abs=1e-12)
    assert io.epochs[0] == io.epochs[-1] == 1.0
    assert events[-2]["state"]["positions"] == io.state.positions


def test_braking_deadline_expiring_during_preflight_never_replaces_goal():
    io, controller, events = setup()
    submit(controller)

    def delay():
        io.clock = 1.3
        reference = sample_trajectory(io.sent[0], 0.3)
        io.state = replace(
            state(),
            observed_at_s=io.clock,
            positions=reference.positions,
            velocities=reference.velocities,
            accelerations=reference.accelerations,
        )

    io.on_collision = delay
    receipt = controller.stop("late-stop", now_s=1, wall_s=10)
    assert receipt.status is ReceiptStatus.REJECTED
    assert receipt.reason.value == "timeout"
    assert len(io.sent) == 1
    assert controller.lifecycle is ControllerLifecycle.FAULTED
    assert events[-1]["device_state"] == "unconfirmed"


def test_late_acceptance_cancels_new_goal_and_never_reports_success():
    io, controller, events = setup()
    original_send = io.send

    def delayed_send(trajectory, **kwargs):
        handle = original_send(trajectory, **kwargs)
        io.clock += 0.3
        return handle

    io.send = delayed_send
    receipt = submit(controller)
    assert receipt.status is ReceiptStatus.REJECTED
    assert receipt.dispatch_attempted
    assert io.cancels == ["1"]
    assert controller.lifecycle is ControllerLifecycle.FAULTED
    assert events[-1]["device_state"] == "unconfirmed"


def test_start_reserves_delivery_time_as_a_validated_constant_prefix():
    io, controller, _ = setup()
    submit(controller)
    assert io.epochs == [1.0]
    for t in (0, 0.1, 0.25):
        desired = sample_trajectory(io.sent[0], t)
        assert desired.positions == (0, 0)
        assert desired.velocities == (0, 0)
        assert desired.accelerations == (0, 0)


def test_blocking_dispatch_journal_cannot_send_after_continuity_deadline():
    io, controller, events = setup()

    def journal(event):
        events.append(event)
        if event["event"] == "dispatch_intent":
            io.clock += 0.3

    controller.emit = journal
    receipt = submit(controller)
    assert receipt.status is ReceiptStatus.REJECTED
    assert not receipt.dispatch_attempted
    assert not io.sent


def test_stop_cannot_adopt_a_scene_changed_since_active_dispatch():
    io, controller, _ = setup()
    submit(controller)
    io.scene = "changed-before-stop"
    receipt = controller.stop("stop", now_s=1, wall_s=10)
    assert receipt.status is ReceiptStatus.REJECTED
    assert receipt.reason.value == "scene_changed"
    assert len(io.sent) == 1
    assert controller.lifecycle is ControllerLifecycle.FAULTED
