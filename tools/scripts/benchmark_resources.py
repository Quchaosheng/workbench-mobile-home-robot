#!/usr/bin/env python3
"""Sample Docker CPU/RAM usage while exercising the read-only API."""

import argparse
import json
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from performance_tools import (
    code_revision,
    percentile,
    software_environment,
    summarize_resource_samples,
    write_json_report,
)


def event_log_bytes(data_dir: Path) -> int:
    """Total size of the event logs the read model serves, bounded by the same cap."""
    if not data_dir.is_dir():
        return 0
    total = 0
    for path in sorted(data_dir.rglob("*.jsonl")):
        total += path.stat().st_size
    return total


def count_runs(data_dir: Path) -> int:
    """Count distinct event-log files, one per run, without parsing them."""
    if not data_dir.is_dir():
        return 0
    return sum(1 for _ in data_dir.rglob("*.jsonl"))


def docker_stats(containers: list[str]) -> list[dict]:
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers],
        check=True,
        capture_output=True,
        text=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def resolve_containers(project: str | None, explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    if not project:
        raise ValueError("provide --project or at least one --container")
    result = subprocess.run(
        ["docker", "compose", "--project-name", project, "ps", "-q"],
        check=True,
        capture_output=True,
        text=True,
    )
    containers = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not containers:
        raise RuntimeError(f"compose project has no running containers: {project}")
    return containers


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect reproducible Docker resource baselines")
    parser.add_argument("--project")
    parser.add_argument("--container", action="append", default=[])
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("runs"))
    args = parser.parse_args()
    if args.samples <= 0 or args.interval <= 0:
        raise ValueError("samples and interval must be positive")
    containers = resolve_containers(args.project, args.container)
    samples: list[dict] = []
    api_latencies: list[float] = []
    api_failures = 0
    started_at = datetime.now(UTC).isoformat()
    event_log_start = event_log_bytes(args.data_dir)
    runs_start = count_runs(args.data_dir)
    for index in range(args.samples):
        probe_started = time.perf_counter()
        try:
            with urllib.request.urlopen(f"{args.url}/api/runs", timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError(f"API returned {response.status}")
            api_latencies.append((time.perf_counter() - probe_started) * 1000)
        except OSError as exc:
            # A single failed probe is measurement, not a fatal error: the
            # failure rate is part of the budget being established.
            api_failures += 1
            if index + 1 == args.samples and not api_latencies:
                raise RuntimeError(f"API probe failed: {exc}") from exc
        samples.extend(docker_stats(containers))
        if index + 1 < args.samples:
            time.sleep(args.interval)
    event_log_end = event_log_bytes(args.data_dir)
    runs_end = count_runs(args.data_dir)
    completed_runs = max(0, runs_end - runs_start)
    event_growth = max(0, event_log_end - event_log_start)
    probes = len(api_latencies) + api_failures
    report = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "environment": software_environment(),
        "revision": code_revision(),
        "containers": containers,
        "sample_count": args.samples,
        "interval_s": args.interval,
        "resources": summarize_resource_samples(samples),
        "event_log": {
            "max_bytes": event_log_end,
            "growth_bytes": event_growth,
            "growth_bytes_per_run": (event_growth / completed_runs) if completed_runs else event_growth,
            "completed_runs": completed_runs,
            "concurrent_runs_max": runs_end,
        },
        "api": {
            "samples": probes,
            "latency_p50_ms": percentile(api_latencies, 0.50),
            "latency_p95_ms": percentile(api_latencies, 0.95),
            "latency_p99_ms": percentile(api_latencies, 0.99),
            "failure_rate": (api_failures / probes) if probes else 0.0,
            "unit": "ms",
        },
    }
    write_json_report(args.output, report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
