# ruff: noqa: E501

from __future__ import annotations

import ast
import csv
import hashlib
import json
import math
import operator
import re
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "design-spec.json").read_text(encoding="utf-8"))
OUT = ROOT / "generated"
CALCULATION_VERSION = "revision-d-directional-stability-1"
DIRECTIONS = ("+X", "-X", "+Y", "-Y")
REQUIRED_MASS_FIELDS = {
    "id",
    "name",
    "mass_kg",
    "xyz",
    "reference_frame",
    "source",
    "uncertainty_kg",
    "status",
    "inclusion_rule",
}


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mass_model_payload(spec: dict[str, object] | None = None) -> dict[str, object]:
    source = SPEC if spec is None else spec
    return {"mass_model": source["mass_model"], "components": source["components"]}


def mass_model_sha256(spec: dict[str, object] | None = None) -> str:
    try:
        return hashlib.sha256(_canonical_json(mass_model_payload(spec))).hexdigest()
    except (TypeError, ValueError):
        return ""


def validate_mass_model(
    spec: dict[str, object] | None = None, ledger_rows: list[dict[str, str]] | None = None
) -> dict[str, object]:
    source = SPEC if spec is None else spec
    model = source.get("mass_model")
    components = source.get("components")
    errors: list[str] = []
    model_is_object = isinstance(model, dict)
    components_are_list = isinstance(components, list)
    if not model_is_object:
        errors.append("mass_model must be an object")
        model = {}
    if not components_are_list:
        errors.append("components must be a list")
        components = []
    required_model_fields = {
        "model_id",
        "revision",
        "authoritative_source",
        "units",
        "reference_frame",
        "tolerance_kg",
        "component_ids",
        "owner_approvals",
    }
    checks: dict[str, bool] = {
        "mass_model_metadata_is_complete": required_model_fields <= set(model),
        "mass_model_revision_matches_spec": model.get("revision") == source.get("revision"),
        "mass_model_units_are_controlled": model.get("units") == {"mass": "kg", "position": "mm"},
        "mass_model_reference_frame_is_present": isinstance(model.get("reference_frame"), str)
        and bool(model.get("reference_frame")),
        "mass_model_tolerance_is_positive": _is_finite_number(model.get("tolerance_kg"))
        and math.isfinite(float(model.get("tolerance_kg", 0)))
        and float(model.get("tolerance_kg", 0)) > 0,
        "owner_approval_records_are_explicit": isinstance(model.get("owner_approvals"), list)
        and {item.get("role") for item in model.get("owner_approvals", []) if isinstance(item, dict)}
        >= {"Product", "Mechanical", "Hardware", "Safety"},
        "components_are_present": bool(components),
    }
    ids: list[object] = []
    valid_components: list[dict[str, object]] = []
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            errors.append(f"component[{index}] must be an object")
            continue
        ids.append(component.get("id"))
        missing = REQUIRED_MASS_FIELDS - set(component)
        if missing:
            errors.append(f"component[{index}] missing fields: {sorted(missing)}")
        mass = component.get("mass_kg")
        uncertainty = component.get("uncertainty_kg")
        xyz = component.get("xyz")
        numeric_values = [mass, uncertainty, *(xyz if isinstance(xyz, list) else [])]
        if not all(_is_finite_number(value) for value in numeric_values):
            errors.append(f"component[{index}] has non-finite numeric values")
        if _is_finite_number(mass) and float(mass) <= 0:
            errors.append(f"component[{index}] mass must be positive")
        if _is_finite_number(uncertainty) and float(uncertainty) < 0:
            errors.append(f"component[{index}] uncertainty must not be negative")
        if not isinstance(xyz, list) or len(xyz) != 3:
            errors.append(f"component[{index}] xyz must contain three values")
        if component.get("reference_frame") != model.get("reference_frame"):
            errors.append(f"component[{index}] reference frame mismatch")
        if not isinstance(component.get("source"), str) or not component.get("source"):
            errors.append(f"component[{index}] source is missing")
        if component.get("status") not in {"ESTIMATE", "MEASURED", "NOT_EXECUTED"}:
            errors.append(f"component[{index}] status is not controlled")
        if component.get("inclusion_rule") not in {"AUTHORITATIVE_CURRENT", "EXCLUDED"}:
            errors.append(f"component[{index}] inclusion rule is not controlled")
        if component.get("inclusion_rule") == "EXCLUDED" and not component.get("exclusion_reason"):
            errors.append(f"component[{index}] exclusion reason is missing")
        if component.get("inclusion_rule") == "AUTHORITATIVE_CURRENT":
            valid_components.append(component)
    declared_ids = model.get("component_ids", [])
    declared_ids_valid = isinstance(declared_ids, list) and all(isinstance(item, str) and item for item in declared_ids)
    checks.update(
        {
            "component_ids_are_unique": all(isinstance(item, str) and item for item in ids)
            and len(ids) == len(set(ids)),
            "component_ids_match_model": declared_ids_valid and set(ids) == set(declared_ids),
            "component_records_are_valid": not errors,
            "authoritative_components_are_present": bool(valid_components),
        }
    )
    total_mass = sum(float(item["mass_kg"]) for item in valid_components) if valid_components and not errors else None
    center_of_gravity = (
        [
            sum(float(item["mass_kg"]) * float(item["xyz"][axis]) for item in valid_components) / total_mass
            for axis in range(3)
        ]
        if total_mass
        else None
    )
    checks["authoritative_mass_is_positive"] = total_mass is not None and math.isfinite(total_mass) and total_mass > 0
    if ledger_rows is not None:
        required_ledger_fields = {
            "component_id",
            "model_revision",
            "mass_kg",
            "x_mm",
            "y_mm",
            "z_mm",
            "reference_frame",
            "source",
            "uncertainty_kg",
            "inclusion_rule",
            "status",
        }
        checks["mass_ledger_schema_is_current"] = bool(ledger_rows) and required_ledger_fields <= set(ledger_rows[0])
        ledger_ids = [
            row.get("component_id") for row in ledger_rows if row.get("inclusion_rule") == "AUTHORITATIVE_CURRENT"
        ]
        checks["mass_ledger_has_no_duplicate_ids"] = len(ledger_ids) == len(set(ledger_ids))
        checks["mass_ledger_matches_authoritative_components"] = set(ledger_ids) == {
            item.get("id") for item in valid_components
        }
        tolerance = float(model.get("tolerance_kg", 0.001)) if _is_finite_number(model.get("tolerance_kg")) else 0.001
        component_by_id = {item.get("id"): item for item in valid_components}
        ledger_matches = True
        for row in ledger_rows:
            if row.get("inclusion_rule") != "AUTHORITATIVE_CURRENT":
                continue
            component = component_by_id.get(row.get("component_id"))
            if (
                component is None
                or row.get("model_revision") != model.get("revision")
                or row.get("reference_frame") != model.get("reference_frame")
            ):
                ledger_matches = False
                continue
            try:
                ledger_matches = (
                    ledger_matches and abs(float(row["mass_kg"]) - float(component["mass_kg"])) <= tolerance
                )
                ledger_matches = ledger_matches and row.get("component") == component.get("name")
                ledger_matches = ledger_matches and row.get("source") == component.get("source")
                ledger_matches = ledger_matches and row.get("status") == component.get("status")
                ledger_matches = ledger_matches and row.get("inclusion_rule") == component.get("inclusion_rule")
                ledger_matches = (
                    ledger_matches
                    and abs(float(row["uncertainty_kg"]) - float(component["uncertainty_kg"])) <= tolerance
                )
                ledger_matches = ledger_matches and all(
                    abs(float(row[f"{axis}_mm"]) - float(component["xyz"][position])) <= tolerance
                    for position, axis in enumerate(("x", "y", "z"))
                )
            except (KeyError, TypeError, ValueError):
                ledger_matches = False
        checks["mass_ledger_values_match_authoritative_source"] = ledger_matches
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "errors": errors,
        "total_mass_kg": total_mass,
        "center_of_gravity_mm": [round(value, 1) for value in center_of_gravity] if center_of_gravity else None,
        "component_count": len(valid_components),
        "mass_model_sha256": mass_model_sha256(source) if model_is_object and components_are_list else None,
    }


def calculate_directional_stability(
    center_of_gravity_mm: list[float],
    support_polygon: dict[str, object],
    pose_identifier: str,
    threshold_deg: float,
    ground_plane_z_mm: float = 0.0,
) -> dict[str, object]:
    """Calculate directional tip margins for an axis-aligned support polygon."""
    directions = support_polygon.get("edges", {})
    results: dict[str, object] = {}
    for direction in DIRECTIONS:
        edge = directions.get(direction) if isinstance(directions, dict) else None
        valid = isinstance(edge, dict) and edge.get("axis") in {"x", "y"}
        try:
            axis = 0 if isinstance(edge, dict) and edge.get("axis") == "x" else 1
            coordinate = float(edge["coordinate_mm"])
            cg_axis = float(center_of_gravity_mm[axis])
            cg_z = float(center_of_gravity_mm[2])
            margin = coordinate - cg_axis if direction.startswith("+") else cg_axis - coordinate
            angle = math.degrees(math.atan2(margin, cg_z - ground_plane_z_mm))
            valid = (
                valid and math.isfinite(margin) and math.isfinite(angle) and margin >= 0 and cg_z > ground_plane_z_mm
            )
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            valid = False
            margin = None
            angle = None
        results[direction] = {
            "direction": direction,
            "support_margin_mm": round(margin, 1) if margin is not None else None,
            "tip_angle_deg": round(angle, 1) if angle is not None else None,
            "limiting_support_edge": edge.get("name") if isinstance(edge, dict) else None,
            "pose_identifier": pose_identifier,
            "threshold_deg": threshold_deg,
            "pass": bool(valid and angle >= threshold_deg),
        }
    valid_results = [item for item in results.values() if isinstance(item, dict) and item["tip_angle_deg"] is not None]
    minimum = min(valid_results, key=lambda item: item["tip_angle_deg"]) if valid_results else None
    return {
        "pose_identifier": pose_identifier,
        "directions": results,
        "minimum_tip_angle_deg": minimum["tip_angle_deg"] if minimum else None,
        "limiting_direction": minimum["direction"] if minimum else None,
        "limiting_support_edge": minimum["limiting_support_edge"] if minimum else None,
        "pass": len(valid_results) == len(DIRECTIONS) and all(item["pass"] for item in valid_results),
    }


def _invalid_analysis(validation: dict[str, object]) -> dict[str, object]:
    return {
        "status": SPEC.get("validation_status", "CONCEPT_PHYSICAL_VALIDATION_REQUIRED"),
        "mass_kg": None,
        "center_of_gravity_mm": None,
        "mass_model": {"validation": validation},
        "stability": {"checks": {"directional_results_are_complete": False}, "cases": {}},
        "checks": {"mass_model_is_valid": False, "directional_stability_is_valid": False},
        "geometry_checks": {},
        "validation_errors": validation.get("errors", []),
    }


SCAD_PATH = ROOT / "cad" / "desk_robot.scad"
SCAD_MANIFEST_PATH = ROOT / "cad" / "scad-parameter-manifest.json"
SCAD_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*([^;]+);", re.MULTILINE)
_SCAD_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
SCAD_BOOLEAN_LITERALS = frozenset({"true", "false"})
SCAD_ALLOWED_FUNCTIONS = {"abs": abs, "cos": math.cos, "sin": math.sin, "sqrt": math.sqrt, "tan": math.tan}


def _evaluate_scad_expression(expression: str, resolved: dict[str, float]) -> float:
    """Evaluate one OpenSCAD constant expression with a restricted AST walk.

    OpenSCAD constants in this file are plain arithmetic over millimetre
    literals. Anything outside that grammar is rejected rather than executed,
    so a hand-edited SCAD file cannot smuggle arbitrary code into validation.
    """
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"malformed OpenSCAD expression: {expression!r}") from exc

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError(f"unsupported OpenSCAD constant: {expression!r}")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in SCAD_BOOLEAN_LITERALS:
                raise ValueError(f"{node.id} is a boolean flag, not a dimension: {expression!r}")
            if node.id not in resolved:
                raise ValueError(f"unknown OpenSCAD identifier: {node.id}")
            return resolved[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
            value = visit(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            operation = _SCAD_OPERATORS.get(type(node.op))
            if operation is None:
                raise ValueError(f"unsupported OpenSCAD operator: {expression!r}")
            return operation(visit(node.left), visit(node.right))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords or len(node.args) != 1:
                raise ValueError(f"unsupported OpenSCAD call: {expression!r}")
            function = SCAD_ALLOWED_FUNCTIONS.get(node.func.id)
            if function is None:
                raise ValueError(f"unsupported OpenSCAD function: {node.func.id}")
            return function(visit(node.args[0]))
        raise ValueError(f"unsupported OpenSCAD expression: {expression!r}")

    value = visit(tree)
    if not math.isfinite(value):
        raise ValueError(f"OpenSCAD constant is not finite: {expression!r}")
    return value


def read_scad_constants(path: Path) -> dict[str, str]:
    """Return the raw top-level ``NAME = expression`` assignments of a SCAD file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read OpenSCAD source: {exc}") from exc
    return {name: expression for name, expression in SCAD_ASSIGNMENT.findall(text)}


def resolve_spec_path(spec: dict[str, object], path: str) -> float:
    """Follow a dotted ``a.b.c`` path in the design specification."""
    node: object = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"design specification has no {path}")
        node = node[part]
    if not _is_finite_number(node):
        raise ValueError(f"design specification {path} is not a finite number")
    return float(node)


def _scad_result(checks: dict[str, bool], errors: list[str], **extra: object) -> dict[str, object]:
    return {"checks": checks, "errors": errors, "pass": all(checks.values()), **extra}


def validate_scad_parameters(spec: dict[str, object] | None = None) -> dict[str, object]:
    """Check the OpenSCAD parameter set against the authoritative design spec.

    The SCAD file hard-codes Revision D dimensions independently of the design
    specification, so the generated STEP, drawings and BOM can be regenerated
    from inputs that no longer describe the same geometry. Every SCAD constant
    must therefore be classified, and every shared value must match the spec.
    """
    source = SPEC if spec is None else spec
    checks: dict[str, bool] = {}
    errors: list[str] = []
    try:
        manifest = json.loads(SCAD_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _scad_result({"scad_manifest_is_present": False}, [f"cannot read the SCAD parameter manifest: {exc}"])

    checks["scad_manifest_is_present"] = isinstance(manifest, dict)
    checks["scad_manifest_revision_matches_spec"] = manifest.get("revision") == source.get("revision")
    checks["scad_manifest_declares_authoritative_source"] = (
        manifest.get("authoritative_source") == "hardware/mechanical/design-spec.json"
    )
    checks["scad_manifest_units_match_spec"] = manifest.get("units") == source.get("units")

    try:
        raw_constants = read_scad_constants(SCAD_PATH)
        scad_hash = sha256_file(SCAD_PATH)
        manifest_hash = sha256_file(SCAD_MANIFEST_PATH)
    except (OSError, ValueError) as exc:
        return _scad_result({**checks, "scad_source_is_readable": False}, [f"cannot read OpenSCAD source: {exc}"])
    checks["scad_source_is_readable"] = bool(raw_constants)

    shared = [entry for entry in manifest.get("shared_parameters", []) if isinstance(entry, dict)]
    derived = [entry for entry in manifest.get("derived_parameters", []) if isinstance(entry, dict)]
    visual_only = [entry for entry in manifest.get("visual_only_parameters", []) if isinstance(entry, dict)]
    dimensional = {entry.get("scad") for entry in [*shared, *derived]}
    flags = {entry.get("scad") for entry in visual_only}

    classified = [entry.get("scad") for entry in [*shared, *derived, *visual_only]]
    duplicates = sorted({name for name in classified if classified.count(name) > 1})
    unclassified = sorted(set(raw_constants) - set(classified))
    stray = sorted(set(classified) - set(raw_constants))
    undocumented = sorted(
        entry.get("scad")
        for entry in [*derived, *visual_only]
        if not str(entry.get("reason", "")).strip()
        or (entry in visual_only and not str(entry.get("owner", "")).strip())
    )
    checks["scad_derived_and_visual_parameters_are_documented"] = (
        bool(derived) and bool(visual_only) and not undocumented
    )
    if undocumented:
        errors.append(f"SCAD parameters without a documented reason or owner: {undocumented}")

    checks["scad_constants_are_classified_once"] = (
        bool(classified) and not duplicates and not unclassified and not stray and set(dimensional).isdisjoint(flags)
    )
    if unclassified:
        errors.append(f"unclassified OpenSCAD constants: {unclassified}")
    if stray:
        errors.append(f"SCAD manifest references missing constants: {stray}")
    if duplicates:
        errors.append(f"duplicate SCAD manifest entries: {duplicates}")

    # Only shared and derived parameters are dimensions, so only they need a
    # numeric value; a visual-only entry may legitimately be a boolean flag.
    resolved: dict[str, float] = {}
    try:
        # File order respects OpenSCAD's define-before-use rule, so a derived
        # expression always sees its inputs already resolved.
        for name in raw_constants:
            if name in dimensional:
                resolved[name] = _evaluate_scad_expression(raw_constants[name], resolved)
    except (KeyError, TypeError, ValueError) as exc:
        return _scad_result(
            {**checks, "scad_constants_are_evaluable": False},
            [*errors, f"cannot evaluate OpenSCAD constants: {exc}"],
        )
    checks["scad_constants_are_evaluable"] = True
    negatives = sorted(name for name, value in resolved.items() if value < 0)
    if negatives:
        errors.append(f"negative OpenSCAD constants: {negatives}")
    checks["scad_constants_are_non_negative"] = not negatives

    parameters: dict[str, dict[str, object]] = {}
    drift: list[str] = []
    for entry in shared:
        name = str(entry.get("scad"))
        spec_path = str(entry.get("spec_path"))
        try:
            expected = resolve_spec_path(source, spec_path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        actual = resolved.get(name)
        tolerance = float(entry.get("tolerance", manifest.get("default_tolerance", 0.0)))
        matches = actual is not None and abs(actual - expected) <= tolerance
        parameters[name] = {"spec_path": spec_path, "scad_value": actual, "spec_value": expected, "pass": matches}
        if not matches:
            drift.append(f"{name}={actual} does not match {spec_path}={expected}")
    checks["shared_parameters_match_design_spec"] = (
        bool(shared) and not drift and set(parameters) == {entry.get("scad") for entry in shared}
    )
    errors.extend(drift)

    return _scad_result(
        checks,
        errors,
        parameters=parameters,
        resolved_constants={name: round(value, 6) for name, value in sorted(resolved.items())},
        visual_only_parameters=sorted(flags),
        source_hashes={"desk_robot_scad_sha256": scad_hash, "scad_parameter_manifest_sha256": manifest_hash},
    )


def geometry_context() -> dict[str, float | str]:
    """Return the shared raised-pose Z datums used by every mechanical exporter."""
    coordinate = SPEC["coordinate_system"]
    travel = float(SPEC["lifting_platform"]["travel"])
    torso_height = float(SPEC["torso"]["height"])
    head_height = float(SPEC["head"]["height"])
    torso_bottom_stowed = float(coordinate["torso_bottom_stowed_z_mm"])
    head_top_stowed = float(coordinate["head_top_stowed_z_mm"])
    return {
        "ground_plane_z": float(coordinate["ground_plane_z_mm"]),
        "base_top_z": float(coordinate["base_top_z_mm"]),
        "travel": travel,
        "torso_bottom_stowed_z": torso_bottom_stowed,
        "torso_bottom_raised_z": torso_bottom_stowed + travel,
        "torso_top_raised_z": torso_bottom_stowed + travel + torso_height,
        "head_top_stowed_z": head_top_stowed,
        "head_top_raised_z": head_top_stowed + travel,
        "head_bottom_raised_z": head_top_stowed + travel - head_height,
        "assembly_pose": str(coordinate["generated_assembly_pose"]),
    }


def analyse() -> dict[str, object]:
    try:
        with (ROOT / "mass-ledger.csv").open(newline="", encoding="utf-8") as handle:
            ledger_rows = list(csv.DictReader(handle))
    except OSError:
        ledger_rows = []
    validation = validate_mass_model(SPEC, ledger_rows)
    if not validation["pass"]:
        return _invalid_analysis(validation)
    scad_validation = validate_scad_parameters(SPEC)
    components = [item for item in SPEC["components"] if item["inclusion_rule"] == "AUTHORITATIVE_CURRENT"]
    total = float(validation["total_mass_kg"])
    arm_mass = max(float(item["mass_kg"]) for item in components if item["name"].endswith("seven_axis_arm"))

    def load_case(name: str) -> dict[str, object]:
        case = SPEC["analysis_load_cases"][name]
        followers = set(SPEC["lift_follower_components"])
        overrides = case["position_overrides"]
        positioned: list[dict[str, object]] = []
        for item in components:
            xyz = list(overrides.get(item["name"], item["xyz"]))
            if item["name"] in followers and item["name"] not in overrides:
                xyz[2] += case["lift_followers_z_offset_mm"]
            positioned.append({**item, "xyz": xyz})
        cg = [
            sum(float(item["mass_kg"]) * float(item["xyz"][axis]) for item in positioned) / total for axis in range(3)
        ]
        return {
            "description": case["description"],
            "center_of_gravity_mm": [round(value, 1) for value in cg],
        }

    try:
        navigation = load_case("navigation_low")
        manipulation = load_case("stabilized_manipulation")
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        invalid_validation = dict(validation)
        invalid_validation["errors"] = [*validation.get("errors", []), f"invalid analysis load case: {exc}"]
        invalid_validation["pass"] = False
        return _invalid_analysis(invalid_validation)
    manipulation_cg = manipulation["center_of_gravity_mm"]
    stability_spec = SPEC.get("stability_analysis", {})
    if not isinstance(stability_spec, dict):
        stability_spec = {}
    stability_load_cases = SPEC.get("stability_load_cases", {})
    if not isinstance(stability_load_cases, dict):
        stability_load_cases = {}
    support_polygons = stability_spec.get("support_polygons", {})
    if not isinstance(support_polygons, dict):
        support_polygons = {}
    thresholds = stability_spec.get("thresholds_deg", {})
    if not isinstance(thresholds, dict):
        thresholds = {}
    polygons_are_valid = True
    for configuration in ("drive", "stabilized"):
        polygon = support_polygons.get(configuration)
        vertices = polygon.get("vertices_mm") if isinstance(polygon, dict) else None
        edges = polygon.get("edges") if isinstance(polygon, dict) else None
        polygons_are_valid = polygons_are_valid and isinstance(vertices, list) and len(vertices) >= 4
        polygons_are_valid = polygons_are_valid and isinstance(edges, dict) and set(edges) >= set(DIRECTIONS)
        if isinstance(vertices, list):
            polygons_are_valid = polygons_are_valid and all(
                isinstance(vertex, list) and len(vertex) == 2 and all(_is_finite_number(value) for value in vertex)
                for vertex in vertices
            )
        if isinstance(edges, dict):
            polygons_are_valid = polygons_are_valid and all(
                isinstance(edges.get(direction), dict)
                and edges[direction].get("axis") in {"x", "y"}
                and isinstance(edges[direction].get("name"), str)
                and _is_finite_number(edges[direction].get("coordinate_mm"))
                and math.isfinite(float(edges[direction]["coordinate_mm"]))
                for direction in DIRECTIONS
            )
    thresholds_are_valid = all(
        _is_finite_number(thresholds.get(configuration))
        and math.isfinite(float(thresholds[configuration]))
        and float(thresholds[configuration]) > 0
        for configuration in ("drive", "stabilized")
    )
    try:
        support_polygons_match_chassis = (
            support_polygons["drive"]["edges"]["+X"]["coordinate_mm"] == float(SPEC["chassis"]["track"]) / 2
            and support_polygons["drive"]["edges"]["-X"]["coordinate_mm"] == -float(SPEC["chassis"]["track"]) / 2
            and support_polygons["drive"]["edges"]["+Y"]["coordinate_mm"] == float(SPEC["chassis"]["wheelbase"]) / 2
            and support_polygons["drive"]["edges"]["-Y"]["coordinate_mm"] == -float(SPEC["chassis"]["wheelbase"]) / 2
            and support_polygons["stabilized"]["edges"]["+X"]["coordinate_mm"]
            == float(SPEC["chassis"]["stabilized_support_width"]) / 2
            and support_polygons["stabilized"]["edges"]["-X"]["coordinate_mm"]
            == -float(SPEC["chassis"]["stabilized_support_width"]) / 2
            and support_polygons["stabilized"]["edges"]["+Y"]["coordinate_mm"]
            == float(SPEC["chassis"]["stabilized_support_depth"]) / 2
            and support_polygons["stabilized"]["edges"]["-Y"]["coordinate_mm"]
            == -float(SPEC["chassis"]["stabilized_support_depth"]) / 2
        )
    except (KeyError, TypeError, ValueError):
        support_polygons_match_chassis = False
    stability_cases: dict[str, object] = {}
    for case_id, case_spec in stability_load_cases.items():
        if not isinstance(case_spec, dict):
            stability_cases[case_id] = {"pass": False, "directions": {}}
            continue
        source_case = case_spec.get("source_load_case")
        configuration = case_spec.get("configuration")
        source = (
            navigation
            if source_case == "navigation_low"
            else manipulation
            if source_case == "stabilized_manipulation"
            else None
        )
        polygon = support_polygons.get(configuration)
        threshold = thresholds.get(configuration)
        if source is None or polygon is None or threshold is None:
            stability_cases[case_id] = {
                "pass": False,
                "directions": {},
                "pose_identifier": case_spec.get("pose_identifier"),
            }
            continue
        try:
            result = calculate_directional_stability(
                source["center_of_gravity_mm"],
                polygon,
                str(case_spec.get("pose_identifier", case_id)),
                float(threshold),
                float(stability_spec.get("ground_plane_z_mm", 0.0)),
            )
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            result = {
                "pose_identifier": str(case_spec.get("pose_identifier", case_id)),
                "directions": {},
                "minimum_tip_angle_deg": None,
                "limiting_direction": None,
                "limiting_support_edge": None,
                "pass": False,
            }
        result.update(
            {
                "configuration": configuration,
                "source_load_case": source_case,
                "dynamic_stability_factor": case_spec.get("dynamic_stability_factor", 1.0),
                "description": case_spec.get("description", source.get("description")),
            }
        )
        try:
            dynamic_factor_valid = (
                math.isfinite(float(result["dynamic_stability_factor"]))
                and float(result["dynamic_stability_factor"]) > 0
            )
        except (TypeError, ValueError):
            dynamic_factor_valid = False
        result["dynamic_stability_factor_valid"] = dynamic_factor_valid
        result["pass"] = result["pass"] and dynamic_factor_valid
        stability_cases[case_id] = result
    gate_results: dict[str, object] = {}
    for configuration in ("drive", "stabilized"):
        cases = [item for item in stability_cases.values() if item.get("configuration") == configuration]
        directional_minimums: dict[str, object] = {}
        for direction in DIRECTIONS:
            candidates = [
                item["directions"][direction]
                for item in cases
                if direction in item.get("directions", {})
                and item["directions"][direction]["tip_angle_deg"] is not None
            ]
            directional_minimums[direction] = (
                min(candidates, key=lambda item: item["tip_angle_deg"]) if candidates else None
            )
        valid = (
            stability_spec.get("directions") == list(DIRECTIONS)
            and polygons_are_valid
            and support_polygons_match_chassis
            and thresholds_are_valid
            and all(directional_minimums.values())
            and bool(cases)
            and all(item.get("pass", False) for item in cases)
        )
        limiting = min(
            (item for item in directional_minimums.values() if item),
            key=lambda item: item["tip_angle_deg"],
            default=None,
        )
        gate_results[configuration] = {
            "threshold_deg": thresholds.get(configuration),
            "case_ids": [
                case_id for case_id, item in stability_cases.items() if item.get("configuration") == configuration
            ],
            "directional_minimums": directional_minimums,
            "minimum_tip_angle_deg": limiting["tip_angle_deg"] if limiting else None,
            "limiting_direction": limiting["direction"] if limiting else None,
            "limiting_support_edge": limiting["limiting_support_edge"] if limiting else None,
            "pass": bool(valid),
        }
    drive_minimum = gate_results["drive"]["minimum_tip_angle_deg"]
    stabilized_minimum = gate_results["stabilized"]["minimum_tip_angle_deg"]
    drive_tip_angle = float(drive_minimum) if drive_minimum is not None else 0.0
    stabilized_tip_angle = float(stabilized_minimum) if stabilized_minimum is not None else 0.0
    drive_margins = [
        item["support_margin_mm"] for item in gate_results["drive"]["directional_minimums"].values() if item
    ]
    stabilized_margins = [
        item["support_margin_mm"] for item in gate_results["stabilized"]["directional_minimums"].values() if item
    ]
    navigation["minimum_support_margin_mm"] = round(min(drive_margins), 1) if drive_margins else None
    navigation["tip_angle_deg"] = round(drive_tip_angle, 1)
    manipulation["minimum_support_margin_mm"] = round(min(stabilized_margins), 1) if stabilized_margins else None
    manipulation["tip_angle_deg"] = round(stabilized_tip_angle, 1)
    stability_checks = {
        "declared_directions_are_complete": stability_spec.get("directions") == list(DIRECTIONS),
        "calculation_version_is_current": stability_spec.get("calculation_version") == CALCULATION_VERSION,
        "support_polygons_are_defined": polygons_are_valid,
        "support_polygons_match_chassis": support_polygons_match_chassis,
        "stability_thresholds_are_valid": thresholds_are_valid,
        "dynamic_stability_factors_are_valid": all(
            item.get("dynamic_stability_factor_valid", False) for item in stability_cases.values()
        ),
        "required_stability_cases_are_present": set(stability_cases) >= set(stability_spec.get("required_cases", [])),
        "drive_directional_results_are_complete": bool(gate_results["drive"]["pass"]),
        "stabilized_directional_results_are_complete": bool(gate_results["stabilized"]["pass"]),
        "gates_use_directional_minimums": all(
            gate_results[configuration]["minimum_tip_angle_deg"] is not None
            and all(gate_results[configuration]["directional_minimums"].values())
            and gate_results[configuration]["minimum_tip_angle_deg"]
            == min(item["tip_angle_deg"] for item in gate_results[configuration]["directional_minimums"].values())
            for configuration in ("drive", "stabilized")
        ),
    }
    drop_energy = total * 9.80665 * SPEC["impact"]["drop_height_m"]
    stop_distance = drop_energy / (total * SPEC["impact"]["design_deceleration_g"] * 9.80665) * 1000
    tray = SPEC["electronics_tray"]
    pcb_width, pcb_depth, _ = tray["pcb_envelope"]
    service_margin = [(tray["width"] - pcb_width) / 2, (tray["depth"] - pcb_depth) / 2]
    geometry = geometry_context()
    enclosure_height = float(SPEC["enclosure"]["height"])
    stowed_height = float(SPEC["enclosure"]["stowed_height"])
    return {
        "status": SPEC["validation_status"],
        "mass_kg": round(total, 3),
        "mass_model": {
            "model_id": SPEC["mass_model"]["model_id"],
            "revision": SPEC["mass_model"]["revision"],
            "authoritative_source": SPEC["mass_model"]["authoritative_source"],
            "source_hashes": {
                "design_spec_sha256": sha256_file(ROOT / "design-spec.json"),
                "mass_ledger_sha256": sha256_file(ROOT / "mass-ledger.csv"),
                "legacy_mass_ledger_sha256": sha256_file(ROOT / "mass-ledger-legacy.csv")
                if (ROOT / "mass-ledger-legacy.csv").exists()
                else None,
            },
            "mass_model_sha256": validation["mass_model_sha256"],
            "units": SPEC["mass_model"]["units"],
            "reference_frame": SPEC["mass_model"]["reference_frame"],
            "component_count": validation["component_count"],
            "mass_kg": round(total, 3),
            "center_of_gravity_mm": manipulation_cg,
            "status": "ESTIMATE",
            "validation_status": SPEC["validation_status"],
            "owner_approval_status": "REQUIRED",
        },
        "center_of_gravity_mm": manipulation_cg,
        "load_cases": {
            "navigation_low": navigation,
            "stabilized_manipulation": manipulation,
        },
        "static_tip_angle_deg": round(stabilized_tip_angle, 1),
        "drive_footprint_tip_angle_deg": round(drive_tip_angle, 1),
        "stabilized_tip_angle_deg": round(stabilized_tip_angle, 1),
        "stability": {
            "calculation_version": CALCULATION_VERSION,
            "coordinate_convention": stability_spec.get("coordinate_convention"),
            "tip_angle_formula": stability_spec.get("tip_angle_formula"),
            "ground_plane_z_mm": stability_spec.get("ground_plane_z_mm"),
            "directions": list(DIRECTIONS),
            "support_polygons": support_polygons,
            "cases": stability_cases,
            "directional_results": stability_cases,
            "gates": gate_results,
            "checks": stability_checks,
        },
        "payload_only_moment_nm": round(
            SPEC["manipulator"]["continuous_payload"]["mass_kg"]
            * 9.80665
            * SPEC["manipulator"]["continuous_payload"]["reach_mm"]
            / 1000,
            1,
        ),
        "arm_plus_payload_screen_moment_nm": round(
            (SPEC["manipulator"]["continuous_payload"]["mass_kg"] + arm_mass)
            * 9.80665
            * SPEC["manipulator"]["continuous_payload"]["reach_mm"]
            / 1000,
            1,
        ),
        "bimanual_shared_workspace_screen_moment_nm": round(
            (2 * arm_mass + SPEC["manipulator"]["bimanual_payload"]["mass_kg"])
            * 9.80665
            * SPEC["manipulator"]["bimanual_payload"]["reach_mm"]
            / 1000,
            1,
        ),
        "drop_energy_j": round(drop_energy, 1),
        "minimum_energy_absorber_stroke_mm": round(stop_distance, 1),
        "vent_area_ratio_outlet_to_inlet": round(
            SPEC["ventilation"]["outlet_area_mm2"] / SPEC["ventilation"]["inlet_area_mm2"], 2
        ),
        "checks": {
            "mass_model_is_valid": bool(validation["pass"]),
            "drive_tip_angle_at_least_25_deg": bool(gate_results["drive"]["pass"]),
            "stabilized_tip_angle_at_least_35_deg": bool(gate_results["stabilized"]["pass"]),
            **stability_checks,
            "outlet_area_at_least_inlet": SPEC["ventilation"]["outlet_area_mm2"]
            >= SPEC["ventilation"]["inlet_area_mm2"],
            "absorber_at_least_derived_stroke": SPEC["impact"]["effective_absorber_stroke_mm"] >= stop_distance,
            "pcb_fits_electronics_tray": pcb_width <= tray["width"] and pcb_depth <= tray["depth"],
            "pcb_edge_service_margin_met": min(service_margin) >= tray["minimum_edge_service_margin"],
        },
        "pcb_tray_margin_mm": [tray["width"] - pcb_width, tray["depth"] - pcb_depth],
        "pcb_edge_service_margin_mm": service_margin,
        "geometry_reference": {
            "assembly_pose": geometry["assembly_pose"],
            "base_top_z_mm": geometry["base_top_z"],
            "torso_bottom_raised_z_mm": geometry["torso_bottom_raised_z"],
            "head_top_raised_z_mm": geometry["head_top_raised_z"],
            "head_top_stowed_z_mm": geometry["head_top_stowed_z"],
        },
        "geometry_checks": {
            "stowed_plus_travel_matches_raised_height": abs(stowed_height + geometry["travel"] - enclosure_height)
            <= float(SPEC["coordinate_system"]["height_tolerance_mm"]),
            "raised_height_matches_enclosure_height": abs(geometry["head_top_raised_z"] - enclosure_height)
            <= float(SPEC["coordinate_system"]["height_tolerance_mm"]),
            "torso_starts_above_base": geometry["torso_bottom_raised_z"] > geometry["base_top_z"],
        },
        "scad_parameters": scad_validation,
        "scad_parameter_checks": scad_validation["checks"],
    }


def step_box(width: float, depth: float, height: float) -> str:
    # AP203 faceted envelope with six closed faces; origin is centred on X/Y.
    pts = [
        (-width / 2, -depth / 2, 0),
        (width / 2, -depth / 2, 0),
        (width / 2, depth / 2, 0),
        (-width / 2, depth / 2, 0),
        (-width / 2, -depth / 2, height),
        (width / 2, -depth / 2, height),
        (width / 2, depth / 2, height),
        (-width / 2, depth / 2, height),
    ]
    lines = [
        "ISO-10303-21;",
        "HEADER;",
        "FILE_DESCRIPTION(('DESK ROBOT ENVELOPE'),'2;1');",
        "FILE_NAME('enclosure.step','2026-08-06T00:00:00',('Workbench-1'),('Quchaosheng'),'Codex','Workbench-1','');",
        "FILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));",
        "ENDSEC;",
        "DATA;",
        "#1=APPLICATION_CONTEXT('configuration controlled 3d designs of mechanical parts and assemblies');",
        "#2=PRODUCT_CONTEXT('',#1,'mechanical');",
        "#3=PRODUCT('DESK_ROBOT_ENVELOPE','DESK_ROBOT_ENVELOPE','',(#2));",
    ]
    for index, point in enumerate(pts, start=10):
        lines.append(f"#{index}=CARTESIAN_POINT('',({point[0]:.3f},{point[1]:.3f},{point[2]:.3f}));")
    lines += [
        "#30=POLYLINE('',(#10,#11,#12,#13,#10));",
        "#31=POLYLINE('',(#14,#15,#16,#17,#14));",
        "#32=POLYLINE('',(#10,#11,#15,#14,#10));",
        "#33=POLYLINE('',(#11,#12,#16,#15,#11));",
        "#34=POLYLINE('',(#12,#13,#17,#16,#12));",
        "#35=POLYLINE('',(#13,#10,#14,#17,#13));",
        "ENDSEC;",
        "END-ISO-10303-21;",
    ]
    return "\n".join(lines) + "\n"


def export_solid_step(path: Path) -> bool:
    try:
        import cadquery as cq
    except ImportError:
        return False

    enclosure = SPEC["enclosure"]
    width, depth, height = enclosure["width"], enclosure["depth"], enclosure["height"]
    wall, radius = enclosure["wall"], enclosure["corner_radius"]
    outer = cq.Workplane("XY").box(width, depth, height).edges("|Z").fillet(radius).translate((0, 0, height / 2))
    inner_height = height - wall
    inner = (
        cq.Workplane("XY")
        .box(width - 2 * wall, depth - 2 * wall, inner_height)
        .edges("|Z")
        .fillet(radius - wall)
        .translate((0, 0, wall + inner_height / 2))
    )
    shell = outer.cut(inner)
    display_width, display_height = SPEC["head"]["display_cutout"]
    display_radius = SPEC["head"]["display_corner_radius_mm"]
    display = (
        cq.Workplane("XY")
        .box(display_width, wall * 4, display_height)
        .edges("|Y")
        .fillet(display_radius)
        .translate((0, -depth / 2, height - SPEC["head"]["height"] / 2))
    )
    shell = shell.cut(display)
    cq.exporters.export(shell, str(path), exportType="STEP")
    normalized = "\n".join(line.rstrip() for line in path.read_text(encoding="ascii").splitlines()) + "\n"
    path.write_text(normalized, encoding="ascii", newline="\n")
    return True


def normalize_step(path: Path) -> None:
    normalized = "\n".join(line.rstrip() for line in path.read_text(encoding="ascii").splitlines()) + "\n"
    path.write_text(normalized, encoding="ascii", newline="\n")


def export_cad_package() -> bool:
    try:
        import cadquery as cq
    except ImportError:
        return False

    chassis_spec = SPEC["chassis"]
    head_spec = SPEC["head"]
    torso_spec = SPEC["torso"]
    geometry = geometry_context()
    base_body_bottom = float(SPEC["chassis"]["ground_clearance"])
    base_top = geometry["base_top_z"]
    torso_bottom = geometry["torso_bottom_raised_z"]
    head_top = geometry["head_top_raised_z"]
    head_height = float(head_spec["height"])
    head_depth = float(head_spec["depth"])
    head_angle = math.radians(float(head_spec["tilt_deg"]))
    head_half_z = (head_height / 2) * math.cos(head_angle) + (head_depth / 2) * math.sin(head_angle)
    head_center_z = head_top - head_half_z
    head_bottom = head_center_z - head_half_z

    chassis = (
        cq.Workplane("XY")
        .box(chassis_spec["width"], chassis_spec["depth"], base_top - base_body_bottom)
        .edges("|Z")
        .fillet(45)
        .translate((0, 0, (base_body_bottom + base_top) / 2))
    )
    drive_module_solids = [chassis.val()]
    steering_angle = math.radians(18)
    wheel_axis = cq.Vector(math.cos(steering_angle), math.sin(steering_angle), 0)
    for x in (-195, 195):
        for y in (-185, 185):
            wheel_center = cq.Vector(x, y, chassis_spec["wheel_diameter_mm"] / 2)
            wheel_width = chassis_spec["wheel_width_mm"]
            wheel = cq.Solid.makeCylinder(
                chassis_spec["wheel_diameter_mm"] / 2,
                wheel_width,
                wheel_center - wheel_axis.multiply(wheel_width / 2),
                wheel_axis,
            )
            hub = cq.Solid.makeCylinder(
                24,
                wheel_width + 6,
                wheel_center - wheel_axis.multiply((wheel_width + 6) / 2),
                wheel_axis,
            )
            fork = cq.Workplane("XY").box(62, 58, 42).edges("|Z").fillet(14).translate((x, y, 88)).val()
            steering_bearing = cq.Solid.makeCylinder(31, 34, cq.Vector(x, y, 96), cq.Vector(0, 0, 1))
            drive_module_solids.extend((wheel, hub, fork, steering_bearing))
    mobile_base = cq.Compound.makeCompound(drive_module_solids)
    lift_lower_height = geometry["torso_bottom_stowed_z"] - base_top
    lift_lower = (
        cq.Workplane("XY")
        .box(210, 175, lift_lower_height)
        .edges("|Z")
        .fillet(22)
        .translate((0, 12, base_top + lift_lower_height / 2))
    )
    lift_carriage_height = 350
    lift_carriage_top = torso_bottom - 10
    lift_upper = (
        cq.Workplane("XY")
        .box(170, 138, lift_carriage_height)
        .edges("|Z")
        .fillet(18)
        .translate((0, 12, lift_carriage_top - lift_carriage_height / 2))
    )
    lift_plate = cq.Workplane("XY").box(270, 228, 28).edges("|Z").fillet(24).translate((0, 12, torso_bottom - 14))
    lifting_platform = lift_lower.union(lift_upper).union(lift_plate)
    navigation_lifting_platform = lift_lower.union(lift_upper.translate((0, 0, -geometry["travel"]))).union(
        lift_plate.translate((0, 0, -geometry["travel"]))
    )
    torso = (
        cq.Workplane("XY")
        .box(torso_spec["width"], torso_spec["depth"], torso_spec["height"])
        .edges("|Z")
        .fillet(38)
        .translate((0, 0, torso_bottom + torso_spec["height"] / 2))
    )
    head = (
        cq.Workplane("XY")
        .box(head_spec["width"], head_spec["depth"], head_height)
        .edges("|Z")
        .fillet(30)
        .rotate((0, 0, 0), (1, 0, 0), head_spec["tilt_deg"])
        .translate((0, -12, head_center_z))
    )
    face_lens = (
        cq.Workplane("XY")
        .box(head_spec["display_cutout"][0], 5, head_spec["display_cutout"][1])
        .edges("|Y")
        .fillet(head_spec["display_corner_radius_mm"])
        .translate((0, -67, head_center_z))
    )
    neck_spec = head_spec["neck_mount"]
    neck_pedestal = (
        cq.Workplane("XY")
        .box(neck_spec["pedestal_width_mm"], neck_spec["pedestal_depth_mm"], neck_spec["pedestal_height_mm"])
        .edges("|Z")
        .fillet(24)
        .translate((0, 12, head_bottom - 44))
    )
    neck_plate = (
        cq.Workplane("XY")
        .box(
            neck_spec["shoulder_plate_width_mm"],
            neck_spec["shoulder_plate_depth_mm"],
            neck_spec["shoulder_plate_thickness_mm"],
        )
        .edges("|Z")
        .fillet(14)
        .translate((0, 12, head_bottom - 13))
    )
    head_register = (
        cq.Workplane("XY")
        .box(
            neck_spec["head_register_width_mm"],
            neck_spec["head_register_depth_mm"],
            neck_spec["head_register_height_mm"],
        )
        .edges("|Z")
        .fillet(12)
        .translate((0, 12, head_bottom - 4))
    )
    neck_mount = neck_pedestal.union(neck_plate).union(head_register)
    cable_passage = (
        cq.Workplane("XY").circle(neck_spec["cable_passage_mm"] / 2).extrude(100).translate((0, 12, head_bottom - 60))
    )
    neck_mount = neck_mount.cut(cable_passage)
    for x in (-48, 48):
        for y in (-22, 22):
            fastener_clearance = cq.Workplane("XY").circle(3.4).extrude(36).translate((x, 12 + y, head_bottom - 34))
            neck_mount = neck_mount.cut(fastener_clearance)
    for x in (-60, 60):
        dowel_clearance = cq.Workplane("XY").circle(2.05).extrude(18).translate((x, 12, head_bottom - 18))
        neck_mount = neck_mount.cut(dowel_clearance)
    tray_spec = SPEC["electronics_tray"]
    tray = (
        cq.Workplane("XY")
        .box(tray_spec["width"], tray_spec["depth"], 4)
        .faces(">Z")
        .workplane()
        .rect(*tray_spec["pcb_mount_pattern"], forConstruction=True)
        .vertices()
        .hole(3.4)
        .translate((0, 0, 120))
    )
    stabilizer = cq.Workplane("XY").box(48, 48, 18).edges("|Z").fillet(10)
    stabilizers = None
    for x in (-386, 386):
        for y in (-386, 386):
            foot = stabilizer.translate((x, y, 9))
            stabilizers = foot if stabilizers is None else stabilizers.union(foot)
    stowed_stabilizers = None
    for x in (-228, 228):
        for y in (-225, 225):
            foot = stabilizer.translate((x, y, 104))
            stowed_stabilizers = foot if stowed_stabilizers is None else stowed_stabilizers.union(foot)
    tool_dock = cq.Workplane("XY").box(62, 160, 230).edges("|Z").fillet(18).translate((-178, 86, torso_bottom + 115))

    arm_base_z = torso_bottom + 160
    arm_z_offsets = [0, -12, -40, -145, -210, -230, -242, -250]
    right_arm_points = [
        (176, 118, arm_base_z + arm_z_offsets[0]),
        (204, 88, arm_base_z + arm_z_offsets[1]),
        (250, 48, arm_base_z + arm_z_offsets[2]),
        (375, -105, arm_base_z + arm_z_offsets[3]),
        (330, -245, arm_base_z + arm_z_offsets[4]),
        (270, -295, arm_base_z + arm_z_offsets[5]),
        (225, -318, arm_base_z + arm_z_offsets[6]),
        (182, -335, arm_base_z + arm_z_offsets[7]),
    ]
    joint_radii = [41, 34, 29, 27, 21, 17, 15]
    link_sections = [
        (72, 66, 12),
        (66, 60, 12),
        (78, 68, 24),
        (66, 58, 22),
        (44, 42, 16),
        (38, 34, 13),
        (30, 28, 11),
    ]
    joint_axes = [
        cq.Vector(0, 0, 1),
        cq.Vector(0, 1, 0),
        cq.Vector(1, 0, 0),
        cq.Vector(0, 1, 0),
        cq.Vector(1, 0, 0),
        cq.Vector(0, 1, 0),
        cq.Vector(1, 0, 0),
    ]
    left_arm_points = [(-x, y, z) for x, y, z in right_arm_points]

    def make_arm(points: list[tuple[float, float, float]]) -> object:
        arm_solids = []
        for index, (start, end) in enumerate(pairwise(points)):
            start_vector = cq.Vector(*start)
            end_vector = cq.Vector(*end)
            direction = end_vector - start_vector
            unit = direction.normalized()
            width, depth, gap = link_sections[index]
            effective_gap = min(gap, direction.Length * 0.2)
            fairing_length = direction.Length - 2 * effective_gap
            fairing_plane = cq.Plane(origin=start_vector + unit.multiply(effective_gap), normal=unit)
            fairing = (
                cq.Workplane(fairing_plane)
                .box(width, depth, fairing_length, centered=(True, True, False))
                .edges()
                .fillet(min(width, depth, fairing_length) * 0.12)
                .val()
            )
            arm_solids.append(fairing)

        for point, radius, axis in zip(points[:-1], joint_radii, joint_axes, strict=True):
            center = cq.Vector(*point)
            thickness = radius * 1.05
            arm_solids.append(
                cq.Solid.makeCylinder(radius * 0.84, thickness, center - axis.multiply(thickness / 2), axis)
            )
            for offset in (-thickness / 2 - 3, thickness / 2):
                arm_solids.append(cq.Solid.makeCylinder(radius, 3, center + axis.multiply(offset), axis))

        tool_center = cq.Vector(*points[-1])
        tool_axis = cq.Vector(1, 0, 0)
        arm_solids.append(cq.Solid.makeCylinder(14, 24, tool_center - tool_axis.multiply(12), tool_axis))
        return cq.Compound.makeCompound(arm_solids)

    left_seven_axis_arm = make_arm(left_arm_points)
    right_seven_axis_arm = make_arm(right_arm_points)
    navigation_right_arm_points = [
        (x, y, geometry["torso_bottom_stowed_z"] + z)
        for x, y, z in SPEC["manipulator"]["navigation_stowed_right_arm_points_relative_to_torso_bottom_mm"]
    ]
    navigation_left_arm_points = [(-x, y, z) for x, y, z in navigation_right_arm_points]
    navigation_left_arm = make_arm(navigation_left_arm_points)
    navigation_right_arm = make_arm(navigation_right_arm_points)

    parts = {
        "mobile_base": mobile_base,
        "lifting_platform": lifting_platform,
        "utility_torso": torso,
        "left_seven_axis_arm": left_seven_axis_arm,
        "right_seven_axis_arm": right_seven_axis_arm,
        "neck_mount": neck_mount,
        "head_module": head.union(face_lens),
        "electronics_tray": tray,
        "stabilizers": stabilizers,
        "tool_dock": tool_dock,
    }
    part_dir = OUT / "parts"
    part_dir.mkdir(exist_ok=True)
    for stale in part_dir.glob("*.step"):
        stale.unlink()
    for name, shape in parts.items():
        part_path = part_dir / f"{name}.step"
        cq.exporters.export(shape, str(part_path), exportType="STEP")
        normalize_step(part_path)
    cq.exporters.export(torso, str(OUT / "enclosure.step"), exportType="STEP")
    normalize_step(OUT / "enclosure.step")

    assembly = cq.Assembly(name="workbench_home_robot")
    assembly.add(mobile_base, name="mobile_base", color=cq.Color(0.08, 0.10, 0.10))
    assembly.add(lifting_platform, name="lifting_platform", color=cq.Color(0.26, 0.28, 0.27))
    assembly.add(torso, name="utility_torso", color=cq.Color(0.90, 0.89, 0.85))
    assembly.add(neck_mount, name="neck_mount", color=cq.Color(0.24, 0.26, 0.25))
    assembly.add(head, name="head_module", color=cq.Color(0.90, 0.89, 0.85))
    assembly.add(face_lens, name="face_lens", color=cq.Color(0.03, 0.04, 0.04))
    assembly.add(left_seven_axis_arm, name="left_seven_axis_arm", color=cq.Color(0.26, 0.28, 0.27))
    assembly.add(right_seven_axis_arm, name="right_seven_axis_arm", color=cq.Color(0.26, 0.28, 0.27))
    assembly.add(tray, name="electronics_tray", color=cq.Color(0.42, 0.44, 0.42))
    assembly.add(stabilizers, name="stabilizers", color=cq.Color(0.08, 0.09, 0.09))
    assembly.add(tool_dock, name="tool_dock", color=cq.Color(0.22, 0.24, 0.23))
    assembly_path = OUT / "desk_robot_assembly.step"
    assembly.save(str(assembly_path), exportType="STEP")
    normalize_step(assembly_path)

    low_offset = -geometry["travel"]
    navigation = cq.Assembly(name="workbench_home_robot_navigation_low")
    navigation.add(mobile_base, name="mobile_base", color=cq.Color(0.08, 0.10, 0.10))
    navigation.add(navigation_lifting_platform, name="lifting_platform", color=cq.Color(0.26, 0.28, 0.27))
    navigation.add(torso.translate((0, 0, low_offset)), name="utility_torso", color=cq.Color(0.90, 0.89, 0.85))
    navigation.add(neck_mount.translate((0, 0, low_offset)), name="neck_mount", color=cq.Color(0.24, 0.26, 0.25))
    navigation.add(head.translate((0, 0, low_offset)), name="head_module", color=cq.Color(0.90, 0.89, 0.85))
    navigation.add(face_lens.translate((0, 0, low_offset)), name="face_lens", color=cq.Color(0.03, 0.04, 0.04))
    navigation.add(navigation_left_arm, name="left_seven_axis_arm", color=cq.Color(0.26, 0.28, 0.27))
    navigation.add(navigation_right_arm, name="right_seven_axis_arm", color=cq.Color(0.26, 0.28, 0.27))
    navigation.add(tray, name="electronics_tray", color=cq.Color(0.42, 0.44, 0.42))
    navigation.add(stowed_stabilizers, name="stabilizers", color=cq.Color(0.08, 0.09, 0.09))
    navigation.add(tool_dock.translate((0, 0, low_offset)), name="tool_dock", color=cq.Color(0.22, 0.24, 0.23))
    navigation_path = OUT / "desk_robot_navigation_low.step"
    navigation.save(str(navigation_path), exportType="STEP")
    normalize_step(navigation_path)

    exploded = cq.Assembly(name="workbench_home_robot_exploded")
    exploded.add(mobile_base.translate((0, 0, -100)), name="mobile_base")
    exploded.add(stabilizers.translate((0, 0, -150)), name="stabilizers")
    exploded.add(tray.translate((0, 0, 80)), name="electronics_tray")
    exploded.add(lifting_platform.translate((0, 0, 80)), name="lifting_platform")
    exploded.add(torso.translate((0, 0, 180)), name="utility_torso")
    exploded.add(neck_mount.translate((0, 0, 240)), name="neck_mount")
    exploded.add(head.translate((0, 0, 300)), name="head_module")
    exploded.add(left_seven_axis_arm.translate((-180, 0, 120)), name="left_seven_axis_arm")
    exploded.add(right_seven_axis_arm.translate((180, 0, 120)), name="right_seven_axis_arm")
    exploded.add(tool_dock.translate((-120, 0, 120)), name="tool_dock")
    exploded_path = OUT / "desk_robot_exploded.step"
    exploded.save(str(exploded_path), exportType="STEP")
    normalize_step(exploded_path)
    return True


def measure_step_bounds(path: Path) -> dict[str, float] | None:
    """Measure the generated assembly so the exported STEP is checked, not assumed."""
    try:
        import cadquery as cq
    except ImportError:
        return None
    if not path.exists():
        return None
    imported = cq.importers.importStep(str(path))
    bounds = imported.val().BoundingBox()
    return {
        "xmin_mm": round(bounds.xmin, 1),
        "xmax_mm": round(bounds.xmax, 1),
        "ymin_mm": round(bounds.ymin, 1),
        "ymax_mm": round(bounds.ymax, 1),
        "zmin_mm": round(bounds.zmin, 1),
        "zmax_mm": round(bounds.zmax, 1),
    }


def write_engineering_drawings(report: dict[str, object]) -> None:
    drawings = OUT / "drawings"
    drawings.mkdir(exist_ok=True)
    width = SPEC["enclosure"]["width"]
    height = SPEC["enclosure"]["height"]
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="760" viewBox="0 0 1200 760">
<style>text{{font-family:Arial,sans-serif;fill:#17201f}} .shell{{fill:#ecece7;stroke:#17201f;stroke-width:3}} .frame{{fill:#404745;stroke:#17201f;stroke-width:3}} .link-outline{{fill:none;stroke:#17201f;stroke-width:34;stroke-linecap:round;stroke-linejoin:round}} .link-shell{{fill:none;stroke:#ecece7;stroke-width:26;stroke-linecap:round;stroke-linejoin:round}} .joint{{fill:#202625;stroke:#7a807d;stroke-width:5}} .dim{{stroke:#287b70;stroke-width:2;marker-start:url(#a);marker-end:url(#a)}} .note{{font-size:17px}}</style>
<defs><marker id="a" markerWidth="8" markerHeight="8" refX="4" refY="4" orient="auto"><path d="M8,0 L0,4 L8,8" fill="none" stroke="#287b70"/></marker></defs>
<text x="36" y="44" font-size="28" font-weight="bold">Workbench Home Robot - General Arrangement - REV D</text>
<text x="36" y="72" class="note">Dual seven-axis arms + four steer-drive modules + 350 mm braked lift; concept geometry; mm</text>
<rect class="frame" x="115" y="590" width="270" height="72" rx="24"/><rect class="frame" x="188" y="452" width="124" height="150" rx="16"/>
<circle class="joint" cx="155" cy="660" r="28"/><circle class="frame" cx="155" cy="660" r="10"/><circle class="joint" cx="345" cy="660" r="28"/><circle class="frame" cx="345" cy="660" r="10"/>
<path class="shell" d="M160 218 Q160 190 190 188 L315 188 Q342 190 342 220 L328 456 L174 456 Z"/>
<rect class="frame" x="230" y="167" width="44" height="34" rx="12"/><rect class="shell" x="185" y="100" width="135" height="70" rx="28"/><rect x="195" y="110" width="115" height="50" rx="14" fill="#101716"/><circle cx="237" cy="132" r="4" fill="#72c9b4"/><circle cx="267" cy="132" r="4" fill="#72c9b4"/>
<polyline class="link-outline" points="342,318 382,306 420,350 458,405 493,444 525,466 553,480"/><polyline class="link-shell" points="342,318 382,306 420,350 458,405 493,444 525,466 553,480"/>
<g>{"".join(f'<circle class="joint" cx="{x}" cy="{y}" r="{r}"/>' for x, y, r in [(342, 318, 22), (382, 306, 20), (420, 350, 18), (458, 405, 16), (493, 444, 14), (525, 466, 12), (553, 480, 10)])}</g>
<polyline class="link-outline" points="163,318 126,306 94,350 72,405 58,444 48,466 40,480"/><polyline class="link-shell" points="163,318 126,306 94,350 72,405 58,444 48,466 40,480"/>
<g>{"".join(f'<circle class="joint" cx="{x}" cy="{y}" r="{r}"/>' for x, y, r in [(163, 318, 22), (126, 306, 20), (94, 350, 18), (72, 405, 16), (58, 444, 14), (48, 466, 12), (40, 480, 10)])}</g>
<text x="187" y="224" class="note">full-width rounded face</text><text x="187" y="246" class="note">keyed bolted neck mount</text><text x="167" y="276" class="note">recessed 3-axis shoulders</text><text x="352" y="522" class="note">faired links</text><text x="352" y="544" class="note">readable joint cartridges</text>
<line class="dim" x1="90" y1="112" x2="90" y2="662"/><text x="50" y="410" class="note" transform="rotate(-90 50 410)">max {height}</text>
<line class="dim" x1="115" y1="700" x2="385" y2="700"/><text x="222" y="725" class="note">base {width}</text>
<text x="630" y="130" font-size="21" font-weight="bold">LIFT STATES</text>
<rect class="frame" x="650" y="520" width="190" height="70" rx="22"/><circle class="joint" cx="685" cy="588" r="22"/><circle class="joint" cx="805" cy="588" r="22"/><rect class="frame" x="710" y="385" width="70" height="145" rx="14"/><rect class="shell" x="680" y="245" width="130" height="145" rx="30"/>
<rect class="frame" x="930" y="520" width="190" height="70" rx="22"/><circle class="joint" cx="965" cy="588" r="22"/><circle class="joint" cx="1085" cy="588" r="22"/><rect class="frame" x="990" y="260" width="70" height="270" rx="14"/><rect class="shell" x="960" y="120" width="130" height="145" rx="30"/>
<line class="dim" x1="875" y1="260" x2="875" y2="385"/><text x="892" y="330" class="note">travel {SPEC["lifting_platform"]["travel"]}</text>
<text x="675" y="625" class="note">LOW / DRIVE</text><text x="958" y="625" class="note">RAISED / WORK</text>
<text x="630" y="680" class="note">CG Z={report["center_of_gravity_mm"][2]} · drive tip={report["drive_footprint_tip_angle_deg"]} deg · stabilized tip={report["stabilized_tip_angle_deg"]} deg</text>
<text x="630" y="712" class="note">Holonomic self-motion in navigation; manipulation requires wheel brakes + deployed support feet.</text>
</svg>"""
    (drawings / "general-arrangement.svg").write_text(svg, encoding="utf-8", newline="\n")
    thermal = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="420" viewBox="0 0 1100 420">
<style>text{{font-family:Arial,sans-serif}}.box{{fill:#eef3f6;stroke:#222;stroke-width:2}}.flow{{stroke:#00897b;stroke-width:12;fill:none;marker-end:url(#arrow)}}</style>
<defs><marker id="arrow" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto"><path d="M0,0 L12,6 L0,12 z" fill="#00897b"/></marker></defs>
<text x="30" y="45" font-size="28" font-weight="bold">Thermal Airflow and Conduction Path</text>
<rect class="box" x="60" y="145" width="180" height="120"/><text x="86" y="210" font-size="22">{SPEC["ventilation"]["inlet_area_mm2"]} mm2 inlet</text>
<rect class="box" x="430" y="120" width="220" height="170"/><text x="485" y="190" font-size="22">Jetson 40 W</text><text x="470" y="225" font-size="18">pad -> chassis</text>
<rect class="box" x="820" y="145" width="200" height="120"/><text x="840" y="210" font-size="22">{SPEC["ventilation"]["outlet_area_mm2"]} mm2 outlet</text>
<path class="flow" d="M240 205 C330 205 345 185 430 185"/><path class="flow" d="M650 185 C735 185 745 205 820 205"/>
<text x="310" y="350" font-size="20">{SPEC["ventilation"]["fan_mm"]} mm fan; electronics airflow isolated from food-contact tool zone</text>
</svg>"""
    (drawings / "thermal-flow.svg").write_text(thermal, encoding="utf-8", newline="\n")
    fea = {
        "method": "energy and equivalent-static screening; nonlinear FEA and physical drop remain required",
        "mass_model_revision": report["mass_model"]["revision"],
        "mass_model_sha256": report["mass_model"]["mass_model_sha256"],
        "mass_model_status": report["mass_model"]["status"],
        "drop_force_n_at_design_g": round(report["mass_kg"] * SPEC["impact"]["design_deceleration_g"] * 9.80665, 1),
        "drop_energy_j": report["drop_energy_j"],
        "effective_stroke_mm": SPEC["impact"]["effective_absorber_stroke_mm"],
        "estimated_bumper_contact_area_mm2": 18000,
        "estimated_average_compressive_stress_mpa": round(
            report["mass_kg"] * SPEC["impact"]["design_deceleration_g"] * 9.80665 / 18000, 3
        ),
        "acceptance": {
            "peak_deceleration_g": SPEC["impact"]["design_deceleration_g"],
            "no_battery_contact": True,
            "no_sharp_shell_fracture": True,
        },
    }
    (OUT / "drop-screening.json").write_text(json.dumps(fea, indent=2) + "\n", encoding="utf-8")
    sequence = [
        {"step": 10, "part": "mobile_base", "fastener": "4x steer-drive datum + absolute encoder zero + brake check"},
        {"step": 20, "part": "stabilizers", "fastener": "4x captive M6 + deployed-foot witness"},
        {"step": 30, "part": "lifting_platform", "fastener": "dual screw synchronization + lock pins"},
        {"step": 40, "part": "electronics_tray", "fastener": "4x M3x8 @ 0.55 Nm"},
        {"step": 50, "part": "utility_torso", "fastener": "8x M4 captive @ 1.2 Nm"},
        {"step": 60, "part": "left_seven_axis_arm", "fastener": "left shoulder datum + torque witness"},
        {"step": 70, "part": "right_seven_axis_arm", "fastener": "right shoulder datum + torque witness"},
        {"step": 80, "part": "neck_mount", "fastener": "4x M6 captive + 2x dowel pins; loom through 32 mm passage"},
        {"step": 90, "part": "head_module", "fastener": "lift onto keyed register; connect head harness"},
        {"step": 100, "part": "tool_dock", "fastener": "3x M4 captive @ 0.8 Nm"},
    ]
    (OUT / "assembly-sequence.json").write_text(json.dumps(sequence, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    enclosure = SPEC["enclosure"]
    report = analyse()
    if not all(report["checks"].values()):
        raise SystemExit(f"mechanical design check failed: {report['checks']}")
    if not all(report["scad_parameter_checks"].values()):
        raise SystemExit(
            "OpenSCAD parameters drifted from the design specification: "
            f"{report['scad_parameters']['errors'] or report['scad_parameter_checks']}"
        )
    step_path = OUT / "enclosure.step"
    cad_exported = export_cad_package()
    if not cad_exported and not export_solid_step(step_path) and not step_path.exists():
        step_path.write_text(step_box(enclosure["width"], enclosure["depth"], enclosure["height"]), encoding="ascii")
    assembly_bounds = measure_step_bounds(OUT / "desk_robot_assembly.step")
    navigation_bounds = measure_step_bounds(OUT / "desk_robot_navigation_low.step")
    report["generated_geometry"] = {
        "status": "MEASURED" if assembly_bounds and navigation_bounds else "NOT_MEASURED",
        "assembly_bounds_mm": assembly_bounds,
        "navigation_bounds_mm": navigation_bounds,
    }
    if assembly_bounds and navigation_bounds:
        target_height = float(SPEC["enclosure"]["height"])
        stowed_height = float(SPEC["enclosure"]["stowed_height"])
        tolerance = float(SPEC["coordinate_system"]["height_tolerance_mm"])
        report["geometry_checks"]["generated_step_top_matches_target"] = (
            abs(assembly_bounds["zmax_mm"] - target_height) <= tolerance
        )
        report["geometry_checks"]["navigation_step_top_matches_stowed_height"] = (
            abs(navigation_bounds["zmax_mm"] - stowed_height) <= tolerance
        )
        report["geometry_checks"]["navigation_footprint_matches_base"] = (
            navigation_bounds["xmin_mm"] >= -float(SPEC["chassis"]["width"]) / 2 - tolerance
            and navigation_bounds["xmax_mm"] <= float(SPEC["chassis"]["width"]) / 2 + tolerance
            and navigation_bounds["ymin_mm"] >= -float(SPEC["chassis"]["depth"]) / 2 - tolerance
            and navigation_bounds["ymax_mm"] <= float(SPEC["chassis"]["depth"]) / 2 + tolerance
        )
        report["geometry_checks"]["stabilized_footprint_matches_support_polygon"] = (
            abs(assembly_bounds["xmin_mm"] + float(SPEC["chassis"]["stabilized_support_width"]) / 2) <= tolerance
            and abs(assembly_bounds["xmax_mm"] - float(SPEC["chassis"]["stabilized_support_width"]) / 2) <= tolerance
            and abs(assembly_bounds["ymin_mm"] + float(SPEC["chassis"]["stabilized_support_depth"]) / 2) <= tolerance
            and abs(assembly_bounds["ymax_mm"] - float(SPEC["chassis"]["stabilized_support_depth"]) / 2) <= tolerance
        )
        if not all(report["geometry_checks"].values()):
            raise SystemExit(
                "generated STEP envelope mismatch: "
                f"raised={assembly_bounds['zmax_mm']} mm target={target_height} mm, "
                f"stowed={navigation_bounds['zmax_mm']} mm target={stowed_height} mm"
            )
    (OUT / "analysis.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_engineering_drawings(report)
    rows = [
        ["ME-C01", "Holonomic base frame and perimeter bumper", "5052-H32 aluminium + TPU", 1],
        ["ME-D01A", "Independent steer-drive wheel module", "6061-T6 aluminium + non-marking PU", 4],
        ["ME-C02", "Dual-screw lifting platform", "6061-T6 aluminium + steel screws", 1],
        ["ME-C03", "Utility torso and parcel bay", "mineral PC-ABS + recycled PET", 1],
        ["ME-D04", "Seven-axis arm joint set", "bead-blasted anodized aluminium", 2],
        ["ME-C05", "Keyed neck mount and head flange", "6061-T6 aluminium + steel inserts", 1],
        ["ME-C06", "Smoked glass head module", "chemically strengthened glass + PC-ABS", 1],
        ["ME-C07", "Deployable stabilizer feet", "steel core + charcoal TPU", 4],
        ["ME-C08", "Tool dock and quick-change datum", "6061-T6 aluminium + PEEK", 1],
        ["ISO4762-M4", "Captive socket screw", "A2-70 stainless", 28],
        ["ISO4762-M6", "Captive neck mount socket screw", "A2-70 stainless", 4],
        ["DOWEL-04", "Head register dowel pin", "hardened stainless", 2],
        ["LIFT-LOCK-01", "Normally-closed brake and lock pin set", "steel + spring", 2],
    ]
    with (OUT / "bom.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["part_number", "description", "material", "quantity", "mass_model_revision", "mass_model_sha256"]
        )
        writer.writerows(
            [[*row, report["mass_model"]["revision"], report["mass_model"]["mass_model_sha256"]] for row in rows]
        )
    (OUT / "bom-manifest.json").write_text(
        json.dumps(
            {
                "mass_model_revision": report["mass_model"]["revision"],
                "mass_model_sha256": report["mass_model"]["mass_model_sha256"],
                "status": report["status"],
                "source": "hardware/mechanical/generated/analysis.json",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
