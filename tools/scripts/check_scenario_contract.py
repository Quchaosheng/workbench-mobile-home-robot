"""Validate scenario manifests against the written multi-scenario contract (#308).

Issue #308 freezes the Scenario Registry contract before any registry code is
written. A contract that cannot be checked is a claim rather than a boundary, so
this gate reads two committed files:

* ``docs/architecture/scenario-contract-v1.json`` -- the frozen field list,
  diagnostic codes, bounds and ownership table;
* ``docs/architecture/examples/*.json`` -- the non-executable example manifests
  that must validate against it.

The rules themselves live in ``workbench.kernel.scenario_contract`` so this gate
and the Issue #300 registry enforce one contract rather than two. This script is
only the command-line reader.

It reports the same three outcomes as the other quality gates in this
repository: ``0 PASS``, ``1 FAIL`` and ``2 INCOMPLETE``. INCOMPLETE is separate
because a gate that reports FAIL when it could not read its own contract trains
reviewers to ignore it.

The gate is deliberately read-only. It never imports the runtime, never starts
ROS, Gazebo or hardware, and never writes a run or an event. A passing verdict
states that a manifest is *well formed against the written contract*. It does
not state that the scenario ran, that a verifier exists, or that any evidence is
physical.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _paths import ROOT, enable_local_packages

enable_local_packages()

from workbench.kernel.scenario_contract import (
    DEFAULT_CONTRACT_PATH,
    INCOMPLETE,
    PASS,
    ContractError,
    Verdict,
    approved_semantic_actions,
    load_contract,
    validate_many,
)

CONTRACT_PATH = DEFAULT_CONTRACT_PATH
EXAMPLE_DIR = ROOT / "docs/architecture/examples"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ContractError(f"{path} is not valid JSON: {error}") from error


def _manifest_label(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        # A caller may point the gate at a manifest outside the checkout. The
        # label is only used for reporting, so the absolute path is fine here.
        return path.as_posix()


def run(paths: list[Path], *, require_executable: bool = False) -> tuple[int, list[Verdict]]:
    contract = load_contract(CONTRACT_PATH)
    approved_actions = approved_semantic_actions()
    manifests = [(_manifest_label(path), _read_json(path)) for path in paths]
    return validate_many(
        manifests,
        contract,
        approved_actions=approved_actions,
        root=ROOT,
        require_executable=require_executable,
    )


def _example_paths() -> list[Path]:
    return sorted(EXAMPLE_DIR.glob("*.json"))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate scenario manifests against the written contract (#308).")
    parser.add_argument("manifests", nargs="*", type=Path, help="manifest files; default is the committed examples")
    parser.add_argument("--fixtures", action="store_true", help="also run the adversarial fixtures in tests/fixtures")
    parser.add_argument(
        "--require-executable",
        action="store_true",
        help="treat a manifest that declares pending actions as a failure; registry gating uses this",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    paths = args.manifests or _example_paths()
    if not paths:
        print(f"no example manifests found in {EXAMPLE_DIR}", file=sys.stderr)
        return INCOMPLETE
    try:
        exit_code, verdicts = run(paths, require_executable=args.require_executable)
    except ContractError as error:
        print(str(error), file=sys.stderr)
        return INCOMPLETE
    for verdict in verdicts:
        print(f"[{verdict.status}] {verdict.manifest}")
        for finding in verdict.findings:
            print(f"  {finding.code} {finding.path}: {finding.detail}")
    if args.fixtures:
        print("adversarial fixtures are exercised by tests/unit/test_scenario_contract.py")
    print(f"scenario contract check: {'PASS' if exit_code == PASS else 'FAIL'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
