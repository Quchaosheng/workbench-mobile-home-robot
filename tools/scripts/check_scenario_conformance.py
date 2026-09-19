#!/usr/bin/env python3
"""Gate every registered scenario against the shared fail-closed boundaries (#304).

Issue #304 asks for one registry-wide validation command that a local scenario
cannot bypass. Contract validation already rejects a malformed manifest, but it
cannot reject one that is well formed and proves no evidence boundary at all,
which is the gap a new scenario slips through.

This gate closes that gap by joining three sources:

* ``sim/registry/**`` -- the live registry, loaded through the Issue #300 reader,
  which supplies the identity set so the matrix is discovered rather than listed;
* ``tools/qa/scenario-conformance-v1.json`` -- the committed case set, one per
  identity and one per required dimension, each naming a test that must resolve;
* the World Model verifiers -- driven by derived probes that mutate a confirmed
  state and require a refusal, so the rules are demonstrated rather than declared.

It reports the same three outcomes as the other quality gates in this repository:
``0 PASS``, ``1 FAIL`` and ``2 INCOMPLETE``. INCOMPLETE is separate because a gate
that reports FAIL when it could not read its own registry trains reviewers to
ignore it.

A failing run names the scenario identity and the rule, so CI does not require a
reader to guess which manifest is wrong.

The gate is read-only. It never starts ROS, Gazebo or hardware, never writes a
run or an event, and never edits a fixture. A passing verdict states that every
registered scenario is documented, testable and honest about its evidence
status. It does not state that a scenario ran, and it is not physical evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _paths import ROOT, enable_local_packages

enable_local_packages()

from workbench.kernel.scenario_conformance import (
    DEFAULT_CORPUS_PATH,
    REQUIRED_DIMENSIONS,
    ScenarioConformanceError,
    evaluate,
    load_corpus,
)
from workbench.kernel.scenario_contract import INCOMPLETE, PASS, ContractError
from workbench.kernel.scenario_registry import ScenarioRegistryError

REGISTRY_ROOT = ROOT / "sim/registry"


def _report(verdicts, limit: int = 40) -> None:
    failures = [verdict for verdict in verdicts if not verdict.ok]
    for verdict in failures[:limit]:
        for finding in verdict.findings:
            print(
                f"  {finding.code} {finding.identity} [{finding.dimension}]: {finding.detail}",
                file=sys.stderr,
            )
    if len(failures) > limit:
        print(f"  ... and {len(failures) - limit} more failing scenario(s)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate every registered scenario against the shared conformance boundaries."
    )
    parser.add_argument(
        "--corpus", type=Path, default=ROOT / DEFAULT_CORPUS_PATH, help="the committed case set to enforce"
    )
    parser.add_argument("--registry-root", type=Path, default=REGISTRY_ROOT, help="registry manifest root to read")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    try:
        corpus = load_corpus(args.corpus)
        from workbench.kernel.scenario_capability import load_registered

        registered = load_registered(ROOT, registry_root=args.registry_root)
    except ScenarioConformanceError as error:
        print(f"INCOMPLETE: {error.code}: {error}", file=sys.stderr)
        return INCOMPLETE
    except (ContractError, ScenarioRegistryError) as error:
        print(f"INCOMPLETE: {getattr(error, 'code', 'SCENARIO_REGISTRY_INVALID')}: {error}", file=sys.stderr)
        return INCOMPLETE
    except Exception as error:  # noqa: BLE001 - a registry that cannot load is INCOMPLETE, not FAIL
        print(f"INCOMPLETE: the registry could not be loaded: {error}", file=sys.stderr)
        return INCOMPLETE

    try:
        exit_code, verdicts = evaluate(
            root=ROOT,
            registry_entries=registered,
            corpus=corpus,
            corpus_path=args.corpus,
        )
    except ScenarioConformanceError as error:
        print(f"INCOMPLETE: {error.code}: {error}", file=sys.stderr)
        return INCOMPLETE

    if args.as_json:
        print(
            json.dumps(
                {
                    "exit_code": exit_code,
                    "corpus_version": corpus["corpus_version"],
                    "required_dimensions": list(REQUIRED_DIMENSIONS),
                    "scenario_count": len(registered),
                    "failures": [verdict.as_dict() for verdict in verdicts if not verdict.ok],
                    "scenarios": [verdict.as_dict() for verdict in verdicts],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return exit_code

    for verdict in verdicts:
        if verdict.ok:
            print(f"OK {verdict.identity}: {len(verdict.covered)} dimension(s) covered")
    print(
        f"{'PASS' if exit_code == PASS else 'FAIL'}: {len(registered)} registered scenario(s) "
        f"over {len(REQUIRED_DIMENSIONS)} required dimension(s)"
    )
    _report(verdicts)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
