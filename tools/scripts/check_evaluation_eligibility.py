#!/usr/bin/env python3
"""Decide whether an evaluation run directory may be release evidence.

This is the independent check behind the numbers in a release report. It reads
the event logs, the provenance record, the scenario manifests and the human audit
and recomputes the verdict with ``release_eligibility.evaluate_eligibility`` - the
same predicate the runner and the report use. It exits non-zero when a run is not
eligible, so it can gate a promotion step.

Nothing is repaired here. A missing provenance record, an edited log, a changed
manifest and an incomplete audit are each reported as a reason, and the reason
names which input disagreed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _jsonio import JsonInputError, load_json
from _paths import enable_local_packages

enable_local_packages()

from collect_metrics import load_runs
from release_eligibility import (
    discover_manifests,
    evaluate_eligibility,
    load_provenance,
    manifest_search_dirs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether evaluation runs may be release evidence")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--human-audit", type=Path)
    parser.add_argument(
        "--scenario-dir",
        type=Path,
        action="append",
        default=[],
        help="Directory to search for scenario manifests; defaults to sim/scenarios",
    )
    parser.add_argument("--output", type=Path, help="Write the verdict as JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        runs = load_runs(args.run_dir)
    except RuntimeError as exc:
        print(f"eligibility refused: {exc}", file=sys.stderr)
        return 2
    try:
        provenance: dict[str, Any] | None = load_provenance(args.provenance)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"eligibility refused: provenance is unusable: {exc}", file=sys.stderr)
        return 2
    audit: dict[str, Any] | None = None
    if args.human_audit is not None:
        try:
            audit = load_json(args.human_audit)
        except JsonInputError as exc:
            print(f"eligibility refused: human audit is unusable: {exc}", file=sys.stderr)
            return 2
        if not isinstance(audit, dict):
            print("eligibility refused: human audit is not an object", file=sys.stderr)
            return 2

    scenario_dirs = args.scenario_dir or manifest_search_dirs(args.run_dir)
    verdict = evaluate_eligibility(
        runs=runs,
        provenance=provenance,
        manifests=discover_manifests(scenario_dirs),
        audit=audit,
    )
    verdict["run_count"] = len(runs)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(verdict, indent=2, sort_keys=True))
    if not verdict["eligible"]:
        print(f"refused: {len(verdict['reasons'])} reason(s)", file=sys.stderr)
        return 1
    print(f"eligible: {len(runs)} run(s) verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
