"""Parameter schemas for the nine semantic-action tools.

Each tool is defined by its ActionType, a set of required and optional parameter
keys, and the expected Python type for each parameter value.  The schemas here
are the single source of truth consumed by ToolRegistry; A5 (Policy Validator)
reads the same registry to build its whitelist.
"""

from __future__ import annotations

from workbench_contracts import ActionType

# ---------------------------------------------------------------------------
# per-tool definitions
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[ActionType, dict[str, object]] = {
    ActionType.OBSERVE: {
        "description": "Observe the workspace or a specific entity.  target_id is optional.",
        "target_id_required": False,
        "required_params": frozenset[str](),
        "optional_params": frozenset({"required_confidence", "attributes"}),
        "param_types": {
            "required_confidence": float,
            "attributes": list,
        },
        "param_constraints": {
            "required_confidence": {"finite": True, "minimum": 0.0, "maximum": 1.0},
        },
        "relational_constraints": frozenset[str](),
    },
    ActionType.GRASP: {
        "description": "Grasp a known entity.  target_id is required — a grasp "
        "without a target is a physical safety hazard.",
        "target_id_required": True,
        "required_params": frozenset[str](),
        "optional_params": frozenset[str](),
        "param_types": {},
        "param_constraints": {},
        "relational_constraints": frozenset[str](),
    },
    ActionType.PLACE: {
        "description": "Place a grasped entity into a destination.  target_id and destination_id are required.",
        "target_id_required": True,
        "required_params": frozenset({"destination_id"}),
        "optional_params": frozenset(
            {
                "routing_reason",
                "routing_priority",
                "policy_version",
                "identity_guard",
                "manifest_guard",
                "manifest_id",
                "destination_capacity",
                "destination_occupancy_after",
                "destination_remaining_after",
            }
        ),
        "param_types": {
            "destination_id": str,
            "routing_reason": str,
            "routing_priority": str,
            "policy_version": str,
            "identity_guard": str,
            "manifest_guard": str,
            "manifest_id": str,
            "destination_capacity": int,
            "destination_occupancy_after": int,
            "destination_remaining_after": int,
        },
        "param_constraints": {
            "destination_id": {"non_blank": True},
            "routing_reason": {"non_blank": True},
            "routing_priority": {"non_blank": True},
            "policy_version": {"non_blank": True},
            "identity_guard": {"non_blank": True},
            "manifest_guard": {"non_blank": True},
            "manifest_id": {"non_blank": True},
            "destination_capacity": {"minimum": 0},
            "destination_occupancy_after": {"minimum": 0},
            "destination_remaining_after": {"minimum": 0},
        },
        "relational_constraints": frozenset({"destination_counts_consistent"}),
    },
    ActionType.ASK_CONFIRM: {
        "description": "Ask a human operator for confirmation before proceeding.",
        "target_id_required": False,
        "required_params": frozenset({"question"}),
        "optional_params": frozenset({"timeout_s"}),
        "param_types": {
            "question": str,
            "timeout_s": int,
        },
        "param_constraints": {
            "question": {"non_blank": True},
            "timeout_s": {"minimum": 1, "maximum": 600},
        },
        "relational_constraints": frozenset[str](),
    },
    ActionType.EXPRESS: {
        "description": "Express an emotion state (idle / thinking / uncertain / pleased).",
        "target_id_required": False,
        "required_params": frozenset({"emotion_state"}),
        "optional_params": frozenset({"duration_ms"}),
        "param_types": {
            "emotion_state": str,
            "duration_ms": int,
        },
        "param_constraints": {
            "emotion_state": {"non_blank": True},
            "duration_ms": {"minimum": 1, "maximum": 600000},
        },
        "relational_constraints": frozenset[str](),
    },
    ActionType.NAVIGATE: {
        "description": "Navigate to a configured waypoint; the executor resolves target_id.",
        "target_id_required": True,
        "required_params": frozenset[str](),
        "optional_params": frozenset[str](),
        "param_types": {},
        "param_constraints": {},
        "relational_constraints": frozenset[str](),
    },
    ActionType.OPEN: {
        "description": "Open a configured articulated entity; motion policy and geometry remain executor-owned.",
        "target_id_required": True,
        "required_params": frozenset[str](),
        "optional_params": frozenset[str](),
        "param_types": {},
        "param_constraints": {},
        "relational_constraints": frozenset[str](),
    },
    ActionType.CLOSE: {
        "description": "Close a configured articulated entity; motion policy and geometry remain executor-owned.",
        "target_id_required": True,
        "required_params": frozenset[str](),
        "optional_params": frozenset[str](),
        "param_types": {},
        "param_constraints": {},
        "relational_constraints": frozenset[str](),
    },
    ActionType.STOP: {
        "description": "Safe-stop the current run.  No required parameters.",
        "target_id_required": False,
        "required_params": frozenset[str](),
        "optional_params": frozenset({"reason"}),
        "param_types": {
            "reason": str,
        },
        "param_constraints": {
            "reason": {"non_blank": True},
        },
        "relational_constraints": frozenset[str](),
    },
}

# Additional per-tool runtime constraints.
EXPRESS_EMOTION_STATES: frozenset[str] = frozenset({"idle", "thinking", "uncertain", "pleased"})

# Allow-list for observe attributes (used by policy-based planners and validated
# by the registry).
KNOWN_OBSERVE_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "presence",
        "identity",
        "orientation",
        "label_status",
        "condition",
        "tracking_id",
        "barcode",
        "parcel_uid",
    }
)
