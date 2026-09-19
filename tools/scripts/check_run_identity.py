#!/usr/bin/env python3
"""Check every stored simulation run is attributed to a scenario definition (#302).

A run directory that names no scenario, or names a version the registry does not
carry, or records an evidence policy or verifier rule the registry no longer
matches, is a run whose reduction and verification cannot be trusted. This gate
reads the stored bundles and the live registry and refuses that state.

Exit codes match the other gates: ``0 PASS``, ``1 FAIL``, ``2 INCOMPLETE``.
INCOMPLETE is separate because a gate that reports FAIL when it found no run to
inspect trains reviewers to ignore it.

The gate is read-only. It starts no simulator, writes no run or event, and its
verdict never claims that a run executed or that any evidence is physical.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _paths import ROOT, enable_local_packages

enable_local_packages()

from workbench.kernel.scenario_contract import FAIL, INCOMPLETE, PASS, ContractError
from workbench.kernel.scenario_identity import (
    EMITTED_CODES,
    RunIdentityError,
    load_registry_entries,
    scan_run_root,
)
from workbench.kernel.scenario_registry import ScenarioRegistryError

DEFAULT_RUNS_ROOT = ROOT / "runs"


def _report(verdicts, limit: int = 40) -> None:
    failures = [verdict for verdict in verdicts if not verdict.ok]
    for verdict in failures[:limit]:
        for finding in verdict.findings:
            print(
                f"  {finding.code} {finding.identity} [{verdict.run_id}]: {finding.detail}",
                file=sys.stderr,
            )
    if len(failures) > limit:
        print(f"  ... and {len(failures) - limit} more failing run(s)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate that every stored run is attributed to its scenario definition."
    )
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT, help="run directory root to inspect")
    parser.add_argument(
        "--registry-root", type=Path, default=ROOT / "sim/registry", help="registry manifest root to read"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    try:
        registry = load_registry_entries(ROOT, registry_root=args.registry_root)
    except RunIdentityError as error:
        print(f"INCOMPLETE: {error.code}: {error}", file=sys.stderr)
        return INCOMPLETE
    except (ContractError, ScenarioRegistryError) as error:
        print(
            f"INCOMPLETE: {getattr(error, 'code', 'SCENARIO_REGISTRY_INVALID')}: {error}",
            file=sys.stderr,
        )
        return INCOMPLETE

    try:
        verdicts = scan_run_root(ROOT, runs_root=args.runs_root, registry=registry)
    except RunIdentityError as error:
        print(f"INCOMPLETE: {error.code}: {error}", file=sys.stderr)
        return INCOMPLETE

    if not verdicts:
        print(
            f"INCOMPLETE: no run directory under {args.runs_root} carries a metadata.json; "
            "nothing was attributed and a pass would be vacuous",
            file=sys.stderr,
        )
        return INCOMPLETE

    exit_code = PASS if all(verdict.ok for verdict in verdicts) else FAIL

    if args.as_json:
        print(
            json.dumps(
                {
                    "exit_code": exit_code,
                    "registry_scenarios": len(registry),
                    "inspected": len(verdicts),
                    "diagnostic_codes": list(EMITTED_CODES),
                    "failures": [verdict.as_dict() for verdict in verdicts if not verdict.ok],
                    "runs": [verdict.as_dict() for verdict in verdicts],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return exit_code

    for verdict in verdicts:
        if verdict.ok:
            print(f"OK  {verdict.run_id}: {verdict.identity} {verdict.identity_hash[:16]}")
    _report(verdicts)
    print(
        f"{'PASS' if exit_code == PASS else 'FAIL'}: {len(verdicts)} stored run(s) attributed over "
        f"{len(registry)} registered scenario(s)"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
