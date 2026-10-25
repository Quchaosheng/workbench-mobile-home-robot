#!/usr/bin/env python3
"""Gate the runtime-assurance register (Assurance Track v1).

The register is two committed artifacts: the invariants the runtime claims and the
acceptance matrix of fault cases that must hold them. A claim in a JSON file is
worthless unless something resolves it, so this gate joins every claim to code:

* an ``ENFORCED`` invariant must name a ``path.py::symbol`` that is defined in
  that file, and at least one test function that is defined in its test module;
* a ``NOT_IMPLEMENTED`` invariant must state a gap and cite no evidence;
* every acceptance row must name a declared terminal state, a three-valued
  verdict, and at least one declared invariant;
* only the nominal case may expect ``confirmed``; a fault case that expects a
  completion is refused by name;
* the generated page must be the document the two artifacts generate.

It reports the same three outcomes as the other quality gates in this repository:
``0 PASS``, ``1 FAIL`` and ``2 INCOMPLETE``. INCOMPLETE is separate because a gate
that reports FAIL when it could not read its own register trains reviewers to
ignore it.

The gate is read-only. It imports no runtime module, starts no ROS, Gazebo or
hardware, and never writes a run. A passing verdict says the register is
internally consistent and its evidence resolves. It does not say a fault was
injected on hardware, and it is not a functional-safety certification.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _paths import ROOT, enable_local_packages

enable_local_packages()

from workbench.kernel.assurance import (
    GENERATED_PATH,
    INVARIANTS_PATH,
    MATRIX_PATH,
    AssuranceError,
    generated_document,
    load_documents,
    validate,
)
from workbench.kernel.scenario_contract import FAIL, INCOMPLETE, PASS


def _report(verdict, limit: int = 40) -> None:
    failures = [finding for finding in verdict.findings if finding.severity != "notice"]
    for finding in failures[:limit]:
        print(f"  {finding.code} {finding.path}: {finding.detail}", file=sys.stderr)
    if len(failures) > limit:
        print(f"  ... and {len(failures) - limit} more finding(s)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the runtime-assurance register and its acceptance matrix.")
    parser.add_argument("--invariants", type=Path, default=INVARIANTS_PATH, help="invariant register JSON")
    parser.add_argument("--matrix", type=Path, default=MATRIX_PATH, help="acceptance matrix JSON")
    parser.add_argument(
        "--check-generated",
        action="store_true",
        help="also require the committed markdown page to match the two artifacts",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    try:
        documents = load_documents(args.invariants, args.matrix)
    except AssuranceError as error:
        print(f"INCOMPLETE: {error}", file=sys.stderr)
        return INCOMPLETE

    verdict = validate(documents, root=ROOT)
    if verdict.exit_code != PASS:
        _report(verdict)

    if args.check_generated:
        expected = generated_document(documents)
        try:
            actual = GENERATED_PATH.read_text(encoding="utf-8")
        except OSError as error:
            print(f"FAIL: cannot read the generated page {GENERATED_PATH}: {error}", file=sys.stderr)
            return FAIL
        if actual != expected:
            print(
                "FAIL: ASSURANCE_GENERATED_STALE docs/architecture/assurance-track.md is not the document the "
                "register generates; run the generator and commit the result",
                file=sys.stderr,
            )
            return FAIL

    invariants = documents.invariants["invariants"]
    rows = documents.matrix["rows"]
    enforced = sum(1 for entry in invariants if entry.get("status") == "ENFORCED")
    executed = sum(1 for row in rows if row.get("coverage") != "NOT_EXECUTED")

    if args.as_json:
        print(
            json.dumps(
                {
                    "exit_code": verdict.exit_code,
                    "invariants": len(invariants),
                    "enforced_invariants": enforced,
                    "acceptance_cases": len(rows),
                    "executed_cases": executed,
                    "findings": [
                        {"code": finding.code, "path": finding.path, "detail": finding.detail}
                        for finding in verdict.findings
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            f"{verdict.status}: {enforced}/{len(invariants)} invariants enforced, "
            f"{executed}/{len(rows)} acceptance cases executed"
        )
    return verdict.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
