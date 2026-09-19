#!/usr/bin/env python3
"""Check every stored run declares the inputs its determinism depends on (#313).

A run directory records a status, a seed and a scene hash. Nothing checked that
the provenance those depend on - the seed, the clock and time source the events
were stamped on, the ordering rule, the adapter versions, and which environment
class the run belongs to - was recorded at all, or that the environment class the
run claims is one the runner that ran could honestly produce.

That is the gap: two runs of one scenario with different seeds reduced to
different states under one identity, and a scripted fixture could be compared to
a Gazebo run as though their state hashes meant the same thing.

Exit codes match the other gates: ``0 PASS``, ``1 FAIL``, ``2 INCOMPLETE``.
INCOMPLETE is separate because a gate that reports FAIL when it found no run to
inspect trains reviewers to ignore it.

The gate is read-only. It starts no simulator, writes no run or event, and its
verdict never claims a run executed or that any evidence is physical.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _paths import ROOT, enable_local_packages

enable_local_packages()

from workbench.kernel.run_provenance import (
    EMITTED_CODES,
    ProvenanceFinding,
    ProvenanceVerdict,
    verify_bundle_provenance,
)

DEFAULT_RUNS_ROOT = ROOT / "runs"


def _verdict_for(run_dir: Path, metadata: dict) -> ProvenanceVerdict:
    """Check one run's provenance, using the runner it recorded as the claim."""

    return verify_bundle_provenance(
        run_id=run_dir.name,
        metadata=metadata,
        runner=metadata.get("runner"),
        status=metadata.get("status"),
    )


def scan_run_root(runs_root: Path) -> list[ProvenanceVerdict]:
    """Check every run directory that carries a ``metadata.json``."""

    verdicts: list[ProvenanceVerdict] = []
    if not runs_root.is_dir():
        return verdicts
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        metadata_path = run_dir / "metadata.json"
        if not metadata_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            verdicts.append(
                ProvenanceVerdict(
                    run_id=run_dir.name,
                    ok=False,
                    findings=(
                        ProvenanceFinding(
                            code="RUN_PROVENANCE_MALFORMED",
                            run_id=run_dir.name,
                            detail=f"{metadata_path} is not valid JSON",
                        ),
                    ),
                )
            )
            continue
        if not isinstance(metadata, dict):
            continue
        verdicts.append(_verdict_for(run_dir, metadata))
    return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate that every stored run declares its determinism inputs.")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT, help="run directory root to inspect")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    verdicts = scan_run_root(args.runs_root)
    if not verdicts:
        print(
            f"INCOMPLETE: no run directory under {args.runs_root} carries a metadata.json; "
            "nothing was inspected and a pass would be vacuous",
            file=sys.stderr,
        )
        return 2

    exit_code = 0 if all(verdict.ok for verdict in verdicts) else 1

    if args.as_json:
        print(
            json.dumps(
                {
                    "exit_code": exit_code,
                    "inspected": len(verdicts),
                    "diagnostic_codes": list(EMITTED_CODES),
                    "runs": [verdict.as_dict() for verdict in verdicts],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return exit_code

    for verdict in verdicts:
        if verdict.ok:
            print(f"OK  {verdict.run_id}: {verdict.environment_class} {verdict.provenance_hash[:16]}")
        else:
            for finding in verdict.findings:
                print(f"  {finding.code} [{verdict.run_id}]: {finding.detail}", file=sys.stderr)

    classes = sorted({verdict.environment_class for verdict in verdicts if verdict.ok})
    print(
        f"{'PASS' if exit_code == 0 else 'FAIL'}: {len(verdicts)} stored run(s) declare their provenance; "
        f"environment class(es) present: {', '.join(classes) if classes else 'none'}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
