"""Read-only projection of the readiness report for the dashboard (#581).

``docs/evaluation/readiness-report-v1.json`` is the generated statement of which
evidence class each scenario actually reaches. ``apps/dashboard/`` is a read-only
UI, and until this module it never showed that: a user could open a run, see a
green "confirmed" row, and have no way to learn that the run is a
``SCRIPTED_FIXTURE`` and that ``release_eligible`` is false for every scenario in
the repository.

That is the one place where the repository's central honesty mechanism was
invisible to the person looking at the screen, which is exactly the misreading the
whole evidence-class design exists to prevent.

What this module does
---------------------

It reads the committed report and turns it into a shape the UI renders. It does
not re-derive the class, does not compute an axis, and does not soften one. The
counts are computed here rather than in the browser so the arithmetic is testable
and the UI cannot disagree with the gate.

Three distinctions it refuses to lose
-------------------------------------

* ``SCRIPTED_FIXTURE`` is not ``GAZEBO`` and neither is ``physical``. A scenario
  that reaches only the software axis says so, and no axis is inferred from a
  neighbouring one.
* ``release_eligible: false`` is shown, not omitted. A page that hides the false
  value because it looks bad is how a fixture run reads as a released capability.
* A missing or stale report is **unavailable**, never an empty report. "This
  scenario has no evidence class" and "I could not read the file that says what
  the class is" are different facts, and rendering both as a blank panel is lying
  by omission.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[3]
REPORT_PATH = "docs/evaluation/readiness-report-v1.json"

#: The four axes the report defines, in the order a reader should meet them:
#: weakest evidence first, so a green row cannot be read before its class is.
EVIDENCE_CLASSES: tuple[str, ...] = ("software", "scripted_fixture", "gazebo", "physical")

#: Execution statuses the report may carry. Anything else is a shape this module
#: does not understand, which is unavailable rather than a guess.
EXECUTION_STATUSES: tuple[str, ...] = (
    "SCRIPTED_FIXTURE",
    "GAZEBO",
    "PHYSICAL",
    "NOT_EXECUTED",
    "BLOCKED",
)

#: One line per class, in the language the dashboard already uses. The report's own
#: token is kept alongside it and never replaced: a translated label alone would let
#: the token drift out of the UI.
CLASS_LABELS = {
    "software": "软件",
    "scripted_fixture": "脚本夹具",
    "gazebo": "Gazebo 仿真",
    "physical": "物理实机",
}

STATUS_LABELS = {
    "SCRIPTED_FIXTURE": "脚本夹具（非仿真、非实机）",
    "GAZEBO": "Gazebo 仿真",
    "PHYSICAL": "物理实机",
    "NOT_EXECUTED": "未执行",
    "BLOCKED": "受阻",
}

#: A status that must never be styled as a success. The UI reads this rather than
#: deciding for itself, so "not executed" cannot acquire a green badge by accident.
NON_SUCCESS_STATUSES: frozenset[str] = frozenset({"NOT_EXECUTED", "BLOCKED"})

#: The sentence the panel shows when the report is missing or unreadable. It is
#: deliberately not "no data": the class is unknown, not absent.
UNAVAILABLE_MESSAGE = "就绪报告不可用，无法判断证据等级。不推断为任何等级。"


class ReadinessUnavailable(RuntimeError):
    """The report could not be read, so no class may be claimed from it."""


def _read(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ReadinessUnavailable(f"cannot read {relative}: {error}") from error
    except json.JSONDecodeError as error:
        raise ReadinessUnavailable(f"{relative} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ReadinessUnavailable(f"{relative} must be a JSON object")
    return payload


def _scenario_row(entry: dict[str, Any]) -> dict[str, Any]:
    status = str(entry.get("execution_status", ""))
    if status not in EXECUTION_STATUSES:
        raise ReadinessUnavailable(f"scenario {entry.get('identity')!r} has status {status!r}")

    raw_classes = entry.get("evidence_classes")
    if not isinstance(raw_classes, dict):
        raise ReadinessUnavailable(f"scenario {entry.get('identity')!r} has no evidence_classes")

    axes = {}
    for name in EVIDENCE_CLASSES:
        value = raw_classes.get(name)
        if not isinstance(value, bool):
            # A missing axis is not `false`. `false` is a claim that the axis was
            # not reached; a missing key is a report this module cannot read.
            raise ReadinessUnavailable(f"scenario {entry.get('identity')!r} has no boolean {name!r} axis")
        axes[name] = {"reached": value, "label": CLASS_LABELS[name]}

    reached = [name for name, axis in axes.items() if axis["reached"]]
    eligible = entry.get("release_eligible")
    if not isinstance(eligible, bool):
        raise ReadinessUnavailable(f"scenario {entry.get('identity')!r} has no boolean release_eligible")

    return {
        "identity": entry.get("identity"),
        "scenario_id": entry.get("scenario_id"),
        "scenario_version": entry.get("scenario_version"),
        "task_family": entry.get("task_family"),
        "goal": entry.get("goal"),
        "execution_status": status,
        "execution_status_label": STATUS_LABELS[status],
        "evidence_classes": axes,
        "reached_classes": reached,
        "reached_label": "、".join(CLASS_LABELS[name] for name in reached) or "无",
        "release_eligible": eligible,
        # The UI must not style this row as a success. Both the fact and the
        # decision are sent, so a template that forgets the fact still has the
        # decision to render.
        "renders_as_success": eligible and status not in NON_SUCCESS_STATUSES,
        "software_readiness": entry.get("software_readiness"),
        "missing_dimensions": list(entry.get("missing_dimensions") or []),
    }


def project(root: Path | str | None = None) -> dict[str, Any]:
    """The dashboard projection: per-scenario class, the axes, and the counts."""
    base = Path(root) if root is not None else DEFAULT_ROOT
    report = _read(base, REPORT_PATH)

    scenarios = [_scenario_row(entry) for entry in report.get("scenarios") or []]
    by_status: dict[str, int] = {}
    for row in scenarios:
        by_status[row["execution_status"]] = by_status.get(row["execution_status"], 0) + 1

    eligible = sum(1 for row in scenarios if row["release_eligible"])
    return {
        "read_only": True,
        "report_version": report.get("report_version"),
        "generated_at": report.get("generated_at"),
        "source_commit": report.get("source_commit"),
        "edit_policy": report.get("edit_policy"),
        "vocabulary": [
            {"name": name, "label": CLASS_LABELS[name], "index": index}
            for index, name in enumerate(EVIDENCE_CLASSES, start=1)
        ],
        "scenarios": scenarios,
        "summary": {
            "scenarios": len(scenarios),
            "release_eligible": eligible,
            "not_release_eligible": len(scenarios) - eligible,
            "by_status": by_status,
        },
        # Stated as its own field because "0 release eligible" is the fact a reader
        # most needs and the one a summary line is most likely to round away.
        "no_release_eligible_scenario": eligible == 0,
    }


def unavailable(reason: str) -> dict[str, Any]:
    """The shape the UI renders when the report cannot be read.

    It carries the message rather than an empty list, so a blank panel cannot be
    mistaken for "every scenario is fine".
    """
    return {
        "read_only": True,
        "available": False,
        "message": UNAVAILABLE_MESSAGE,
        "reason": reason,
        "scenarios": [],
        "summary": None,
    }
