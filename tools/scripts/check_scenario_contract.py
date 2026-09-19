"""Validate scenario manifests against the written multi-scenario contract (#308).

Issue #308 freezes the Scenario Registry contract before any registry code is
written. A contract that cannot be checked is a claim rather than a boundary, so
this detector reads two committed files:

* ``docs/architecture/scenario-contract-v1.json`` -- the frozen field list,
  diagnostic codes, bounds and ownership table;
* ``docs/architecture/examples/*.json`` -- the non-executable example manifests
  that must validate against it.

It reports the same three outcomes as the other quality gates in this
repository: ``0 PASS``, ``1 FAIL`` and ``2 INCOMPLETE``. INCOMPLETE is separate
because a gate that reports FAIL when it could not read its own contract trains
reviewers to ignore it.

The detector is deliberately read-only. It never imports the runtime, never
starts ROS, Gazebo or hardware, and never writes a run or an event. A passing
verdict states that a manifest is *well formed against the written contract*.
It does not state that the scenario ran, that a verifier exists, or that any
evidence is physical.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from _paths import ROOT, enable_local_packages

PASS = 0
FAIL = 1
INCOMPLETE = 2

CONTRACT_PATH = ROOT / "docs/architecture/scenario-contract-v1.json"
EXAMPLE_DIR = ROOT / "docs/architecture/examples"
FIXTURE_DIR = ROOT / "tests/fixtures/scenario-contract"

# Fields the contract declares as optional. Anything outside required + optional
# is unknown, and an unknown field fails closed rather than being ignored.
_KEYS_WITH_DIAGNOSTIC = (
    "required_fields",
    "optional_fields",
    "diagnostic_codes",
    "bounds",
    "allowed_adapters",
    "forbidden_field_names",
    "forbidden_field_substrings",
    "evidence_status_vocabulary",
    "non_release_eligible_status",
    "scenario_id_pattern",
    "scenario_version_pattern",
    "compatibility",
)
_CONTRACT_KEYS = frozenset(_KEYS_WITH_DIAGNOSTIC) | {
    "contract_version",
    "status",
    "issue",
    "decision_record",
    "ownership",
    "semantic_action_source",
    "pending_semantic_actions",
}

# A field name that carries stop authority or a raw control value. The shared
# runtime boundary is the whole point of the contract, so these are rejected by
# name before any value is inspected.
_FORBIDDEN_INLINE_KEYS = frozenset({"policy", "policy_impl", "verifier_impl", "verifier_code", "bypass"})

# Every diagnostic the detector can emit. The contract must declare all of them,
# so a code cannot be added here without documenting it for operators.
_EMITTED_CODES = (
    "SCENARIO_MISSING_FIELD",
    "SCENARIO_UNKNOWN_FIELD",
    "SCENARIO_FORBIDDEN_FIELD",
    "SCENARIO_INVALID_ID",
    "SCENARIO_INVALID_VERSION",
    "SCENARIO_DUPLICATE_ID",
    "SCENARIO_INVALID_ACTION",
    "SCENARIO_PENDING_ACTION",
    "SCENARIO_INVALID_EVIDENCE_STATUS",
    "SCENARIO_INVALID_ADAPTER",
    "SCENARIO_INVALID_VERIFIER",
    "SCENARIO_OVERSIZED_VALUE",
)


class ContractError(RuntimeError):
    """The detector cannot produce a trustworthy verdict."""


@dataclass
class Finding:
    """One contract violation.

    ``severity`` separates a manifest that breaks the contract from one that
    satisfies it but declares work the shared runtime cannot execute yet. The
    second is not a pass in disguise: it is reported as ``NOT_EXECUTABLE`` and
    ``--require-executable`` turns it into a failure for registry gating.
    """

    code: str
    path: str
    detail: str
    severity: str = "error"


@dataclass
class Verdict:
    status: str
    exit_code: int
    manifest: str
    findings: list[Finding] = field(default_factory=list)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ContractError(f"{path} is not valid JSON: {error}") from error


def _load_contract() -> dict[str, Any]:
    contract = _read_json(CONTRACT_PATH)
    if not isinstance(contract, dict):
        raise ContractError(f"{CONTRACT_PATH} must be a JSON object")
    missing = sorted(_CONTRACT_KEYS - set(contract))
    if missing:
        raise ContractError(f"{CONTRACT_PATH} is missing contract keys: {', '.join(missing)}")
    unknown = sorted(set(contract) - _CONTRACT_KEYS)
    if unknown:
        raise ContractError(f"{CONTRACT_PATH} has unknown contract keys: {', '.join(unknown)}")
    for name, pattern in (
        ("scenario_id_pattern", contract["scenario_id_pattern"]),
        ("scenario_version_pattern", contract["scenario_version_pattern"]),
    ):
        try:
            re.compile(pattern)
        except re.error as error:
            raise ContractError(f"{CONTRACT_PATH} {name} is not a valid regex: {error}") from error
    return contract


def _approved_semantic_actions() -> frozenset[str]:
    """Return the shared ActionType vocabulary.

    The action contract is imported rather than copied so a scenario manifest
    cannot drift into a second, private action vocabulary.
    """

    enable_local_packages()
    try:
        from workbench_contracts import ActionType
    except ImportError as error:  # pragma: no cover - a broken checkout is INCOMPLETE, not FAIL
        raise ContractError(f"cannot import the shared ActionType contract: {error}") from error
    return frozenset(member.value for member in ActionType)


def _iter_key_paths(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    """Flatten every key name and its JSON path, including nested objects."""

    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            found.append((key, path))
            found.extend(_iter_key_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_iter_key_paths(item, f"{prefix}[{index}]"))
    return found


def _within(value: Any, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _safe_repo_path(value: str) -> bool:
    if value != value.strip() or "\\" in value:
        return False
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if windows.drive or windows.root or posix.is_absolute():
        return False
    if not posix.parts or any(part in {"", ".", ".."} for part in posix.parts):
        return False
    target = (ROOT / posix.as_posix()).resolve()
    try:
        target.relative_to(ROOT.resolve())
    except ValueError:
        return False
    return True


def validate_manifest(
    manifest: Any,
    contract: dict[str, Any],
    *,
    approved_actions: frozenset[str],
    name: str = "<manifest>",
) -> Verdict:
    """Validate one manifest object against the written contract."""

    required = contract["required_fields"]
    optional = contract["optional_fields"]
    codes = contract["diagnostic_codes"]
    bounds = contract["bounds"]
    max_items = bounds["max_list_items"]
    max_string = bounds["max_string_length"]
    forbidden_names = frozenset(contract["forbidden_field_names"])
    forbidden_substrings = tuple(contract["forbidden_field_substrings"])
    pending_actions = frozenset(contract.get("pending_semantic_actions", []))
    allowed_adapters = frozenset(contract["allowed_adapters"])

    findings: list[Finding] = []

    def add(code: str, path: str, detail: str, severity: str = "error") -> None:
        findings.append(Finding(code, path, detail, severity))

    required_codes = {finding_code for finding_code in _EMITTED_CODES}
    absent_codes = sorted(required_codes - set(codes))
    if absent_codes:
        raise ContractError(f"{CONTRACT_PATH} is missing diagnostic codes: {', '.join(absent_codes)}")

    if not isinstance(manifest, dict):
        add("SCENARIO_MISSING_FIELD", "$", "manifest must be a JSON object")
        return Verdict("FAIL", FAIL, name, findings)

    known = set(required) | set(optional)

    for key in required:
        if key not in manifest:
            add("SCENARIO_MISSING_FIELD", f"$.{key}", f"required field {key!r} is absent")
    for key, path in _iter_key_paths(manifest):
        if key in _FORBIDDEN_INLINE_KEYS:
            add("SCENARIO_FORBIDDEN_FIELD", path, f"{key!r} would declare a second policy or verifier implementation")
        elif path.count(".") == 1 and path.count("[") == 0 and key not in known:
            add("SCENARIO_UNKNOWN_FIELD", path, f"{key!r} is not part of the v1 contract")
        if key in forbidden_names or any(fragment in key for fragment in forbidden_substrings):
            add("SCENARIO_FORBIDDEN_FIELD", path, f"{key!r} names raw control, trajectory or stop authority")

    scenario_id = manifest.get("scenario_id")
    if isinstance(scenario_id, str) and not re.fullmatch(contract["scenario_id_pattern"], scenario_id):
        add("SCENARIO_INVALID_ID", "$.scenario_id", f"{scenario_id!r} does not match the stable identity pattern")

    version = manifest.get("scenario_version")
    if isinstance(version, str) and not re.fullmatch(contract["scenario_version_pattern"], version):
        add("SCENARIO_INVALID_VERSION", "$.scenario_version", f"{version!r} is not an exact MAJOR.MINOR version")

    for key in ("goal", "evidence_policy", "verifier", "evidence_status", "scenario_id", "scenario_version"):
        value = manifest.get(key)
        if value is not None and not _within(value, max_string):
            add("SCENARIO_OVERSIZED_VALUE", f"$.{key}", f"{key!r} exceeds {max_string} characters")

    actions = manifest.get("semantic_actions")
    if isinstance(actions, list):
        if not actions:
            add("SCENARIO_INVALID_ACTION", "$.semantic_actions", "at least one semantic action is required")
        if len(actions) > max_items:
            add("SCENARIO_OVERSIZED_VALUE", "$.semantic_actions", f"more than {max_items} entries")
        for index, action in enumerate(actions):
            if action in approved_actions:
                continue
            if action in pending_actions:
                add(
                    "SCENARIO_PENDING_ACTION",
                    f"$.semantic_actions[{index}]",
                    f"{action!r} is declared by a future issue and cannot execute until the shared ActionType adds it",
                    severity="notice",
                )
                continue
            add("SCENARIO_INVALID_ACTION", f"$.semantic_actions[{index}]", f"{action!r} is not a semantic action type")
    elif actions is not None:
        add("SCENARIO_INVALID_ACTION", "$.semantic_actions", "semantic_actions must be a list")

    adapters = manifest.get("required_adapters")
    if isinstance(adapters, list):
        if not adapters:
            add("SCENARIO_INVALID_ADAPTER", "$.required_adapters", "at least one adapter capability is required")
        if len(adapters) > max_items:
            add("SCENARIO_OVERSIZED_VALUE", "$.required_adapters", f"more than {max_items} entries")
        for index, adapter in enumerate(adapters):
            if adapter not in allowed_adapters:
                add("SCENARIO_INVALID_ADAPTER", f"$.required_adapters[{index}]", f"unknown capability {adapter!r}")
    elif adapters is not None:
        add("SCENARIO_INVALID_ADAPTER", "$.required_adapters", "required_adapters must be a list")

    evidence_status = manifest.get("evidence_status")
    if isinstance(evidence_status, str) and evidence_status not in contract["evidence_status_vocabulary"]:
        add("SCENARIO_INVALID_EVIDENCE_STATUS", "$.evidence_status", f"{evidence_status!r} is outside the vocabulary")

    verifier = manifest.get("verifier")
    if isinstance(verifier, str):
        module_path, separator, entry_point = verifier.partition("::")
        reason: str | None = None
        if separator != "::" or not entry_point:
            reason = "a verifier must name an explicit path.py::function entry point"
        elif not module_path.endswith(".py"):
            reason = "the module half must be a .py file"
        elif not _safe_repo_path(module_path):
            reason = "the module half must be a repository-relative path with no traversal"
        elif not (ROOT / module_path).is_file():
            reason = "the named module does not exist in this repository"
        if reason is not None:
            add("SCENARIO_INVALID_VERIFIER", "$.verifier", f"{verifier!r}: {reason}")

    non_goals = manifest.get("non_goals")
    if isinstance(non_goals, list) and len(non_goals) > max_items:
        add("SCENARIO_OVERSIZED_VALUE", "$.non_goals", f"more than {max_items} entries")

    recovery = manifest.get("recovery_policy")
    if recovery is not None and not isinstance(recovery, dict):
        add("SCENARIO_UNKNOWN_FIELD", "$.recovery_policy", "recovery_policy must be an object")

    return _verdict(name, findings)


def _verdict(name: str, findings: list[Finding]) -> Verdict:
    errors = [finding for finding in findings if finding.severity == "error"]
    if errors:
        return Verdict("FAIL", FAIL, name, findings)
    if findings:
        return Verdict("NOT_EXECUTABLE", PASS, name, findings)
    return Verdict("PASS", PASS, name, findings)


def _duplicate_findings(verdicts: list[tuple[str, dict[str, Any]]]) -> list[Finding]:
    seen: dict[tuple[Any, Any], str] = {}
    findings: list[Finding] = []
    for name, manifest in verdicts:
        if not isinstance(manifest, dict):
            continue
        key = (manifest.get("scenario_id"), manifest.get("scenario_version"))
        if key in seen:
            findings.append(
                Finding(
                    "SCENARIO_DUPLICATE_ID",
                    "$.scenario_id",
                    f"{name} duplicates the identity declared by {seen[key]}",
                )
            )
        else:
            seen[key] = name
    return findings


def _manifest_label(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        # A caller may point the detector at a manifest outside the checkout. The
        # label is only used for reporting, so the absolute path is fine here.
        return path.as_posix()


def run(paths: list[Path], *, require_executable: bool = False) -> tuple[int, list[Verdict]]:
    contract = _load_contract()
    approved_actions = _approved_semantic_actions()
    verdicts: list[Verdict] = []
    loaded: list[tuple[str, dict[str, Any]]] = []
    for path in paths:
        manifest = _read_json(path)
        label = _manifest_label(path)
        loaded.append((label, manifest))
        verdicts.append(validate_manifest(manifest, contract, approved_actions=approved_actions, name=label))
    for finding in _duplicate_findings(loaded):
        verdicts.append(Verdict("FAIL", FAIL, "<manifest set>", [finding]))
    if require_executable:
        for verdict in verdicts:
            if verdict.status != "NOT_EXECUTABLE":
                continue
            verdict.status = "FAIL"
            verdict.exit_code = FAIL
            for finding in verdict.findings:
                if finding.severity == "notice":
                    finding.severity = "error"
                    finding.detail += " (registry gating requires executable manifests)"
    return (FAIL if any(verdict.exit_code == FAIL for verdict in verdicts) else PASS), verdicts


def _example_paths() -> list[Path]:
    return sorted(EXAMPLE_DIR.glob("*.json"))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate scenario manifests against the written contract (#308).")
    parser.add_argument("manifests", nargs="*", type=Path, help="manifest files; default is the committed examples")
    parser.add_argument("--fixtures", action="store_true", help="also run the adversarial fixtures in tests/fixtures")
    parser.add_argument(
        "--require-executable",
        action="store_true",
        help="treat a manifest that declares pending actions as a failure; registry gating uses this",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    paths = args.manifests or _example_paths()
    if not paths:
        print(f"no example manifests found in {EXAMPLE_DIR}", file=sys.stderr)
        return INCOMPLETE
    try:
        exit_code, verdicts = run(paths, require_executable=args.require_executable)
    except ContractError as error:
        print(str(error), file=sys.stderr)
        return INCOMPLETE
    for verdict in verdicts:
        print(f"[{verdict.status}] {verdict.manifest}")
        for finding in verdict.findings:
            print(f"  {finding.code} {finding.path}: {finding.detail}")
    if args.fixtures:
        print("adversarial fixtures are exercised by tests/unit/test_scenario_contract.py")
    print(f"scenario contract check: {'PASS' if exit_code == PASS else 'FAIL'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
