#!/usr/bin/env python3
"""Sample Docker CPU/RAM usage while exercising the read-only API."""

import argparse
import json
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from performance_tools import software_environment, summarize_resource_samples, write_json_report


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
    args = parser.parse_args()
    if args.samples <= 0 or args.interval <= 0:
        raise ValueError("samples and interval must be positive")
    containers = resolve_containers(args.project, args.container)
    samples: list[dict] = []
    started_at = datetime.now(UTC).isoformat()
    for index in range(args.samples):
        try:
            with urllib.request.urlopen(f"{args.url}/api/runs", timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError(f"API returned {response.status}")
        except OSError as exc:
            raise RuntimeError(f"API probe failed: {exc}") from exc
        samples.extend(docker_stats(containers))
        if index + 1 < args.samples:
            time.sleep(args.interval)
    report = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "environment": software_environment(),
        "containers": containers,
        "sample_count": args.samples,
        "interval_s": args.interval,
        "resources": summarize_resource_samples(samples),
    }
    write_json_report(args.output, report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
