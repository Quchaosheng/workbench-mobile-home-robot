#!/usr/bin/env python3
"""Gazebo acceptance harness for the Revision D model (Issue #327).

Usage:
    python3 robot/description/revision_d/tools/gazebo_acceptance.py
    python3 robot/description/revision_d/tools/gazebo_acceptance.py --simulator-available true

The issue asks for Gazebo coverage of spawn, joint-limit rejection,
self/forbidden-volume collision checks, lift states, stabilizer states and clean
shutdown/restart.

Without a simulator every case is reported **NOT_EXECUTED** and the exit code is
2. That is deliberate and matches `tests/regression/run_frozen_scenarios.py`:
an unrun case is never reported as a pass just because the harness completed.
A caller that needs the cases to be green must provide the simulator.
"""

from __future__ import annotations

import argparse
import json
import shutil

CASES = (
    "spawn",
    "joint_limit_rejection",
    "self_collision",
    "forbidden_volume",
    "lift_states",
    "stabilizer_states",
    "shutdown_restart",
)
NOT_EXECUTED = "NOT_EXECUTED"


def simulator_available() -> bool:
    return shutil.which("gz") is not None or shutil.which("ign") is not None


def run(*, simulator_available: bool) -> dict:
    """Return the truthful result of every case.

    The simulator-backed branch is intentionally not a stub that returns zero:
    it raises until the Gazebo scenario runner for Revision D lands, so nobody
    can read a fabricated pass out of this file.
    """
    if not simulator_available:
        return {
            "status": NOT_EXECUTED,
            "reason": "no Gazebo on PATH; the #327 Gazebo acceptance cases were not run",
            "cases": {name: NOT_EXECUTED for name in CASES},
            "exit_code": 2,
        }

    raise RuntimeError(
        "a Gazebo installation was found, but the Revision D scenario runner is not "
        "implemented yet; refusing to report these cases as passing. Implement the "
        "spawn / limit / collision / lift / stabilizer / shutdown bodies here before "
        "claiming them (#327)."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simulator-available",
        choices=("true", "false"),
        help="override detection; useful for asserting the fail-closed path",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    available = simulator_available()
    if args.simulator_available is not None:
        available = args.simulator_available == "true"

    result = run(simulator_available=available)
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Revision D Gazebo acceptance: {result['status']}")
        if result.get("reason"):
            print(f"reason: {result['reason']}")
        for name, status in result["cases"].items():
            print(f"  {name}: {status}")
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
