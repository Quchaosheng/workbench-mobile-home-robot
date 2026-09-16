from collections import Counter

from _jsonio import load_json
from _paths import ROOT, enable_local_packages

enable_local_packages()

from scenario_tools import (
    FROZEN_DISTRIBUTION,
    P2_FAULT_TYPES,
    P2_SCENE_VARIANTS,
    SCENE_TASKS,
    materialize_scenario,
    validate_simulation_manifest,
)


def main() -> int:
    scenario_files = sorted((ROOT / "sim" / "scenarios").rglob("*.json"))
    if not scenario_files:
        raise RuntimeError("no scenario manifests found")
    manifests = []
    for path in scenario_files:
        payload = load_json(path)
        validate_simulation_manifest(payload)
        first = materialize_scenario(payload)
        second = materialize_scenario(payload)
        if first != second:
            raise RuntimeError(f"same seed produced different scene configs: {path}")
        manifests.append((path, payload))

    scenario_ids = [manifest["scenario_id"] for _, manifest in manifests]
    seeds = [manifest["seed"] for _, manifest in manifests]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise RuntimeError("scenario_id values must be unique")
    if len(seeds) != len(set(seeds)):
        raise RuntimeError("scenario seeds must be unique")

    frozen = [manifest for path, manifest in manifests if path.parent.name == "frozen"]
    distribution = Counter(manifest["fault_type"] for manifest in frozen)
    if dict(sorted(distribution.items())) != FROZEN_DISTRIBUTION:
        raise RuntimeError(f"P1 frozen distribution must be {FROZEN_DISTRIBUTION}, got {dict(distribution)}")

    expanded = [manifest for path, manifest in manifests if path.parent.name == "expanded"]
    if expanded:
        expected_total = len(frozen) + 6 * len(SCENE_TASKS)
        if len(frozen) + len(expanded) != expected_total:
            raise RuntimeError(
                f"expanded benchmark must contain {expected_total} total scenarios, found {len(frozen) + len(expanded)}"
            )
        variants = {manifest.get("scene_variant") for manifest in expanded}
        if not P2_SCENE_VARIANTS.issubset(variants):
            raise RuntimeError(f"P2 scene variants missing: {sorted(P2_SCENE_VARIANTS - variants)}")
        faults = {manifest["fault_type"] for _, manifest in manifests}
        if not P2_FAULT_TYPES.issubset(faults):
            raise RuntimeError(f"P2 fault injection types missing: {sorted(P2_FAULT_TYPES - faults)}")
        for variant, expected_task_id in SCENE_TASKS.items():
            variant_manifests = [manifest for manifest in expanded if manifest.get("scene_variant") == variant]
            if len(variant_manifests) != 6:
                raise RuntimeError(f"scene variant {variant} must contain 6 scenarios")
            task_ids = {manifest["task_id"] for manifest in variant_manifests}
            if task_ids != {expected_task_id}:
                raise RuntimeError(f"scene variant {variant} must use {expected_task_id}, got {sorted(task_ids)}")

    print(
        f"scenario validation passed for {len(frozen)} frozen and {len(expanded)} expanded manifest(s); "
        "same-seed materialization is deterministic"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
