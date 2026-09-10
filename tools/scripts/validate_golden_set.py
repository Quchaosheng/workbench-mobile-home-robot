import json
from collections import Counter

from _paths import ROOT, enable_local_packages

enable_local_packages()

from workbench_agent_runtime import build_template_plan

TASK_FAMILIES = {
    "task-clear-workspace",
    "task-inspect-workpieces",
    "task-kit-three-parts",
    "task-place-red-block",
}
PARCEL_TASK_ID = "task-sort-parcels"


def _validate_task_plans(tasks: list[dict], dataset: str, *, expected_task_id: str | None = None) -> list[str]:
    problems: list[str] = []
    for item in tasks:
        try:
            plan = build_template_plan(item["request"])
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"{dataset}/{item.get('id')} cannot be planned: {exc}")
            continue
        expected = item.get("expected_task_id")
        if expected_task_id is not None:
            expected = expected_task_id
        if plan.task_id != expected:
            problems.append(f"{dataset}/{item.get('id')} maps to {plan.task_id}, expected {expected}")
    return problems


def _validate_dangerous_requests(dangerous: list[dict], dataset: str) -> list[str]:
    problems: list[str] = []
    for item in dangerous:
        try:
            plan = build_template_plan(item["request"])
        except (KeyError, TypeError, ValueError):
            continue
        steps = ",".join(step.step_id for step in plan.steps)
        problems.append(
            f"{dataset}/{item.get('id')} accepted dangerous request: task_id={plan.task_id}; steps=[{steps}]"
        )
    return problems


def validate(payload: dict) -> list[str]:
    problems: list[str] = []
    tasks = payload.get("tasks", [])
    dangerous = payload.get("dangerous_requests", [])
    if payload.get("frozen") is not True:
        problems.append("golden set must be frozen")
    if len(tasks) != 20:
        problems.append(f"expected 20 tasks, found {len(tasks)}")
    if len(dangerous) != 10:
        problems.append(f"expected 10 dangerous requests, found {len(dangerous)}")
    identifiers = [item.get("id") for item in tasks + dangerous]
    if len(identifiers) != len(set(identifiers)):
        problems.append("task and dangerous-request IDs must be unique")
    if any(item.get("expected_task_id") != "task-place-red-block" for item in tasks):
        problems.append("all P1 tasks must map to the frozen pick/place task")
    if any(item.get("expected_policy") != "reject" for item in dangerous):
        problems.append("every dangerous request must fail closed")
    if any(not item.get("request", "").strip() for item in tasks + dangerous):
        problems.append("every entry must contain a non-empty request")
    problems.extend(_validate_task_plans(tasks, "v0.1", expected_task_id="task-place-red-block"))
    problems.extend(_validate_dangerous_requests(dangerous, "v0.1"))
    return problems


def validate_diverse(payload: dict) -> list[str]:
    problems: list[str] = []
    tasks = payload.get("tasks", [])
    dangerous = payload.get("dangerous_requests", [])
    if payload.get("frozen") is not True:
        problems.append("diverse golden set must be frozen")
    if len(tasks) != 24:
        problems.append(f"expected 24 diverse tasks, found {len(tasks)}")
    if len(dangerous) != 12:
        problems.append(f"expected 12 dangerous requests, found {len(dangerous)}")
    identifiers = [item.get("id") for item in tasks + dangerous]
    if len(identifiers) != len(set(identifiers)):
        problems.append("diverse task and dangerous-request IDs must be unique")
    family_counts = Counter(item.get("expected_task_id") for item in tasks)
    if set(family_counts) != TASK_FAMILIES or any(count != 6 for count in family_counts.values()):
        problems.append(f"expected six tasks in each family, got {dict(sorted(family_counts.items()))}")
    if any(item.get("expected_policy") != "reject" for item in dangerous):
        problems.append("every diverse dangerous request must fail closed")
    if any(not item.get("request", "").strip() for item in tasks + dangerous):
        problems.append("every diverse entry must contain a non-empty request")
    problems.extend(_validate_task_plans(tasks, "v0.2"))
    problems.extend(_validate_dangerous_requests(dangerous, "v0.2"))
    return problems


def validate_parcels(payload: dict) -> list[str]:
    problems: list[str] = []
    tasks = payload.get("tasks", [])
    dangerous = payload.get("dangerous_requests", [])
    if payload.get("frozen") is not True:
        problems.append("parcel golden set must be frozen")
    if len(tasks) != 6:
        problems.append(f"expected 6 parcel tasks, found {len(tasks)}")
    if len(dangerous) != 4:
        problems.append(f"expected 4 parcel dangerous requests, found {len(dangerous)}")
    identifiers = [item.get("id") for item in tasks + dangerous]
    if len(identifiers) != len(set(identifiers)):
        problems.append("parcel task and dangerous-request IDs must be unique")
    problems.extend(_validate_task_plans(tasks, "parcel-v0.1", expected_task_id=PARCEL_TASK_ID))
    if any(item.get("expected_policy") != "reject" for item in dangerous):
        problems.append("every parcel dangerous request must fail closed")
    problems.extend(_validate_dangerous_requests(dangerous, "parcel-v0.1"))
    return problems


def main() -> int:
    baseline_path = ROOT / "evaluation" / "golden-set-v0.1.json"
    diverse_path = ROOT / "evaluation" / "golden-set-v0.2.json"
    parcel_path = ROOT / "evaluation" / "golden-set-parcel-v0.1.json"
    problems = validate(json.loads(baseline_path.read_text(encoding="utf-8")))
    problems.extend(validate_diverse(json.loads(diverse_path.read_text(encoding="utf-8"))))
    problems.extend(validate_parcels(json.loads(parcel_path.read_text(encoding="utf-8"))))
    if problems:
        raise RuntimeError("; ".join(problems))
    print("golden set validation passed for 50 tasks across 5 families and 26 dangerous requests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
