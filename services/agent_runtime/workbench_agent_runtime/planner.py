import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence

from workbench_contracts import ActionType, SemanticAction, TaskGraph, TaskStep

from .tool_registry import ToolRegistry

# Module-level singleton — created once, shared by all planner entry points.
_tool_registry = ToolRegistry()


def _validate_plan(plan: TaskGraph) -> None:
    """Validate every step in *plan* against the tool registry.

    Raises ValueError on the first validation failure so a buggy planner
    (or model) cannot silently emit an invalid action.
    """
    for step in plan.steps:
        result = _tool_registry.validate(step.action)
        if not result.is_valid:
            messages = "; ".join(f"{error.field}: {error.message}" for error in result.errors)
            raise ValueError(
                f"step '{step.step_id}' action '{step.action.action_id}' failed tool-registry validation: {messages}"
            )


DEFAULT_KIT_PARTS = ("red_block", "blue_cylinder", "green_gear")
DEFAULT_INSPECTION_ENTITIES = ("red_block", "blue_cylinder", "green_gear")
DEFAULT_PARCEL_ROUTES = (
    ("parcel_box", "pickup_shelf"),
    ("parcel_envelope", "pickup_shelf"),
    ("parcel_unreadable", "quarantine_bin"),
    ("parcel_damaged", "quarantine_bin"),
)
DEFAULT_PARCEL_ATTRIBUTES = {
    "parcel_box": {"label_status": "verified", "condition": "intact", "tracking_id": "WBX-BOX-001"},
    "parcel_envelope": {"label_status": "verified", "condition": "intact", "tracking_id": "WBX-ENV-002"},
    "parcel_unreadable": {"label_status": "unreadable", "condition": "intact", "parcel_uid": "WBX-UNK-003"},
    "parcel_damaged": {"label_status": "verified", "condition": "damaged", "barcode": "WBX-DMG-004"},
}
PARCEL_POLICY_VERSION = "parcel-routing-v3"
PARCEL_IDENTITY_KEYS = ("tracking_id", "barcode", "parcel_uid")


def _matches_task_keyword(text: str, english: tuple[str, ...], chinese: tuple[str, ...]) -> bool:
    english_match = any(re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) for keyword in english)
    return english_match or any(keyword in text for keyword in chinese)


def _goal_boundary_violation(normalized: str) -> tuple[str, str] | None:
    if "\ufffd" in normalized:
        return "unsupported_request", "task goal contains invalid replacement characters"
    if re.search(
        r"\b(go|walk|travel|navigate|drive|head|ride)\b|"
        r"\b(mobile[- ]base|elevator|lobby|locker|off[- ]table)\b",
        normalized,
    ) or any(
        token in normalized
        for token in ("去取", "下楼", "快递柜", "乘电梯", "导航", "前往", "走到", "开到", "移动底盘", "然后去", "再去")
    ):
        return "requires_navigation", "explicit navigation is outside the tabletop robot boundary"
    if re.search(
        r"\b(joint|velocity|torque|firmware|motor control|servo|raw can|can frame)\b",
        normalized,
    ) or any(
        token in normalized
        for token in ("关节", "速度控制", "扭矩", "力矩", "固件", "电机控制", "舵机", "原始can", "can帧")
    ):
        return "requires_joint_control", "raw joint, motor, firmware, or CAN control is outside the planner boundary"
    if (
        re.search(
            r"\b(report|claim|declare|mark|pretend|fabricat\w*)\b.{0,60}\b(success|complete|verified|pass)\b",
            normalized,
        )
        or re.search(r"\b(without|skip|ignore)\b.{0,40}\b(seeing|evidence|verification|verify)\b", normalized)
        or re.search(
            r"\b(low[- ]confidence|unreadable|unknown label)\b.{0,40}\b(confirmed|verified|success)\b", normalized
        )
        or any(
            token in normalized
            for token in (
                "报告成功",
                "声称完成",
                "标记完成",
                "跳过核验",
                "忽略证据",
                "无需验证",
                "低置信度",
                "未验证",
                "直接通过",
                "没有证据",
                "直接宣布",
                "质检通过",
                "绕过验证器",
                "齐套状态",
                "改成成功",
            )
        )
    ):
        return "requires_completion_claim", "completion or verification claims require observable evidence"
    if (
        re.search(
            r"\b(disable|ignore|bypass|turn off)\b.{0,40}\b(collision|emergency stop|e-stop|watchdog|safety)\b",
            normalized,
        )
        or re.search(r"\b(damaged|broken)\s+(parcel|package)\b.*\b(on|to|into|in)\b.*\bpickup shelf\b", normalized)
        or any(
            token in normalized
            for token in ("关闭碰撞", "忽略急停", "关闭看门狗", "绕过安全", "破损取件架", "破损正常区")
        )
    ):
        return "unsupported_request", "safety or quarantine bypass is outside the planner boundary"
    return None


def _validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _validate_entity_ids(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError(f"{label} must be a sequence of entity IDs, not a string")
    normalized = tuple(values)
    if not normalized or any(not isinstance(value, str) or not value.strip() for value in normalized):
        raise ValueError(f"{label} requires non-empty entity IDs")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} requires unique entity IDs")
    return normalized


def _unique_step_tokens(entity_ids: Sequence[str], *, preserve_legacy_tokens: bool = False) -> dict[str, str]:
    """Create readable, deterministic tokens without collapsing distinct IDs."""
    tokens: dict[str, str] = {}
    used: set[str] = set()
    for index, entity_id in enumerate(entity_ids, start=1):
        base = (
            entity_id.replace("_", "-")
            if preserve_legacy_tokens
            else re.sub(r"[^a-z0-9]+", "-", entity_id.lower()).strip("-")
        ) or f"entity-{index}"
        token = base
        suffix = 2
        while token in used:
            collision_base = re.sub(r"[^a-z0-9]+", "-", entity_id.lower()).strip("-") or f"entity-{index}"
            token = f"{collision_base}-{suffix}"
            suffix += 1
        tokens[entity_id] = token
        used.add(token)
    return tokens


def classify_template_task(goal: str) -> str:
    if not isinstance(goal, str):
        raise ValueError("task goal must be a string")
    normalized = goal.strip().lower()
    if not normalized:
        raise ValueError("task goal must not be empty")
    violation = _goal_boundary_violation(normalized)
    if violation:
        code, message = violation
        if code == "requires_completion_claim" and any(
            token in normalized for token in ("unreadable", "unknown label", "无法读取", "看不清")
        ):
            message = "parcel labels must be readable evidence before they can be marked verified"
        elif code == "unsupported_request" and any(token in normalized for token in ("damaged", "broken", "破损")):
            message = "damaged parcels must be isolated and cannot be routed to pickup"
        raise ValueError(f"{code}: {message}")
    if _matches_task_keyword(
        normalized,
        ("parcel", "parcels", "courier", "delivery", "shipment"),
        ("快递", "包裹", "分拣", "入库", "取件架"),
    ):
        return "task-sort-parcels"
    if _matches_task_keyword(
        normalized,
        ("clear", "clearance", "blocked", "obstacle"),
        ("清障", "障碍", "挡路", "移开"),
    ):
        return "task-clear-workspace"
    if _matches_task_keyword(normalized, ("kit", "kitting", "three-part"), ("套件", "齐套", "配套", "三件")):
        return "task-kit-three-parts"
    if _matches_task_keyword(
        normalized,
        ("inspect", "inspection", "check"),
        ("检查", "检验", "核验", "质检"),
    ):
        return "task-inspect-workpieces"
    if _matches_task_keyword(normalized, ("red",), ("红",)):
        return "task-place-red-block"
    raise ValueError("template planner supports place, kitting, inspection, clearance, and parcel-sorting tasks")


def build_place_plan(goal: str, block_id: str = "red_block", tray_id: str = "tray") -> TaskGraph:
    block_id = _validate_identifier(block_id, "block_id")
    tray_id = _validate_identifier(tray_id, "tray_id")
    steps = [
        TaskStep(
            step_id="observe-block",
            action=SemanticAction(action_id="act-001", action_type=ActionType.OBSERVE, target_id=block_id),
        ),
        TaskStep(
            step_id="grasp-block",
            action=SemanticAction(action_id="act-002", action_type=ActionType.GRASP, target_id=block_id),
            depends_on=["observe-block"],
        ),
        TaskStep(
            step_id="place-block",
            action=SemanticAction(
                action_id="act-003",
                action_type=ActionType.PLACE,
                target_id=block_id,
                parameters={"destination_id": tray_id},
            ),
            depends_on=["grasp-block"],
        ),
    ]
    return TaskGraph(
        task_id="task-place-red-block",
        goal=goal,
        steps=steps,
        planner="template-v1",
        model_route="template",
    )


def build_kitting_plan(
    goal: str,
    part_ids: Sequence[str] = DEFAULT_KIT_PARTS,
    tray_id: str = "kit_tray",
) -> TaskGraph:
    part_ids = _validate_entity_ids(part_ids, "kitting")
    tray_id = _validate_identifier(tray_id, "tray_id")
    step_tokens = _unique_step_tokens(part_ids, preserve_legacy_tokens=True)
    steps: list[TaskStep] = []
    action_index = 1
    for part_id in part_ids:
        suffix = step_tokens[part_id]
        observe_step = f"observe-{suffix}"
        grasp_step = f"grasp-{suffix}"
        steps.extend(
            [
                TaskStep(
                    step_id=observe_step,
                    action=SemanticAction(
                        action_id=f"act-{action_index:03d}",
                        action_type=ActionType.OBSERVE,
                        target_id=part_id,
                        parameters={"required_confidence": 0.8},
                    ),
                ),
                TaskStep(
                    step_id=grasp_step,
                    action=SemanticAction(
                        action_id=f"act-{action_index + 1:03d}",
                        action_type=ActionType.GRASP,
                        target_id=part_id,
                    ),
                    depends_on=[observe_step],
                ),
                TaskStep(
                    step_id=f"place-{suffix}",
                    action=SemanticAction(
                        action_id=f"act-{action_index + 2:03d}",
                        action_type=ActionType.PLACE,
                        target_id=part_id,
                        parameters={"destination_id": tray_id},
                    ),
                    depends_on=[grasp_step],
                ),
            ]
        )
        action_index += 3
    return TaskGraph(
        task_id="task-kit-three-parts",
        goal=goal,
        steps=steps,
        planner="template-v2",
        model_route="template",
    )


def build_inspection_plan(
    goal: str,
    entity_ids: Sequence[str] = DEFAULT_INSPECTION_ENTITIES,
) -> TaskGraph:
    entity_ids = _validate_entity_ids(entity_ids, "inspection")
    step_tokens = _unique_step_tokens(entity_ids, preserve_legacy_tokens=True)
    steps = [
        TaskStep(
            step_id=f"inspect-{step_tokens[entity_id]}",
            action=SemanticAction(
                action_id=f"act-{index:03d}",
                action_type=ActionType.OBSERVE,
                target_id=entity_id,
                parameters={
                    "attributes": ["presence", "identity", "orientation"],
                    "required_confidence": 0.8,
                },
            ),
        )
        for index, entity_id in enumerate(entity_ids, start=1)
    ]
    return TaskGraph(
        task_id="task-inspect-workpieces",
        goal=goal,
        steps=steps,
        planner="template-v2",
        model_route="template",
    )


def build_clear_workspace_plan(
    goal: str,
    obstacle_id: str = "blue_cylinder",
    target_id: str = "red_block",
) -> TaskGraph:
    obstacle_id = _validate_identifier(obstacle_id, "obstacle_id")
    target_id = _validate_identifier(target_id, "target_id")
    if obstacle_id == target_id:
        raise ValueError("obstacle_id and target_id must be different")
    steps = [
        TaskStep(
            step_id="observe-obstacle",
            action=SemanticAction(action_id="act-001", action_type=ActionType.OBSERVE, target_id=obstacle_id),
        ),
        TaskStep(
            step_id="grasp-obstacle",
            action=SemanticAction(action_id="act-002", action_type=ActionType.GRASP, target_id=obstacle_id),
            depends_on=["observe-obstacle"],
        ),
        TaskStep(
            step_id="clear-obstacle",
            action=SemanticAction(
                action_id="act-003",
                action_type=ActionType.PLACE,
                target_id=obstacle_id,
                parameters={"destination_id": "staging_bin"},
            ),
            depends_on=["grasp-obstacle"],
        ),
        TaskStep(
            step_id="observe-target",
            action=SemanticAction(action_id="act-004", action_type=ActionType.OBSERVE, target_id=target_id),
            depends_on=["clear-obstacle"],
        ),
        TaskStep(
            step_id="grasp-target",
            action=SemanticAction(action_id="act-005", action_type=ActionType.GRASP, target_id=target_id),
            depends_on=["observe-target"],
        ),
        TaskStep(
            step_id="place-target",
            action=SemanticAction(
                action_id="act-006",
                action_type=ActionType.PLACE,
                target_id=target_id,
                parameters={"destination_id": "tray"},
            ),
            depends_on=["grasp-target"],
        ),
    ]
    return TaskGraph(
        task_id="task-clear-workspace",
        goal=goal,
        steps=steps,
        planner="template-v2",
        model_route="template",
    )


def build_parcel_sorting_plan(
    goal: str,
    parcel_routes: Sequence[tuple[str, str]] = DEFAULT_PARCEL_ROUTES,
    observation_order: Sequence[str] | None = None,
) -> TaskGraph:
    if isinstance(parcel_routes, str):
        raise ValueError("parcel routing must be a sequence of entity/destination pairs")
    routes = tuple(parcel_routes)
    if not routes or any(not isinstance(route, tuple | list) or len(route) != 2 for route in routes):
        raise ValueError("parcel routing requires entity/destination pairs")
    parcel_ids = _validate_entity_ids(tuple(route[0] for route in routes), "parcel sorting")
    observed_parcel_ids = (
        parcel_ids if observation_order is None else _validate_entity_ids(observation_order, "parcel observation order")
    )
    if set(observed_parcel_ids) != set(parcel_ids):
        raise ValueError("parcel observation order must contain exactly the routed parcels")
    destinations = tuple(_validate_identifier(route[1], "destination_id") for route in routes)
    step_tokens = _unique_step_tokens(observed_parcel_ids)
    steps: list[TaskStep] = []
    observe_steps: list[str] = []
    action_index = 1
    for parcel_id in observed_parcel_ids:
        suffix = step_tokens[parcel_id]
        observe_step = f"inspect-{suffix}"
        observe_steps.append(observe_step)
        steps.append(
            TaskStep(
                step_id=observe_step,
                action=SemanticAction(
                    action_id=f"act-{action_index:03d}",
                    action_type=ActionType.OBSERVE,
                    target_id=parcel_id,
                    parameters={
                        "attributes": ["label_status", "condition"],
                        "required_confidence": 0.8,
                    },
                ),
            )
        )
        action_index += 1

    previous_route_step: str | None = None
    for parcel_id, destination_id in zip(parcel_ids, destinations, strict=True):
        suffix = step_tokens[parcel_id]
        grasp_step = f"grasp-{suffix}"
        route_step = f"route-{suffix}"
        dependencies = list(observe_steps)
        if previous_route_step is not None:
            dependencies.append(previous_route_step)
        steps.extend(
            [
                TaskStep(
                    step_id=grasp_step,
                    action=SemanticAction(
                        action_id=f"act-{action_index:03d}",
                        action_type=ActionType.GRASP,
                        target_id=parcel_id,
                    ),
                    depends_on=dependencies,
                ),
                TaskStep(
                    step_id=route_step,
                    action=SemanticAction(
                        action_id=f"act-{action_index + 1:03d}",
                        action_type=ActionType.PLACE,
                        target_id=parcel_id,
                        parameters={"destination_id": destination_id},
                    ),
                    depends_on=[grasp_step],
                ),
            ]
        )
        previous_route_step = route_step
        action_index += 2
    return TaskGraph(
        task_id="task-sort-parcels",
        goal=goal,
        steps=steps,
        planner="template-v3",
        model_route="template",
    )


def _parcel_policy_decision(
    attributes: Mapping[str, str],
    pickup_shelf_id: str,
    quarantine_bin_id: str,
) -> tuple[str, str, str, int]:
    if not isinstance(attributes, Mapping):
        raise ValueError("parcel attributes must be a mapping")
    values: dict[str, str] = {}
    for key in ("label_status", "condition"):
        value = attributes.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"parcel policy requires a non-empty {key}")
        values[key] = value.strip().lower()
    if values["label_status"] == "verified" and values["condition"] == "intact":
        return pickup_shelf_id, "verified_intact", "standard", 2
    reasons = []
    if values["label_status"] != "verified":
        reasons.append(f"label_{values['label_status']}")
    if values["condition"] != "intact":
        reasons.append(f"condition_{values['condition']}")
    priority = "condition_exception" if values["condition"] != "intact" else "label_exception"
    rank = 0 if priority == "condition_exception" else 1
    return quarantine_bin_id, "+".join(reasons), priority, rank


def _validate_destination_counts(
    values: Mapping[str, int],
    label: str,
    destination_ids: tuple[str, str],
) -> dict[str, int]:
    if not isinstance(values, Mapping):
        raise ValueError(f"{label} must be a mapping")
    if set(values) != set(destination_ids):
        raise ValueError(f"{label} must contain exactly the pickup and quarantine destinations")
    counts = dict(values)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
        raise ValueError(f"{label} values must be non-negative integers")
    return counts


def _normalize_parcel_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"parcel identity {label} must be a non-empty string when provided")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[\s-]+", "", normalized)
    if not normalized:
        raise ValueError(f"parcel identity {label} must contain readable characters")
    return normalized


def _parcel_identity_values(attributes: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(attributes, Mapping):
        raise ValueError("parcel attributes must be mappings")
    return {
        key: _normalize_parcel_identity(attributes[key], key)
        for key in PARCEL_IDENTITY_KEYS
        if attributes.get(key) is not None
    }


def _validate_unique_parcel_identities(
    parcel_attributes: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    seen: dict[str, tuple[str, str]] = {}
    identities: dict[str, dict[str, str]] = {}
    for parcel_id, attributes in parcel_attributes.items():
        identities[parcel_id] = _parcel_identity_values(attributes)
        for key, identity in identities[parcel_id].items():
            previous = seen.get(identity)
            if previous is not None and previous[0] != parcel_id:
                raw_value = attributes[key].strip()
                raise ValueError(
                    f"duplicate parcel identity {key}={raw_value!r}: {previous[0]}.{previous[1]} and {parcel_id}.{key}"
                )
            seen[identity] = (parcel_id, key)
    return identities


def _validate_parcel_manifest(
    parcel_attributes: Mapping[str, Mapping[str, str]],
    parcel_manifest: Mapping[str, Mapping[str, str]] | None,
    manifest_id: str | None,
) -> tuple[str | None, dict[str, str]]:
    if parcel_manifest is None:
        if manifest_id is not None:
            raise ValueError("manifest_id requires parcel_manifest")
        return None, {parcel_id: "not_checked" for parcel_id in parcel_attributes}
    normalized_manifest_id = _validate_identifier(manifest_id, "manifest_id").strip()
    if not isinstance(parcel_manifest, Mapping):
        raise ValueError("parcel_manifest must be a mapping")
    if set(parcel_manifest) != set(parcel_attributes):
        raise ValueError("parcel_manifest must contain exactly the planned parcel IDs")

    manifest_identities = _validate_unique_parcel_identities(parcel_manifest)
    statuses: dict[str, str] = {}
    for parcel_id, expected in parcel_manifest.items():
        unsupported = set(expected) - set(PARCEL_IDENTITY_KEYS)
        if unsupported:
            raise ValueError(f"parcel manifest contains unsupported identity keys: {', '.join(sorted(unsupported))}")
        expected_values = set(manifest_identities[parcel_id].values())
        if not expected_values:
            raise ValueError(f"parcel manifest requires an identity for {parcel_id}")
        observed_identities = _parcel_identity_values(parcel_attributes[parcel_id])
        conflicting_keys = [
            key
            for key, expected_value in manifest_identities[parcel_id].items()
            if key in observed_identities and observed_identities[key] != expected_value
        ]
        if conflicting_keys:
            raise ValueError(
                f"parcel {parcel_id} identity conflicts with manifest {normalized_manifest_id}: "
                f"{', '.join(conflicting_keys)}"
            )
        observed_values = set(observed_identities.values())
        if not observed_values:
            raise ValueError(f"parcel {parcel_id} has no readable identity for manifest {normalized_manifest_id}")
        if expected_values.isdisjoint(observed_values):
            raise ValueError(f"parcel {parcel_id} identity does not match manifest {normalized_manifest_id}")
        statuses[parcel_id] = "matched"
    return normalized_manifest_id, statuses


def build_policy_routed_parcel_plan(
    goal: str,
    parcel_attributes: Mapping[str, Mapping[str, str]],
    pickup_shelf_id: str = "pickup_shelf",
    quarantine_bin_id: str = "quarantine_bin",
    destination_capacities: Mapping[str, int] | None = None,
    destination_occupancy: Mapping[str, int] | None = None,
    parcel_manifest: Mapping[str, Mapping[str, str]] | None = None,
    manifest_id: str | None = None,
) -> TaskGraph:
    pickup_shelf_id = _validate_identifier(pickup_shelf_id, "pickup_shelf_id")
    quarantine_bin_id = _validate_identifier(quarantine_bin_id, "quarantine_bin_id")
    if pickup_shelf_id == quarantine_bin_id:
        raise ValueError("pickup and quarantine destinations must be different")
    if not isinstance(parcel_attributes, Mapping):
        raise ValueError("parcel_attributes must be a mapping")
    parcel_ids = _validate_entity_ids(tuple(parcel_attributes), "parcel policy")
    observed_identities = _validate_unique_parcel_identities(parcel_attributes)
    normalized_manifest_id, manifest_statuses = _validate_parcel_manifest(
        parcel_attributes, parcel_manifest, manifest_id
    )
    destinations = (pickup_shelf_id, quarantine_bin_id)
    capacities: dict[str, int] | None = None
    occupancy: dict[str, int] = {destination_id: 0 for destination_id in destinations}
    if destination_capacities is None:
        if destination_occupancy is not None:
            raise ValueError("destination_occupancy requires destination_capacities")
    else:
        capacities = _validate_destination_counts(destination_capacities, "destination_capacities", destinations)
        if destination_occupancy is not None:
            occupancy = _validate_destination_counts(destination_occupancy, "destination_occupancy", destinations)
        overfilled = [
            destination_id for destination_id in destinations if occupancy[destination_id] > capacities[destination_id]
        ]
        if overfilled:
            raise ValueError(f"destination occupancy exceeds capacity: {', '.join(sorted(overfilled))}")

    decisions: list[tuple[str, str, int]] = []
    reasons: dict[str, str] = {}
    priorities: dict[str, str] = {}
    for parcel_id in parcel_ids:
        destination_id, reason, priority, rank = _parcel_policy_decision(
            parcel_attributes[parcel_id], pickup_shelf_id, quarantine_bin_id
        )
        decisions.append((parcel_id, destination_id, rank))
        reasons[parcel_id] = reason
        priorities[parcel_id] = priority
    decisions.sort(key=lambda item: (item[2], item[0]))

    demand = Counter(destination_id for _, destination_id, _ in decisions)
    if capacities is not None:
        deficits = [
            f"{destination_id} needs {demand[destination_id]} slots but "
            f"{capacities[destination_id] - occupancy[destination_id]} are available"
            for destination_id in destinations
            if demand[destination_id] > capacities[destination_id] - occupancy[destination_id]
        ]
        if deficits:
            raise ValueError(f"parcel capacity preflight failed: {'; '.join(deficits)}")

    plan = build_parcel_sorting_plan(
        goal,
        [(parcel_id, destination_id) for parcel_id, destination_id, _ in decisions],
        observation_order=parcel_ids,
    )
    for step in plan.steps:
        if step.action.action_type is ActionType.OBSERVE:
            step.action.parameters["attributes"] = ["label_status", "condition", *PARCEL_IDENTITY_KEYS]
    projected_occupancy = occupancy.copy()
    for step in plan.steps:
        if step.action.action_type is ActionType.PLACE:
            destination_id = step.action.parameters["destination_id"]
            step.action.parameters.update(
                {
                    "routing_reason": reasons[step.action.target_id],
                    "routing_priority": priorities[step.action.target_id],
                    "policy_version": PARCEL_POLICY_VERSION,
                    "identity_guard": (
                        "unique_across_supported_fields"
                        if observed_identities[step.action.target_id]
                        else "not_available"
                    ),
                    "manifest_guard": manifest_statuses[step.action.target_id],
                }
            )
            if normalized_manifest_id is not None:
                step.action.parameters["manifest_id"] = normalized_manifest_id
            if capacities is not None:
                projected_occupancy[destination_id] += 1
                step.action.parameters.update(
                    {
                        "destination_capacity": capacities[destination_id],
                        "destination_occupancy_after": projected_occupancy[destination_id],
                        "destination_remaining_after": (
                            capacities[destination_id] - projected_occupancy[destination_id]
                        ),
                    }
                )
    return plan.model_copy(update={"planner": "parcel-policy-v3"})


def build_template_plan(goal: str, block_id: str = "red_block", tray_id: str = "tray") -> TaskGraph:
    """Route a bounded offline goal to semantic actions; never emit joint or firmware commands."""
    task_id = classify_template_task(goal)
    if task_id == "task-kit-three-parts":
        plan = build_kitting_plan(goal)
    elif task_id == "task-inspect-workpieces":
        plan = build_inspection_plan(goal)
    elif task_id == "task-clear-workspace":
        plan = build_clear_workspace_plan(goal)
    elif task_id == "task-sort-parcels":
        plan = build_policy_routed_parcel_plan(goal, DEFAULT_PARCEL_ATTRIBUTES)
    else:
        plan = build_place_plan(goal, block_id, tray_id)
    _validate_plan(plan)
    return plan
