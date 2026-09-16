from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAD_PATH = ROOT / "hardware/mechanical/cad/desk_robot.scad"
MANIFEST_PATH = ROOT / "hardware/mechanical/cad/scad-parameter-manifest.json"


def load_generator():
    path = ROOT / "hardware/mechanical/tools/generate_artifacts.py"
    spec = importlib.util.spec_from_file_location("mechanical_issue_328_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_current_scad_parameters_match_the_design_spec() -> None:
    generator = load_generator()
    result = generator.validate_scad_parameters()
    assert result["pass"] is True
    assert result["errors"] == []
    assert all(result["checks"].values())


def test_every_scad_constant_is_classified_and_shared_values_match() -> None:
    generator = load_generator()
    result = generator.validate_scad_parameters()
    raw = generator.read_scad_constants(SCAD_PATH)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    classified = [
        entry["scad"]
        for entry in manifest["shared_parameters"] + manifest["derived_parameters"] + manifest["visual_only_parameters"]
    ]
    assert sorted(classified) == sorted(raw)
    assert set(result["parameters"]) == {entry["scad"] for entry in manifest["shared_parameters"]}
    for name, comparison in result["parameters"].items():
        assert comparison["pass"] is True, name
        assert comparison["scad_value"] == comparison["spec_value"]


def test_derived_values_follow_their_declared_expressions() -> None:
    generator = load_generator()
    resolved = generator.validate_scad_parameters()["resolved_constants"]
    spec = json.loads((ROOT / "hardware/mechanical/design-spec.json").read_text(encoding="utf-8"))
    # The raised pose is derived, and it must agree with the specification datums.
    assert resolved["TORSO_BOTTOM"] == resolved["TORSO_BOTTOM_STOWED"] + resolved["LIFT_EXTENSION"]
    assert resolved["HEAD_TOP"] == resolved["HEAD_TOP_STOWED"] + resolved["LIFT_EXTENSION"]
    assert resolved["BASE_TOP"] == resolved["GROUND"] + resolved["BASE_H"]
    assert resolved["HEAD_TOP"] == float(spec["enclosure"]["height"])


def test_a_changed_spec_dimension_breaks_the_shared_parameter_check() -> None:
    generator = load_generator()
    spec = json.loads((ROOT / "hardware/mechanical/design-spec.json").read_text(encoding="utf-8"))
    drifted = copy.deepcopy(spec)
    drifted["torso"]["height"] = float(drifted["torso"]["height"]) + 25
    result = generator.validate_scad_parameters(drifted)
    assert result["pass"] is False
    assert result["checks"]["shared_parameters_match_design_spec"] is False
    assert result["parameters"]["TORSO_H"]["pass"] is False
    assert result["parameters"]["TORSO_H"]["spec_value"] == spec["torso"]["height"] + 25
    assert any("TORSO_H" in error for error in result["errors"])


def test_a_missing_or_unit_mismatched_spec_path_fails_closed() -> None:
    generator = load_generator()
    spec = json.loads((ROOT / "hardware/mechanical/design-spec.json").read_text(encoding="utf-8"))
    for path in ("torso.height_mm", "torso", "torso.height.value"):
        with_unit = copy.deepcopy(spec)
        with_unit["torso"]["width"] = "420 mm"
        try:
            generator.resolve_spec_path(with_unit, "torso.width")
        except ValueError as exc:
            assert "not a finite number" in str(exc)
        else:
            raise AssertionError("a non-numeric design spec value must fail closed")
        try:
            generator.resolve_spec_path(spec, path)
        except ValueError as exc:
            assert path in str(exc)
        else:
            raise AssertionError(f"missing spec path {path} must fail closed")


def test_negative_and_non_finite_scad_constants_fail_closed() -> None:
    generator = load_generator()
    resolved = {"OK": 1.0}
    for expression in ("-5", "1/0", "(1/0)*2"):
        try:
            value = generator._evaluate_scad_expression(expression, resolved)
        except (ValueError, ZeroDivisionError):
            continue
        assert not (isinstance(value, float) and not generator.math.isfinite(value)), expression


def test_scad_expressions_outside_the_arithmetic_grammar_are_rejected() -> None:
    generator = load_generator()
    for expression in ("__import__('os')", "open('x')", "[1,2]", "{'a': 1}", "lambda: 1", "UNKNOWN_NAME"):
        try:
            generator._evaluate_scad_expression(expression, {"KNOWN": 1.0})
        except ValueError:
            continue
        raise AssertionError(f"unsupported OpenSCAD expression accepted: {expression}")


def test_manifest_drift_is_detected_without_editing_the_design_dimensions(tmp_path) -> None:
    generator = load_generator()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    # An approved derived dimension must be recorded as derived, not silently
    # promoted to a shared spec-backed value.
    promoted = copy.deepcopy(manifest)
    promoted["shared_parameters"].append({"scad": "HEAD_BOTTOM", "spec_path": "head.height"})
    promoted["derived_parameters"] = [
        entry for entry in promoted["derived_parameters"] if entry["scad"] != "HEAD_BOTTOM"
    ]
    patched = tmp_path / "scad-parameter-manifest.json"
    patched.write_text(json.dumps(promoted), encoding="utf-8")
    original = generator.SCAD_MANIFEST_PATH
    try:
        generator.SCAD_MANIFEST_PATH = patched
        result = generator.validate_scad_parameters()
    finally:
        generator.SCAD_MANIFEST_PATH = original
    assert result["pass"] is False
    assert result["parameters"]["HEAD_BOTTOM"]["pass"] is False


def test_an_unclassified_scad_constant_fails_closed(tmp_path) -> None:
    generator = load_generator()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    dropped = copy.deepcopy(manifest)
    dropped["visual_only_parameters"] = []
    patched = tmp_path / "scad-parameter-manifest.json"
    patched.write_text(json.dumps(dropped), encoding="utf-8")
    original = generator.SCAD_MANIFEST_PATH
    try:
        generator.SCAD_MANIFEST_PATH = patched
        result = generator.validate_scad_parameters()
    finally:
        generator.SCAD_MANIFEST_PATH = original
    assert result["pass"] is False
    assert result["checks"]["scad_constants_are_classified_once"] is False
    assert any("unclassified" in error for error in result["errors"])


def test_a_manifest_without_a_reason_or_owner_fails_closed(tmp_path) -> None:
    generator = load_generator()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    undocumented = copy.deepcopy(manifest)
    undocumented["visual_only_parameters"][0].pop("owner")
    patched = tmp_path / "scad-parameter-manifest.json"
    patched.write_text(json.dumps(undocumented), encoding="utf-8")
    original = generator.SCAD_MANIFEST_PATH
    try:
        generator.SCAD_MANIFEST_PATH = patched
        result = generator.validate_scad_parameters()
    finally:
        generator.SCAD_MANIFEST_PATH = original
    assert result["pass"] is False
    assert result["checks"]["scad_derived_and_visual_parameters_are_documented"] is False


def test_generated_analysis_records_scad_evidence_and_status() -> None:
    report = json.loads((ROOT / "hardware/mechanical/generated/analysis.json").read_text(encoding="utf-8"))
    assert report["status"] == "CONCEPT_PHYSICAL_VALIDATION_REQUIRED"
    assert all(report["scad_parameter_checks"].values())
    hashes = report["scad_parameters"]["source_hashes"]
    assert len(hashes["desk_robot_scad_sha256"]) == 64
    assert len(hashes["scad_parameter_manifest_sha256"]) == 64
    assert report["scad_parameters"]["visual_only_parameters"] == ["STABILIZERS_DEPLOYED"]
