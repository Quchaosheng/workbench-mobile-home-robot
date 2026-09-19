"""The scenario capability matrix, shared by the generator and the gate (#311).

Issue #308 froze what a scenario manifest *may* say and Issue #300 made the
registry refuse a manifest that breaks those rules. Neither answers the question
a contributor actually asks before touching a boundary: *which* scenarios exist,
*what* each one can do, *which* team owns each half, and *how strong* its current
evidence is.

This module answers that from one committed artifact,
``docs/architecture/scenario-capability-matrix-v1.json``. The matrix is not a
second manifest format. It is a join over identities that the registry already
owns, plus an explicit row for every family that is planned, blocked or not yet
registered, so an absent capability is visible instead of silent.

Three properties are deliberate, and each has a test:

* **The join is bidirectional.** A registered identity with no row fails, and a
  row that names no registered identity fails. One direction alone would let the
  matrix rot into a list of aspirations.
* **Evidence status never inflates.** A row may restate or weaken the manifest's
  ``evidence_status``, never strengthen it. A row that claims ``PHYSICAL`` over a
  ``SCRIPTED_FIXTURE`` manifest is the exact dishonesty this repository keeps
  fixing, so it is refused by name rather than by review.
* **The motion boundary holds.** A row may name semantic actions and adapter
  capabilities. It may not carry joint trajectories, controller goals, torque or
  velocity limits, CAN frames or emergency-stop authority, and it may not claim
  an action the shared ``ActionType`` does not define.

Like the contract module it builds on, this module is read-only: it never imports
the runtime, never starts ROS, Gazebo or hardware, and never writes a run, an
event or a file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scenario_contract import (
    ERROR,
    FAIL,
    PASS,
    Finding,
    Verdict,
    iter_key_paths,
    safe_repo_path,
)
from .scenario_registry import load_registry

DEFAULT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MATRIX_PATH = DEFAULT_ROOT / "docs/architecture/scenario-capability-matrix-v1.json"

MATRIX_VERSION = "scenario-capability-matrix-v1"

# A row status is an answer to "how was this scenario actually exercised", which
# is a different question from the manifest's own evidence_status. The rank
# matters: a row may never claim a rank above the rank its manifest earns.
ENVIRONMENT_RANK = {
    "NOT_EXECUTED": 0,
    "BLOCKED": 0,
    "SCRIPTED_FIXTURE": 1,
    "GAZEBO": 2,
    "PHYSICAL": 3,
}
ENVIRONMENT_STATUSES = tuple(ENVIRONMENT_RANK)

# A row is release eligible only when it was exercised in a real environment.
# A scripted fixture, a plan and a blocked row never are.
RELEASE_ELIGIBLE_ENVIRONMENTS = frozenset({"GAZEBO", "PHYSICAL"})

# Which manifest evidence_status permits which row status. A row may weaken
# freely; it may only match its own class or a strictly weaker one.
MANIFEST_MAX_RANK = {
    "NOT_EXECUTED": 0,
    "BLOCKED": 0,
    "SCRIPTED_FIXTURE": 1,
    "GAZEBO": 2,
    "PHYSICAL": 3,
}

# The four ownership columns Issue #311 requires to be explicit, and the
# ADR-0006 concern each one is answered by.
OWNERSHIP_CONCERNS = {
    "scenario_rules_owner": "scenario identity and manifest fields",
    "adapter_owner": "adapter implementation and ActionResult",
    "evidence_owner": "evidence status and release eligibility",
    "release_owner": "evidence status and release eligibility",
}

ROW_KEYS = frozenset(
    {
        "scenario_id",
        "scenario_version",
        "task_family",
        "status",
        "semantic_actions",
        "required_adapters",
        "missing_capabilities",
        "verifier",
        "recovery_policy",
        "evidence_status",
        "environment_status",
        "scenario_rules_owner",
        "adapter_owner",
        "evidence_owner",
        "release_owner",
        "manifest",
        "tests",
        "evidence_report",
        "notes",
    }
)

REQUIRED_ROW_KEYS = frozenset(
    {
        "scenario_id",
        "scenario_version",
        "task_family",
        "status",
        "environment_status",
        "scenario_rules_owner",
        "adapter_owner",
        "evidence_owner",
        "release_owner",
        "missing_capabilities",
    }
)

MATRIX_KEYS = frozenset({"matrix_version", "status", "issue", "decision_record", "environment_statuses", "rows"})

STATUSES = frozenset({"REGISTERED", "PLANNED", "BLOCKED", "NOT_REGISTERED"})

# Every diagnostic this module can emit. Each has a test that asserts it against
# an otherwise-valid matrix, so a matrix that fails everything cannot pass.
EMITTED_CODES = (
    "MATRIX_MISSING_ROW",
    "MATRIX_UNREGISTERED_ROW",
    "MATRIX_DUPLICATE_ROW",
    "MATRIX_MISSING_FIELD",
    "MATRIX_UNKNOWN_FIELD",
    "MATRIX_FORBIDDEN_FIELD",
    "MATRIX_INVALID_STATUS",
    "MATRIX_INVALID_ENVIRONMENT_STATUS",
    "MATRIX_STATUS_EXCEEDS_EVIDENCE",
    "MATRIX_INVALID_ACTION",
    "MATRIX_INVALID_ADAPTER",
    "MATRIX_INVALID_OWNER",
    "MATRIX_MALFORMED_JSON",
    "MATRIX_UNRESOLVED_EVIDENCE",
    "MATRIX_GENERATED_STALE",
)

# A row that names raw control, a trajectory or stop authority. This mirrors the
# manifest contract on purpose: the matrix must not become the back door that
# the manifest layer closed.
FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "joint_positions",
        "joint_trajectory",
        "joint_velocities",
        "velocity",
        "velocities",
        "torque",
        "torques",
        "effort",
        "can_frame",
        "can_frames",
        "controller_goal",
        "controller_goals",
        "trajectory",
        "emergency_stop",
        "estop",
        "e_stop",
        "safe_enable",
        "stop_authority",
        "motor_command",
    }
)
FORBIDDEN_FIELD_SUBSTRINGS = ("joint_", "_torque", "can_", "_estop", "velocity", "velocities", "torque")

# A row may name at most one of these. `verifier_impl`, `policy` and `bypass`
# would declare a second implementation of a boundary that is already owned.
FORBIDDEN_INLINE_KEYS = frozenset({"policy", "policy_impl", "verifier_impl", "verifier_code", "bypass"})


class CapabilityMatrixError(RuntimeError):
    """The matrix, or one row, cannot be judged."""


@dataclass(frozen=True)
class CapabilityRow:
    """One matrix row, frozen so a caller cannot mutate the published view."""

    scenario_id: str
    scenario_version: str
    task_family: str
    status: str
    environment_status: str
    semantic_actions: tuple[str, ...]
    required_adapters: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    verifier: str
    recovery_policy: tuple[str, ...]
    evidence_status: str
    scenario_rules_owner: str
    adapter_owner: str
    evidence_owner: str
    release_owner: str
    manifest: str
    tests: tuple[str, ...]
    evidence_report: str
    notes: str

    @property
    def identity(self) -> str:
        return f"{self.scenario_id}@{self.scenario_version}"

    @property
    def registered(self) -> bool:
        return self.status == "REGISTERED"

    @property
    def release_eligible(self) -> bool:
        return self.status == "REGISTERED" and self.environment_status in RELEASE_ELIGIBLE_ENVIRONMENTS


def load_matrix(matrix_path: Path = DEFAULT_MATRIX_PATH) -> dict[str, Any]:
    """Read and structurally check the committed matrix document."""

    try:
        payload = json.loads(matrix_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CapabilityMatrixError(f"cannot read {matrix_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise CapabilityMatrixError(f"{matrix_path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise CapabilityMatrixError(f"{matrix_path} must be a JSON object")

    missing = sorted(MATRIX_KEYS - set(payload))
    if missing:
        raise CapabilityMatrixError(f"{matrix_path} is missing matrix keys: {', '.join(missing)}")
    unknown = sorted(set(payload) - MATRIX_KEYS)
    if unknown:
        raise CapabilityMatrixError(f"{matrix_path} has unknown matrix keys: {', '.join(unknown)}")
    if payload["matrix_version"] != MATRIX_VERSION:
        raise CapabilityMatrixError(
            f"{matrix_path} declares matrix_version {payload['matrix_version']!r}; "
            f"this reader implements {MATRIX_VERSION!r}"
        )
    declared = tuple(payload["environment_statuses"])
    if declared != ENVIRONMENT_STATUSES:
        raise CapabilityMatrixError(
            f"{matrix_path} must declare the environment statuses {ENVIRONMENT_STATUSES}, not {declared}"
        )
    if not isinstance(payload["rows"], list):
        raise CapabilityMatrixError(f"{matrix_path} rows must be a list")
    return payload


def _add(findings: list[Finding], code: str, path: str, detail: str, severity: str = ERROR) -> None:
    findings.append(Finding(code, path, detail, severity))


def _check_references(
    row: dict[str, Any],
    *,
    name: str,
    root: Path,
    findings: list[Finding],
) -> None:
    """A row that points at tests, a manifest or a report must point at real files.

    A matrix link is how a reader gets from a capability claim to the thing that
    supports it. A link to a path that does not exist is worse than no link,
    because it reads as evidence.
    """

    references: list[tuple[str, str]] = []
    manifest = row.get("manifest")
    if isinstance(manifest, str) and manifest:
        references.append((f"{name}.manifest", manifest))
    report = row.get("evidence_report")
    if isinstance(report, str) and report:
        references.append((f"{name}.evidence_report", report))
    tests = row.get("tests")
    if isinstance(tests, list):
        for position, entry in enumerate(tests):
            if isinstance(entry, str) and entry:
                references.append((f"{name}.tests[{position}]", entry))

    for path, value in references:
        if not safe_repo_path(value, root):
            _add(
                findings,
                "MATRIX_UNRESOLVED_EVIDENCE",
                path,
                f"{value!r} is not a repository-relative path with no traversal",
            )
        elif not (root / value).exists():
            _add(findings, "MATRIX_UNRESOLVED_EVIDENCE", path, f"{value!r} does not exist in this repository")


def validate_row(
    row: Any,
    *,
    contract: dict[str, Any],
    approved_actions: frozenset[str],
    index: int,
    registered: dict[str, dict[str, Any]],
    root: Path = DEFAULT_ROOT,
) -> Verdict:
    """Validate one matrix row against the contract and the live registry."""

    name = f"rows[{index}]"
    findings: list[Finding] = []

    if not isinstance(row, dict):
        _add(findings, "MATRIX_MISSING_FIELD", name, "a matrix row must be a JSON object")
        return Verdict("FAIL", FAIL, name, findings)

    for key in sorted(REQUIRED_ROW_KEYS - set(row)):
        _add(findings, "MATRIX_MISSING_FIELD", f"{name}.{key}", f"required row field {key!r} is absent")

    for key, path in iter_key_paths(row):
        if key in FORBIDDEN_INLINE_KEYS:
            _add(
                findings,
                "MATRIX_FORBIDDEN_FIELD",
                path,
                f"{key!r} would declare a second policy or verifier implementation",
            )
        elif key in FORBIDDEN_FIELD_NAMES or any(fragment in key for fragment in FORBIDDEN_FIELD_SUBSTRINGS):
            _add(findings, "MATRIX_FORBIDDEN_FIELD", path, f"{key!r} names raw control, trajectory or stop authority")
        elif path.count(".") == 1 and path.count("[") == 0 and key not in ROW_KEYS:
            _add(findings, "MATRIX_UNKNOWN_FIELD", path, f"{key!r} is not part of the capability matrix schema")

    status = row.get("status")
    if status is not None and status not in STATUSES:
        _add(findings, "MATRIX_INVALID_STATUS", f"{name}.status", f"{status!r} is not a matrix status")

    environment = row.get("environment_status")
    if environment is not None and environment not in ENVIRONMENT_RANK:
        _add(
            findings,
            "MATRIX_INVALID_ENVIRONMENT_STATUS",
            f"{name}.environment_status",
            f"{environment!r} is not an environment status",
        )

    for key in ("scenario_rules_owner", "adapter_owner", "evidence_owner", "release_owner"):
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and value.strip()):
            _add(findings, "MATRIX_INVALID_OWNER", f"{name}.{key}", f"{key!r} must name a non-empty owner")

    actions = row.get("semantic_actions")
    pending = frozenset(contract.get("pending_semantic_actions", []))
    if actions is not None:
        if not isinstance(actions, list) or not actions:
            _add(findings, "MATRIX_INVALID_ACTION", f"{name}.semantic_actions", "must be a non-empty list when present")
        else:
            for position, action in enumerate(actions):
                if action in approved_actions or action in pending:
                    continue
                _add(
                    findings,
                    "MATRIX_INVALID_ACTION",
                    f"{name}.semantic_actions[{position}]",
                    f"{action!r} is not a semantic action type",
                )

    adapters = row.get("required_adapters")
    allowed_adapters = frozenset(contract["allowed_adapters"])
    if adapters is not None:
        if not isinstance(adapters, list) or not adapters:
            _add(
                findings,
                "MATRIX_INVALID_ADAPTER",
                f"{name}.required_adapters",
                "must be a non-empty list when present",
            )
        else:
            for position, adapter in enumerate(adapters):
                if adapter not in allowed_adapters:
                    _add(
                        findings,
                        "MATRIX_INVALID_ADAPTER",
                        f"{name}.required_adapters[{position}]",
                        f"unknown capability {adapter!r}",
                    )

    missing_capabilities = row.get("missing_capabilities")
    if missing_capabilities is not None:
        if not isinstance(missing_capabilities, list):
            _add(findings, "MATRIX_MISSING_FIELD", f"{name}.missing_capabilities", "must be a list")
        else:
            for position, capability in enumerate(missing_capabilities):
                if capability not in {"not_available", "blocked"}:
                    _add(
                        findings,
                        "MATRIX_INVALID_STATUS",
                        f"{name}.missing_capabilities[{position}]",
                        f"{capability!r} must be 'not_available' or 'blocked'; "
                        "support is never inferred from a fixture passing",
                    )

    if status == "REGISTERED" and not (isinstance(row.get("verifier"), str) and row["verifier"].strip()):
        _add(
            findings,
            "MATRIX_MISSING_FIELD",
            f"{name}.verifier",
            "a REGISTERED row must name the verifier entry point that decides it",
        )

    _check_references(row, name=name, root=root, findings=findings)

    # The join, in both directions, so the matrix cannot rot.
    scenario_id = row.get("scenario_id")
    scenario_version = row.get("scenario_version")
    if isinstance(scenario_id, str) and isinstance(scenario_version, str):
        identity = f"{scenario_id}@{scenario_version}"
        if status == "REGISTERED" and identity not in registered:
            _add(
                findings,
                "MATRIX_UNREGISTERED_ROW",
                f"{name}.scenario_id",
                f"{identity} is declared REGISTERED but the registry does not load it",
            )
        manifest = registered.get(identity)
        if manifest is not None:
            # A row may weaken evidence; strengthening it is the dishonest case.
            declared = row.get("evidence_status")
            if declared is not None and declared != manifest["evidence_status"]:
                _add(
                    findings,
                    "MATRIX_STATUS_EXCEEDS_EVIDENCE",
                    f"{name}.evidence_status",
                    f"row declares {declared!r} but the manifest declares {manifest['evidence_status']!r}",
                )
            maximum = MANIFEST_MAX_RANK.get(manifest["evidence_status"], 0)
            if environment is not None and ENVIRONMENT_RANK.get(environment, 0) > maximum:
                _add(
                    findings,
                    "MATRIX_STATUS_EXCEEDS_EVIDENCE",
                    f"{name}.environment_status",
                    f"row claims {environment!r} over a {manifest['evidence_status']!r} manifest",
                )

    failed = any(finding.severity == ERROR for finding in findings)
    return Verdict("FAIL" if failed else "PASS", FAIL if failed else PASS, name, findings)


def validate_matrix(
    matrix: dict[str, Any],
    *,
    registered: dict[str, dict[str, Any]],
    contract: dict[str, Any],
    approved_actions: frozenset[str],
    root: Path = DEFAULT_ROOT,
    require_registered: bool = False,
) -> tuple[int, list[Verdict]]:
    """Validate every row, then the join in both directions."""

    rows = matrix["rows"]
    verdicts = [
        validate_row(
            row,
            contract=contract,
            approved_actions=approved_actions,
            index=index,
            registered=registered,
            root=root,
        )
        for index, row in enumerate(rows)
    ]

    seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        scenario_id = row.get("scenario_id")
        scenario_version = row.get("scenario_version")
        if not isinstance(scenario_id, str) or not isinstance(scenario_version, str):
            continue
        identity = f"{scenario_id}@{scenario_version}"
        if identity in seen:
            verdicts.append(
                Verdict(
                    "FAIL",
                    FAIL,
                    f"rows[{index}]",
                    [
                        Finding(
                            "MATRIX_DUPLICATE_ROW",
                            "rows",
                            f"{identity} is already declared by rows[{seen[identity]}]",
                        )
                    ],
                )
            )
        else:
            seen[identity] = index

    for identity in sorted(set(registered) - set(seen)):
        verdicts.append(
            Verdict(
                "FAIL",
                FAIL,
                "<matrix>",
                [
                    Finding(
                        "MATRIX_MISSING_ROW",
                        "rows",
                        f"registered identity {identity} has no capability matrix row",
                    )
                ],
            )
        )

    if require_registered:
        # A row that is deliberately unregistered is published intent and is
        # allowed by default. Callers that need every row live pass this flag,
        # which turns the intent rows into failures without deleting them.
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("status") != "REGISTERED":
                verdicts.append(
                    Verdict(
                        "FAIL",
                        FAIL,
                        f"rows[{index}]",
                        [
                            Finding(
                                "MATRIX_UNREGISTERED_ROW",
                                "rows",
                                "only registered scenarios may be published when gating requires it",
                            )
                        ],
                    )
                )

    exit_code = FAIL if any(verdict.exit_code == FAIL for verdict in verdicts) else PASS
    return exit_code, verdicts


def load_registered(repo_root: Path = DEFAULT_ROOT, *, registry_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the live registry as an ``identity -> manifest`` map."""

    registry = load_registry(
        registry_root if registry_root is not None else repo_root / "sim/registry",
        repo_root=repo_root,
    )
    return {entry.identity: entry.as_dict() for entry in registry.entries}


def rows_to_markdown(matrix: dict[str, Any]) -> str:
    """Render the committed markdown table from the JSON, deterministically."""

    lines = [
        "<!-- Generated from docs/architecture/scenario-capability-matrix-v1.json. Do not edit by hand. -->",
        "",
        "| Scenario | Status | Environment | Semantic actions | Adapters | Missing capabilities "
        "| Evidence status | Release eligible | Rules owner | Adapter owner | Evidence owner | Release owner |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(matrix["rows"], key=lambda item: (item["scenario_id"], item["scenario_version"])):
        identity = f"`{row['scenario_id']}@{row['scenario_version']}`"
        actions = ", ".join(row.get("semantic_actions") or []) or "-"
        adapters = ", ".join(row.get("required_adapters") or []) or "-"
        missing = ", ".join(row.get("missing_capabilities") or []) or "-"
        release_eligible = (
            "yes"
            if row.get("status") == "REGISTERED" and row.get("environment_status") in RELEASE_ELIGIBLE_ENVIRONMENTS
            else "no"
        )
        cells = [
            identity,
            row["status"],
            row["environment_status"],
            actions,
            adapters,
            missing,
            row.get("evidence_status", "-"),
            release_eligible,
            row["scenario_rules_owner"],
            row["adapter_owner"],
            row["evidence_owner"],
            row["release_owner"],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def generated_document(matrix: dict[str, Any]) -> str:
    """The full committed markdown page: prose plus the generated table."""

    return "\n".join(
        [
            "# Scenario capability matrix",
            "",
            "One authoritative view of what each scenario can do, which boundary owns each",
            "half, and how strong its current evidence is. Issue #311 requires this to be",
            "machine-readable where practical, so the table below is **generated** from",
            "`docs/architecture/scenario-capability-matrix-v1.json`.",
            "",
            "Regenerate and verify with:",
            "",
            "```bash",
            "python3 tools/scripts/check_scenario_capabilities.py --check-generated",
            "```",
            "",
            "## How to read a row",
            "",
            "- **Status** is the registration state: `REGISTERED`, `PLANNED`, `BLOCKED` or `NOT_REGISTERED`.",
            "- **Environment** is how the scenario was actually exercised. It may restate or",
            "  weaken the manifest `evidence_status`; a row that claims a stronger class than",
            "  its manifest is refused with `MATRIX_STATUS_EXCEEDS_EVIDENCE`.",
            "- **Missing capabilities** is `not_available` or `blocked`. A passing fixture never",
            "  implies support.",
            "- **Release eligible** is `yes` only for a registered row whose environment is",
            "  `GAZEBO` or `PHYSICAL`. Scripted fixtures are never release eligible.",
            "",
            "Ownership columns are the ADR-0006 ownership table projected per scenario:",
            "scenario rules, adapter implementation, evidence and the release decision.",
            "",
            "## Boundaries this matrix may not cross",
            "",
            "A row may name semantic actions and adapter capabilities only. It must not carry",
            "joint trajectories, controller goals, torque or velocity limits, CAN frames or",
            "emergency-stop authority. Motion and the MCU keep controller and stop authority;",
            "a scenario can request a semantic action, never implement one.",
            "",
            "## Matrix",
            "",
            rows_to_markdown(matrix),
        ]
    )
