"""Validate fail-closed Jetson BSP image build inputs."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "bsp/image/build-inputs.yaml"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
UNRESOLVED_PREFIXES = ("TBD_", "NOT_BUILT")
GROUP_FIELDS = {
    "jetpack": ("version", "source", "sha256", "retrieval"),
    "l4t": ("version", "source", "sha256", "retrieval"),
    "kernel": ("version", "source", "sha256", "retrieval", "config", "config_sha256"),
    "device_tree": ("source", "source_sha256", "dtb_sha256"),
    "rootfs": (
        "base_source",
        "base_sha256",
        "retrieval",
        "package_lock",
        "package_lock_sha256",
        "services",
        "services_sha256",
    ),
    "recovery": ("image_source", "image_sha256", "rollback_procedure"),
    "toolchain": ("container_digest", "retrieval", "compiler"),
}
INPUT_GROUPS = tuple(GROUP_FIELDS)
OUTPUT_FIELDS = ("boot_image_sha256", "rootfs_image_sha256", "recovery_image_sha256")
RETRIEVAL_FIELDS = {"url", "size_bytes", "sha256"}
BUILD_RECORD_FIELDS = {"command", "log", "artifacts"}
DIGEST_FIELDS = (
    ("jetpack", "sha256"),
    ("l4t", "sha256"),
    ("kernel", "sha256"),
    ("kernel", "config_sha256"),
    ("device_tree", "source_sha256"),
    ("device_tree", "dtb_sha256"),
    ("rootfs", "base_sha256"),
    ("rootfs", "package_lock_sha256"),
    ("rootfs", "services_sha256"),
    ("recovery", "image_sha256"),
)
NETWORK_INPUTS = (
    ("jetpack", "source", "sha256"),
    ("l4t", "source", "sha256"),
    ("kernel", "source", "sha256"),
    ("rootfs", "base_source", "base_sha256"),
)
LOCAL_INPUTS = (
    ("kernel", "config", "config_sha256", "kernel config"),
    ("device_tree", "source", "source_sha256", "device-tree source"),
    ("rootfs", "services", "services_sha256", "rootfs services"),
    ("rootfs", "package_lock", "package_lock_sha256", "rootfs package lock"),
    ("recovery", "image_source", "image_sha256", "recovery image"),
)


def _unresolved(value: object) -> bool:
    return not isinstance(value, str) or not value or value.startswith(UNRESOLVED_PREFIXES)


def _repository_file(root: Path, value: object) -> Path | None:
    if _unresolved(value):
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    root = root.resolve()
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root)
        return candidate if candidate.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _sha256_file(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _retrieval_record(field: str, record: object, errors: list[str]) -> dict | None:
    if not isinstance(record, dict) or set(record) != RETRIEVAL_FIELDS:
        errors.append(f"{field} requires a retrieval record with exactly url, size_bytes and sha256")
        return None
    if not _positive_integer(record.get("size_bytes")):
        errors.append(f"{field} retrieval size_bytes must be a positive integer")
    return record


def _network_retrieval_errors(field: str, digest: str, source: object, record: object, errors: list[str]) -> None:
    record = _retrieval_record(field, record, errors)
    if record is None:
        return
    url = record.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        errors.append(f"{field} retrieval url must use HTTPS")
    elif isinstance(source, str) and not _unresolved(source) and url != source:
        errors.append(f"{field} retrieval url must match the declared source")
    if record.get("sha256") != digest:
        errors.append(f"{field} retrieval sha256 must match the declared digest")


def _container_retrieval_errors(field: str, digest: str, record: object, errors: list[str]) -> None:
    record = _retrieval_record(field, record, errors)
    if record is None:
        return
    url = record.get("url")
    pinned = f"@{digest}"
    if not isinstance(url, str) or not url.endswith(pinned) or not url[: -len(pinned)].strip():
        errors.append(f"{field} retrieval url must be a registry reference pinned to the digest")
    if record.get("sha256") != digest.removeprefix("sha256:"):
        errors.append(f"{field} retrieval sha256 must match the declared digest")


def _built_digests(outputs: object, device_tree: object, errors: list[str]) -> dict[str, str]:
    built: dict[str, str] = {}
    for field in OUTPUT_FIELDS:
        value = outputs.get(field) if isinstance(outputs, dict) else None
        if not isinstance(value, str):
            errors.append(f"{field} output must be a lowercase hexadecimal digest or NOT_BUILT")
            continue
        if value == "NOT_BUILT":
            continue
        if not SHA256.fullmatch(value):
            errors.append(f"{field} output must be a lowercase hexadecimal digest or NOT_BUILT")
            continue
        built[field] = value
    dtb = device_tree.get("dtb_sha256") if isinstance(device_tree, dict) else None
    if isinstance(dtb, str) and not _unresolved(dtb) and SHA256.fullmatch(dtb):
        built["dtb_sha256"] = dtb
    return built


def _build_record_errors(record: object, built: dict[str, str], root: Path, errors: list[str]) -> None:
    if not isinstance(record, dict) or set(record) != BUILD_RECORD_FIELDS:
        errors.extend(f"{field} requires a recorded build output" for field in built)
        return
    command = record.get("command")
    if not isinstance(command, str) or not command.strip():
        errors.append("build record command must be a non-empty string")
    if _repository_file(root, record.get("log")) is None:
        errors.append("build record log must reference a repository file")
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("build record artifacts must be a mapping")
        return
    for field in sorted(set(artifacts) - set(built)):
        errors.append(f"build record artifact {field} has no declared digest")
    for field, digest in built.items():
        artifact = artifacts.get(field)
        path = _repository_file(root, artifact)
        if path is None:
            errors.append(f"{field} requires a recorded build artifact")
            continue
        actual = _sha256_file(path)
        if actual is None:
            errors.append(f"{field} build artifact {artifact} is unreadable")
        elif actual != digest:
            errors.append(f"{field} does not match build artifact {artifact}: expected {digest}, actual {actual}")


def validate_image_inputs(manifest: object, root: Path = ROOT) -> list[str]:
    if not isinstance(manifest, dict):
        return ["image build manifest must be a mapping"]
    errors: list[str] = []
    inputs = manifest.get("inputs")
    outputs = manifest.get("outputs")
    rules = manifest.get("rules")
    if not isinstance(inputs, dict) or set(inputs) != set(INPUT_GROUPS):
        return ["image build manifest requires the exact controlled input groups"]
    if not isinstance(outputs, dict) or set(outputs) != set(OUTPUT_FIELDS):
        errors.append("image build manifest requires the exact controlled outputs")
    if not isinstance(rules, dict) or not all(value is True for value in rules.values()):
        errors.append("image build manifest safety rules must all be true")

    groups: dict[str, dict] = {}
    unresolved = False
    for group_name in INPUT_GROUPS:
        group = inputs.get(group_name)
        if not isinstance(group, dict):
            errors.append(f"{group_name} input must be a mapping")
            continue
        groups[group_name] = group
        if set(group) != set(GROUP_FIELDS[group_name]):
            errors.append(f"{group_name} input requires the exact controlled fields")
        for field, value in group.items():
            if field == "retrieval":
                continue
            if not isinstance(value, str):
                errors.append(f"{group_name}.{field} must be a string")
                unresolved = True
            else:
                unresolved |= _unresolved(value)

    for group_name, field in DIGEST_FIELDS:
        group = groups.get(group_name)
        if group is None:
            continue
        digest = group.get(field)
        if isinstance(digest, str) and not _unresolved(digest) and not SHA256.fullmatch(digest):
            errors.append(f"{group_name}.{field} must be lowercase hexadecimal")

    for group_name, source_field, digest_field in NETWORK_INPUTS:
        group = groups.get(group_name)
        if group is None:
            continue
        source = group.get(source_field)
        if isinstance(source, str) and not _unresolved(source) and not source.startswith("https://"):
            errors.append(f"{group_name} source must use HTTPS")
        digest = group.get(digest_field)
        if isinstance(digest, str) and not _unresolved(digest) and SHA256.fullmatch(digest):
            _network_retrieval_errors(f"{group_name}.{digest_field}", digest, source, group.get("retrieval"), errors)

    toolchain = groups.get("toolchain")
    if toolchain is not None:
        container_digest = toolchain.get("container_digest")
        if isinstance(container_digest, str) and not _unresolved(container_digest):
            if not OCI_DIGEST.fullmatch(container_digest):
                errors.append("toolchain.container_digest must be an OCI sha256 digest")
            else:
                _container_retrieval_errors(
                    "toolchain.container_digest", container_digest, toolchain.get("retrieval"), errors
                )

    for group_name, path_field, digest_field, label in LOCAL_INPUTS:
        group = groups.get(group_name)
        if group is None:
            continue
        path_value = group.get(path_field)
        path = _repository_file(root, path_value)
        if path is None:
            if isinstance(path_value, str) and not _unresolved(path_value):
                errors.append(f"{label} must reference a repository file")
            continue
        digest = group.get(digest_field)
        if not isinstance(digest, str) or _unresolved(digest) or not SHA256.fullmatch(digest):
            continue
        actual = _sha256_file(path)
        if actual is None:
            errors.append(f"{group_name}.{digest_field} cannot be verified: {path_value} is unreadable")
        elif actual != digest:
            errors.append(
                f"{group_name}.{digest_field} does not match {path_value}: expected {digest}, actual {actual}"
            )

    recovery = groups.get("recovery")
    if recovery is not None:
        rollback = recovery.get("rollback_procedure")
        if isinstance(rollback, str) and not _unresolved(rollback) and _repository_file(root, rollback) is None:
            errors.append("rollback procedure must reference a repository file")

    built = _built_digests(outputs, groups.get("device_tree"), errors)
    built_outputs = any(field in built for field in OUTPUT_FIELDS)
    build_record = manifest.get("build_record")
    if built:
        _build_record_errors(build_record, built, root, errors)
    elif build_record is not None:
        errors.append("build record requires built output hashes")

    build_ready = manifest.get("build_ready") is True
    if build_ready and unresolved:
        errors.append("unresolved image inputs must block build_ready")
    if build_ready and errors:
        errors.append("invalid image inputs must block build_ready")
    if built_outputs and not build_ready:
        errors.append("built output hashes require build_ready inputs")
    expected_status = "inputs_locked" if build_ready and not errors else "inputs_unresolved"
    if manifest.get("status") != expected_status:
        errors.append(f"image build status must be {expected_status}")
    return errors


def validate() -> list[str]:
    return validate_image_inputs(yaml.safe_load(MANIFEST.read_text(encoding="utf-8")))


if __name__ == "__main__":
    failures = validate()
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        raise SystemExit(1)
    print("BSP image input validation passed")
