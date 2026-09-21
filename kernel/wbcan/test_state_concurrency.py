#!/usr/bin/env python3
"""Bounded wbcan controller-state and queue concurrency probe."""

import argparse
import dataclasses
import errno
import json
import os
import platform
import queue
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

REPORT_SCHEMA_VERSION = "wbcan-stress-report-v2"
REPORT_RESULTS = {"PASS", "FAIL", "NOT_EXECUTED"}
REQUIRED_STAGES = (
    "reconfiguration",
    "drop_fault_accounting",
    "link_lifecycle",
    "stop_drain",
    "bus_off_restart",
    "restart_cancellation",
    "restart_stop_race",
    "queue_recovery",
    "stats_sampling",
    "repeated_tx_full",
    "slow_receiver",
    "multi_producer_saturation",
    "unload_reload",
    "cleanup",
)
MIN_PRODUCERS = 2
MAX_PRODUCERS = 8
MIN_FRAMES_PER_PRODUCER = 10
MAX_FRAMES_PER_PRODUCER = 5000
MIN_RELOAD_CYCLES = 1
MAX_RELOAD_CYCLES = 8
MIN_TX_FULL_ATTEMPTS = 1
MAX_TX_FULL_ATTEMPTS = 1000
MIN_SLOW_RECEIVER_FRAMES = 10
MAX_SLOW_RECEIVER_FRAMES = 5000
MAX_TOTAL_DURATION_MS = 120_000
MAX_STAGE_DURATION_MS = 30_000
MAX_STATS_SAMPLES = 100_000


@dataclasses.dataclass(frozen=True)
class StressProfile:
    """A reproducible, bounded workload definition.

    The release values are intentionally fixed.  They are the workload used
    by the privileged CI job and are not a latency or hard-real-time SLA.
    """

    name: str
    producer_count: int
    frames_per_producer: int
    tx_full_attempts: int
    slow_receiver_frames: int
    reload_cycles: int
    baseline_duration_ms: int
    max_duration_ms: int
    max_stage_duration_ms: int
    max_no_progress_ms: int
    baseline_runs: int
    budget_basis: str


PROFILES: dict[str, StressProfile] = {
    "developer-smoke": StressProfile(
        name="developer-smoke",
        producer_count=2,
        frames_per_producer=25,
        tx_full_attempts=4,
        slow_receiver_frames=100,
        reload_cycles=1,
        baseline_duration_ms=30_000,
        max_duration_ms=30_000,
        max_stage_duration_ms=8_000,
        max_no_progress_ms=2_000,
        baseline_runs=0,
        budget_basis="bounded local smoke profile; not release evidence",
    ),
    "release": StressProfile(
        name="release",
        producer_count=4,
        frames_per_producer=250,
        tx_full_attempts=16,
        slow_receiver_frames=500,
        reload_cycles=3,
        baseline_duration_ms=15_000,
        max_duration_ms=60_000,
        max_stage_duration_ms=12_000,
        max_no_progress_ms=4_000,
        baseline_runs=3,
        budget_basis=(
            "three repeated virtual CI baselines recorded in Issue #157 after "
            "the #274, #280, and #282 milestones, with bounded recovery headroom"
        ),
    ),
}

# These are the three previously successful privileged virtual-CAN runs used
# to derive the fixed release budget.  The first run predates a stress JSON
# artifact, so its duration is the conservative interval between the last
# timestamp-probe line and the stress completion line in the job log.  The
# latter two use the elapsed duration in their stress JSON artifacts, rounded
# up to a whole millisecond. Keeping the method in the artifact prevents these
# values from being mistaken for a physical or hard-real-time benchmark.
RELEASE_BASELINE_EVIDENCE: tuple[dict[str, int | str], ...] = (
    {
        "milestone": "#274",
        "run_id": "33034552431",
        "job_id": "98394315180",
        "commit": "56ef916f9800f38518265e5508ec621967c4c8f3",
        "producer_count": 4,
        "frames_per_producer": 250,
        "observed_duration_ms": 6301,
        "measurement": (
            "kernel-module log interval from timestamp probe completion to stress completion, "
            "rounded up; no stress JSON artifact"
        ),
        "stress_artifact_id": "not-available",
        "run_url": "https://github.com/Quchaosheng/workbench-mobile-home-robot/actions/runs/33034552431",
        "job_url": "https://github.com/Quchaosheng/workbench-mobile-home-robot/actions/runs/33034552431/job/98394315180",
    },
    {
        "milestone": "#280",
        "run_id": "33045892298",
        "job_id": "98429673919",
        "commit": "de9ab1fa2bc02eea6c3be7c586497a54485e689d",
        "producer_count": 4,
        "frames_per_producer": 250,
        "observed_duration_ms": 6578,
        "measurement": "stress JSON completed_at - started_at, rounded up",
        "stress_artifact_id": "9635641484",
        "run_url": "https://github.com/Quchaosheng/workbench-mobile-home-robot/actions/runs/33045892298",
        "job_url": "https://github.com/Quchaosheng/workbench-mobile-home-robot/actions/runs/33045892298/job/98429673919",
    },
    {
        "milestone": "#282",
        "run_id": "33050165163",
        "job_id": "98443442430",
        "commit": "ca0a2e283b95b3e0f1d30a6520acc540cfda0e8c",
        "producer_count": 4,
        "frames_per_producer": 250,
        "observed_duration_ms": 6644,
        "measurement": "stress JSON completed_at - started_at, rounded up",
        "stress_artifact_id": "9637268441",
        "run_url": "https://github.com/Quchaosheng/workbench-mobile-home-robot/actions/runs/33050165163",
        "job_url": "https://github.com/Quchaosheng/workbench-mobile-home-robot/actions/runs/33050165163/job/98443442430",
    },
)


class NotExecutedError(RuntimeError):
    """The host cannot execute the privileged virtual-kernel probe."""


class BudgetExceededError(AssertionError):
    """The probe crossed its declared wall-clock budget."""


def profile_config(name: str) -> StressProfile:
    """Return a named immutable profile or fail before touching the device."""

    try:
        return PROFILES[name]
    except KeyError as exc:
        valid = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown stress profile {name!r}; expected one of: {valid}") from exc


def resolve_profile(
    name: str,
    *,
    producer_count: int | None = None,
    frames_per_producer: int | None = None,
    tx_full_attempts: int | None = None,
    slow_receiver_frames: int | None = None,
    reload_cycles: int | None = None,
) -> StressProfile:
    """Apply bounded developer overrides while keeping release reproducible."""

    base = profile_config(name)
    values = {
        field.name: value for field in dataclasses.fields(base) if (value := getattr(base, field.name)) is not None
    }
    overrides = {
        "producer_count": producer_count,
        "frames_per_producer": frames_per_producer,
        "tx_full_attempts": tx_full_attempts,
        "slow_receiver_frames": slow_receiver_frames,
        "reload_cycles": reload_cycles,
    }
    for field, value in overrides.items():
        if value is not None:
            values[field] = value

    if name == "release":
        changed = [field for field, value in overrides.items() if value is not None and value != getattr(base, field)]
        if changed:
            raise ValueError(f"release profile is fixed; do not override {', '.join(changed)}")

    profile = dataclasses.replace(base, **values)
    _validate_profile_values(profile)
    return profile


def _validate_profile_values(profile: StressProfile) -> None:
    integer_bounds = (
        ("producer_count", profile.producer_count, MIN_PRODUCERS, MAX_PRODUCERS),
        ("frames_per_producer", profile.frames_per_producer, MIN_FRAMES_PER_PRODUCER, MAX_FRAMES_PER_PRODUCER),
        ("tx_full_attempts", profile.tx_full_attempts, MIN_TX_FULL_ATTEMPTS, MAX_TX_FULL_ATTEMPTS),
        ("slow_receiver_frames", profile.slow_receiver_frames, MIN_SLOW_RECEIVER_FRAMES, MAX_SLOW_RECEIVER_FRAMES),
        ("reload_cycles", profile.reload_cycles, MIN_RELOAD_CYCLES, MAX_RELOAD_CYCLES),
        ("baseline_duration_ms", profile.baseline_duration_ms, 1, MAX_TOTAL_DURATION_MS),
        ("max_duration_ms", profile.max_duration_ms, 1, MAX_TOTAL_DURATION_MS),
        ("max_stage_duration_ms", profile.max_stage_duration_ms, 1, MAX_STAGE_DURATION_MS),
        ("max_no_progress_ms", profile.max_no_progress_ms, 0, MAX_TOTAL_DURATION_MS),
        ("baseline_runs", profile.baseline_runs, 0, 100),
    )
    for name, value, minimum, maximum in integer_bounds:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"profile field {name} is outside its bounded range")
    if not isinstance(profile.budget_basis, str) or not profile.budget_basis.strip():
        raise ValueError("profile budget_basis must be non-empty")
    if profile.name == "release" and profile.baseline_runs < 3:
        raise ValueError("release profile requires at least three baseline runs")
    if profile.max_stage_duration_ms > profile.max_duration_ms:
        raise ValueError("profile stage budget cannot exceed its total duration budget")
    if profile.max_no_progress_ms > profile.max_duration_ms:
        raise ValueError("profile fairness budget cannot exceed its total duration budget")
    headroom_factor = 4 if profile.name == "release" else 1
    if profile.baseline_duration_ms * headroom_factor != profile.max_duration_ms:
        raise ValueError("profile duration budget must equal baseline envelope times headroom")


def _profile_payload(profile: StressProfile) -> dict[str, int | str]:
    return {
        "name": profile.name,
        "producer_count": profile.producer_count,
        "frames_per_producer": profile.frames_per_producer,
        "tx_full_attempts": profile.tx_full_attempts,
        "slow_receiver_frames": profile.slow_receiver_frames,
        "reload_cycles": profile.reload_cycles,
        "baseline_duration_ms": profile.baseline_duration_ms,
        "max_duration_ms": profile.max_duration_ms,
        "max_stage_duration_ms": profile.max_stage_duration_ms,
        "max_no_progress_ms": profile.max_no_progress_ms,
        "baseline_runs": profile.baseline_runs,
        "budget_basis": profile.budget_basis,
    }


def _baseline_payload(profile: StressProfile) -> dict[str, Any]:
    """Serialize the evidence that supports a profile's wall-clock budget."""

    if profile.name != "release":
        return {
            "run_count": 0,
            "duration_ms": profile.baseline_duration_ms,
            "observed_max_duration_ms": 0,
            "source": profile.budget_basis,
            "headroom_factor": 1,
            "runs": [],
            "derivation": "developer-smoke is a bounded local profile and has no release baseline claim",
        }

    runs = [dict(run) for run in RELEASE_BASELINE_EVIDENCE]
    observed_max = max(int(run["observed_duration_ms"]) for run in runs)
    return {
        "run_count": len(runs),
        "duration_ms": profile.baseline_duration_ms,
        "observed_max_duration_ms": observed_max,
        "source": profile.budget_basis,
        "headroom_factor": 4,
        "runs": runs,
        "derivation": (
            f"observed_max_duration_ms={observed_max}; "
            f"baseline_duration_ms={profile.baseline_duration_ms} is the rounded baseline envelope; "
            f"max_duration_ms={profile.max_duration_ms}="
            f"{profile.baseline_duration_ms}*4"
        ),
    }


FRAME = struct.Struct("=IB3x8s")
SATURATION_RESERVED_TAIL = b"\0" * 4


def decode_saturation_frame(raw_frame: bytes, expected_ids: set[int]) -> tuple[int, int] | None:
    """Decode only a canonical saturation frame; reject corrupt reserved bytes."""

    if len(raw_frame) != FRAME.size:
        return None
    can_id, length, payload = FRAME.unpack(raw_frame)
    if can_id not in expected_ids or length != 8 or payload[4:] != SATURATION_RESERVED_TAIL:
        return None
    return can_id, int.from_bytes(payload[:4], "big")


ALLOWED_STATES = {
    "error-active",
    "error-warning",
    "error-passive",
    "bus-off",
    "stopped",
    "sleeping",
}
ALLOWED_SEND_ERRORS = {
    errno.EAGAIN,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.ENOBUFS,
    errno.EBUSY,
}


class Probe:
    def __init__(
        self,
        interface: str,
        debugfs: Path,
        module_path: Path | None = None,
        max_duration_ms: int | None = None,
    ) -> None:
        self.interface = interface
        self.debugfs = debugfs
        self.module_path = (module_path or Path(__file__).resolve().with_name("wbcan.ko")).resolve()
        self.module_sys = Path("/sys/module/wbcan")
        self.interface_sys = Path("/sys/class/net") / interface
        self.debugfs_root = debugfs.parent
        self.restart_delay = Path("/sys/module/wbcan/parameters/test_restart_delay_ms")
        self.stop_delay = Path("/sys/module/wbcan/parameters/test_stop_delay_ms")
        self.errors: queue.SimpleQueue[Exception] = queue.SimpleQueue()
        self.abort = threading.Event()
        self.worker_threads: set[threading.Thread] = set()
        self.sockets: weakref.WeakSet[socket.socket] = weakref.WeakSet()
        self.max_duration_ms = max_duration_ms
        self.started_monotonic = time.monotonic()
        self.deadline = self.started_monotonic + max_duration_ms / 1000 if max_duration_ms is not None else None
        self.cleanup_mode = False

    def ensure_budget(self) -> None:
        if self.cleanup_mode or self.deadline is None:
            return
        if time.monotonic() >= self.deadline:
            raise BudgetExceededError(f"probe exceeded its {self.max_duration_ms} ms wall-clock budget")

    def remaining_seconds(self) -> float | None:
        if self.cleanup_mode or self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def command(self, *arguments: str, enforce_budget: bool = True) -> None:
        if enforce_budget:
            self.ensure_budget()
        remaining = self.remaining_seconds() if enforce_budget else None
        if remaining is not None and remaining <= 0:
            raise BudgetExceededError(f"probe exceeded its {self.max_duration_ms} ms wall-clock budget")
        timeout = min(2.0, remaining) if remaining is not None else 2.0
        try:
            subprocess.run(arguments, check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", errors="replace").strip() if exc.stderr else ""
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"command failed ({exc.returncode}): {' '.join(arguments)}{suffix}") from exc

    def arm(self, value: str) -> None:
        self.ensure_budget()
        (self.debugfs / "inject").write_text(f"{value}\n", encoding="ascii")

    def status(self) -> dict[str, str]:
        fields = dict(
            line.split(maxsplit=1)
            for line in (self.debugfs / "status").read_text(encoding="ascii").splitlines()
            if line.strip()
        )
        current = fields.get("state", "").strip()
        if current not in ALLOWED_STATES:
            raise AssertionError(f"invalid controller state snapshot: {current!r}")
        if current in {"stopped", "bus-off", "sleeping"} and fields.get("queue_stopped") != "yes":
            raise AssertionError(f"terminal state has an awake TX queue: {current!r}")
        return fields

    def state(self) -> str:
        return self.status()["state"].strip()

    def counter(self, name: str) -> int:
        value = self.status().get(name)
        if value is None:
            raise AssertionError(f"missing status counter: {name}")
        return int(value)

    def netdev_stats(self) -> dict[str, int]:
        root = Path("/sys/class/net") / self.interface / "statistics"
        names = ("tx_packets", "tx_bytes", "tx_dropped", "rx_packets", "rx_bytes", "rx_dropped")
        return {name: int((root / name).read_text(encoding="ascii")) for name in names}

    def wait_for_state(self, expected: str, timeout: float = 1.0) -> None:
        self.ensure_budget()
        deadline = time.monotonic() + timeout
        current = self.state()
        while current != expected and time.monotonic() < deadline:
            self.ensure_budget()
            time.sleep(0.002)
            current = self.state()
        if current != expected:
            raise AssertionError(f"state did not become {expected!r}: {current!r}")

    def wait_for_counter(self, name: str, minimum: int, timeout: float = 1.0) -> None:
        self.ensure_budget()
        deadline = time.monotonic() + timeout
        current = self.counter(name)
        while current < minimum and time.monotonic() < deadline:
            self.ensure_budget()
            time.sleep(0.002)
            current = self.counter(name)
        if current < minimum:
            raise AssertionError(f"{name} did not reach {minimum}: {current}")

    def raw_socket(self) -> socket.socket:
        self.ensure_budget()
        can_socket = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        # A broken queue must turn into a bounded probe failure, never an
        # unbounded blocking send.  The driver-level retry contract is tested
        # by the tx-full stage; this timeout is the outer safety net.
        can_socket.settimeout(0.2)
        try:
            can_socket.bind((self.interface,))
        except Exception:
            can_socket.close()
            raise
        self.sockets.add(can_socket)
        return can_socket

    def open_socket_count(self) -> int:
        return sum(can_socket.fileno() >= 0 for can_socket in self.sockets)

    def close_all_sockets(self) -> None:
        for can_socket in list(self.sockets):
            can_socket.close()

    def live_worker_names(self) -> list[str]:
        return sorted(thread.name for thread in self.worker_threads if thread.is_alive())

    def wait_for_path(self, path: Path, present: bool, timeout: float = 2.0) -> None:
        self.ensure_budget()
        deadline = time.monotonic() + timeout
        while path.exists() != present and time.monotonic() < deadline:
            self.ensure_budget()
            time.sleep(0.01)
        if path.exists() != present:
            state = "present" if present else "absent"
            raise AssertionError(f"path did not become {state} within {timeout:.1f}s: {path}")

    def wait_for_absence(self, path: Path, timeout: float = 2.0) -> None:
        self.wait_for_path(path, False, timeout)

    def wait_for_presence(self, path: Path, timeout: float = 2.0) -> None:
        self.wait_for_path(path, True, timeout)

    def send_frames(self, count: int, can_id: int) -> None:
        can_socket = self.raw_socket()
        try:
            for sequence in range(count):
                self.ensure_budget()
                if self.abort.is_set():
                    return
                payload = sequence.to_bytes(4, "big") + SATURATION_RESERVED_TAIL
                try:
                    can_socket.send(FRAME.pack(can_id, 8, payload))
                except OSError as exc:
                    if exc.errno not in ALLOWED_SEND_ERRORS:
                        raise
                time.sleep(0.0005)
        finally:
            can_socket.close()

    def send_until_stopped(self, stop: threading.Event, can_id: int) -> None:
        can_socket = self.raw_socket()
        can_socket.setblocking(False)
        sequence = 0
        try:
            while not stop.is_set():
                self.ensure_budget()
                payload = sequence.to_bytes(4, "big") + SATURATION_RESERVED_TAIL
                try:
                    can_socket.send(FRAME.pack(can_id, 8, payload))
                except OSError as exc:
                    if exc.errno not in ALLOWED_SEND_ERRORS:
                        raise
                sequence = (sequence + 1) & 0xFFFFFFFF
                time.sleep(0.0001)
        finally:
            can_socket.close()

    def assert_send_rejected(self, can_socket: socket.socket, can_id: int) -> None:
        try:
            can_socket.send(FRAME.pack(can_id, 1, b"X" + b"\0" * 7))
        except OSError as exc:
            if exc.errno not in ALLOWED_SEND_ERRORS:
                raise
        else:
            raise AssertionError("CAN send unexpectedly succeeded while the link was stopped")

    def read_status(self, count: int) -> None:
        for _ in range(count):
            self.ensure_budget()
            if self.abort.is_set():
                return
            self.state()

    def rearm_faults(self, count: int) -> None:
        for _ in range(count):
            self.ensure_budget()
            if self.abort.is_set():
                return
            self.arm("stuff-err 1")
            self.arm("none 0")

    def cycle_links(self, count: int) -> None:
        for _ in range(count):
            self.ensure_budget()
            if self.abort.is_set():
                return
            self.command("ip", "link", "set", self.interface, "down")
            self.wait_for_state("stopped")
            self.command("ip", "link", "set", self.interface, "up")
            self.wait_for_state("error-active")

    def capture(self, operation: Callable[..., None], *arguments: object) -> None:
        try:
            operation(*arguments)
        except Exception as exc:  # noqa: BLE001 - propagate worker failure to the main probe.
            self.errors.put(exc)
            # Stop sibling workers promptly.  The main thread still drains
            # and reports the queued exception, while bounded workers that
            # observe this event can leave their own resources cleanly.
            self.abort.set()

    def finish_threads(self, *threads: threading.Thread) -> None:
        stuck = [thread for thread in threads if thread.is_alive()]
        for thread in stuck:
            thread.join(timeout=0.1)
        errors: list[Exception] = []
        while True:
            try:
                errors.append(self.errors.get_nowait())
            except queue.Empty:
                break
        still_stuck = [thread.name for thread in threads if thread.is_alive()]
        if still_stuck:
            errors.append(AssertionError(f"concurrent probe exceeded time bound: {still_stuck}"))
        if errors:
            raise ExceptionGroup("concurrent wbcan worker failures", errors)

    def run_threads(self, *threads: threading.Thread) -> None:
        self.ensure_budget()
        self.worker_threads.update(threads)
        for thread in threads:
            thread.start()
        thread_budget = 5.0
        remaining = self.remaining_seconds()
        if remaining is not None:
            thread_budget = min(thread_budget, remaining)
        deadline = time.monotonic() + thread_budget
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        failed = False
        try:
            self.finish_threads(*threads)
        except Exception:
            failed = True
            self.abort.set()
            for thread in threads:
                thread.join(timeout=0.2)
            raise
        finally:
            if not failed:
                self.abort.clear()

    def exercise_reconfiguration(self) -> dict[str, int]:
        fault_rearms = 150
        status_reads = 600
        frames = 400
        self.run_threads(
            threading.Thread(target=self.capture, args=(self.send_frames, frames, 0x730), name="tx-rearm", daemon=True),
            threading.Thread(
                target=self.capture, args=(self.read_status, status_reads), name="status-rearm", daemon=True
            ),
            threading.Thread(
                target=self.capture, args=(self.rearm_faults, fault_rearms), name="fault-rearm", daemon=True
            ),
        )
        return {"frames_requested": frames, "status_reads": status_reads, "fault_rearms": fault_rearms}

    def exercise_drop_faults(self, attempts: int = 4) -> dict[str, int]:
        """Prove that intentional loss is counted and normal delivery recovers."""

        self.ensure_budget()
        sender = self.raw_socket()
        receiver = self.raw_socket()
        drop_tx = 0
        drop_rx = 0
        before_driver_dropped = self.netdev_stats()["rx_dropped"]
        try:
            self.arm(f"drop-tx {attempts}")
            for sequence in range(attempts):
                self.ensure_budget()
                sender.send(FRAME.pack(0x738, 8, sequence.to_bytes(8, "big")))
                drop_tx += 1
            if select.select([receiver], [], [], 0.05)[0]:
                raise AssertionError("drop-tx delivered an intentionally dropped frame")

            self.arm(f"drop-rx {attempts}")
            for sequence in range(attempts):
                self.ensure_budget()
                sender.send(FRAME.pack(0x738, 8, (attempts + sequence).to_bytes(8, "big")))
                drop_rx += 1
            if select.select([receiver], [], [], 0.05)[0]:
                raise AssertionError("drop-rx delivered an intentionally dropped frame")
            if self.counter("shots_left") != 0:
                raise AssertionError("drop fault did not consume its configured shots")
            driver_dropped = self.netdev_stats()["rx_dropped"] - before_driver_dropped
            if driver_dropped != drop_rx:
                raise AssertionError(
                    "intentional drop-rx accounting is not visible in netdev statistics: "
                    f"expected={drop_rx} actual={driver_dropped}"
                )

            self.arm("none 0")
            sender.send(FRAME.pack(0x738, 2, b"OK" + b"\0" * 6))
            if not select.select([receiver], [], [], 1)[0]:
                raise AssertionError("normal delivery did not recover after intentional drops")
            can_id, length, payload = FRAME.unpack(receiver.recv(FRAME.size))
            if can_id != 0x738 or length != 2 or payload[:2] != b"OK":
                raise AssertionError("post-drop recovery frame was corrupted")
            return {
                "drop_tx_expected": drop_tx,
                "drop_rx_expected": drop_rx,
                "intentional_loss": drop_tx + drop_rx,
                "unexplained_loss": 0,
                "recovery_frames": 1,
                "driver_rx_dropped": driver_dropped,
            }
        finally:
            sender.close()
            receiver.close()

    def exercise_link_lifecycle(self) -> dict[str, int]:
        self.arm("none 0")
        cycles = 8
        self.run_threads(
            threading.Thread(target=self.capture, args=(self.send_frames, 500, 0x731), name="tx-link", daemon=True),
            threading.Thread(target=self.capture, args=(self.read_status, 800), name="status-link", daemon=True),
            threading.Thread(target=self.capture, args=(self.cycle_links, cycles), name="link-cycle", daemon=True),
        )
        return {"cycles": cycles, "frames_requested": 500, "status_reads": 800}

    def exercise_stop_drain(self) -> None:
        self.arm("none 0")
        before_tx = self.counter("tx_frames")
        stop = threading.Event()
        sender = threading.Thread(
            target=self.capture,
            args=(self.send_until_stopped, stop, 0x736),
            name="tx-stop-drain",
            daemon=True,
        )
        self.worker_threads.add(sender)
        sender.start()
        failures: list[Exception] = []
        try:
            self.wait_for_counter("tx_frames", before_tx + 50)
            self.command("ip", "link", "set", self.interface, "down")
            self.wait_for_state("stopped")
            stopped_tx = self.counter("tx_frames")
            time.sleep(0.075)
            if self.counter("tx_frames") != stopped_tx:
                raise AssertionError("TX advanced after link stop returned")
        except Exception as exc:  # noqa: BLE001 - combine the cutover and worker failures.
            failures.append(exc)
        finally:
            stop.set()
            sender.join(timeout=2)

        try:
            self.finish_threads(sender)
        except Exception as exc:  # noqa: BLE001 - combine the cutover and worker failures.
            failures.append(exc)
        if failures:
            raise ExceptionGroup("link-stop drain failures", failures)
        self.command("ip", "link", "set", self.interface, "up")
        self.wait_for_state("error-active")

    def exercise_bus_off_restart(self) -> None:
        self.command("ip", "link", "set", self.interface, "down")
        self.command("ip", "link", "set", self.interface, "type", "can", "restart-ms", "50")
        self.command("ip", "link", "set", self.interface, "up")
        sender = self.raw_socket()
        try:
            for sequence in range(8):
                self.ensure_budget()
                self.arm("bus-off 1")
                sender.send(FRAME.pack(0x732, 1, bytes([sequence]) + b"\0" * 7))
                self.wait_for_state("bus-off")
                self.wait_for_state("error-active", timeout=2)
        finally:
            sender.close()

    def exercise_restart_cancellation(self) -> None:
        self.command("ip", "link", "set", self.interface, "down")
        self.command("ip", "link", "set", self.interface, "type", "can", "restart-ms", "250")
        self.command("ip", "link", "set", self.interface, "up")
        sender = self.raw_socket()
        try:
            self.ensure_budget()
            self.arm("bus-off 1")
            sender.send(FRAME.pack(0x732, 1, b"\xff" + b"\0" * 7))
            self.wait_for_state("bus-off")
        finally:
            sender.close()
        self.command("ip", "link", "set", self.interface, "down")
        stopped_tx = self.counter("tx_frames")
        time.sleep(0.35)
        self.wait_for_state("stopped")
        if self.counter("tx_frames") != stopped_tx:
            raise AssertionError("TX advanced after link stop returned")
        self.command("ip", "link", "set", self.interface, "up")

    def exercise_restart_stop_race(self) -> None:
        down: threading.Thread | None = None
        try:
            for sequence in range(4):
                self.ensure_budget()
                self.command("ip", "link", "set", self.interface, "down")
                self.command("ip", "link", "set", self.interface, "type", "can", "restart-ms", "50")
                self.command("ip", "link", "set", self.interface, "up")
                before_restart = self.counter("restart_attempts")
                before_stop = self.counter("stop_attempts")
                # Hold stop on both sides of STOPPED publication. Restart
                # deterministically wins after the first drain, then status
                # observes the atomic STOPPED + queue-stopped commit before
                # close_candev() can perform the final defensive drain.
                self.restart_delay.write_text("100\n", encoding="ascii")
                self.stop_delay.write_text("250\n", encoding="ascii")
                sender = self.raw_socket()
                try:
                    self.arm("bus-off 1")
                    sender.send(FRAME.pack(0x734, 1, bytes([sequence]) + b"\0" * 7))
                    self.wait_for_state("bus-off")
                    down = threading.Thread(
                        target=self.capture,
                        args=(self.command, "ip", "link", "set", self.interface, "down"),
                        name="stop-during-restart",
                        daemon=True,
                    )
                    self.worker_threads.add(down)
                    down.start()
                    self.wait_for_counter("stop_attempts", before_stop + 1)
                    self.wait_for_counter("restart_attempts", before_restart + 1)
                    deadline = time.monotonic() + 1
                    saw_active = False
                    saw_stopped = False
                    while down.is_alive() and time.monotonic() < deadline:
                        current = self.state()
                        saw_active |= current == "error-active"
                        saw_stopped |= current == "stopped"
                        time.sleep(0.002)
                    if not saw_active:
                        raise AssertionError("restart did not commit while stop was draining")
                    if not saw_stopped:
                        raise AssertionError("STOPPED publication was not observable before stop completed")
                    down.join(timeout=2)
                    self.finish_threads(down)
                    self.wait_for_state("stopped")
                    stopped_tx = self.counter("tx_frames")
                    self.assert_send_rejected(sender, 0x735)
                    time.sleep(0.075)
                    if self.state() != "stopped":
                        raise AssertionError("restart raced past link stop")
                    if self.counter("tx_frames") != stopped_tx:
                        raise AssertionError("TX advanced while restart raced with link stop")
                finally:
                    sender.close()

                # In the opposite ordering, restart has entered the
                # callback but stop publishes STOPPED before the callback
                # takes the TX lock. The BUS_OFF guard must reject the stale
                # restart after close_candev() waits for it.
                self.command("ip", "link", "set", self.interface, "up")
                self.restart_delay.write_text("300\n", encoding="ascii")
                self.stop_delay.write_text("0\n", encoding="ascii")
                before_restart = self.counter("restart_attempts")
                sender = self.raw_socket()
                try:
                    self.arm("bus-off 1")
                    sender.send(FRAME.pack(0x737, 1, bytes([sequence]) + b"\0" * 7))
                    self.wait_for_state("bus-off")
                    self.wait_for_counter("restart_attempts", before_restart + 1)
                finally:
                    sender.close()
                started = time.monotonic()
                self.command("ip", "link", "set", self.interface, "down")
                if time.monotonic() - started < 0.1:
                    raise AssertionError("link stop did not wait for entered restart callback")
                self.wait_for_state("stopped")
                time.sleep(0.075)
                if self.state() != "stopped":
                    raise AssertionError("stale restart overwrote STOPPED state")
        finally:
            if down is not None and down.is_alive():
                down.join(timeout=2)
            self.restart_delay.write_text("0\n", encoding="ascii")
            self.stop_delay.write_text("0\n", encoding="ascii")

        self.command("ip", "link", "set", self.interface, "up")

    def verify_queue_recovery(self) -> None:
        self.ensure_budget()
        self.arm("none 0")
        sender = self.raw_socket()
        peer = self.raw_socket()
        try:
            sender.send(FRAME.pack(0x733, 2, b"OK" + b"\0" * 6))
            if not select.select([peer], [], [], 1)[0]:
                raise AssertionError("TX queue remained stopped after recovery")
            can_id, length, payload = FRAME.unpack(peer.recv(FRAME.size))
            if can_id != 0x733 or length != 2 or payload[:2] != b"OK":
                raise AssertionError("post-recovery frame was corrupted")
        finally:
            sender.close()
            peer.close()

    def exercise_stats_sampling(self) -> dict[str, int]:
        self.arm("none 0")
        stop = threading.Event()
        samples: list[dict[str, int]] = []

        def sample() -> None:
            while not stop.is_set():
                self.ensure_budget()
                if len(samples) >= MAX_STATS_SAMPLES:
                    self.errors.put(AssertionError("statistics sampler exceeded its sample bound"))
                    self.abort.set()
                    return
                samples.append(self.netdev_stats())
                time.sleep(0.0005)

        sampler = threading.Thread(
            target=self.capture,
            args=(sample,),
            name="stats-sampler",
            daemon=True,
        )
        sender = threading.Thread(
            target=self.capture,
            args=(self.send_frames, 600, 0x739),
            name="stats-tx",
            daemon=True,
        )
        self.worker_threads.update((sampler, sender))
        sampler.start()
        sender.start()
        sender.join(timeout=3)
        if sender.is_alive():
            self.abort.set()
        stop.set()
        sampler.join(timeout=1)
        self.finish_threads(sender, sampler)
        if len(samples) < 2:
            raise AssertionError("statistics sampler collected too few snapshots")
        regressions = 0
        for previous, current in pairwise(samples):
            for name in current:
                if current[name] < previous[name]:
                    regressions += 1
                    raise AssertionError(f"netdev statistic regressed: {name}")
        return {"samples": len(samples), "regressions": regressions}

    def exercise_repeated_tx_full(self, attempts: int = 16) -> dict[str, int]:
        sender = self.raw_socket()
        receiver = self.raw_socket()
        delivered = 0
        try:
            for sequence in range(attempts):
                self.ensure_budget()
                self.arm("tx-full 1")
                sender.send(FRAME.pack(0x73A, 8, sequence.to_bytes(8, "big")))
                if not select.select([receiver], [], [], 1)[0]:
                    raise AssertionError(f"tx-full retry {sequence} did not recover within one second")
                can_id, length, payload = FRAME.unpack(receiver.recv(FRAME.size))
                if can_id != 0x73A or length != 8 or int.from_bytes(payload, "big") != sequence:
                    raise AssertionError(f"tx-full retry {sequence} delivered an unexpected frame")
                if select.select([receiver], [], [], 0.01)[0]:
                    raise AssertionError(f"tx-full retry {sequence} delivered a duplicate frame")
                delivered += 1
            self.verify_queue_recovery()
        finally:
            sender.close()
            receiver.close()
        return {"attempts": attempts, "delivered_once": delivered}

    def exercise_slow_receiver(self, frame_count: int = 500) -> dict[str, int]:
        self.arm("none 0")
        sender = self.raw_socket()
        receiver = self.raw_socket()
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        receiver.setblocking(False)
        before_dropped = self.netdev_stats()["rx_dropped"]
        received: list[int] = []
        try:
            for sequence in range(frame_count):
                self.ensure_budget()
                sender.send(FRAME.pack(0x73B, 8, sequence.to_bytes(8, "big")))
            time.sleep(0.05)
            quiet_since = time.monotonic()
            while time.monotonic() - quiet_since < 0.2:
                self.ensure_budget()
                if not select.select([receiver], [], [], 0.02)[0]:
                    continue
                can_id, length, payload = FRAME.unpack(receiver.recv(FRAME.size))
                if can_id != 0x73B or length != 8:
                    raise AssertionError("slow receiver observed an unexpected frame")
                received.append(int.from_bytes(payload, "big"))
                quiet_since = time.monotonic()
            received_set = set(received)
            duplicate = len(received) - len(received_set)
            unexpected = len(received_set - set(range(frame_count)))
            driver_dropped = self.netdev_stats()["rx_dropped"] - before_dropped
            if duplicate or unexpected or driver_dropped:
                raise AssertionError(
                    "slow receiver produced unexplained delivery anomalies: "
                    f"duplicate={duplicate} unexpected={unexpected} driver_rx_dropped={driver_dropped}"
                )
            self.verify_queue_recovery()
            return {
                "sent": frame_count,
                "received": len(received),
                "expected_socket_loss": frame_count - len(received_set),
                "duplicate": duplicate,
                "unexpected": unexpected,
                "driver_rx_dropped": driver_dropped,
            }
        finally:
            sender.close()
            receiver.close()

    def exercise_multi_producer_saturation(
        self,
        producer_count: int,
        frames_per_producer: int,
        max_no_progress_ms: int = MAX_TOTAL_DURATION_MS,
    ) -> dict[str, Any]:
        self.arm("none 0")
        can_ids = tuple(0x740 + index for index in range(producer_count))
        sent: dict[int, list[int]] = {can_id: [] for can_id in can_ids}
        received: dict[int, list[int]] = {can_id: [] for can_id in can_ids}
        arrivals: dict[int, list[float]] = {can_id: [] for can_id in can_ids}
        producer_started: dict[int, float] = {}
        producer_barrier = threading.Barrier(producer_count)
        producers_done = threading.Event()
        remaining = producer_count
        remaining_lock = threading.Lock()
        unexpected_frames = 0
        receiver = self.raw_socket()
        receiver.setblocking(False)

        def produce(can_id: int) -> None:
            nonlocal remaining
            can_socket: socket.socket | None = None
            try:
                producer_started[can_id] = time.monotonic()
                can_socket = self.raw_socket()
                can_socket.setblocking(False)
                try:
                    producer_barrier.wait(timeout=1.0)
                except threading.BrokenBarrierError as exc:
                    raise AssertionError("saturation producers did not start concurrently") from exc
                for sequence in range(frames_per_producer):
                    self.ensure_budget()
                    if self.abort.is_set():
                        return
                    payload = sequence.to_bytes(4, "big") + SATURATION_RESERVED_TAIL
                    try:
                        can_socket.send(FRAME.pack(can_id, 8, payload))
                    except OSError as exc:
                        if exc.errno not in ALLOWED_SEND_ERRORS:
                            raise
                    else:
                        sent[can_id].append(sequence)
            finally:
                if can_socket is not None:
                    can_socket.close()
                with remaining_lock:
                    remaining -= 1
                    if remaining == 0:
                        producers_done.set()

        def receive() -> None:
            nonlocal unexpected_frames
            deadline = time.monotonic() + 5
            quiet_since: float | None = None
            while time.monotonic() < deadline:
                self.ensure_budget()
                if self.abort.is_set():
                    return
                readable = select.select([receiver], [], [], 0.02)[0]
                if readable:
                    raw_frame = receiver.recv(FRAME.size)
                    if len(raw_frame) != FRAME.size:
                        unexpected_frames += 1
                        quiet_since = None
                        continue
                    decoded = decode_saturation_frame(raw_frame, set(received))
                    if decoded is not None:
                        can_id, sequence = decoded
                        received[can_id].append(sequence)
                        arrivals[can_id].append(time.monotonic())
                    else:
                        # Do not silently discard a frame from another CAN
                        # ID, an error frame, or a frame with an unexpected
                        # DLC.  These are unexplained delivery anomalies.
                        unexpected_frames += 1
                    quiet_since = None
                    continue
                if producers_done.is_set():
                    quiet_since = quiet_since or time.monotonic()
                    if time.monotonic() - quiet_since >= 0.2:
                        return
            raise AssertionError("multi-producer receiver exceeded its five-second bound")

        receiver_thread = threading.Thread(target=self.capture, args=(receive,), name="saturation-rx", daemon=True)
        producer_threads = tuple(
            threading.Thread(
                target=self.capture,
                args=(produce, can_id),
                name=f"saturation-tx-{can_id:x}",
                daemon=True,
            )
            for can_id in can_ids
        )
        try:
            self.run_threads(receiver_thread, *producer_threads)
        finally:
            receiver.close()

        metrics = analyze_delivery(sent, received, arrivals, frames_per_producer, producer_started)
        if unexpected_frames:
            raise AssertionError(f"multi-producer receiver observed {unexpected_frames} unexpected frame(s)")
        failures = [
            producer
            for producer in metrics
            if producer["sent"] != frames_per_producer
            or producer["received"] != frames_per_producer
            or producer["longest_no_progress_ms"] > max_no_progress_ms
            or any(producer[name] for name in ("lost", "duplicate", "unexpected"))
        ]
        if failures:
            raise AssertionError(f"multi-producer saturation accounting failed: {failures}")
        return {
            "producer_count": producer_count,
            "frames_per_producer": frames_per_producer,
            "max_no_progress_ms": max_no_progress_ms,
            "unexpected_frames": unexpected_frames,
            "producers": metrics,
        }

    def _assert_fresh_device(self) -> None:
        fields = self.status()
        if fields.get("state") != "stopped" or fields.get("queue_stopped") != "yes":
            raise AssertionError("reloaded wbcan did not start in the stopped state")
        if fields.get("armed_fault") != "none" or fields.get("shots_left") != "0":
            raise AssertionError("reloaded wbcan retained fault configuration")
        for name in (
            "tx_frames",
            "rx_frames",
            "candidates",
            "injected",
            "restart_attempts",
            "stop_attempts",
            "bus_errors",
        ):
            if self.counter(name) != 0:
                raise AssertionError(f"reloaded wbcan retained {name}")

    def _configure_link(self, *, enforce_budget: bool = True) -> None:
        self.command(
            "ip",
            "link",
            "set",
            self.interface,
            "type",
            "can",
            "restart-ms",
            "100",
            enforce_budget=enforce_budget,
        )
        self.command("ip", "link", "set", self.interface, "up", enforce_budget=enforce_budget)
        self.wait_for_state("error-active")

    def _verify_reload_delivery(self, can_id: int) -> None:
        sender = self.raw_socket()
        receiver = self.raw_socket()
        receiver.setblocking(False)
        try:
            sender.send(FRAME.pack(can_id, 8, can_id.to_bytes(4, "big") + b"reload!!"[:4]))
            if not select.select([receiver], [], [], 1)[0]:
                raise AssertionError("reloaded wbcan did not deliver its first frame")
            received_id, length, payload = FRAME.unpack(receiver.recv(FRAME.size))
            expected_payload = can_id.to_bytes(4, "big") + b"reload!!"[:4]
            if received_id != can_id or length != 8 or payload != expected_payload:
                raise AssertionError("reloaded wbcan delivered a stale or corrupted frame")
            if select.select([receiver], [], [], 0.05)[0]:
                raise AssertionError("reloaded wbcan delivered an unexpected extra frame")
        finally:
            sender.close()
            receiver.close()
        if self.counter("tx_frames") != 1 or self.counter("rx_frames") != 1:
            raise AssertionError("reloaded wbcan did not reset and account for one frame")

    def _unload_module(self) -> None:
        self.close_all_sockets()
        if self.open_socket_count():
            raise AssertionError("open CAN sockets remained before module unload")
        if self.interface_sys.exists():
            self.command("ip", "link", "set", self.interface, "down")
            self.wait_for_state("stopped")
        self.command("rmmod", "wbcan")
        self.wait_for_absence(self.module_sys)
        self.wait_for_absence(self.interface_sys)
        self.wait_for_absence(self.debugfs_root)
        if self.open_socket_count():
            raise AssertionError("open CAN sockets remained after module unload")

    def _load_module(self) -> None:
        self.command("insmod", str(self.module_path))
        self.wait_for_presence(self.module_sys)
        self.wait_for_presence(self.interface_sys)
        self.wait_for_presence(self.debugfs_root)
        self.wait_for_presence(self.debugfs)
        self._assert_fresh_device()
        self.restart_delay.write_text("0\n", encoding="ascii")
        self.stop_delay.write_text("0\n", encoding="ascii")
        self._configure_link()

    def _module_paths_ready(self) -> bool:
        return all(path.exists() for path in (self.module_sys, self.interface_sys, self.debugfs_root, self.debugfs))

    def _wait_module_absence(self) -> None:
        self.wait_for_absence(self.module_sys, timeout=2.0)
        self.wait_for_absence(self.interface_sys, timeout=2.0)
        self.wait_for_absence(self.debugfs_root, timeout=2.0)

    def _restore_device(self) -> None:
        """Best-effort fail-safe restoration after a reload failure."""

        previous_cleanup_mode = self.cleanup_mode
        # Restoration is part of failure handling. It must remain possible
        # after the workload deadline has expired, while still keeping every
        # individual command bounded by command()/wait_*() timeouts.
        self.cleanup_mode = True
        try:
            self.close_all_sockets()
            if not self._module_paths_ready():
                # A failed insmod can leave a partially initialized module
                # behind.  Remove that partial instance before trying a clean
                # load; otherwise the next insmod may fail on a stale
                # netdev/debugfs name and cleanup would not be recoverable.
                if self.module_sys.exists():
                    self.command("rmmod", "wbcan", enforce_budget=False)
                    self._wait_module_absence()
                elif any(path.exists() for path in (self.interface_sys, self.debugfs_root, self.debugfs)):
                    raise RuntimeError("wbcan left partial state without a loaded module")
                self.command("insmod", str(self.module_path), enforce_budget=False)
                self.wait_for_presence(self.module_sys, timeout=2.0)
                self.wait_for_presence(self.interface_sys, timeout=2.0)
                self.wait_for_presence(self.debugfs_root, timeout=2.0)
                self.wait_for_presence(self.debugfs, timeout=2.0)
            if not self._module_paths_ready():
                raise RuntimeError("cannot restore wbcan after a failed reload")
            self.command("ip", "link", "set", self.interface, "down", enforce_budget=False)
            self.arm("none 0")
            self.restart_delay.write_text("0\n", encoding="ascii")
            self.stop_delay.write_text("0\n", encoding="ascii")
            self._configure_link(enforce_budget=False)
        finally:
            self.cleanup_mode = previous_cleanup_mode

    def _restore_for_cleanup(self) -> None:
        """Retry restoration once, but never hide a failed first attempt."""

        try:
            self._restore_device()
        except Exception as first_error:
            try:
                self._restore_device()
            except Exception as second_error:  # noqa: BLE001 - both failures are actionable.
                raise ExceptionGroup(
                    "wbcan cleanup restoration failed twice",
                    [first_error, second_error],
                ) from first_error
            raise RuntimeError("wbcan cleanup restoration recovered on its second attempt") from first_error

    def exercise_unload_reload(self, cycles: int) -> dict[str, int | bool]:
        """Unload and reload the singleton repeatedly under a fixed bound."""

        self.ensure_budget()
        initial_open_sockets = self.open_socket_count()
        initial_live_threads = len(self.live_worker_names())
        if initial_open_sockets or initial_live_threads:
            raise AssertionError(
                "reload stage started with live resources: "
                f"sockets={initial_open_sockets} threads={initial_live_threads}"
            )

        completed = 0
        absence_checks = {"module": 0, "interface": 0, "debugfs": 0}
        delivered = 0
        try:
            for cycle in range(cycles):
                self.ensure_budget()
                self._unload_module()
                for name in absence_checks:
                    absence_checks[name] += 1
                self._load_module()
                self._verify_reload_delivery(0x750 + cycle)
                delivered += 1
                self.close_all_sockets()
                if self.open_socket_count():
                    raise AssertionError("reload cycle left an open CAN socket")
                completed += 1
        except Exception as exc:
            try:
                self._restore_device()
            except Exception as restore_exc:  # noqa: BLE001 - both failures are actionable.
                raise ExceptionGroup("reload failure and restoration failure", [exc, restore_exc]) from exc
            raise

        return {
            "requested_cycles": cycles,
            "completed_cycles": completed,
            "module_absence_checks": absence_checks["module"],
            "interface_absence_checks": absence_checks["interface"],
            "debugfs_absence_checks": absence_checks["debugfs"],
            "post_reload_frames": delivered,
            "stale_frames": 0,
            "open_socket_count": self.open_socket_count(),
            "live_thread_count": len(self.live_worker_names()),
        }

    def cleanup(self) -> dict[str, int | bool]:
        """Reap probe resources and leave a usable, fault-free interface."""

        self.cleanup_mode = True
        self.abort.set()
        for thread in tuple(self.worker_threads):
            thread.join(timeout=0.25)
        if self.live_worker_names():
            self.close_all_sockets()
            for thread in tuple(self.worker_threads):
                thread.join(timeout=0.25)
        live_threads = self.live_worker_names()
        if live_threads:
            raise AssertionError(f"probe worker threads survived cleanup: {live_threads}")
        self.close_all_sockets()
        if self.open_socket_count():
            raise AssertionError("CAN sockets survived cleanup")

        self._restore_for_cleanup()
        fields = self.status()
        details: dict[str, int | bool] = {
            "open_socket_count": self.open_socket_count(),
            "live_thread_count": len(self.live_worker_names()),
            "module_loaded": self.module_sys.exists(),
            "interface_present": self.interface_sys.exists(),
            "debugfs_present": self.debugfs.exists(),
            "link_active": fields.get("state") == "error-active" and fields.get("queue_stopped") == "no",
            "fault_cleared": fields.get("armed_fault") == "none" and fields.get("shots_left") == "0",
            "subprocesses_reaped": True,
        }
        if not all(
            details[name]
            for name in ("module_loaded", "interface_present", "debugfs_present", "link_active", "fault_cleared")
        ):
            raise AssertionError(f"cleanup verification failed: {details}")
        self.abort.clear()
        return details


def analyze_delivery(
    sent: dict[int, list[int]],
    received: dict[int, list[int]],
    arrivals: dict[int, list[float]],
    requested: int,
    producer_started: dict[int, float] | None = None,
) -> list[dict[str, int | str]]:
    """Return per-producer delivery metrics and arrival-gap fairness evidence.

    The no-progress interval starts at producer start and covers the gaps up
    to each delivered frame.  Idle time after a producer has delivered its
    final frame is intentionally excluded because no further delivery was
    pending during that interval.
    """

    metrics: list[dict[str, int | str]] = []
    for can_id in sorted(sent):
        sent_sequences = sent[can_id]
        received_sequences = received.get(can_id, [])
        sent_set = set(sent_sequences)
        received_set = set(received_sequences)
        timestamps = arrivals.get(can_id, [])
        gaps = [current - previous for previous, current in pairwise(timestamps)]
        if timestamps and producer_started is not None and can_id in producer_started:
            gaps.append(max(0.0, timestamps[0] - producer_started[can_id]))
        metrics.append(
            {
                "can_id": f"0x{can_id:03x}",
                "requested": requested,
                "sent": len(sent_sequences),
                "received": len(received_sequences),
                "lost": len(sent_set - received_set),
                "duplicate": len(received_sequences) - len(received_set),
                "reordered": sum(current < previous for previous, current in pairwise(received_sequences)),
                "unexpected": len(received_set - sent_set),
                "longest_no_progress_ms": round(max(gaps, default=0) * 1000),
            }
        )
    return metrics


def _integer(value: object, name: str, minimum: int = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"stress report has invalid {name}")
    if maximum is not None and value > maximum:
        raise ValueError(f"stress report has invalid {name}")
    return value


def _validate_report_profile(payload: object) -> StressProfile:
    if not isinstance(payload, dict):
        raise ValueError("stress report profile_config must be an object")
    names = tuple(field.name for field in dataclasses.fields(StressProfile))
    missing = [name for name in names if name not in payload]
    if missing:
        raise ValueError(f"stress report profile_config is missing {missing[0]}")
    unexpected = sorted(set(payload) - set(names))
    if unexpected:
        raise ValueError(f"stress report profile_config has unexpected field {unexpected[0]!r}")
    if not isinstance(payload.get("name"), str) or not payload["name"].strip():
        raise ValueError("stress report profile_config name must be non-empty")
    for name in (
        "producer_count",
        "frames_per_producer",
        "tx_full_attempts",
        "slow_receiver_frames",
        "reload_cycles",
        "baseline_duration_ms",
        "max_duration_ms",
        "max_stage_duration_ms",
        "max_no_progress_ms",
        "baseline_runs",
    ):
        if not isinstance(payload.get(name), int) or isinstance(payload[name], bool):
            raise ValueError(f"stress report profile_config field {name} must be an integer")
    if not isinstance(payload.get("budget_basis"), str) or not payload["budget_basis"].strip():
        raise ValueError("stress report profile_config budget_basis must be non-empty")
    profile = StressProfile(
        name=payload["name"],
        producer_count=payload["producer_count"],
        frames_per_producer=payload["frames_per_producer"],
        tx_full_attempts=payload["tx_full_attempts"],
        slow_receiver_frames=payload["slow_receiver_frames"],
        reload_cycles=payload["reload_cycles"],
        baseline_duration_ms=payload["baseline_duration_ms"],
        max_duration_ms=payload["max_duration_ms"],
        max_stage_duration_ms=payload["max_stage_duration_ms"],
        max_no_progress_ms=payload["max_no_progress_ms"],
        baseline_runs=payload["baseline_runs"],
        budget_basis=payload["budget_basis"],
    )
    _validate_profile_values(profile)
    base = profile_config(profile.name)
    if profile.name == "release" and profile != base:
        raise ValueError("release stress report profile_config does not match the fixed release profile")
    for name in (
        "baseline_duration_ms",
        "max_duration_ms",
        "max_stage_duration_ms",
        "max_no_progress_ms",
        "baseline_runs",
        "budget_basis",
    ):
        if getattr(profile, name) != getattr(base, name):
            raise ValueError(f"stress report profile_config changed fixed field {name}")
    return profile


def _validate_budget(report: dict[str, object], profile: StressProfile) -> None:
    elapsed_ms = _integer(report.get("elapsed_ms"), "elapsed_ms")
    budget = report.get("budget")
    if not isinstance(budget, dict):
        raise ValueError("stress report budget must be an object")
    expected = {
        "max_duration_ms": profile.max_duration_ms,
        "max_stage_duration_ms": profile.max_stage_duration_ms,
        "elapsed_ms": elapsed_ms,
        "producer_count": profile.producer_count,
        "frames_per_producer": profile.frames_per_producer,
        "tx_full_attempts": profile.tx_full_attempts,
        "slow_receiver_frames": profile.slow_receiver_frames,
        "reload_cycles": profile.reload_cycles,
        "total_frame_budget": profile.producer_count * profile.frames_per_producer,
    }
    for name, expected_value in expected.items():
        if budget.get(name) != expected_value:
            raise ValueError(f"stress report budget field {name} is inconsistent")
    within_budget = budget.get("within_budget")
    if not isinstance(within_budget, bool) or within_budget != (elapsed_ms <= profile.max_duration_ms):
        raise ValueError("stress report budget within_budget is inconsistent")
    baseline = budget.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("stress report budget requires baseline provenance")
    _integer(baseline.get("run_count"), "baseline.run_count", 0, 100)
    baseline_duration_ms = _integer(baseline.get("duration_ms"), "baseline.duration_ms", 1, MAX_TOTAL_DURATION_MS)
    observed_max_duration_ms = _integer(
        baseline.get("observed_max_duration_ms"),
        "baseline.observed_max_duration_ms",
        0,
        MAX_TOTAL_DURATION_MS,
    )
    if not isinstance(baseline.get("source"), str) or not baseline["source"].strip():
        raise ValueError("stress report baseline source must be non-empty")
    headroom = _integer(baseline.get("headroom_factor"), "baseline.headroom_factor", 1, 100)
    derivation = baseline.get("derivation")
    if not isinstance(derivation, str) or not derivation.strip():
        raise ValueError("stress report baseline derivation must be non-empty")
    runs = baseline.get("runs")
    if not isinstance(runs, list):
        raise ValueError("stress report baseline runs must be a list")
    if baseline["run_count"] != len(runs):
        raise ValueError("stress report baseline run_count disagrees with runs")
    if baseline_duration_ms != profile.baseline_duration_ms:
        raise ValueError("stress report baseline duration does not match profile provenance")
    if baseline["source"] != profile.budget_basis:
        raise ValueError("stress report baseline source does not match profile provenance")
    expected_headroom = 4 if profile.name == "release" else 1
    if headroom != expected_headroom:
        raise ValueError("stress report baseline headroom is inconsistent with profile")
    if profile.name == "release":
        if baseline["run_count"] != profile.baseline_runs or headroom < 2:
            raise ValueError("release stress report requires repeated baseline evidence and headroom")
        if observed_max_duration_ms != max(_validate_baseline_run(run, profile) for run in runs):
            raise ValueError("stress report baseline observed maximum disagrees with runs")
        if observed_max_duration_ms > baseline_duration_ms:
            raise ValueError("stress report baseline envelope is below observed duration")
        if profile.max_duration_ms != baseline_duration_ms * headroom:
            raise ValueError("stress report release budget is not derived from baseline headroom")
    elif runs or observed_max_duration_ms != 0:
        raise ValueError("developer-smoke report cannot claim release baseline runs")
    if baseline != _baseline_payload(profile):
        raise ValueError("stress report baseline provenance does not match the fixed evidence")
    if report.get("result") == "PASS" and not within_budget:
        raise ValueError("passing stress report exceeded its wall-clock budget")


def _validate_baseline_run(payload: object, profile: StressProfile) -> int:
    if not isinstance(payload, dict):
        raise ValueError("stress report baseline run must be an object")
    required = {
        "milestone",
        "run_id",
        "job_id",
        "commit",
        "producer_count",
        "frames_per_producer",
        "observed_duration_ms",
        "measurement",
        "stress_artifact_id",
        "run_url",
        "job_url",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"stress report baseline run is missing {missing[0]}")
    unexpected = sorted(set(payload) - required)
    if unexpected:
        raise ValueError(f"stress report baseline run has unexpected field {unexpected[0]!r}")
    for name in (
        "milestone",
        "run_id",
        "job_id",
        "commit",
        "measurement",
        "stress_artifact_id",
        "run_url",
        "job_url",
    ):
        if not isinstance(payload[name], str) or not payload[name].strip():
            raise ValueError(f"stress report baseline run requires non-empty {name}")
    _integer(payload["producer_count"], "baseline run producer_count", MIN_PRODUCERS, MAX_PRODUCERS)
    _integer(
        payload["frames_per_producer"],
        "baseline run frames_per_producer",
        MIN_FRAMES_PER_PRODUCER,
        MAX_FRAMES_PER_PRODUCER,
    )
    observed = _integer(payload["observed_duration_ms"], "baseline run observed_duration_ms", 1, MAX_TOTAL_DURATION_MS)
    if (
        payload["producer_count"] != profile.producer_count
        or payload["frames_per_producer"] != profile.frames_per_producer
    ):
        raise ValueError("stress report baseline workload does not match the release profile")
    if not payload["run_url"].startswith("https://github.com/") or not payload["job_url"].startswith(
        "https://github.com/"
    ):
        raise ValueError("stress report baseline run URLs must point to GitHub")
    return observed


def _validate_evidence_boundary(report: dict[str, object]) -> None:
    boundary = report.get("evidence_boundary")
    if not isinstance(boundary, dict):
        raise ValueError("stress report requires evidence_boundary")
    if boundary.get("scope") != "virtual-wbcan-only":
        raise ValueError("stress report evidence boundary scope is invalid")
    for name in ("physical_can", "mcu", "actuator", "hard_real_time"):
        if boundary.get(name) != "NOT_EXECUTED":
            raise ValueError(f"stress report cannot claim {name} evidence")


def _validate_stage_order(stage_names: list[str]) -> None:
    if len(stage_names) != len(set(stage_names)):
        raise ValueError("stress report contains duplicate stages")
    unknown = [name for name in stage_names if name not in REQUIRED_STAGES]
    if unknown:
        raise ValueError(f"stress report contains unknown stage {unknown[0]!r}")
    non_cleanup = stage_names[:-1] if stage_names and stage_names[-1] == "cleanup" else stage_names
    expected_prefix = list(REQUIRED_STAGES[: len(non_cleanup)])
    if non_cleanup != expected_prefix:
        raise ValueError("stress report stages are not an ordered execution prefix")


def validate_stress_report(report: object) -> None:
    if not isinstance(report, dict):
        raise ValueError("stress report must be a JSON object")
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError("stress report has an unsupported schema_version")
    if report.get("scope") != "virtual-wbcan-only":
        raise ValueError("stress report must retain the virtual-wbcan-only scope")
    result = report.get("result")
    if result not in REPORT_RESULTS:
        raise ValueError("stress report result must be PASS, FAIL, or NOT_EXECUTED")
    for name in ("interface", "kernel", "python", "started_at", "completed_at"):
        if not isinstance(report.get(name), str) or not report[name].strip():
            raise ValueError(f"stress report requires non-empty {name}")
    try:
        started_at = int(report["started_at"])
        completed_at = int(report["completed_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("stress report timestamps must be integer strings") from exc
    if started_at < 0 or completed_at < started_at:
        raise ValueError("stress report timestamps are inconsistent")
    profile = _validate_report_profile(report.get("profile_config"))
    if report.get("profile") != profile.name:
        raise ValueError("stress report profile name is inconsistent")
    environment = report.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("stress report environment must be an object")
    for name in ("kernel", "python", "machine", "module_path"):
        if not isinstance(environment.get(name), str) or not environment[name].strip():
            raise ValueError(f"stress report environment requires {name}")
    if environment["kernel"] != report["kernel"] or environment["python"] != report["python"]:
        raise ValueError("stress report environment disagrees with top-level provenance")
    _integer(environment.get("euid"), "environment.euid")
    provenance = report.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("stress report provenance must be an object")
    for name in ("runner", "script"):
        if not isinstance(provenance.get(name), str) or not provenance[name].strip():
            raise ValueError(f"stress report provenance requires {name}")
    argv = provenance.get("argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise ValueError("stress report provenance argv must be a list of strings")
    _validate_evidence_boundary(report)
    _validate_budget(report, profile)

    stages = report.get("stages")
    if not isinstance(stages, list):
        raise ValueError("stress report stages must be a list")
    stage_names: list[str] = []
    failed = False
    not_executed_stage = False
    failure_seen = False
    for stage in stages:
        if not isinstance(stage, dict):
            raise ValueError("stress report stage must be an object")
        name = stage.get("name")
        stage_result = stage.get("result")
        duration_ms = stage.get("duration_ms")
        if not isinstance(name, str) or not name:
            raise ValueError("stress report stage requires a name")
        if failure_seen and name != "cleanup":
            raise ValueError("stress report contains stages after a failed stage")
        if stage_result not in REPORT_RESULTS:
            raise ValueError(f"stress report stage {name!r} has an invalid result")
        stage_max = MAX_TOTAL_DURATION_MS if name == "cleanup" else MAX_STAGE_DURATION_MS
        _integer(duration_ms, f"stage {name}.duration_ms", 0, stage_max)
        if stage_result == "FAIL":
            failed = True
            failure_seen = True
            if not isinstance(stage.get("error"), str) or not stage["error"].strip():
                raise ValueError(f"failed stress report stage {name!r} requires an error")
        elif stage_result == "NOT_EXECUTED":
            not_executed_stage = True
            if not isinstance(stage.get("error"), str) or not stage["error"].strip():
                raise ValueError(f"not-executed stress report stage {name!r} requires an error")
        elif duration_ms > profile.max_stage_duration_ms and name != "cleanup":
            raise ValueError(f"stress report stage {name!r} exceeded its stage budget")
        if name == "reconfiguration" and stage_result == "PASS":
            _validate_reconfiguration_details(stage.get("details"))
        if name == "drop_fault_accounting" and stage_result == "PASS":
            _validate_drop_fault_details(stage.get("details"))
        if name == "link_lifecycle" and stage_result == "PASS":
            _validate_lifecycle_details(stage.get("details"))
        if name == "stats_sampling" and stage_result == "PASS":
            _validate_stats_details(stage.get("details"))
        if name == "multi_producer_saturation" and stage_result == "PASS":
            _validate_saturation_details(stage.get("details"), profile)
        if name == "repeated_tx_full" and stage_result == "PASS":
            _validate_tx_full_details(stage.get("details"), profile)
        if name == "slow_receiver" and stage_result == "PASS":
            _validate_slow_receiver_details(stage.get("details"), profile)
        if name == "unload_reload" and stage_result == "PASS":
            _validate_unload_reload_details(stage.get("details"), profile)
        if name == "cleanup" and stage_result == "PASS":
            _validate_cleanup_details(stage.get("details"))
        stage_names.append(name)

    if result == "NOT_EXECUTED":
        reason = report.get("not_executed_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("not-executed stress report requires a reason")
        if failed or not_executed_stage or any(stage.get("result") == "PASS" for stage in stages):
            raise ValueError("not-executed stress report cannot contain executed stages")
        if stage_names:
            raise ValueError("not-executed stress report cannot contain stages")
        return

    if not_executed_stage:
        raise ValueError("executed stress report cannot contain a NOT_EXECUTED stage")
    _validate_stage_order(stage_names)
    if not stage_names or stage_names[-1] != "cleanup":
        raise ValueError("executed stress report must end with a cleanup stage")
    if result == "PASS":
        if failed or not stage_names or tuple(stage_names) != REQUIRED_STAGES:
            raise ValueError("passing stress report must contain every required passing stage in order")
        if any(stage.get("result") != "PASS" for stage in stages):
            raise ValueError("passing stress report cannot contain non-passing stages")
    elif not failed:
        raise ValueError("failed stress report must contain a failed stage")


def _validate_reconfiguration_details(details: object) -> None:
    if not isinstance(details, dict):
        raise ValueError("passing reconfiguration stage requires details")
    for name, minimum, maximum in (("frames_requested", 1, 1000), ("status_reads", 1, 5000), ("fault_rearms", 1, 2000)):
        _integer(details.get(name), f"reconfiguration.{name}", minimum, maximum)


def _validate_drop_fault_details(details: object) -> None:
    if not isinstance(details, dict):
        raise ValueError("passing drop-fault stage requires details")
    for name in (
        "drop_tx_expected",
        "drop_rx_expected",
        "intentional_loss",
        "unexplained_loss",
        "recovery_frames",
        "driver_rx_dropped",
    ):
        _integer(details.get(name), f"drop_fault_accounting.{name}")
    if details["drop_tx_expected"] < 1 or details["drop_rx_expected"] < 1:
        raise ValueError("drop-fault stage must exercise both intentional drop modes")
    if details["intentional_loss"] != details["drop_tx_expected"] + details["drop_rx_expected"]:
        raise ValueError("drop-fault intentional loss accounting is inconsistent")
    if details["unexplained_loss"] or details["driver_rx_dropped"] != details["drop_rx_expected"]:
        raise ValueError("drop-fault stage contains unexplained loss")
    if details["recovery_frames"] != 1:
        raise ValueError("drop-fault stage must prove one recovery frame")


def _validate_lifecycle_details(details: object) -> None:
    if not isinstance(details, dict):
        raise ValueError("passing link-lifecycle stage requires details")
    for name, minimum, maximum in (("cycles", 1, 100), ("frames_requested", 1, 5000), ("status_reads", 1, 10000)):
        _integer(details.get(name), f"link_lifecycle.{name}", minimum, maximum)


def _validate_stats_details(details: object) -> None:
    if not isinstance(details, dict):
        raise ValueError("passing stats stage requires details")
    _integer(details.get("samples"), "stats_sampling.samples", 2, MAX_STATS_SAMPLES)
    if details.get("regressions") != 0:
        raise ValueError("stats-sampling evidence contains a counter regression")


def _validate_saturation_details(details: object, profile: StressProfile | None = None) -> None:
    if not isinstance(details, dict):
        raise ValueError("passing saturation stage requires details")
    profile = profile or profile_config("developer-smoke")
    producer_count = details.get("producer_count")
    frame_count = details.get("frames_per_producer")
    max_no_progress = details.get("max_no_progress_ms")
    producers = details.get("producers")
    unexpected_frames = details.get("unexpected_frames")
    if producer_count != profile.producer_count or frame_count != profile.frames_per_producer:
        raise ValueError("saturation workload does not match the selected profile")
    if max_no_progress != profile.max_no_progress_ms:
        raise ValueError("saturation fairness budget does not match the selected profile")
    _integer(producer_count, "saturation producer_count", MIN_PRODUCERS, MAX_PRODUCERS)
    _integer(frame_count, "saturation frames_per_producer", MIN_FRAMES_PER_PRODUCER, MAX_FRAMES_PER_PRODUCER)
    _integer(max_no_progress, "saturation max_no_progress_ms", 0, MAX_TOTAL_DURATION_MS)
    _integer(unexpected_frames, "saturation unexpected_frames")
    if unexpected_frames:
        raise ValueError("saturation report contains unexpected frames")
    if not isinstance(producers, list) or len(producers) != producer_count:
        raise ValueError("saturation report must contain one result per producer")
    can_ids: set[str] = set()
    expected_ids = {f"0x{0x740 + index:03x}" for index in range(producer_count)}
    for producer in producers:
        if not isinstance(producer, dict):
            raise ValueError("saturation producer result must be an object")
        can_id = producer.get("can_id")
        if not isinstance(can_id, str) or not can_id.startswith("0x") or len(can_id) != 5 or can_id in can_ids:
            raise ValueError("saturation producer CAN IDs must be unique hexadecimal strings")
        try:
            parsed_id = int(can_id, 16)
        except ValueError as exc:
            raise ValueError("saturation producer CAN IDs must be unique hexadecimal strings") from exc
        if not 0x100 <= parsed_id <= 0x7FF:
            raise ValueError("saturation producer CAN ID is outside the standard CAN range")
        can_ids.add(can_id)
        for name in (
            "requested",
            "sent",
            "received",
            "lost",
            "duplicate",
            "reordered",
            "unexpected",
            "longest_no_progress_ms",
        ):
            value = producer.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"saturation producer {can_id} has invalid {name}")
        if producer["requested"] != frame_count or producer["sent"] != frame_count:
            raise ValueError(f"saturation producer {can_id} did not send its bounded frame budget")
        if producer["received"] != frame_count:
            raise ValueError(f"saturation producer {can_id} did not receive its bounded frame budget")
        if producer["longest_no_progress_ms"] > max_no_progress:
            raise ValueError(f"saturation producer {can_id} exceeded its fairness budget")
        if any(producer[name] for name in ("lost", "duplicate", "unexpected")):
            raise ValueError(f"saturation producer {can_id} contains unexplained delivery anomalies")
    if can_ids != expected_ids:
        raise ValueError("saturation producer CAN IDs are not the disjoint deterministic allocation")


def _validate_tx_full_details(details: object, profile: StressProfile | None = None) -> None:
    if not isinstance(details, dict):
        raise ValueError("passing tx-full stage requires details")
    profile = profile or profile_config("developer-smoke")
    attempts = details.get("attempts")
    delivered = details.get("delivered_once")
    if attempts != profile.tx_full_attempts:
        raise ValueError("tx-full attempts do not match the selected profile")
    _integer(attempts, "tx-full attempts", MIN_TX_FULL_ATTEMPTS, MAX_TX_FULL_ATTEMPTS)
    _integer(delivered, "tx-full delivered_once", 0, MAX_TX_FULL_ATTEMPTS)
    if delivered != attempts:
        raise ValueError("every tx-full retry must deliver exactly once")


def _validate_slow_receiver_details(details: object, profile: StressProfile | None = None) -> None:
    if not isinstance(details, dict):
        raise ValueError("passing slow-receiver stage requires details")
    profile = profile or profile_config("developer-smoke")
    for name in ("sent", "received", "expected_socket_loss", "duplicate", "unexpected", "driver_rx_dropped"):
        value = details.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"slow-receiver evidence has invalid {name}")
    if details["sent"] != profile.slow_receiver_frames:
        raise ValueError("slow-receiver workload does not match the selected profile")
    if details["sent"] < 1 or details["received"] + details["expected_socket_loss"] != details["sent"]:
        raise ValueError("slow-receiver delivery accounting is inconsistent")
    if details["duplicate"] or details["unexpected"] or details["driver_rx_dropped"]:
        raise ValueError("slow-receiver evidence contains unexplained driver anomalies")


def _validate_unload_reload_details(details: object, profile: StressProfile) -> None:
    if not isinstance(details, dict):
        raise ValueError("passing unload-reload stage requires details")
    for name in (
        "requested_cycles",
        "completed_cycles",
        "module_absence_checks",
        "interface_absence_checks",
        "debugfs_absence_checks",
        "post_reload_frames",
        "stale_frames",
        "open_socket_count",
        "live_thread_count",
    ):
        _integer(details.get(name), f"unload_reload.{name}")
    if details["requested_cycles"] != profile.reload_cycles or details["completed_cycles"] != profile.reload_cycles:
        raise ValueError("unload-reload cycle count does not match the selected profile")
    for name in ("module_absence_checks", "interface_absence_checks", "debugfs_absence_checks", "post_reload_frames"):
        if details[name] != profile.reload_cycles:
            raise ValueError(f"unload-reload evidence is incomplete for {name}")
    if details["stale_frames"] or details["open_socket_count"] or details["live_thread_count"]:
        raise ValueError("unload-reload evidence contains stale resources or frames")


def _validate_cleanup_details(details: object) -> None:
    if not isinstance(details, dict):
        raise ValueError("passing cleanup stage requires details")
    _integer(details.get("open_socket_count"), "cleanup.open_socket_count")
    _integer(details.get("live_thread_count"), "cleanup.live_thread_count")
    if details["open_socket_count"] != 0 or details["live_thread_count"] != 0:
        raise ValueError("cleanup stage left live resources")
    for name in (
        "module_loaded",
        "interface_present",
        "debugfs_present",
        "link_active",
        "fault_cleared",
        "subprocesses_reaped",
    ):
        if details.get(name) is not True:
            raise ValueError(f"cleanup stage did not verify {name}")


def write_stress_report(path: Path, report: dict[str, Any]) -> None:
    validate_stress_report(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_stress_report(path: Path) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        report: dict[str, object] = {}
        for key, value in pairs:
            if key in report:
                raise ValueError(f"stress report contains duplicate key {key!r}")
            report[key] = value
        return report

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)


def _stage(
    name: str,
    operation: Callable[[], dict[str, Any] | None],
    stages: list[dict[str, Any]],
    probe: Probe,
    max_duration_ms: int,
) -> None:
    started = time.monotonic()
    try:
        probe.ensure_budget()
        details = operation()
        duration_ms = round((time.monotonic() - started) * 1000)
        if duration_ms > max_duration_ms:
            raise BudgetExceededError(f"stage {name} exceeded its {max_duration_ms} ms budget")
    except Exception as exc:
        stages.append(
            {
                "name": name,
                "result": "FAIL",
                "duration_ms": round((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        raise
    stage: dict[str, Any] = {"name": name, "result": "PASS", "duration_ms": duration_ms}
    if details is not None:
        stage["details"] = details
    stages.append(stage)


def _preflight(interface: str, debugfs: Path, module_path: Path) -> None:
    def safe_exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    def safe_is_file(path: Path) -> bool:
        try:
            return path.is_file()
        except OSError:
            return False

    def safe_is_dir(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False

    problems: list[str] = []
    if getattr(os, "geteuid", lambda: -1)() != 0:
        problems.append("root privileges are required for unload/reload")
    for command in ("ip", "insmod", "rmmod"):
        if shutil.which(command) is None:
            problems.append(f"required command is unavailable: {command}")
    if not safe_is_file(module_path):
        problems.append(f"built module is unavailable: {module_path}")
    if not safe_exists(Path("/sys/module/wbcan")):
        problems.append("wbcan is not loaded")
    if not safe_exists(Path("/sys/class/net").joinpath(interface)):
        problems.append(f"CAN interface is unavailable: {interface}")
    if not safe_is_dir(debugfs):
        problems.append(f"wbcan debugfs directory is unavailable: {debugfs}")
    elif not safe_is_file(debugfs / "status") or not safe_exists(debugfs / "inject"):
        problems.append("wbcan debugfs status/inject files are unavailable")
    if problems:
        raise NotExecutedError("; ".join(problems))


def _build_report(
    *,
    interface: str,
    module_path: Path,
    profile: StressProfile,
    started_at: int,
    completed_at: int,
    elapsed_ms: int,
    result: str,
    stages: list[dict[str, Any]],
    reason: str | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "scope": "virtual-wbcan-only",
        "result": result,
        "profile": profile.name,
        "profile_config": _profile_payload(profile),
        "interface": interface,
        "kernel": platform.release(),
        "python": platform.python_version(),
        "started_at": str(started_at),
        "completed_at": str(completed_at),
        "elapsed_ms": elapsed_ms,
        "environment": {
            "kernel": platform.release(),
            "python": platform.python_version(),
            "machine": platform.machine() or "unknown",
            "module_path": str(module_path),
            "euid": getattr(os, "geteuid", lambda: -1)(),
        },
        "provenance": {
            "runner": "python3 test_state_concurrency.py",
            "script": str(Path(__file__).resolve()),
            "argv": list(sys.argv),
        },
        "evidence_boundary": {
            "scope": "virtual-wbcan-only",
            "physical_can": "NOT_EXECUTED",
            "mcu": "NOT_EXECUTED",
            "actuator": "NOT_EXECUTED",
            "hard_real_time": "NOT_EXECUTED",
        },
        "budget": {
            "max_duration_ms": profile.max_duration_ms,
            "max_stage_duration_ms": profile.max_stage_duration_ms,
            "elapsed_ms": elapsed_ms,
            "within_budget": elapsed_ms <= profile.max_duration_ms,
            "producer_count": profile.producer_count,
            "frames_per_producer": profile.frames_per_producer,
            "tx_full_attempts": profile.tx_full_attempts,
            "slow_receiver_frames": profile.slow_receiver_frames,
            "reload_cycles": profile.reload_cycles,
            "total_frame_budget": profile.producer_count * profile.frames_per_producer,
            "baseline": _baseline_payload(profile),
        },
        "stages": stages,
    }
    if reason is not None:
        report["not_executed_reason"] = reason
    return report


def run_probe(
    interface: str,
    debugfs: Path,
    report_path: Path | None,
    producer_count: int | None = None,
    frames_per_producer: int | None = None,
    *,
    profile_name: str = "developer-smoke",
    tx_full_attempts: int | None = None,
    slow_receiver_frames: int | None = None,
    reload_cycles: int | None = None,
    module_path: Path | None = None,
) -> int:
    profile = resolve_profile(
        profile_name,
        producer_count=producer_count,
        frames_per_producer=frames_per_producer,
        tx_full_attempts=tx_full_attempts,
        slow_receiver_frames=slow_receiver_frames,
        reload_cycles=reload_cycles,
    )
    selected_module = (module_path or Path(__file__).resolve().with_name("wbcan.ko")).resolve()
    started_at = time.time_ns()
    started_monotonic = time.monotonic()
    try:
        _preflight(interface, debugfs, selected_module)
    except NotExecutedError as exc:
        report = _build_report(
            interface=interface,
            module_path=selected_module,
            profile=profile,
            started_at=started_at,
            completed_at=time.time_ns(),
            elapsed_ms=round((time.monotonic() - started_monotonic) * 1000),
            result="NOT_EXECUTED",
            stages=[],
            reason=str(exc),
        )
        if report_path is not None:
            write_stress_report(report_path, report)
        print(f"NOT_EXECUTED: {exc}", file=sys.stderr)
        return 2

    probe = Probe(interface, debugfs, selected_module, profile.max_duration_ms)
    stages: list[dict[str, Any]] = []
    failure: Exception | None = None
    try:
        operations = (
            ("reconfiguration", probe.exercise_reconfiguration),
            ("drop_fault_accounting", probe.exercise_drop_faults),
            ("link_lifecycle", probe.exercise_link_lifecycle),
            ("stop_drain", probe.exercise_stop_drain),
            ("bus_off_restart", probe.exercise_bus_off_restart),
            ("restart_cancellation", probe.exercise_restart_cancellation),
            ("restart_stop_race", probe.exercise_restart_stop_race),
            ("queue_recovery", probe.verify_queue_recovery),
            ("stats_sampling", probe.exercise_stats_sampling),
            ("repeated_tx_full", lambda: probe.exercise_repeated_tx_full(profile.tx_full_attempts)),
            ("slow_receiver", lambda: probe.exercise_slow_receiver(profile.slow_receiver_frames)),
            (
                "multi_producer_saturation",
                lambda: probe.exercise_multi_producer_saturation(
                    profile.producer_count,
                    profile.frames_per_producer,
                    profile.max_no_progress_ms,
                ),
            ),
            ("unload_reload", lambda: probe.exercise_unload_reload(profile.reload_cycles)),
        )
        for name, operation in operations:
            _stage(name, operation, stages, probe, profile.max_stage_duration_ms)
    except Exception as exc:  # noqa: BLE001 - preserve the probe failure through cleanup.
        failure = exc
    finally:
        cleanup_started = time.monotonic()
        probe.cleanup_mode = True
        try:
            cleanup_details = probe.cleanup()
        except Exception as exc:  # noqa: BLE001 - report probe and cleanup failures together.
            stages.append(
                {
                    "name": "cleanup",
                    "result": "FAIL",
                    "duration_ms": round((time.monotonic() - cleanup_started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if failure is None:
                failure = exc
            else:
                failure = ExceptionGroup("probe and cleanup failures", [failure, exc])
        else:
            stages.append(
                {
                    "name": "cleanup",
                    "result": "PASS",
                    "duration_ms": round((time.monotonic() - cleanup_started) * 1000),
                    "details": cleanup_details,
                }
            )

    elapsed_ms = round((time.monotonic() - started_monotonic) * 1000)
    if elapsed_ms > profile.max_duration_ms and failure is None:
        failure = BudgetExceededError(f"probe exceeded its {profile.max_duration_ms} ms wall-clock budget")
        cleanup_stage = next(stage for stage in reversed(stages) if stage["name"] == "cleanup")
        cleanup_stage["result"] = "FAIL"
        cleanup_stage["error"] = str(failure)
    result = "FAIL" if failure is not None else "PASS"
    if report_path is not None:
        report = _build_report(
            interface=interface,
            module_path=selected_module,
            profile=profile,
            started_at=started_at,
            completed_at=time.time_ns(),
            elapsed_ms=elapsed_ms,
            result=result,
            stages=stages,
        )
        write_stress_report(report_path, report)
    if failure is not None:
        raise failure
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("interface", nargs="?")
    parser.add_argument("debugfs", nargs="?", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--validate-report", type=Path)
    parser.add_argument("--write-not-executed-report", type=Path)
    parser.add_argument("--not-executed-reason")
    parser.add_argument("--require-pass", action="store_true")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="developer-smoke")
    parser.add_argument("--module", type=Path, default=Path(__file__).resolve().with_name("wbcan.ko"))
    parser.add_argument("--producers", type=int)
    parser.add_argument("--frames-per-producer", type=int)
    parser.add_argument("--tx-full-attempts", type=int)
    parser.add_argument("--slow-receiver-frames", type=int)
    parser.add_argument("--reload-cycles", type=int)
    args = parser.parse_args()
    if args.write_not_executed_report is not None:
        if (
            args.interface is not None
            or args.debugfs is not None
            or args.report is not None
            or args.validate_report is not None
        ):
            parser.error("--write-not-executed-report cannot be combined with probe or validation arguments")
        if not args.not_executed_reason or not args.not_executed_reason.strip():
            parser.error("--write-not-executed-report requires --not-executed-reason")
        profile = resolve_profile(args.profile)
        started_at = time.time_ns()
        report = _build_report(
            interface="wbcan0",
            module_path=args.module.resolve(),
            profile=profile,
            started_at=started_at,
            completed_at=time.time_ns(),
            elapsed_ms=0,
            result="NOT_EXECUTED",
            stages=[],
            reason=args.not_executed_reason,
        )
        write_stress_report(args.write_not_executed_report, report)
        print(f"wbcan stress report recorded as NOT_EXECUTED: {args.write_not_executed_report}")
        return 0
    if args.validate_report is not None:
        if (
            args.interface is not None
            or args.debugfs is not None
            or args.report is not None
            or args.write_not_executed_report is not None
        ):
            parser.error("--validate-report cannot be combined with probe arguments")
        try:
            report = _load_stress_report(args.validate_report)
            validate_stress_report(report)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        if args.require_pass and report.get("result") != "PASS":
            parser.error(f"stress report result is {report.get('result')!r}, PASS is required")
        print(f"wbcan stress report valid: {args.validate_report} ({report['result']})")
        return 0
    if args.require_pass:
        parser.error("--require-pass requires --validate-report")
    if args.interface is None or args.debugfs is None:
        parser.error("INTERFACE and DEBUGFS_DIRECTORY are required")
    try:
        return run_probe(
            args.interface,
            args.debugfs,
            args.report,
            args.producers,
            args.frames_per_producer,
            profile_name=args.profile,
            tx_full_attempts=args.tx_full_attempts,
            slow_receiver_frames=args.slow_receiver_frames,
            reload_cycles=args.reload_cycles,
            module_path=args.module,
        )
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
