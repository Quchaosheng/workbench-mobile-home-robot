"""The multi-scenario registry contract, shared by the gate and the registry.

Issue #308 froze the contract and shipped it with a detector. Issue #300 adds a
registry that must enforce exactly the same rules. Two validators for one
contract is the drift this project already fixed once for the event store, so
the rules live here and both readers import them:

* ``tools/scripts/check_scenario_contract.py`` gates committed manifests in CI;
* ``workbench.kernel.scenario_registry`` refuses an invalid manifest before a run
  is created.

This module is deliberately data-driven and read-only. It never imports the
runtime, never starts ROS, Gazebo or hardware, and never writes a run or an
event. A passing verdict says a manifest is well formed against the written
contract; it says nothing about whether the scenario runs or whether any
evidence is physical.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

PASS = 0
FAIL = 1
INCOMPLETE = 2

# The repository root, resolved from this file: libs/kernel/workbench/kernel/.
DEFAULT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONTRACT_PATH = DEFAULT_ROOT / "docs/architecture/scenario-contract-v1.json"

CONTRACT_KEYS = frozenset(
    {
        "contract_version",
        "status",
        "issue",
        "decision_record",
        "compatibility",
        "required_fields",
        "optional_fields",
        "semantic_action_source",
        "pending_semantic_actions",
        "scenario_id_pattern",
        "scenario_version_pattern",
        "evidence_status_vocabulary",
        "non_release_eligible_status",
        "forbidden_field_names",
        "forbidden_field_substrings",
        "diagnostic_codes",
        "bounds",
        "allowed_adapters",
        "ownership",
    }
)

# A field that carries stop authority or a raw control value. The shared runtime
# boundary is the point of the contract, so these fail by name before any value
# is read.
FORBIDDEN_INLINE_KEYS = frozenset({"policy", "policy_impl", "verifier_impl", "verifier_code", "bypass"})

# Every diagnostic this module can emit. The contract must declare all of them,
# so a code cannot be added without documenting it for operators.
EMITTED_CODES = (
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

# A notice is not a contract violation. It records that a manifest satisfies the
# contract while declaring work the shared runtime cannot execute yet, which is
# why a hypothetical example can be committed without pretending to run.
ERROR = "error"
NOTICE = "notice"


class ContractError(RuntimeError):
    """The contract or a manifest cannot be judged."""


@dataclass
class Finding:
    """One contract finding, with the severity that decides the verdict."""

    code: str
    path: str
    detail: str
    severity: str = ERROR

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail, "severity": self.severity}


@dataclass
class Verdict:
    status: str
    exit_code: int
    manifest: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == PASS


def load_contract(contract_path: Path = DEFAULT_CONTRACT_PATH) -> dict[str, Any]:
    """Read and structurally check the frozen contract file."""

    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ContractError(f"cannot read {contract_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ContractError(f"{contract_path} is not valid JSON: {error}") from error
    if not isinstance(contract, dict):
        raise ContractError(f"{contract_path} must be a JSON object")

    missing = sorted(CONTRACT_KEYS - set(contract))
    if missing:
        raise ContractError(f"{contract_path} is missing contract keys: {', '.join(missing)}")
    unknown = sorted(set(contract) - CONTRACT_KEYS)
    if unknown:
        raise ContractError(f"{contract_path} has unknown contract keys: {', '.join(unknown)}")
    absent = sorted(set(EMITTED_CODES) - set(contract["diagnostic_codes"]))
    if absent:
        raise ContractError(f"{contract_path} is missing diagnostic codes: {', '.join(absent)}")
    for name in ("scenario_id_pattern", "scenario_version_pattern"):
        try:
            re.compile(contract[name])
        except re.error as error:
            raise ContractError(f"{contract_path} {name} is not a valid regex: {error}") from error
    return contract


def approved_semantic_actions() -> frozenset[str]:
    """Return the shared ActionType vocabulary.

    The action contract is imported rather than copied so a scenario manifest
    cannot drift into a second, private action vocabulary.
    """

    try:
        from workbench_contracts import ActionType
    except ImportError as error:  # pragma: no cover - a broken checkout is INCOMPLETE, not FAIL
        raise ContractError(f"cannot import the shared ActionType contract: {error}") from error
    return frozenset(member.value for member in ActionType)


def iter_key_paths(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    """Flatten every key name and its JSON path, including nested objects."""

    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            found.append((key, path))
            found.extend(iter_key_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(iter_key_paths(item, f"{prefix}[{index}]"))
    return found


def safe_repo_path(value: str, root: Path = DEFAULT_ROOT) -> bool:
    """True when ``value`` is a normalized, in-repository relative path."""

    if value != value.strip() or "\\" in value:
        return False
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if windows.drive or windows.root or posix.is_absolute():
        return False
    if not posix.parts or any(part in {"", ".", ".."} for part in posix.parts):
        return False
    try:
        (root / posix.as_posix()).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def verdict_for(name: str, findings: list[Finding]) -> Verdict:
    """A notice alone is NOT_EXECUTABLE, never a silent pass."""

    if any(finding.severity == ERROR for finding in findings):
        return Verdict("FAIL", FAIL, name, findings)
    if findings:
        return Verdict("NOT_EXECUTABLE", PASS, name, findings)
    return Verdict("PASS", PASS, name, findings)


def validate_manifest(
    manifest: Any,
    contract: Mapping[str, Any],
    *,
    approved_actions: frozenset[str],
    name: str = "<manifest>",
    root: Path = DEFAULT_ROOT,
) -> Verdict:
    """Validate one manifest object against the written contract."""

    required = contract["required_fields"]
    optional = contract["optional_fields"]
    bounds = contract["bounds"]
    max_items = bounds["max_list_items"]
    max_string = bounds["max_string_length"]
    forbidden_names = frozenset(contract["forbidden_field_names"])
    forbidden_substrings = tuple(contract["forbidden_field_substrings"])
    pending_actions = frozenset(contract.get("pending_semantic_actions", []))
    allowed_adapters = frozenset(contract["allowed_adapters"])

    findings: list[Finding] = []

    def add(code: str, path: str, detail: str, severity: str = ERROR) -> None:
        findings.append(Finding(code, path, detail, severity))

    if not isinstance(manifest, dict):
        add("SCENARIO_MISSING_FIELD", "$", "manifest must be a JSON object")
        return verdict_for(name, findings)

    known = set(required) | set(optional)

    for key in required:
        if key not in manifest:
            add("SCENARIO_MISSING_FIELD", f"$.{key}", f"required field {key!r} is absent")
    for key, path in iter_key_paths(manifest):
        # One key yields exactly one code. A forbidden key is also an unknown key,
        # but reporting both would make the primary diagnostic depend on dict
        # order, and the caller reports the first finding.
        if key in FORBIDDEN_INLINE_KEYS:
            add("SCENARIO_FORBIDDEN_FIELD", path, f"{key!r} would declare a second policy or verifier implementation")
        elif key in forbidden_names or any(fragment in key for fragment in forbidden_substrings):
            add("SCENARIO_FORBIDDEN_FIELD", path, f"{key!r} names raw control, trajectory or stop authority")
        elif path.count(".") == 1 and path.count("[") == 0 and key not in known:
            add("SCENARIO_UNKNOWN_FIELD", path, f"{key!r} is not part of the v1 contract")

    scenario_id = manifest.get("scenario_id")
    if isinstance(scenario_id, str) and not re.fullmatch(contract["scenario_id_pattern"], scenario_id):
        add("SCENARIO_INVALID_ID", "$.scenario_id", f"{scenario_id!r} does not match the stable identity pattern")

    version = manifest.get("scenario_version")
    if isinstance(version, str) and not re.fullmatch(contract["scenario_version_pattern"], version):
        add("SCENARIO_INVALID_VERSION", "$.scenario_version", f"{version!r} is not an exact MAJOR.MINOR version")

    for key in ("goal", "evidence_policy", "verifier", "evidence_status", "scenario_id", "scenario_version"):
        value = manifest.get(key)
        if value is not None and not (isinstance(value, str) and len(value) <= max_string):
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
                    severity=NOTICE,
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
        elif not safe_repo_path(module_path, root):
            reason = "the module half must be a repository-relative path with no traversal"
        elif not (root / module_path).is_file():
            reason = "the named module does not exist in this repository"
        if reason is not None:
            add("SCENARIO_INVALID_VERIFIER", "$.verifier", f"{verifier!r}: {reason}")

    non_goals = manifest.get("non_goals")
    if isinstance(non_goals, list) and len(non_goals) > max_items:
        add("SCENARIO_OVERSIZED_VALUE", "$.non_goals", f"more than {max_items} entries")

    recovery = manifest.get("recovery_policy")
    if recovery is not None and not isinstance(recovery, dict):
        add("SCENARIO_UNKNOWN_FIELD", "$.recovery_policy", "recovery_policy must be an object")

    return verdict_for(name, findings)


def duplicate_findings(loaded: Sequence[tuple[str, Mapping[str, Any]]]) -> list[Finding]:
    """Fail closed when two manifests claim one ``scenario_id@scenario_version``."""

    seen: dict[tuple[Any, Any], str] = {}
    findings: list[Finding] = []
    for name, manifest in loaded:
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


def validate_many(
    manifests: Iterable[tuple[str, Any]],
    contract: Mapping[str, Any],
    *,
    approved_actions: frozenset[str],
    root: Path = DEFAULT_ROOT,
    require_executable: bool = False,
) -> tuple[int, list[Verdict]]:
    """Validate a manifest set, including cross-manifest duplicate identity."""

    loaded: list[tuple[str, Any]] = list(manifests)
    verdicts = [
        validate_manifest(manifest, contract, approved_actions=approved_actions, name=name, root=root)
        for name, manifest in loaded
    ]
    verdicts.extend(Verdict("FAIL", FAIL, "<manifest set>", [finding]) for finding in duplicate_findings(loaded))
    if require_executable:
        for verdict in verdicts:
            if verdict.status != "NOT_EXECUTABLE":
                continue
            verdict.status = "FAIL"
            verdict.exit_code = FAIL
            for finding in verdict.findings:
                if finding.severity == NOTICE:
                    finding.severity = ERROR
                    finding.detail += " (registry gating requires executable manifests)"
    return (FAIL if any(verdict.exit_code == FAIL for verdict in verdicts) else PASS), verdicts
