#!/usr/bin/env python3
"""Gate the scenario capability matrix against the live registry (#311).

Issue #311 requires one authoritative view of what each scenario can do and who
owns each boundary, and it requires that a registry entry without a matrix row
* fail a deterministic validation command. This is that command.

The gate reads three committed artifacts and joins them:

* ``docs/architecture/scenario-capability-matrix-v1.json`` -- the machine-readable
  matrix;
* ``sim/registry/**`` -- the live registry, loaded through the Issue #300 reader
  so the same contract rules apply;
* ``docs/architecture/scenario-capability-matrix.md`` -- the generated page that
  a human reads, compared byte-for-byte under ``--check-generated``.

It reports the same three outcomes as the other quality gates in this
repository: ``0 PASS``, ``1 FAIL`` and ``2 INCOMPLETE``. INCOMPLETE is separate
because a gate that reports FAIL when it could not read its own matrix trains
reviewers to ignore it.

The gate is read-only. It never imports the runtime, never starts ROS, Gazebo or
hardware, and never writes a run or an event. A passing verdict states that the
matrix and the registry agree and that no row claims evidence stronger than its
manifest. It does not state that a scenario ran.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _paths import ROOT, enable_local_packages

enable_local_packages()

from workbench.kernel.scenario_capability import (
    DEFAULT_MATRIX_PATH,
    CapabilityMatrixError,
    generated_document,
    load_matrix,
    load_registered,
    validate_matrix,
)
from workbench.kernel.scenario_contract import (
    FAIL,
    INCOMPLETE,
    PASS,
    ContractError,
    approved_semantic_actions,
    load_contract,
)

GENERATED_PATH = ROOT / "docs/architecture/scenario-capability-matrix.md"


def _report(verdicts, limit: int = 40) -> None:
    failures = [verdict for verdict in verdicts if not verdict.ok]
    for verdict in failures[:limit]:
        for finding in verdict.findings:
            print(f"  {finding.code} {finding.path}: {finding.detail}", file=sys.stderr)
    if len(failures) > limit:
        print(f"  ... and {len(failures) - limit} more failing row(s)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the scenario capability matrix against the registry.")
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX_PATH, help="matrix JSON to validate")
    parser.add_argument(
        "--check-generated",
        action="store_true",
        help="also require the committed markdown page to match the JSON",
    )
    parser.add_argument(
        "--require-registered",
        action="store_true",
        help="require every published row to be a registered scenario",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    try:
        matrix = load_matrix(args.matrix)
        contract = load_contract()
        actions = approved_semantic_actions()
        registered = load_registered(ROOT)
    except (CapabilityMatrixError, ContractError) as error:
        print(f"INCOMPLETE: {error}", file=sys.stderr)
        return INCOMPLETE
    except Exception as error:  # noqa: BLE001 - a registry that cannot load is INCOMPLETE, not FAIL
        print(f"INCOMPLETE: the registry could not be loaded: {error}", file=sys.stderr)
        return INCOMPLETE

    exit_code, verdicts = validate_matrix(
        matrix,
        registered=registered,
        contract=contract,
        approved_actions=actions,
        root=ROOT,
        require_registered=args.require_registered,
    )

    rows = matrix["rows"]
    registered_rows = sum(1 for row in rows if isinstance(row, dict) and row.get("status") == "REGISTERED")

    if args.check_generated:
        expected = generated_document(matrix)
        try:
            actual = GENERATED_PATH.read_text(encoding="utf-8")
        except OSError as error:
            print(f"FAIL: cannot read the generated page {GENERATED_PATH}: {error}", file=sys.stderr)
            return FAIL
        if actual != expected:
            print(
                "FAIL: MATRIX_GENERATED_STALE docs/architecture/scenario-capability-matrix.md is not the "
                "document this matrix generates; run the generator and commit the result",
                file=sys.stderr,
            )
            return FAIL

    if args.as_json:
        import json

        print(
            json.dumps(
                {
                    "exit_code": exit_code,
                    "rows": len(rows),
                    "registered_rows": registered_rows,
                    "registered_identities": len(registered),
                    "failures": [
                        {"row": verdict.manifest, "codes": sorted({f.code for f in verdict.findings})}
                        for verdict in verdicts
                        if not verdict.ok
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for verdict in verdicts:
            if verdict.ok and verdict.findings:
                print(f"NOTICE {verdict.manifest}: {verdict.findings[0].detail}")
        print(
            f"{'PASS' if exit_code == PASS else 'FAIL'}: {len(rows)} matrix row(s) "
            f"({registered_rows} registered) over {len(registered)} registered identit(ies)"
        )
        _report(verdicts)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
