#!/usr/bin/env python3
"""Gate the legacy task families against the registry that now owns them (#301).

Issue #301 asks for one selection path without rewriting the existing verifiers
or changing fixture semantics. That is a claim about *equivalence*, and a claim
that cannot be checked is not a boundary, so this gate reads three committed
artifact sets and joins them:

* ``sim/registry/**`` -- the manifests that are authoritative after the migration;
* ``sim/scenarios/**`` -- the legacy regression corpus, whose IDs, seeds and
  materialized scene parameters must be unchanged;
* the verifier entry points both sides name.

The rules live in ``workbench.kernel.scenario_migration`` so the gate and the
tests enforce one migration table rather than two. This script is only the
command-line reader.

It reports the same three outcomes as the other quality gates here: ``0 PASS``,
``1 FAIL`` and ``2 INCOMPLETE``. INCOMPLETE is separate because a gate that
reports FAIL when it could not read its own registry trains reviewers to ignore
it.

The gate is read-only. It never imports the runtime, never starts ROS, Gazebo or
hardware, and never writes a run or an event. A passing verdict states that the
legacy entry points and the registry resolve to the same definition. It does not
state that a scenario ran, and it is not physical evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _paths import ROOT, enable_local_packages

enable_local_packages()

from workbench.kernel.scenario_migration import ScenarioMigrationError, migration_report
from workbench.kernel.scenario_registry import ScenarioRegistryError

PASS = 0
FAIL = 1
INCOMPLETE = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Issue #301 task-family migration.")
    parser.add_argument("--repo-root", type=Path, default=ROOT, help="repository root to read")
    parser.add_argument("--registry-root", type=Path, default=None, help="override the registry manifest root")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    try:
        report = migration_report(args.repo_root, registry_root=args.registry_root)
    except ScenarioRegistryError as error:
        print(f"INCOMPLETE: {getattr(error, 'code', 'SCENARIO_REGISTRY_INVALID')}: {error}", file=sys.stderr)
        return INCOMPLETE
    except ScenarioMigrationError as error:
        print(f"INCOMPLETE: {error.code}: {error}", file=sys.stderr)
        return INCOMPLETE
    except Exception as error:  # noqa: BLE001 - a registry that cannot load is INCOMPLETE, not FAIL
        print(f"INCOMPLETE: the registry could not be loaded: {error}", file=sys.stderr)
        return INCOMPLETE

    families = report["families"]
    registered = sum(1 for family in families if family["registered"])

    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return PASS if report["ok"] else FAIL

    for finding in report["findings"]:
        print(f"{finding['code']} {finding['path']}: {finding['detail']}", file=sys.stderr)
    print(
        f"{'PASS' if report['ok'] else 'FAIL'}: {len(families)} task famil(ies) "
        f"({registered} registered) migrated; legacy entry points deprecated in {report['deprecation_release']}"
    )
    return PASS if report["ok"] else FAIL


if __name__ == "__main__":
    raise SystemExit(main())
