#!/usr/bin/env python3
"""Gate the committed readiness report against its own generator (Issue #307).

Issue #307 asks for a report that is reproducible from a clean checkout, lists
every registered scenario identity, separates the four evidence classes, and
refuses to present ``NOT_EXECUTED``, ``BLOCKED`` or ``release_eligible: false`` as
a success. A generated file alone does not enforce any of that: it is a snapshot,
and a snapshot can be edited, truncated or left behind after the registry grows.

So this gate regenerates the report from the committed inputs and compares it
against the committed artifact, field by field, and then checks the invariants a
comparison cannot express:

* every registered identity has exactly one row, and the row set is derived from
  the live registry rather than from the file;
* a row's evidence classes never exceed what its manifest ``evidence_status``
  supports, so a scripted fixture cannot claim Gazebo or physical success;
* ``rendered_as_success`` is true only for a release-eligible row, so a
  ``release_eligible: false`` row can never be rendered as a pass;
* only ``generated_at`` and ``source_commit`` may differ between the committed
  artifact and a regeneration, because those two describe when and where it was
  built rather than what the software is ready for;
* the markdown page and the README block are the ones the same data generates,
  so a published table cannot disagree with the machine-readable report.

Exit codes match the other gates here: ``0 PASS``, ``1 FAIL``, ``2 INCOMPLETE``.
INCOMPLETE is separate because a gate that reports FAIL when it could not read
its own input teaches reviewers to ignore it.

The gate is read-only. It starts no simulator, writes no run or event, and its
verdict never claims that a scenario ran or that a release is approved.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _paths import ROOT

PASS = 0
FAIL = 1
INCOMPLETE = 2

VOLATILE_FIELDS = ("generated_at", "source_commit")
RELEASE_ELIGIBLE_CLASSES = frozenset({"gazebo", "physical"})

MANIFEST_EVIDENCE_CLASSES: dict[str, frozenset[str]] = {
    "SCRIPTED_FIXTURE": frozenset({"software", "scripted_fixture"}),
    "GAZEBO": frozenset({"software", "scripted_fixture", "gazebo"}),
    "PHYSICAL": frozenset({"software", "scripted_fixture", "gazebo", "physical"}),
    "NOT_EXECUTED": frozenset({"software"}),
    "BLOCKED": frozenset({"software"}),
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _stable(report: dict[str, Any]) -> dict[str, Any]:
    """The report without the two fields that describe when it was built."""

    return {key: value for key, value in report.items() if key not in VOLATILE_FIELDS}


def _registry_identities() -> dict[str, str]:
    from workbench.kernel.scenario_registry import load_registry

    registry = load_registry(ROOT / "sim/registry", repo_root=ROOT)
    return {entry.identity: entry.evidence_status for entry in registry.entries}


def _compare(committed: dict[str, Any], regenerated: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    left = _stable(committed)
    right = _stable(regenerated)
    for key in sorted(set(left) | set(right)):
        if key not in left:
            findings.append(f"the committed report is missing the generated field {key!r}")
        elif key not in right:
            findings.append(f"the committed report carries a field the generator does not produce: {key!r}")
        elif left[key] != right[key]:
            findings.append(f"{key} differs from the report this checkout generates")
    return findings


def _invariants(report: dict[str, Any], manifests: dict[str, str]) -> list[str]:
    findings: list[str] = []
    rows = report.get("scenarios")
    if not isinstance(rows, list):
        return ["the report has no scenarios list"]

    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            findings.append("a scenario row is not an object")
            continue
        identity = row.get("identity")
        if not isinstance(identity, str) or not identity:
            findings.append("a scenario row has no identity")
            continue
        if identity in seen:
            findings.append(f"{identity} is declared by more than one row")
        seen.add(identity)

        declared = row.get("evidence_classes")
        if not isinstance(declared, dict):
            findings.append(f"{identity} has no evidence_classes block")
            continue
        status = row.get("execution_status")
        permitted = MANIFEST_EVIDENCE_CLASSES.get(str(status), frozenset({"software"}))
        for name in ("software", "scripted_fixture", "gazebo", "physical"):
            if declared.get(name) is True and name not in permitted:
                findings.append(
                    f"{identity} claims evidence class {name!r} over a {status!r} manifest; "
                    "a row may not strengthen its evidence"
                )

        release_eligible = row.get("release_eligible")
        claims_release_class = any(declared.get(name) is True for name in RELEASE_ELIGIBLE_CLASSES)
        if release_eligible is True and not claims_release_class:
            findings.append(f"{identity} is release eligible without a gazebo or physical evidence class")
        if release_eligible is not True and row.get("rendered_as_success") is True:
            findings.append(
                f"{identity} is not release eligible but is rendered as success; "
                "NOT_EXECUTED, BLOCKED and release_eligible:false are never a pass"
            )
        if row.get("rendered_as_success") is not release_eligible:
            findings.append(f"{identity} rendered_as_success disagrees with release_eligible")
        if status in {"NOT_EXECUTED", "BLOCKED"} and row.get("rendered_as_success") is True:
            findings.append(f"{identity} is {status} and must not be rendered as a pass")

    for identity in sorted(set(manifests) - seen):
        findings.append(f"registered scenario {identity} has no row in the committed report")

    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = row.get("identity")
        gaps = row.get("missing_dimensions")
        if isinstance(gaps, list) and gaps and row.get("software_readiness") == "ready":
            findings.append(f"{identity} is marked software ready while {gaps} are unproven")
        if isinstance(gaps, list) and not gaps and row.get("software_readiness") != "ready":
            findings.append(f"{identity} proves every dimension but is not marked software ready")
    return findings


README_BEGIN = "<!-- BEGIN GENERATED: scenario-readiness -->"
README_END = "<!-- END GENERATED: scenario-readiness -->"


def _readme_findings(readme: str, expected_block: str, path: Path) -> list[str]:
    begin = readme.find(README_BEGIN)
    end = readme.find(README_END)
    if begin < 0 or end < 0 or end < begin:
        return [f"{path} has no scenario-readiness generated block"]
    committed = readme[begin : end + len(README_END)]
    if committed.strip("\n") != expected_block.strip("\n"):
        return [f"the {path} readiness table is not the table this report generates"]
    return []


def _generated_artifacts(
    current: dict[str, Any],
    page_path: Path,
    readme_path: Path,
    readme_zh_path: Path,
) -> list[str]:
    from readiness_report import markdown_page, readme_block

    findings: list[str] = []
    try:
        page = page_path.read_text(encoding="utf-8")
    except OSError as error:
        return [f"cannot read the generated page {page_path}: {error}"]
    if page != markdown_page(current):
        findings.append(f"{page_path} is not the page this report generates; regenerate and commit it")

    try:
        readme = readme_path.read_text(encoding="utf-8")
    except OSError as error:
        return [*findings, f"cannot read {readme_path}: {error}"]
    findings.extend(_readme_findings(readme, readme_block(current), readme_path))

    if readme_zh_path.is_file():
        try:
            readme_zh = readme_zh_path.read_text(encoding="utf-8")
        except OSError as error:
            return [*findings, f"cannot read {readme_zh_path}: {error}"]
        findings.extend(_readme_findings(readme_zh, readme_block(current, language="zh"), readme_zh_path))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the committed readiness report against its generator.")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "docs/evaluation/readiness-report-v1.json",
        help="the committed readiness report to check",
    )
    parser.add_argument("--page", type=Path, default=ROOT / "docs/evaluation/multi-scenario-readiness.md")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument("--readme-zh", type=Path, default=ROOT / "README.zh-CN.md")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    try:
        committed = _load(args.report)
    except OSError as error:
        print(f"INCOMPLETE: cannot read {args.report}: {error}", file=sys.stderr)
        return INCOMPLETE
    except json.JSONDecodeError as error:
        print(f"INCOMPLETE: {args.report} is not valid JSON: {error}", file=sys.stderr)
        return INCOMPLETE

    if not isinstance(committed, dict):
        print(f"INCOMPLETE: {args.report} must be a JSON object", file=sys.stderr)
        return INCOMPLETE

    from readiness_report import ReadinessReportError, build_report

    try:
        regenerated = build_report()
        manifests = _registry_identities()
    except ReadinessReportError as error:
        print(f"INCOMPLETE: {error}", file=sys.stderr)
        return INCOMPLETE
    except Exception as error:  # noqa: BLE001 - an unreadable registry is INCOMPLETE, not FAIL
        print(f"INCOMPLETE: the readiness report could not be regenerated: {error}", file=sys.stderr)
        return INCOMPLETE

    findings = _compare(committed, regenerated)
    findings.extend(_invariants(committed, manifests))
    findings.extend(_generated_artifacts(committed, args.page, args.readme, args.readme_zh))

    if args.as_json:
        print(
            json.dumps(
                {
                    "exit_code": FAIL if findings else PASS,
                    "registered_identities": len(manifests),
                    "rows": len(committed.get("scenarios") or []),
                    "findings": findings,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return FAIL if findings else PASS

    for finding in findings:
        print(f"  READINESS_REPORT_STALE {finding}", file=sys.stderr)
    if findings:
        print(f"FAIL: {len(findings)} readiness report finding(s)", file=sys.stderr)
        return FAIL
    print(
        f"PASS: {len(committed.get('scenarios') or [])} scenario row(s) match the report this checkout "
        f"generates; {len(manifests)} registered identit(ies)"
    )
    return PASS


if __name__ == "__main__":
    raise SystemExit(main())
