#!/usr/bin/env python3
"""Measure the target module's thermal and power behaviour (#584).

`BSP-PERF-001` in `bsp/validation/bringup-plan.yaml` asks for
``cpu_gpu_thermal_power_and_can_load`` and sits at ``NOT_EXECUTED``, because there
was no procedure to run. This script is that procedure: it samples the module at a
fixed interval while a real workload runs, and writes one checksummed artifact.

Why the workload matters
------------------------

A synthetic ``stress-ng`` loop produces a number about the module. The number this
budget needs is about **this software**, so the default workload is the offline
demo plus the container image the release ships. ``--workload`` selects it, and a
caller that runs something else records that choice in the artifact rather than
leaving the reader to assume.

Why every refusal is named
--------------------------

A report with zeros in it is worse than no report: on a host without an NVIDIA
module ``nvpmodel`` is absent, every counter reads ``None``, and a naive script
writes a plausible-looking artifact that says the module is cool and idle. So:

* a missing ``nvpmodel`` is ``NVP MODEL_UNAVAILABLE``, not an empty power mode;
* a missing thermal zone is ``THERMAL_UNAVAILABLE``, not a temperature of 0;
* a missing workload command is ``WORKLOAD_UNAVAILABLE``, not a run of nothing.

Each refusal exits non-zero and writes **no** artifact. "We could not measure" must
never be recorded as "we measured and it was fine".

What this does not do
---------------------

It does not decide whether the measurement is acceptable. It records what happened
and the digest of the file; the budget in ``docs/performance/target-budget-v1.json``
says what is acceptable, and every row stays ``ESTIMATE`` until a real capture from
a real module is registered under ``BSP-PERF-001``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPORT_SCHEMA = "workbench-target-measurement-v1"

#: Where the module reports these on a Jetson. A different layout is a named
#: refusal rather than a silently empty sample: the point of the artifact is the
#: number, and a missing number must not read as zero.
THERMAL_ROOT = Path("/sys/devices/virtual/thermal")
PROC_STAT = Path("/proc/stat")
PROC_MEMINFO = Path("/proc/meminfo")

#: The workloads the budget wants a number for. `demo` is the offline demo, which
#: runs the runtime the release ships; `container` is the image itself.
WORKLOADS = {
    "demo": [sys.executable, "tools/scripts/demo_scripted.py"],
    "container": ["docker", "run", "--rm", "workbench-1:local", "python", "tools/scripts/demo_scripted.py"],
}

#: How often a sample is taken, and how long the default run is. Ten seconds gives
#: the module time to reach a steady thermal state after a cold start without
#: making CI minutes expensive.
DEFAULT_INTERVAL_S = 1.0
DEFAULT_DURATION_S = 60.0


class MeasurementRefused(RuntimeError):
    """A measurement could not be taken, which is a refusal and not an empty report."""


@dataclass
class Sample:
    elapsed_s: float
    per_core_utilization: list[float]
    memory_used_kb: int
    temperature_c: float
    gpu_utilization: float | None
    clock_mhz: float | None
    throttling: dict[str, Any] = field(default_factory=dict)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _cpu_times() -> list[tuple[int, int]]:
    """(busy, total) jiffies per core, from /proc/stat."""
    times: list[tuple[int, int]] = []
    for line in _read_text(PROC_STAT).splitlines():
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        fields = line.split()[1:]
        values = [int(value) for value in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        times.append((total - idle, total))
    if not times:
        raise MeasurementRefused("PROC_STAT_UNAVAILABLE: no per-core rows in /proc/stat")
    return times


def _memory_used_kb() -> int:
    try:
        text = _read_text(PROC_MEMINFO)
    except OSError as exc:
        raise MeasurementRefused(f"MEMINFO_UNAVAILABLE: {exc}") from exc
    total = available = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            available = int(line.split()[1])
    if total is None or available is None:
        raise MeasurementRefused("MEMINFO_INCOMPLETE: MemTotal or MemAvailable is missing")
    return max(0, total - available)


def _temperature() -> float:
    """The hottest thermal zone, in degrees Celsius."""
    if not THERMAL_ROOT.is_dir():
        raise MeasurementRefused(f"THERMAL_UNAVAILABLE: {THERMAL_ROOT} does not exist")
    hottest: float | None = None
    for zone in sorted(THERMAL_ROOT.glob("thermal_zone*")):
        candidate = zone / "temp"
        if not candidate.is_file():
            continue
        try:
            raw = int(_read_text(candidate).strip())
        except (OSError, ValueError):
            continue
        celsius = raw / 1000.0
        if hottest is None or celsius > hottest:
            hottest = celsius
    if hottest is None:
        raise MeasurementRefused(f"THERMAL_UNAVAILABLE: no readable thermal_zone*/temp under {THERMAL_ROOT}")
    return hottest


def _nvpmodel() -> str:
    """The power mode string, or a named refusal."""
    executable = shutil.which("nvpmodel")
    if executable is None:
        raise MeasurementRefused(
            "NVPMODEL_UNAVAILABLE: `nvpmodel` is not on PATH. This host has no NVIDIA module, "
            "so the power mode cannot be recorded and the measurement is refused rather than "
            "written with an empty mode."
        )
    result = subprocess.run([executable, "-q"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MeasurementRefused(f"NVPMODEL_FAILED: `nvpmodel -q` exited {result.returncode}: {result.stderr.strip()}")
    return " ".join(result.stdout.split())


def _gpu_utilization() -> float | None:
    """GPU busy percentage, or None when no GPU is readable.

    None is not zero: a host with no GPU has no GPU number, and writing 0.0 would
    say the GPU was idle rather than absent.
    """
    if not Path("/usr/bin/tegrastats").exists() and not Path("/usr/sbin/tegrastats").exists():
        return None
    executable = shutil.which("tegrastats") or "/usr/bin/tegrastats"
    try:
        # tegrastats samples once per invocation; one line is enough for a sample.
        result = subprocess.run(
            [executable, "--interval", "1000"], capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"GR3D_FREQ\s+(\d+)%", result.stdout)
    return float(match.group(1)) if match else None


def _clock_state() -> float | None:
    """The current average CPU clock in MHz, or None when it is not readable."""
    for path in Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_cur_freq"):
        try:
            return int(_read_text(path).strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return None


def _throttling() -> dict[str, Any]:
    """The throttling counters, or a recorded absence."""
    counters = {}
    for path in Path("/sys/devices/system/cpu").glob("cpu*/thermal_throttle/*_throttle_count"):
        try:
            counters[path.name.removesuffix("_throttle_count") + "_" + path.parent.name] = int(_read_text(path).strip())
        except (OSError, ValueError):
            continue
    return counters or {"unavailable": "no thermal_throttle counters on this host"}


def _soc_and_jetpack() -> dict[str, Any]:
    """The SoC model and the JetPack release, when the module exposes them."""
    model = None
    compatible = Path("/proc/device-tree/compatible")
    if compatible.is_file():
        model = _read_text(compatible).replace("\x00", " ").strip()
    jetpack = None
    release = Path("/etc/nv_tegra_release")
    if release.is_file():
        jetpack = _read_text(release).strip().splitlines()[0]
    return {"soc": model or platform.machine(), "jetpack": jetpack, "platform": platform.platform()}


def measure(
    *,
    duration_s: float = DEFAULT_DURATION_S,
    interval_s: float = DEFAULT_INTERVAL_S,
    workload: str = "demo",
    command: list[str] | None = None,
) -> dict[str, Any]:
    """Run the workload and sample the module until it finishes or the duration elapses."""
    if duration_s <= 0 or interval_s <= 0:
        raise MeasurementRefused("duration and interval must be positive")
    if interval_s > duration_s:
        raise MeasurementRefused("interval cannot exceed the duration: that would produce no sample")

    selected = command or WORKLOADS.get(workload)
    if not selected:
        raise MeasurementRefused(f"WORKLOAD_UNAVAILABLE: no command for workload {workload!r}")

    # A missing executable is refused before the clock starts, so a typo does not
    # produce a zero-length artifact that looks like a completed measurement.
    if not Path(selected[0]).exists() and shutil.which(selected[0]) is None:
        raise MeasurementRefused(f"WORKLOAD_UNAVAILABLE: {selected[0]!r} is not executable on this host")

    power_mode = _nvpmodel()
    identity = _soc_and_jetpack()

    process = subprocess.Popen(selected, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    samples: list[Sample] = []
    previous = _cpu_times()
    started = time.monotonic()
    try:
        while time.monotonic() - started < duration_s:
            time.sleep(interval_s)
            current = _cpu_times()
            utilization = []
            for index, (busy, total) in enumerate(current):
                if index >= len(previous):
                    utilization.append(0.0)
                    continue
                previous_busy, previous_total = previous[index]
                delta_total = total - previous_total
                utilization.append(round(100.0 * (busy - previous_busy) / delta_total, 2) if delta_total > 0 else 0.0)
            previous = current
            samples.append(
                Sample(
                    elapsed_s=round(time.monotonic() - started, 3),
                    per_core_utilization=utilization,
                    memory_used_kb=_memory_used_kb(),
                    temperature_c=_temperature(),
                    gpu_utilization=_gpu_utilization(),
                    clock_mhz=_clock_state(),
                    throttling=_throttling(),
                )
            )
            if process.poll() is not None:
                break
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    if not samples:
        raise MeasurementRefused("NO_SAMPLES: the run produced no sample, so there is nothing to report")

    return {
        "schema_version": REPORT_SCHEMA,
        "measured_by": "tools/scripts/measure_target_power.py",
        "workload": {"name": workload, "command": selected, "duration_s": duration_s, "interval_s": interval_s},
        "identity": identity,
        "power_mode": power_mode,
        "samples": [sample.__dict__ for sample in samples],
        "summary": {
            "samples": len(samples),
            "memory_peak_kb": max(sample.memory_used_kb for sample in samples),
            "temperature_max_c": max(sample.temperature_c for sample in samples),
            "temperature_min_c": min(sample.temperature_c for sample in samples),
            "cpu_peak_percent": max((max(sample.per_core_utilization, default=0.0) for sample in samples), default=0.0),
            "gpu_peak_percent": max(
                (sample.gpu_utilization for sample in samples if sample.gpu_utilization is not None),
                default=None,
            ),
        },
        # Stated in the artifact, not only in a doc: this run is a measurement of a
        # module, and it does not by itself decide whether the module is acceptable.
        "claims": [
            "a measurement of the module under the named workload",
            "NOT a pass or fail against the budget; docs/performance/target-budget-v1.json decides that",
            "NOT physical validation of the robot",
        ],
    }


def write_artifact(report: dict[str, Any], path: Path) -> str:
    """Write the report and its digest. The digest is what a consumer verifies."""
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, help="write the artifact here; omit for a dry check")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--workload", choices=sorted(WORKLOADS), default="demo")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the measurement could be taken, without writing an artifact",
    )
    args = parser.parse_args(argv)

    try:
        if args.check:
            # Only the refusals: this is the mode a CI host without a module runs,
            # and it has to fail for the same named reason rather than pretending.
            _nvpmodel()
            _temperature()
            print("target measurement inputs are available")
            return 0
        report = measure(duration_s=args.duration, interval_s=args.interval, workload=args.workload)
    except MeasurementRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if args.output is None:
        print(json.dumps(report["summary"], indent=2))
        return 0
    digest = write_artifact(report, args.output)
    print(f"wrote {args.output} (sha256 {digest[:16]}…)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
