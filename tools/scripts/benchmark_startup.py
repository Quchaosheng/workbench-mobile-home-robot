#!/usr/bin/env python3
"""Measure compose build, process start, health and readiness phases."""

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from performance_tools import code_revision, software_environment


def command(args: list[str], environment: dict[str, str]) -> str:
    result = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    return result.stdout.strip()


def wait_http(url: str, timeout_s: float) -> tuple[float, dict]:
    """Poll `url` until it answers 200, bounded by the caller's deadline.

    Each attempt is preceded by a monotonic deadline check, so a slow or
    repeatedly failing endpoint cannot extend the wait past `timeout_s`. When
    the budget is exhausted the raised error names the last observed cause, so a
    timeout is diagnosable instead of a bare "too slow".
    """
    deadline = time.perf_counter() + timeout_s
    attempts = 0
    last_cause = "no attempt was made"
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {url} after {attempts} attempt(s) in {timeout_s}s: {last_cause}")
        attempts += 1
        try:
            with urllib.request.urlopen(url, timeout=min(2.0, remaining)) as response:
                payload = json.loads(response.read())
                if response.status == 200:
                    return time.perf_counter(), payload
                last_cause = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            last_cause = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            last_cause = f"connection error: {exc.reason!r}"
        except (OSError, json.JSONDecodeError) as exc:
            last_cause = f"{type(exc).__name__}"
        # A fixed sleep would both overshoot a short deadline and add latency to
        # a fast one, so the wait is capped by the time actually remaining.
        time.sleep(max(0.0, min(0.25, deadline - time.perf_counter())))


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Workbench-1 compose startup")
    parser.add_argument("--compose-file", type=Path, default=Path("compose.yaml"))
    parser.add_argument("--project", default="workbench-startup-benchmark")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()
    compose = ["docker", "compose", "--project-name", args.project, "--file", str(args.compose_file)]
    environment = os.environ.copy()
    environment["WORKBENCH_PORT"] = str(args.port)
    started_at = datetime.now(UTC).isoformat()
    build_started = time.perf_counter()
    if not args.skip_build:
        build_args = [*compose, "build"]
        if args.no_cache:
            build_args.append("--no-cache")
        command(build_args, environment)
    build_s = time.perf_counter() - build_started if not args.skip_build else None
    up_started = time.perf_counter()
    command([*compose, "up", "-d", "--remove-orphans"], environment)
    health_at, health_payload = wait_http(f"{args.url}/healthz", args.timeout)
    ready_at, ready_payload = wait_http(f"{args.url}/readyz", args.timeout)
    report = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "compose_file": str(args.compose_file),
        "project": args.project,
        "cache_mode": "disabled" if args.no_cache else "standard",
        "environment": software_environment(),
        "revision": code_revision(),
        "target_full_stack_s": 120,
        "phases_s": {
            "image_build": build_s,
            "container_start_to_health": health_at - up_started,
            "container_start_to_ready": ready_at - up_started,
            "full_stack_clone_to_ready": (build_s or 0) + ready_at - up_started,
        },
        "health": health_payload,
        "ready": ready_payload,
        "meets_target": (build_s or 0) + ready_at - up_started < 120,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not args.keep_running:
        subprocess.run(
            [*compose, "down", "--remove-orphans"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
