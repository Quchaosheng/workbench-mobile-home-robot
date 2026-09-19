"""Tests for the Issue #300 fail-closed Scenario Registry.

The registry is a refusal boundary, so the rejections are the specification.
Every malformed, unsafe, duplicated or incompatible manifest is asserted here,
and every accepted case is asserted on a manifest that is otherwise valid so a
registry that rejects everything cannot pass this file.

The tests also pin the three properties the Issue names explicitly: loading is
deterministic and order independent, failures use stable diagnostic codes and
fail closed, and the registry imports no ROS, Gazebo or hardware module.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts"), str(ROOT / "libs/kernel"), str(ROOT / "libs/contracts")]

from workbench.kernel.scenario_registry import (
    ScenarioEntry,
    ScenarioRegistry,
    ScenarioRegistryError,
    load_registry,
)

VERIFIER = "services/world_model/workbench_world_model/verifier.py::verify_object_in_tray"


def valid_manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "scenario_id": "sample-scenario",
        "scenario_version": "1.0",
        "goal": "Do the bounded sample thing",
        "semantic_actions": ["observe", "grasp"],
        "required_adapters": ["motion", "perception"],
        "evidence_policy": "A fresh observation and an ActionResult are required.",
        "verifier": VERIFIER,
        "evidence_status": "SCRIPTED_FIXTURE",
        "non_goals": ["does not claim physical validation"],
        "recovery_policy": {"allowed": ["re_observe"], "max_attempts": 2},
    }
    manifest.update(overrides)
    return manifest


def write_manifests(root: Path, manifests: dict[str, dict[str, Any]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, manifest in manifests.items():
        (root / f"{name}.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


@pytest.fixture
def registry_root(tmp_path: Path) -> Path:
    return tmp_path / "registry"


def load(root: Path, **kwargs: Any) -> ScenarioRegistry:
    return load_registry(root, repo_root=ROOT, **kwargs)


# --- accepted cases ---------------------------------------------------------


def test_valid_manifest_registers(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest()})
    registry = load(registry_root)
    assert len(registry) == 1
    entry = registry.entries[0]
    assert entry.identity == "sample-scenario@1.0"
    assert entry.executable is True
    assert isinstance(entry, ScenarioEntry)


def test_committed_registry_root_loads():
    """The registry-native root committed with this Issue must be valid."""

    registry = load(ROOT / "sim" / "registry")
    assert len(registry) >= 1
    assert registry.resolve("pick-place-red-block", "1.0").executable is True


def test_scripted_fixture_is_not_release_eligible(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(evidence_status="SCRIPTED_FIXTURE")})
    entry = load(registry_root).entries[0]
    assert entry.release_eligible is False


def test_physical_status_is_release_eligible(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(evidence_status="PHYSICAL")})
    assert load(registry_root).entries[0].release_eligible is True


def test_entries_are_immutable(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest()})
    entry = load(registry_root).entries[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.scenario_id = "changed"  # type: ignore[misc]


def test_catalog_is_sorted_and_deterministic(registry_root: Path):
    write_manifests(
        registry_root,
        {
            "zeta": valid_manifest(scenario_id="zeta"),
            "alpha": valid_manifest(scenario_id="alpha"),
            "mid": valid_manifest(scenario_id="mid"),
        },
    )
    identities = [entry.identity for entry in load(registry_root)]
    assert identities == ["alpha@1.0", "mid@1.0", "zeta@1.0"]


def test_loading_is_order_independent(tmp_path: Path):
    """Writing the same files in a different order yields the same catalog.

    The reported ``path`` includes the temp directory, so it is normalized to a
    file name before the comparison; every other field is compared verbatim.
    """

    manifests = {"b": valid_manifest(scenario_id="b"), "a": valid_manifest(scenario_id="a")}
    first = load(write_manifests(tmp_path / "one", dict(manifests)))
    second = load(write_manifests(tmp_path / "two", {key: manifests[key] for key in reversed(list(manifests))}))

    def normalized(registry: ScenarioRegistry) -> list[dict[str, Any]]:
        return [{**entry.as_dict(), "path": Path(entry.path).name} for entry in registry]

    assert normalized(first) == normalized(second)
    assert [entry.identity for entry in first] == ["a@1.0", "b@1.0"]


def test_catalog_is_bounded_and_json_serializable(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest()})
    payload = load(registry_root).catalog()
    assert json.loads(json.dumps(payload)) == payload


# --- fail-closed rejections -------------------------------------------------


@pytest.mark.parametrize(
    "field", ["scenario_id", "scenario_version", "goal", "semantic_actions", "required_adapters", "verifier"]
)
def test_missing_required_field_fails_closed(registry_root: Path, field: str):
    manifest = valid_manifest()
    del manifest[field]
    write_manifests(registry_root, {"sample": manifest})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_MISSING_FIELD"


def test_unknown_field_fails_closed(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(future_field="x")})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_UNKNOWN_FIELD"


@pytest.mark.parametrize(
    "field", ["joint_positions", "torque", "can_frame", "controller_goal", "trajectory", "emergency_stop"]
)
def test_raw_control_field_fails_closed(registry_root: Path, field: str):
    write_manifests(registry_root, {"sample": valid_manifest(**{field: [0.1]})})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_FORBIDDEN_FIELD"


def test_policy_bypass_field_fails_closed(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(bypass="true")})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_FORBIDDEN_FIELD"


def test_second_verifier_implementation_fails_closed(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(verifier_impl="inline")})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_FORBIDDEN_FIELD"


@pytest.mark.parametrize("value", ["../escape", "UPPER", "a/b", ""])
def test_path_traversal_and_unstable_id_fail_closed(registry_root: Path, value: str):
    write_manifests(registry_root, {"sample": valid_manifest(scenario_id=value)})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_INVALID_ID"


@pytest.mark.parametrize("value", ["1", "1.0.0", "^1.0"])
def test_inexact_version_fails_closed(registry_root: Path, value: str):
    write_manifests(registry_root, {"sample": valid_manifest(scenario_version=value)})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_INVALID_VERSION"


def test_unknown_semantic_action_fails_closed(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(semantic_actions=["observe", "levitate"])})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_INVALID_ACTION"


def test_unknown_adapter_fails_closed(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(required_adapters=["telepathy"])})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_INVALID_ADAPTER"


def test_unsafe_verifier_path_fails_closed(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(verifier="../../../etc/passwd.py::verify")})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_INVALID_VERIFIER"


def test_nonexistent_verifier_module_fails_closed(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(verifier="nope/missing.py::verify")})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_INVALID_VERIFIER"


def test_out_of_vocabulary_evidence_status_fails_closed(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(evidence_status="physical")})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_INVALID_EVIDENCE_STATUS"


def test_oversized_value_fails_closed(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(goal="x" * 4096)})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_OVERSIZED_VALUE"


def test_duplicate_identity_fails_closed(registry_root: Path):
    """Two files claiming one identity is an error, not a last-wins overwrite."""

    write_manifests(registry_root, {"one": valid_manifest(), "two": valid_manifest()})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_DUPLICATE_ID"


def test_distinct_versions_are_not_duplicates(registry_root: Path):
    write_manifests(
        registry_root,
        {"one": valid_manifest(scenario_version="1.0"), "two": valid_manifest(scenario_version="2.0")},
    )
    assert len(load(registry_root)) == 2


def test_malformed_json_fails_closed(registry_root: Path):
    registry_root.mkdir(parents=True, exist_ok=True)
    (registry_root / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_REGISTRY_MALFORMED_JSON"


def test_oversized_manifest_fails_closed(registry_root: Path):
    registry_root.mkdir(parents=True, exist_ok=True)
    (registry_root / "huge.json").write_text("x" * (70 * 1024), encoding="utf-8")
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_REGISTRY_MANIFEST_TOO_LARGE"


def test_empty_root_fails_closed(registry_root: Path):
    registry_root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root)
    assert error.value.code == "SCENARIO_REGISTRY_EMPTY"


def test_missing_root_fails_closed(tmp_path: Path):
    with pytest.raises(ScenarioRegistryError) as error:
        load(tmp_path / "absent")
    assert error.value.code == "SCENARIO_REGISTRY_ROOT_MISSING"


def test_one_invalid_manifest_rejects_the_whole_catalog(registry_root: Path):
    """There is no partial catalog: a single bad file fails the load."""

    write_manifests(
        registry_root,
        {"good": valid_manifest(scenario_id="good"), "bad": valid_manifest(scenario_id="BAD")},
    )
    with pytest.raises(ScenarioRegistryError):
        load(registry_root)


# --- resolution and version compatibility -----------------------------------


def test_resolve_requires_an_exact_version(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest()})
    registry = load(registry_root)
    assert registry.resolve("sample-scenario", "1.0").identity == "sample-scenario@1.0"
    with pytest.raises(ScenarioRegistryError) as error:
        registry.resolve("sample-scenario", "2.0")
    assert error.value.code == "SCENARIO_REGISTRY_VERSION_MISMATCH"


def test_unknown_id_is_reported(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest()})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root).resolve("nope")
    assert error.value.code == "SCENARIO_REGISTRY_UNKNOWN_ID"


def test_ambiguous_id_without_version_is_refused(registry_root: Path):
    """Silently choosing the newest version is the implicit upgrade we refuse."""

    write_manifests(
        registry_root,
        {"one": valid_manifest(scenario_version="1.0"), "two": valid_manifest(scenario_version="2.0")},
    )
    registry = load(registry_root)
    with pytest.raises(ScenarioRegistryError) as error:
        registry.resolve("sample-scenario")
    assert error.value.code == "SCENARIO_REGISTRY_AMBIGUOUS_ID"


def test_no_implicit_upgrade_from_a_newer_version(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(scenario_version="2.0")})
    registry = load(registry_root)
    with pytest.raises(ScenarioRegistryError):
        registry.resolve("sample-scenario", "1.0")


# --- non-executable manifests -----------------------------------------------


def test_pending_action_is_not_executable_rather_than_invalid(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(semantic_actions=["observe", "clean_workspace"])})
    entry = load(registry_root).entries[0]
    assert entry.executable is False
    assert entry.notices


def test_require_executable_rejects_a_pending_action(registry_root: Path):
    write_manifests(registry_root, {"sample": valid_manifest(semantic_actions=["observe", "clean_workspace"])})
    with pytest.raises(ScenarioRegistryError) as error:
        load(registry_root, require_executable=True)
    assert error.value.code == "SCENARIO_PENDING_ACTION"


# --- boundary: no runtime, no side effects ----------------------------------


def test_registry_imports_no_runtime_or_transport_module():
    """The registry must stay loadable without ROS, Gazebo, MoveIt or hardware."""

    registry_source = (ROOT / "libs/kernel/workbench/kernel/scenario_registry.py").read_text(encoding="utf-8")
    for forbidden in ("rclpy", "gazebo", "moveit", "socket", "subprocess", "http.client", "requests"):
        assert forbidden not in registry_source, f"registry must not reference {forbidden}"


def test_registry_module_imports_without_optional_runtime(monkeypatch: pytest.MonkeyPatch):
    """A fresh interpreter that blocks the runtime packages still loads the registry."""

    kernel_path = ROOT / "libs/kernel"
    contracts_path = ROOT / "libs/contracts"
    blocker = f"""
import sys

BLOCKED = {{"rclpy", "gazebo", "moveit", "ros2", "tf2_ros", "sensor_msgs"}}


class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked: " + name)
        return None


sys.meta_path.insert(0, _Block())
sys.path[:0] = [{str(kernel_path)!r}, {str(contracts_path)!r}]
from workbench.kernel.scenario_registry import ScenarioRegistryError, load_registry

print("registry import ok")
"""

    result = subprocess.run([sys.executable, "-c", blocker], capture_output=True, text=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr
    assert "registry import ok" in result.stdout


def test_registry_does_not_write_anything(registry_root: Path, tmp_path: Path):
    """Loading a registry must not create a run, an event or a database."""

    write_manifests(registry_root, {"sample": valid_manifest()})
    before = {path for path in ROOT.rglob("*") if path.is_file()}
    load(registry_root)
    after = {path for path in ROOT.rglob("*") if path.is_file()}
    assert after == before


# --- CLI surface ------------------------------------------------------------


def run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools/scripts/sim_cli.py"), *argv],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def test_cli_registry_list_is_deterministic():
    first = run_cli("registry-list")
    second = run_cli("registry-list")
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert "pick-place-red-block@1.0" in first.stdout


def test_cli_registry_list_json_is_bounded():
    result = run_cli("registry-list", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["scenario_count"] == len(payload["scenarios"])


def test_cli_describe_reports_identity_and_eligibility():
    result = run_cli("describe", "pick-place-red-block@1.0")
    assert result.returncode == 0, result.stderr
    assert "pick-place-red-block@1.0" in result.stdout
    assert "release_eligible: false" in result.stdout


def test_cli_describe_unknown_id_is_not_executed():
    result = run_cli("describe", "no-such-scenario")
    assert result.returncode == 2
    assert "SCENARIO_REGISTRY_UNKNOWN_ID" in result.stderr


def test_cli_describe_version_mismatch_is_not_executed():
    result = run_cli("describe", "pick-place-red-block@9.9")
    assert result.returncode == 2
    assert "SCENARIO_REGISTRY_VERSION_MISMATCH" in result.stderr


def test_cli_describe_rejects_a_malformed_identity():
    result = run_cli("describe", "pick-place-red-block@")
    assert result.returncode == 2
    assert "invalid scenario identity" in result.stderr


def test_cli_list_is_unchanged_by_the_registry():
    """The pre-existing regression listing must keep working."""

    result = run_cli("list")
    assert result.returncode == 0, result.stderr
    assert "normal-001" in result.stdout
