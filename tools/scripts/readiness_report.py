#!/usr/bin/env python3
"""One machine-generated multi-scenario software readiness report (Issue #307).

The repository already answers several narrow questions: the registry says which
scenarios exist (Issue #300), the conformance gate proves each one honours the
shared boundaries (Issue #304), the capability matrix names an owner per half
(Issue #311), run identity binds a run to its definition (Issue #302), and the
eligibility predicate decides whether a run may be release evidence. None of them
answers the question a release actually asks:

    for every scenario identity, what is the software ready for, and what was
    never executed?

That is what this module generates, from the committed inputs, with no hand
editing. It reads the live registry, the conformance corpus, the capability
matrix and the derived replay probe, and writes one JSON artifact plus the
markdown page and README block generated from it.

Two properties are deliberate, and each has a test:

* **Four axes, not one ladder.** ``software``, ``scripted_fixture``, ``gazebo``
  and ``physical`` are reported independently. A scripted fixture sets
  ``scripted_fixture`` true and ``release_eligible`` false; it never sets
  ``gazebo`` or ``physical``. A row may not claim a stronger class than its
  manifest's ``evidence_status`` supports, and ``rendered_as_success`` is true
  only for a release-eligible row, so ``NOT_EXECUTED``, ``BLOCKED`` and
  ``release_eligible: false`` cannot be dressed up as a pass.
* **The report is a function of its inputs.** A clean checkout regenerates a
  byte-identical document, and the recorded ``configuration_hash`` covers the
  registry, the contract version, the conformance corpus and the matrix, so a
  changed input changes the hash instead of silently leaving a stale row.

Only two fields are volatile by construction: ``generated_at`` and
``source_commit``. The gate compares every other field, which is what "a
hand-edited outcome is refused" means in practice.

Everything here is read-only. It starts no simulator, writes no run or event, and
its verdict never claims that a scenario ran, that any evidence is physical, or
that a release is approved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _jsonio import load_json
from _paths import ROOT, enable_local_packages

enable_local_packages()

REPORT_VERSION = "multi-scenario-readiness-v1"
GENERATED_BY = "tools/scripts/readiness_report.py"

DEFAULT_REPORT_PATH = ROOT / "docs/evaluation/readiness-report-v1.json"
DEFAULT_PAGE_PATH = ROOT / "docs/evaluation/multi-scenario-readiness.md"
DEFAULT_README_PATH = ROOT / "README.md"
DEFAULT_README_ZH_PATH = ROOT / "README.zh-CN.md"

REGISTRY_ROOT = ROOT / "sim/registry"
CORPUS_PATH = ROOT / "tools/qa/scenario-conformance-v1.json"
MATRIX_PATH = ROOT / "docs/architecture/scenario-capability-matrix-v1.json"

README_BEGIN = "<!-- BEGIN GENERATED: scenario-readiness -->"
README_END = "<!-- END GENERATED: scenario-readiness -->"

# The four evidence axes, in the order a reader should read them: each one is a
# strictly stronger claim than the one before it. They are reported
# independently, and only the last two can make a row release eligible, because
# that is the same rule the capability matrix already enforces.
EVIDENCE_CLASSES: tuple[str, ...] = ("software", "scripted_fixture", "gazebo", "physical")
RELEASE_ELIGIBLE_CLASSES: frozenset[str] = frozenset({"gazebo", "physical"})

# Which manifest evidence_status permits which axis. A manifest that declares
# NOT_EXECUTED supports no execution axis at all, which is the honest reading of
# "nothing ran" rather than a missing default.
MANIFEST_EVIDENCE_CLASSES: Mapping[str, frozenset[str]] = {
    "SCRIPTED_FIXTURE": frozenset({"software", "scripted_fixture"}),
    "GAZEBO": frozenset({"software", "scripted_fixture", "gazebo"}),
    "PHYSICAL": frozenset({"software", "scripted_fixture", "gazebo", "physical"}),
    "NOT_EXECUTED": frozenset({"software"}),
    "BLOCKED": frozenset({"software"}),
}

EDIT_POLICY = (
    "This file is generated. Do not hand-edit a test outcome, a replay hash, an "
    "evidence class or release_eligible: regenerate it with "
    f"`python3 {GENERATED_BY}` and let the gate compare the result. Only "
    "generated_at and source_commit change without a change of inputs."
)

AUTHORITY = {
    "grants": [
        "a machine-generated statement of what the committed software is ready for per scenario identity",
        "a reproduction of that statement by a command",
    ],
    "does_not_grant": [
        "release approval",
        "physical or Gazebo validation",
        "authority to waive a required check or human approval",
    ],
}


class ReadinessReportError(RuntimeError):
    """The report cannot be generated from the committed inputs."""


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _registry_entries() -> list[dict[str, Any]]:
    from workbench.kernel.scenario_registry import load_registry

    registry = load_registry(REGISTRY_ROOT, repo_root=ROOT)
    return [entry.as_dict() for entry in registry.entries]


def _corpus() -> dict[str, Any]:
    from workbench.kernel.scenario_conformance import REQUIRED_DIMENSIONS, load_corpus

    corpus = load_corpus(CORPUS_PATH)
    return {"document": corpus, "required_dimensions": tuple(REQUIRED_DIMENSIONS)}


def _matrix_rows() -> dict[str, dict[str, Any]]:
    from workbench.kernel.scenario_capability import load_matrix

    matrix = load_matrix(MATRIX_PATH)
    rows: dict[str, dict[str, Any]] = {}
    for row in matrix["rows"]:
        rows[f"{row['scenario_id']}@{row['scenario_version']}"] = row
    return rows


def _replay_hash(identity: str) -> str | None:
    """The derived replay digest, or ``None`` when no probe is defined.

    A missing probe is recorded as a limitation rather than raised, because the
    report must still describe a newly registered scenario honestly instead of
    failing to generate at all.
    """

    from workbench.kernel.scenario_conformance import ScenarioConformanceError, replay_digest

    try:
        first, second = replay_digest(identity)
    except ScenarioConformanceError:
        return None
    if first != second:
        raise ReadinessReportError(f"{identity} is not deterministically replayable: {first} != {second}")
    return first


def _proved_dimensions(identity: str, corpus: Mapping[str, Any], required: tuple[str, ...]) -> list[str]:
    cases = corpus.get(identity)
    if not isinstance(cases, list):
        return []
    declared = {case.get("dimension") for case in cases if isinstance(case, Mapping)}
    return [dimension for dimension in required if dimension in declared]


def _cases(identity: str, corpus: Mapping[str, Any]) -> list[dict[str, str]]:
    cases = corpus.get(identity)
    if not isinstance(cases, list):
        return []
    resolved: list[dict[str, str]] = []
    for case in cases:
        if not isinstance(case, Mapping):
            continue
        dimension = case.get("dimension")
        test = case.get("test")
        if isinstance(dimension, str) and isinstance(test, str):
            resolved.append({"dimension": dimension, "test": test})
    return sorted(resolved, key=lambda item: item["dimension"])


def _evidence_classes(execution_status: str) -> dict[str, bool]:
    permitted = MANIFEST_EVIDENCE_CLASSES.get(execution_status, frozenset({"software"}))
    return {name: name in permitted for name in EVIDENCE_CLASSES}


def _limitations(entry: Mapping[str, Any], row: Mapping[str, Any] | None, replay: str | None) -> list[str]:
    notes: list[str] = []
    for goal in entry.get("non_goals") or []:
        if isinstance(goal, str) and goal.strip():
            notes.append(goal.strip())
    if row is not None:
        for capability in row.get("missing_capabilities") or []:
            notes.append(f"missing capability: {capability}")
        note = row.get("notes")
        if isinstance(note, str) and note.strip():
            notes.append(note.strip())
    if replay is None:
        notes.append("no derived replay probe is defined, so the replay hash is unavailable")
    return notes


def _row(
    entry: Mapping[str, Any],
    *,
    corpus: Mapping[str, Any],
    required: tuple[str, ...],
    matrix: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    identity = str(entry["identity"])
    execution_status = str(entry.get("evidence_status", ""))
    classes = _evidence_classes(execution_status)
    proved = _proved_dimensions(identity, corpus, required)
    replay = _replay_hash(identity)
    row = matrix.get(identity)
    owners = (
        {
            "scenario_rules": row.get("scenario_rules_owner"),
            "adapter": row.get("adapter_owner"),
            "evidence": row.get("evidence_owner"),
            "release": row.get("release_owner"),
        }
        if row is not None
        else {}
    )
    release_eligible = bool(entry.get("release_eligible")) and any(classes[name] for name in RELEASE_ELIGIBLE_CLASSES)
    return {
        "identity": identity,
        "scenario_id": entry.get("scenario_id"),
        "scenario_version": entry.get("scenario_version"),
        "task_family": (row or {}).get("task_family"),
        "goal": entry.get("goal"),
        "execution_status": execution_status,
        "evidence_classes": classes,
        "release_eligible": release_eligible,
        "software_readiness": "ready" if len(proved) == len(required) else "incomplete",
        "proved_dimensions": proved,
        "missing_dimensions": [dimension for dimension in required if dimension not in proved],
        "failure_coverage": _cases(identity, corpus),
        "replay_hash": replay,
        "verifier": entry.get("verifier"),
        "semantic_actions": list(entry.get("semantic_actions") or []),
        "required_adapters": list(entry.get("required_adapters") or []),
        "recovery_policy": (row or {}).get("recovery_policy") or [],
        "owners": owners,
        "known_limitations": _limitations(entry, row, replay),
        "rendered_as_success": release_eligible,
    }


def build_report(*, generated_at: str | None = None, source_commit: str | None = None) -> dict[str, Any]:
    """Build the report from the committed inputs, deterministically."""

    entries = _registry_entries()
    corpus_document = _corpus()
    corpus = corpus_document["document"].get("scenarios") or {}
    required = corpus_document["required_dimensions"]
    matrix = _matrix_rows()
    contract = load_json(ROOT / "docs/architecture/scenario-contract-v1.json")

    rows = [_row(entry, corpus=corpus, required=required, matrix=matrix) for entry in entries]

    registered = {row["identity"] for row in rows}
    declared_not_registered = [
        {
            "identity": identity,
            "task_family": row.get("task_family"),
            "status": row.get("status"),
            "environment_status": row.get("environment_status"),
            "missing_capabilities": list(row.get("missing_capabilities") or []),
            "notes": row.get("notes"),
        }
        for identity, row in sorted(matrix.items())
        if identity not in registered
    ]

    inputs = {
        "registry": [
            {
                "identity": entry["identity"],
                "evidence_status": entry["evidence_status"],
                "verifier": entry["verifier"],
                "semantic_actions": list(entry["semantic_actions"]),
                "required_adapters": list(entry["required_adapters"]),
            }
            for entry in entries
        ],
        "contract_version": contract.get("contract_version"),
        "conformance_corpus_version": corpus_document["document"].get("corpus_version"),
        "required_dimensions": list(required),
        "matrix_version": load_json(MATRIX_PATH).get("matrix_version"),
        "matrix_environments": {identity: row.get("environment_status") for identity, row in sorted(matrix.items())},
    }

    return {
        "report_version": REPORT_VERSION,
        "generated_by": GENERATED_BY,
        "edit_policy": EDIT_POLICY,
        "authority": AUTHORITY,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "source_commit": source_commit if source_commit is not None else _git_commit(),
        "configuration_hash": _sha256(inputs),
        "evidence_class_vocabulary": {
            "classes": list(EVIDENCE_CLASSES),
            "release_eligible_classes": sorted(RELEASE_ELIGIBLE_CLASSES),
            "rule": (
                "each class is reported independently; software means the definition is registered, contract "
                "valid and proven by a committed test per required dimension, scripted_fixture means a committed "
                "scripted fixture exercised it, gazebo and physical mean a real environment did;"
                " only gazebo and physical can make a row release eligible"
            ),
        },
        "generated_fields": [
            "generated_at",
            "source_commit",
            "configuration_hash",
            "scenarios[].evidence_classes",
            "scenarios[].release_eligible",
            "scenarios[].software_readiness",
            "scenarios[].proved_dimensions",
            "scenarios[].missing_dimensions",
            "scenarios[].replay_hash",
            "scenarios[].rendered_as_success",
        ],
        "summary": {
            "registered_count": len(rows),
            "software_ready_count": sum(1 for row in rows if row["software_readiness"] == "ready"),
            "scripted_fixture_count": sum(1 for row in rows if row["evidence_classes"]["scripted_fixture"]),
            "gazebo_count": sum(1 for row in rows if row["evidence_classes"]["gazebo"]),
            "physical_count": sum(1 for row in rows if row["evidence_classes"]["physical"]),
            "release_eligible_count": sum(1 for row in rows if row["release_eligible"]),
            "not_executed_or_blocked_count": sum(
                1 for row in rows if row["execution_status"] in {"NOT_EXECUTED", "BLOCKED"}
            ),
        },
        "limitations": [
            "every committed scenario is a scripted fixture: no Gazebo or physical run backs any row",
            "NOT_EXECUTED and BLOCKED rows are reported as such and are never rendered as a pass",
            "this report grants no release approval and replaces no required check or human approval",
        ],
        "scenarios": sorted(rows, key=lambda row: row["identity"]),
        "declared_not_registered": declared_not_registered,
    }


def markdown_page(report: Mapping[str, Any]) -> str:
    """Render the committed markdown page from the report, deterministically."""

    summary = report["summary"]
    lines = [
        "# Multi-scenario software readiness",
        "",
        "<!-- Generated from docs/evaluation/readiness-report-v1.json. Do not edit by hand. -->",
        "",
        "This page is generated by `tools/scripts/readiness_report.py`. It states what the",
        "committed software is ready for per scenario identity. It grants no release",
        "approval and is not physical or Gazebo evidence.",
        "",
        f"- Code revision: `{report['source_commit']}`",
        f"- Configuration hash: `{report['configuration_hash']}`",
        f"- Registered identities: {summary['registered_count']}",
        f"- Software ready: {summary['software_ready_count']}",
        f"- Scripted fixture: {summary['scripted_fixture_count']}",
        f"- Gazebo: {summary['gazebo_count']}",
        f"- Physical: {summary['physical_count']}",
        f"- Release eligible: {summary['release_eligible_count']}",
        f"- NOT_EXECUTED or BLOCKED: {summary['not_executed_or_blocked_count']}",
        "",
        "## Registered scenarios",
        "",
        "| Scenario | Software | Scripted fixture | Gazebo | Physical | Release eligible | Execution status |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["scenarios"]:
        classes = row["evidence_classes"]
        lines.append(
            "| `{identity}` | {software} | {scripted} | {gazebo} | {physical} | {eligible} | {status} |".format(
                identity=row["identity"],
                software="yes" if classes["software"] else "no",
                scripted="yes" if classes["scripted_fixture"] else "no",
                gazebo="yes" if classes["gazebo"] else "no",
                physical="yes" if classes["physical"] else "no",
                eligible="yes" if row["release_eligible"] else "no",
                status=row["execution_status"],
            )
        )
    lines.extend(
        [
            "",
            "## Declared and not registered",
            "",
            "| Scenario | Task family | Status | Environment | Missing capabilities |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in report["declared_not_registered"]:
        missing = ", ".join(row["missing_capabilities"]) or "-"
        lines.append(
            f"| `{row['identity']}` | {row['task_family']} | {row['status']} | "
            f"{row['environment_status']} | {missing} |"
        )
    lines.extend(["", "## Known limitations", ""])
    for limitation in report["limitations"]:
        lines.append(f"- {limitation}")
    lines.extend(
        [
            "",
            "## Verifying locally",
            "",
            "```bash",
            "python3 tools/scripts/readiness_report.py --output docs/evaluation/readiness-report-v1.json",
            "python3 tools/scripts/check_readiness_report.py",
            "python3 -m pytest tests/unit/test_readiness_report.py -v",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def readme_block(report: Mapping[str, Any], *, language: str = "en") -> str:
    """Render the README capability table from the same report data.

    One generator serves both READMEs, so the English and Chinese tables cannot
    disagree about a row: a scenario that is not release eligible in one is not
    release eligible in the other.
    """

    if language == "zh":
        lines = [
            README_BEGIN,
            "",
            "### 场景就绪状态",
            "",
            "由 `tools/scripts/readiness_report.py` 依据"
            " [`docs/evaluation/readiness-report-v1.json`](docs/evaluation/readiness-report-v1.json)"
            " 生成。它说明当前提交的软件对每个场景做到什么程度。它不代表发布批准。"
            "它也不是物理或 Gazebo 证据。详见"
            " [多场景就绪报告](docs/evaluation/multi-scenario-readiness.md)。",
            "",
            "| 场景 | 证据类别 | 可否发布 | 执行状态 |",
            "| --- | --- | --- | --- |",
        ]
        for row in report["scenarios"]:
            classes = [name for name in EVIDENCE_CLASSES if row["evidence_classes"][name]]
            lines.append(
                f"| `{row['identity']}` | {', '.join(classes)} | "
                f"{'是' if row['release_eligible'] else '否'} | {row['execution_status']} |"
            )
        lines.extend(["", README_END, ""])
        return "\n".join(lines)

    lines = [
        README_BEGIN,
        "",
        "### Scenario readiness",
        "",
        "Generated from [`docs/evaluation/readiness-report-v1.json`](docs/evaluation/readiness-report-v1.json)"
        " by `tools/scripts/readiness_report.py`. It states what the committed software is ready for; it grants no"
        " release approval and is not physical or Gazebo evidence. See"
        " [multi-scenario readiness](docs/evaluation/multi-scenario-readiness.md).",
        "",
        "| Scenario | Evidence class | Release eligible | Execution status |",
        "| --- | --- | --- | --- |",
    ]
    for row in report["scenarios"]:
        classes = [name for name in EVIDENCE_CLASSES if row["evidence_classes"][name]]
        lines.append(
            f"| `{row['identity']}` | {', '.join(classes)} | "
            f"{'yes' if row['release_eligible'] else 'no'} | {row['execution_status']} |"
        )
    lines.extend(["", README_END, ""])
    return "\n".join(lines)


def apply_readme_block(text: str, block: str) -> str:
    """Replace the generated README block, or append it when absent.

    Replacement is idempotent: running the generator twice in a row must produce
    the same bytes, so the separators around the block are normalized rather than
    appended to. Re-running the generator is the normal case, not an edge case.
    """

    block = block.strip("\n")
    if README_BEGIN in text and README_END in text:
        head, _, remainder = text.partition(README_BEGIN)
        _, _, tail = remainder.partition(README_END)
        parts = [head.rstrip("\n"), "", block]
        suffix = tail.strip("\n")
        if suffix:
            parts.extend(["", suffix])
        return "\n".join(parts) + "\n"
    return text.rstrip("\n") + "\n\n" + block + "\n"


def write_outputs(
    report: Mapping[str, Any],
    *,
    report_path: Path,
    page_path: Path,
    readme_path: Path,
    readme_zh_path: Path | None = None,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    page_path.write_text(markdown_page(report), encoding="utf-8")
    readme_path.write_text(apply_readme_block(readme_path.read_text(encoding="utf-8"), readme_block(report)))
    if readme_zh_path is not None and readme_zh_path.is_file():
        readme_zh_path.write_text(
            apply_readme_block(
                readme_zh_path.read_text(encoding="utf-8"),
                readme_block(report, language="zh"),
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the multi-scenario software readiness report.")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--page", type=Path, default=DEFAULT_PAGE_PATH)
    parser.add_argument("--readme", type=Path, default=DEFAULT_README_PATH)
    parser.add_argument("--readme-zh", type=Path, default=DEFAULT_README_ZH_PATH)
    parser.add_argument("--print", action="store_true", dest="as_stdout", help="print the report instead of writing")
    args = parser.parse_args(argv)

    try:
        report = build_report()
    except ReadinessReportError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    if args.as_stdout:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    write_outputs(
        report,
        report_path=args.output,
        page_path=args.page,
        readme_path=args.readme,
        readme_zh_path=args.readme_zh,
    )
    print(
        f"wrote {args.output.relative_to(ROOT) if args.output.is_relative_to(ROOT) else args.output} "
        f"({report['summary']['registered_count']} scenario(s), configuration_hash {report['configuration_hash'][:12]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
