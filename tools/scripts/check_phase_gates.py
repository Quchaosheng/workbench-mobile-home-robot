#!/usr/bin/env python3
"""Gate the committed phase status against its own generator (Issue #314).

Issue #314 asks for a phase status that release notes can link and that cannot be
hand-waved. A generated file alone enforces none of that: it is a snapshot, and a
snapshot can be edited, truncated, or left behind after a phase completes or a
gate breaks.

So this gate regenerates the status from the live gates and compares it field by
field, excluding only the two fields that describe when and where it was built.
It then checks the invariants a comparison cannot express:

* every phase in the declaration has exactly one row, and the row set is derived
  from the declaration rather than from the file;
* a phase with a probe whose gate is not PASS is never marked ``completed``, so a
  missing or failing gate cannot be rendered as a finished phase;
* a phase that is not complete reached no evidence class, so it cannot borrow the
  class a neighbouring phase earned;
* ``release_eligible`` is true only for a phase whose evidence class is gazebo or
  physical, so a software-only phase is never presented as a physical capability;
* a probe whose gate answers INCOMPLETE, or a delivery probe that answers
  NOT_DELIVERED, is reported as such and never as a pass;
* the markdown page, the release-notes block and both README blocks are the ones
  the same data generates, so a published table cannot disagree with the
  machine-readable status.

Exit codes match the other gates here: ``0 PASS``, ``1 FAIL``, ``2 INCOMPLETE``.
INCOMPLETE is separate because a gate that reports FAIL when it could not read
its own input teaches reviewers to ignore it.

The gate is read-only. It starts no simulator, writes no run or event, and its
verdict never claims that a phase ran, that evidence is physical, or that a
release or sign-off is approved.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _paths import ROOT
from phase_gates import (
    NOTES_BEGIN,
    NOTES_END,
    PHASES,
    README_BEGIN,
    README_END,
    build_status,
    markdown_page,
    readme_block,
    release_notes_block,
)

PASS = 0
FAIL = 1
INCOMPLETE = 2

VOLATILE_FIELDS = ("generated_at", "source_commit")
RELEASE_ELIGIBLE_CLASSES = frozenset({"gazebo", "physical"})
VERDICT_LABELS = frozenset({"PASS", "FAIL", "INCOMPLETE", "NOT_DELIVERED"})
PHASE_STATUS_LABELS = frozenset({"COMPLETE", "INCOMPLETE", "FAIL", "BLOCKED", "NOT_DELIVERED"})


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _stable(status: dict[str, Any]) -> dict[str, Any]:
    """The status without the two fields that describe when it was built."""

    return {key: value for key, value in status.items() if key not in VOLATILE_FIELDS}


def _compare(committed: dict[str, Any], regenerated: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    left = _stable(committed)
    right = _stable(regenerated)
    for key in sorted(set(left) | set(right)):
        if key not in left:
            findings.append(f"the committed status is missing the generated field {key!r}")
        elif key not in right:
            findings.append(f"the committed status carries a field the generator does not produce: {key!r}")
        elif left[key] != right[key]:
            findings.append(f"{key} differs from the status this checkout generates")
    return findings


def _invariants(status: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    rows = status.get("phases")
    if not isinstance(rows, list):
        return ["the status has no phases list"]

    declared = {phase["phase"] for phase in PHASES}
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            findings.append("a phase row is not an object")
            continue
        number = row.get("phase")
        if not isinstance(number, int):
            findings.append("a phase row has no phase number")
            continue
        if number in seen:
            findings.append(f"phase {number} is declared by more than one row")
        seen.add(number)

        probes = row.get("probes")
        if not isinstance(probes, list) or not probes:
            findings.append(f"phase {number} declares no probes")
            continue
        labels = [probe.get("gate_status") for probe in probes if isinstance(probe, dict)]
        unknown = sorted({label for label in labels if label not in VERDICT_LABELS})
        if unknown:
            findings.append(f"phase {number} has an unrecognised probe verdict: {', '.join(map(str, unknown))}")
            continue

        gate_status = row.get("gate_status")
        if gate_status not in PHASE_STATUS_LABELS:
            findings.append(f"phase {number} has an unrecognised gate_status {gate_status!r}")
        completed = row.get("completed")
        if completed and any(label != "PASS" for label in labels):
            findings.append(f"phase {number} is marked completed while a declared gate is not PASS")
        if not completed and labels and all(label == "PASS" for label in labels):
            findings.append(f"phase {number} passes every declared gate but is not marked completed")

        evidence = row.get("evidence_class")
        eligible = row.get("release_eligible")
        if eligible and evidence not in RELEASE_ELIGIBLE_CLASSES:
            findings.append(
                f"phase {number} is release eligible on evidence class {evidence!r}, which is not gazebo or physical"
            )
        if gate_status in {"BLOCKED", "NOT_DELIVERED"} and evidence != "none":
            findings.append(f"phase {number} was never delivered but claims the evidence class {evidence!r}")

        signatures = row.get("sign_off")
        if not isinstance(signatures, list) or not signatures:
            findings.append(f"phase {number} records no owner sign-off")
            continue
        outstanding = row.get("sign_off_outstanding")
        expected = [
            entry["role"] for entry in signatures if isinstance(entry, dict) and entry.get("status") != "approved"
        ]
        if outstanding != expected:
            findings.append(f"phase {number} does not list the sign-off it still needs")

    if seen != declared:
        missing = sorted(declared - seen)
        extra = sorted(seen - declared)
        if missing:
            findings.append(f"the committed status is missing phase(s): {', '.join(map(str, missing))}")
        if extra:
            findings.append(f"the committed status carries undeclared phase(s): {', '.join(map(str, extra))}")

    return findings


def _block_matches(committed: str, expected: str, path: Path) -> list[str]:
    if committed.strip("\n") != expected.strip("\n"):
        return [f"the {path} generated block is not the block this status generates"]
    return []


def _generated_artifacts(
    status: dict[str, Any],
    page_path: Path,
    notes_path: Path,
    readme_path: Path,
    readme_zh_path: Path,
) -> list[str]:
    findings: list[str] = []
    try:
        page = page_path.read_text(encoding="utf-8")
    except OSError as error:
        return [f"cannot read the generated page {page_path}: {error}"]
    if page != markdown_page(status):
        findings.append(f"{page_path} is not the page this status generates; regenerate and commit it")

    def _extract(text: str, begin: str, end: str) -> str | None:
        start = text.find(begin)
        stop = text.find(end)
        if start < 0 or stop < 0 or stop < start:
            return None
        return text[start : stop + len(end)]

    try:
        readme = readme_path.read_text(encoding="utf-8")
    except OSError as error:
        return [*findings, f"cannot read {readme_path}: {error}"]
    block = _extract(readme, README_BEGIN, README_END)
    if block is None:
        findings.append(f"{readme_path} has no phase-status generated block")
    else:
        findings.extend(_block_matches(block, readme_block(status), readme_path))

    if notes_path.is_file():
        try:
            notes = notes_path.read_text(encoding="utf-8")
        except OSError as error:
            return [*findings, f"cannot read {notes_path}: {error}"]
        notes_block = _extract(notes, NOTES_BEGIN, NOTES_END)
        if notes_block is None:
            findings.append(f"{notes_path} has no phase-status generated block")
        else:
            findings.extend(_block_matches(notes_block, release_notes_block(status), notes_path))

    if readme_zh_path.is_file():
        try:
            readme_zh = readme_zh_path.read_text(encoding="utf-8")
        except OSError as error:
            return [*findings, f"cannot read {readme_zh_path}: {error}"]
        block_zh = _extract(readme_zh, README_BEGIN, README_END)
        if block_zh is None:
            findings.append(f"{readme_zh_path} has no phase-status generated block")
        else:
            findings.extend(_block_matches(block_zh, readme_block(status, language="zh"), readme_zh_path))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the committed phase status against its generator.")
    parser.add_argument("--status", type=Path, default=ROOT / "docs/releases/phase-status-v1.json")
    parser.add_argument("--page", type=Path, default=ROOT / "docs/releases/phase-gates.md")
    parser.add_argument("--release-notes", type=Path, default=ROOT / "docs/releases/release-notes.md")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument("--readme-zh", type=Path, default=ROOT / "README.zh-CN.md")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    try:
        committed = _load(args.status)
    except OSError as error:
        print(f"INCOMPLETE: cannot read {args.status}: {error}", file=sys.stderr)
        return INCOMPLETE
    except json.JSONDecodeError as error:
        print(f"INCOMPLETE: {args.status} is not valid JSON: {error}", file=sys.stderr)
        return INCOMPLETE

    if not isinstance(committed, dict):
        print(f"INCOMPLETE: {args.status} must be a JSON object", file=sys.stderr)
        return INCOMPLETE

    try:
        regenerated = build_status()
    except Exception as error:  # noqa: BLE001 - an unreadable input is INCOMPLETE, not FAIL
        print(f"INCOMPLETE: the phase status could not be regenerated: {error}", file=sys.stderr)
        return INCOMPLETE

    findings = _compare(committed, regenerated)
    findings.extend(_invariants(committed))
    findings.extend(_generated_artifacts(committed, args.page, args.release_notes, args.readme, args.readme_zh))

    if args.as_json:
        print(
            json.dumps(
                {
                    "exit_code": FAIL if findings else PASS,
                    "phases": len(committed.get("phases") or []),
                    "summary": committed.get("summary"),
                    "findings": findings,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return FAIL if findings else PASS

    for finding in findings:
        print(f"  PHASE_STATUS_STALE {finding}", file=sys.stderr)
    if findings:
        print(f"FAIL: {len(findings)} phase status finding(s)", file=sys.stderr)
        return FAIL

    summary = committed.get("summary") or {}
    print(
        f"PASS: {len(committed.get('phases') or [])} phase(s) match the status this checkout generates; "
        f"{summary.get('complete_count')} complete, {summary.get('blocked_count')} blocked, "
        f"{summary.get('release_eligible_count')} release eligible"
    )
    return PASS


if __name__ == "__main__":
    raise SystemExit(main())
