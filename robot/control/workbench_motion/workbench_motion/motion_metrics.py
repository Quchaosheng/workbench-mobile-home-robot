"""Strict motion research journal and feedback-derived metrics, without ROS."""

from __future__ import annotations

import json
import math
import os
from itertools import pairwise
from pathlib import Path


class MotionJournal:
    """Append research samples; this is not the World Model event store."""

    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._file = path.open("x", encoding="utf-8")
        self.events: list[dict] = []
        self._sequence = 0

    def append(self, event: dict) -> None:
        row = {**event, "run_id": self.run_id, "schema_version": "motion-journal-v1", "sequence": self._sequence + 1}
        encoded = json.dumps(row, allow_nan=False, separators=(",", ":"), sort_keys=True)
        self._file.write(encoded + "\n")
        self._file.flush()
        if event.get("event") != "sample":
            os.fsync(self._file.fileno())
        self.events.append(json.loads(encoded))
        self._sequence += 1

    def release_events(self):
        """Release finalized trial rows; the on-disk sequence remains intact."""
        self.events.clear()

    def close(self):
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()


def _rms(values: list[list[float]], times: list[float]) -> list[float]:
    duration = times[-1] - times[0]
    return [
        math.sqrt(
            sum(
                (left[j] ** 2 + right[j] ** 2) * (b - a) / 2
                for left, right, a, b in zip(values, values[1:], times, times[1:], strict=False)
            )
            / duration
        )
        for j in range(len(values[0]))
    ]


def evaluate_samples(
    rows: list[dict], *, position_tolerance: float = 0.02, velocity_tolerance: float = 0.01, dwell_s: float = 0.2
) -> dict:
    samples = []
    for row in rows:
        if row.get("event") != "sample":
            continue
        if samples and row["state"]["observed_at_s"] == samples[-1]["state"]["observed_at_s"]:
            if row["state"] != samples[-1]["state"]:
                raise ValueError("conflicting feedback at identical timestamp")
            continue
        samples.append(row)
    if len(samples) < 2:
        return {"valid": False, "reason": "insufficient_samples", "samples": len(samples)}
    times = [row["state"]["observed_at_s"] for row in samples]
    if not all(math.isfinite(t) for t in times) or any(b <= a for a, b in pairwise(times)):
        raise ValueError("feedback timestamps must increase")
    names = samples[0]["state"]["joint_names"]
    size = len(names)
    for row in samples:
        if row["state"]["joint_names"] != names:
            raise ValueError("feedback joint schema changed")
        for source in (row["state"], row["desired"]):
            for field in ("positions", "velocities"):
                if len(source[field]) != size or not all(math.isfinite(v) for v in source[field]):
                    raise ValueError("metrics require complete finite arrays")
    position_error = [
        [a - b for a, b in zip(row["desired"]["positions"], row["state"]["positions"], strict=True)] for row in samples
    ]
    velocity_error = [
        [a - b for a, b in zip(row["desired"]["velocities"], row["state"]["velocities"], strict=True)]
        for row in samples
    ]
    acceleration = [
        [
            (b - a) / (right["state"]["observed_at_s"] - left["state"]["observed_at_s"])
            for a, b in zip(left["state"]["velocities"], right["state"]["velocities"], strict=True)
        ]
        for left, right in pairwise(samples)
    ]
    midpoints = [(a + b) / 2 for a, b in pairwise(times)]
    jerk = [
        [(b - a) / dt for a, b in zip(left, right, strict=True)]
        for left, right, dt in zip(
            acceleration, acceleration[1:], (b - a for a, b in pairwise(midpoints)), strict=False
        )
    ]
    target = samples[-1]["desired"]["positions"]
    settling_start = None
    for row in samples:
        settled = (
            all(abs(a - b) <= position_tolerance for a, b in zip(row["state"]["positions"], target, strict=True))
            and max(map(abs, row["state"]["velocities"])) <= velocity_tolerance
        )
        if not settled:
            settling_start = None
        elif settling_start is None:
            settling_start = row["state"]["observed_at_s"]
    settling_time = None
    if settling_start is not None and times[-1] - settling_start >= dwell_s:
        settling_time = settling_start - times[0]
    rms_q, rms_v = _rms(position_error, times), _rms(velocity_error, times)
    result = {
        "schema_version": "motion-metrics-v1",
        "valid": True,
        "samples": len(samples),
        "joint_names": names,
        "duration_s": times[-1] - times[0],
        "feedback_hz": (len(samples) - 1) / (times[-1] - times[0]),
        "rms_position_error_rad": rms_q,
        "max_position_error_rad": [max(abs(row[j]) for row in position_error) for j in range(size)],
        "rms_velocity_error_rad_s": rms_v,
        "max_velocity_error_rad_s": [max(abs(row[j]) for row in velocity_error) for j in range(size)],
        "acceleration_estimate_max_rad_s2": [max(abs(row[j]) for row in acceleration) for j in range(size)],
        "jerk_estimate_max_rad_s3": [max(abs(row[j]) for row in jerk) for j in range(size)] if jerk else None,
        "smoothness_integral_squared_jerk": sum(
            sum(v * v for v in row) * dt for row, dt in zip(jerk, (b - a for a, b in pairwise(midpoints)), strict=True)
        )
        if jerk
        else None,
        "derivative_source": "finite_difference_of_measured_velocity_at_feedback_timestamps",
        "settling_time_s": settling_time,
    }
    json.dumps(result, allow_nan=False)
    return result
