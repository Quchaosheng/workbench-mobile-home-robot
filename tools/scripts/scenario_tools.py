import hashlib
import json
import random
from pathlib import Path
from typing import Any

from _jsonio import load_json
from workbench_contracts import ScenarioManifest

FROZEN_DISTRIBUTION = {
    "grasp_failure": 3,
    "moving_target": 3,
    "none": 3,
    "occlusion": 3,
}

P2_SCENE_VARIANTS = {"path_blocked", "low_light", "multi_object", "parcel_intake"}
SIMULATION_MANIFEST_EXTENSIONS = frozenset({"scene_variant"})
P2_FAULT_TYPES = {
    "actuator_timeout",
    "camera_dropout",
    "grasp_failure",
    "moving_target",
    "occlusion",
    "stale_observation",
}

SCENE_TASKS = {
    "low_light": "task-inspect-workpieces",
    "multi_object": "task-kit-three-parts",
    "path_blocked": "task-clear-workspace",
    "parcel_intake": "task-sort-parcels",
}

TASK_PROFILES: dict[str, dict[str, Any]] = {
    "task-place-red-block": {
        "goal": "Place the red block in the tray",
        "goal_zh": "把红色模块放进托盘",
        "entities": ("red_block",),
        "operations": (("red_block", "tray"),),
        "claim": "red_block in tray",
        "required_conditions": ("red_block in:tray",),
    },
    "task-kit-three-parts": {
        "goal": "Assemble a three-part kit in the kit tray",
        "goal_zh": "将红块、蓝色圆柱和绿色齿轮配成一套",
        "entities": ("red_block", "blue_cylinder", "green_gear"),
        "operations": (
            ("red_block", "kit_tray"),
            ("blue_cylinder", "kit_tray"),
            ("green_gear", "kit_tray"),
        ),
        "claim": "required kit contents present with no extras",
        "required_conditions": (
            "red_block in:kit_tray",
            "blue_cylinder in:kit_tray",
            "green_gear in:kit_tray",
            "no extra parts in:kit_tray",
        ),
    },
    "task-inspect-workpieces": {
        "goal": "Inspect presence, identity, and orientation of three workpieces",
        "goal_zh": "检验三种工件的在位、身份与朝向",
        "entities": ("red_block", "blue_cylinder", "green_gear"),
        "operations": (),
        "claim": "all workpieces inspected above confidence threshold",
        "required_conditions": (
            "red_block confidence>=0.8",
            "blue_cylinder confidence>=0.8",
            "green_gear confidence>=0.8",
        ),
    },
    "task-clear-workspace": {
        "goal": "Clear the blocking cylinder, then place the red block in the tray",
        "goal_zh": "先清走挡路圆柱,再把红块放入托盘",
        "entities": ("blue_cylinder", "red_block"),
        "operations": (("blue_cylinder", "staging_bin"), ("red_block", "tray")),
        "claim": "obstacle cleared and red_block in tray",
        "required_conditions": ("blue_cylinder in:staging_bin", "red_block in:tray"),
    },
    "task-sort-parcels": {
        "goal": "Scan the parcel batch, route verified intact parcels to pickup, and isolate exceptions",
        "goal_zh": "先扫描整批快递,完好核验件入取件架,标签异常或破损件进入隔离",
        "entities": ("parcel_box", "parcel_unreadable", "parcel_damaged"),
        # The evaluator derives parcel operations from the production policy planner.
        "operations": (),
        "attributes": {
            "parcel_box": {
                "label_status": "verified",
                "condition": "intact",
                "tracking_id": "WBX-BOX-20260807",
            },
            "parcel_unreadable": {
                "label_status": "unreadable",
                "condition": "intact",
                "parcel_uid": "WBX-UNK-20260807",
            },
            "parcel_damaged": {
                "label_status": "verified",
                "condition": "damaged",
                "barcode": "WBX-DMG-20260807",
            },
        },
        "manifest_id": "WB-INBOUND-20260807-003",
        "parcel_manifest": {
            "parcel_box": {"tracking_id": "WBX BOX 20260807"},
            "parcel_unreadable": {"parcel_uid": "WBX-UNK-20260807"},
            "parcel_damaged": {"tracking_id": "WBX-DMG-20260807"},
        },
        "claim": "verified intact parcels routed to pickup and all exceptions isolated",
        "required_conditions": (
            "parcel_box in:pickup_shelf",
            "parcel_unreadable in:quarantine_bin",
            "parcel_damaged in:quarantine_bin",
            "full batch observed before manipulation",
            "all non-verified or non-intact parcels isolated",
        ),
    },
}

ENTITY_APPEARANCE = {
    "red_block": {"entity_type": "block", "colour": "red"},
    "blue_cylinder": {"entity_type": "cylinder", "colour": "blue"},
    "green_gear": {"entity_type": "gear", "colour": "green"},
    "parcel_box": {"entity_type": "parcel", "colour": "kraft"},
    "parcel_envelope": {"entity_type": "envelope", "colour": "white"},
    "parcel_unreadable": {"entity_type": "parcel", "colour": "grey"},
    "parcel_damaged": {"entity_type": "parcel", "colour": "orange"},
}


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_simulation_manifest(payload: dict[str, Any]) -> ScenarioManifest:
    """Validate the canonical manifest plus reviewed simulator extensions."""

    canonical_fields = set(ScenarioManifest.model_fields)
    unknown_fields = set(payload) - canonical_fields - SIMULATION_MANIFEST_EXTENSIONS
    if unknown_fields:
        raise ValueError(f"unknown simulation manifest fields: {sorted(unknown_fields)}")

    canonical_payload = {field: payload[field] for field in canonical_fields if field in payload}
    manifest = ScenarioManifest.model_validate(canonical_payload, strict=True)

    scene_variant = payload.get("scene_variant")
    if scene_variant is not None and (type(scene_variant) is not str or scene_variant not in P2_SCENE_VARIANTS):
        raise ValueError(f"unsupported scene_variant: {scene_variant!r}")
    return manifest


def materialize_scenario(manifest: dict[str, Any]) -> dict[str, Any]:
    """Build the deterministic scene parameters owned by the scenario seed."""
    rng = random.Random(manifest["seed"])
    scene_variant = manifest.get("scene_variant", "baseline")
    task_id = manifest["task_id"]
    profile = TASK_PROFILES.get(task_id)
    if profile is None:
        raise ValueError(f"unsupported task_id: {task_id}")
    return {
        "scenario_id": manifest["scenario_id"],
        "seed": manifest["seed"],
        "task_id": task_id,
        "goal": profile["goal"],
        "fault_type": manifest["fault_type"],
        "scene_variant": scene_variant,
        "lighting_lux": 110 if scene_variant == "low_light" else 520,
        "path_obstacle": scene_variant == "path_blocked",
        "camera_noise": round(rng.uniform(0.005, 0.025), 6),
        "required_entities": list(profile["entities"]),
        "required_conditions": list(profile["required_conditions"]),
        "objects": [
            {
                "entity_id": entity_id,
                **ENTITY_APPEARANCE[entity_id],
                "attributes": dict(profile.get("attributes", {}).get(entity_id, {})),
                "x": round(rng.uniform(-0.18, 0.18), 6),
                "y": round(rng.uniform(-0.12, 0.12), 6),
                "yaw": round(rng.uniform(-3.141593, 3.141593), 6),
            }
            for entity_id in profile["entities"]
        ],
    }


def load_manifest(path: Path) -> dict[str, Any]:
    return load_json(path)


def reproducibility_hash(path: Path) -> str:
    return canonical_hash(materialize_scenario(load_manifest(path)))
