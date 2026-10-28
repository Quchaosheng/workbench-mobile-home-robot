"""Read-only projection of the assurance register for the dashboard.

The register lives in two committed JSON files that a gate already validates:

* ``docs/assurance/invariants-v1.json`` - the properties the runtime claims, each
  with a resolving enforcement symbol and the tests that prove it;
* ``docs/assurance/acceptance-matrix-v1.json`` - one case per fault, with the
  terminal state it must reach and the coverage class it actually has.

This module does not re-derive those rules and does not soften them. Its only job is
to turn the two files into a shape the UI can render without inventing anything, and
to keep three distinctions that the files make and a careless UI would lose:

* ``ENFORCED`` vs ``NOT_IMPLEMENTED`` - a gap is shown as a gap, never as zero;
* ``SIMULATED`` vs ``NOT_EXECUTED`` - a case that never ran is not a case that passed;
* the claim scope - the page says runtime assurance, not functional safety.

A missing or malformed register is reported as unavailable rather than as an empty
register. "No invariants" and "could not read the invariants" are different facts, and
a dashboard that renders both as an empty list is lying by omission.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[3]
INVARIANTS_PATH = "docs/assurance/invariants-v1.json"
MATRIX_PATH = "docs/assurance/acceptance-matrix-v1.json"

INVARIANT_STATUSES = ("ENFORCED", "NOT_IMPLEMENTED")
COVERAGE_CLASSES = ("SIMULATED", "HARDWARE_TESTED", "NOT_EXECUTED")

# One line the UI must show verbatim. It is the shortest true statement of the
# boundary, and paraphrasing it is how a caveat turns into a claim.
CLAIM_SCOPE = "运行时保障与验收证据，非功能安全认证"

# A gap needs a one-line Chinese label, because "INV-09" alone tells a reader nothing.
# The register's own wording is kept alongside it and never rephrased: this table
# only says what kind of thing is missing.
GAP_LABELS = {
    "INV-09": "资源 claim 独占：同一机器人资源不能被两个运行同时持有",
    "resource-conflict": "资源冲突验收用例：等 claim 实现后才有执行点",
}

STATUS_LABELS = {
    "ENFORCED": "已强制执行",
    "NOT_IMPLEMENTED": "未实现",
}

COVERAGE_LABELS = {
    "SIMULATED": "仿真",
    "HARDWARE_TESTED": "硬件验证",
    "NOT_EXECUTED": "未执行",
}


class AssuranceUnavailable(RuntimeError):
    """The register could not be read, so nothing may be claimed about it."""


def _read(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise AssuranceUnavailable(f"cannot read {relative}: {error}") from error
    except json.JSONDecodeError as error:
        raise AssuranceUnavailable(f"{relative} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise AssuranceUnavailable(f"{relative} must be a JSON object")
    return payload


def project(root: Path | str | None = None) -> dict[str, Any]:
    """The dashboard projection: invariants, acceptance matrix, and the counts.

    Counts are computed here rather than in the browser so the same arithmetic is
    testable and the UI cannot disagree with the gate about what "enforced" means.
    """
    base = Path(root) if root is not None else DEFAULT_ROOT
    invariants_doc = _read(base, INVARIANTS_PATH)
    matrix_doc = _read(base, MATRIX_PATH)

    invariants = []
    for entry in invariants_doc.get("invariants", []):
        status = str(entry.get("status", ""))
        if status not in INVARIANT_STATUSES:
            raise AssuranceUnavailable(f"invariant {entry.get('id')!r} has status {status!r}")
        invariants.append(
            {
                "id": entry.get("id"),
                "statement": entry.get("statement"),
                "runtime_boundary": entry.get("runtime_boundary"),
                "status": status,
                "status_label": STATUS_LABELS[status],
                "enforced_by": entry.get("enforced_by"),
                "evidence": list(entry.get("evidence") or []),
                "gap": entry.get("gap"),
            }
        )

    cases = []
    for row in matrix_doc.get("rows", []):
        coverage = str(row.get("coverage", ""))
        if coverage not in COVERAGE_CLASSES:
            raise AssuranceUnavailable(f"case {row.get('case_id')!r} has coverage {coverage!r}")
        state = row.get("expected_terminal_state") or {}
        cases.append(
            {
                "case_id": row.get("case_id"),
                "fault": row.get("fault"),
                "trigger": row.get("trigger"),
                "expected_terminal_state": state,
                "expected_verdict": row.get("expected_verdict"),
                "invariants": list(row.get("invariants") or []),
                "coverage": coverage,
                "coverage_label": COVERAGE_LABELS[coverage],
                "executed": coverage != "NOT_EXECUTED",
                "evidence": list(row.get("evidence") or []),
                "gap": row.get("gap"),
            }
        )

    enforced = sum(1 for entry in invariants if entry["status"] == "ENFORCED")
    executed = sum(1 for case in cases if case["executed"])
    return {
        "read_only": True,
        "claim_scope": CLAIM_SCOPE,
        "register_status": invariants_doc.get("status"),
        "invariants_version": invariants_doc.get("invariants_version"),
        "matrix_version": matrix_doc.get("matrix_version"),
        "invariants": invariants,
        "cases": cases,
        "counts": {
            "invariants": len(invariants),
            "enforced": enforced,
            "not_implemented": len(invariants) - enforced,
            "cases": len(cases),
            "executed": executed,
            "not_executed": len(cases) - executed,
        },
        # Stated separately from the counts, because "8 of 9" reads as progress while
        # the one that is missing is the one that decides who owns the robot.
        "gaps": [
            {
                "kind": "invariant",
                "id": entry["id"],
                "label": GAP_LABELS.get(entry["id"], entry["statement"]),
                "detail": entry["gap"],
            }
            for entry in invariants
            if entry["status"] == "NOT_IMPLEMENTED"
        ]
        + [
            {
                "kind": "case",
                "id": case["case_id"],
                "label": GAP_LABELS.get(case["case_id"], case["trigger"]),
                "detail": case["gap"],
            }
            for case in cases
            if not case["executed"]
        ],
    }
