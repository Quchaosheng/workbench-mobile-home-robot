#!/usr/bin/env python3
"""One machine-generated phase-gate status for incremental delivery (Issue #314).

Issue #314 asks for something narrower than a roadmap. The multi-scenario epic
(#309) landed in four phases, and each phase is a set of Issues whose committed
gates already exist. A release should be able to point at one generated artifact
that answers:

    which phase is actually complete, which gate is still unproven, and what
    evidence class does each phase reach?

Prose answers that badly, because the answer changes the moment a gate breaks.
So the status is generated from the live gates, and the only committed constants
are the *declarations*: which Issues a phase contains, its entry and exit
criteria, the evidence it must carry and who signs it off. Everything a reader
could mistake for an outcome - ``gate_status``, ``completed``, ``evidence_class``
and ``release_eligible`` - is computed here and compared by the gate.

Three properties are deliberate, and each has a test:

* **A phase is complete only when it proves itself.** Every declared probe is
  re-run in-process and must PASS; a probe that answers INCOMPLETE (it could not
  read its input) holds the phase at ``INCOMPLETE`` the same way #307's own gate
  reports INCOMPLETE rather than FAIL. A declared probe that no longer exists is
  a FAIL, because a silently vanished gate is exactly the hand-waved completion
  this Issue wants to stop.
* **Software readiness is not a physical capability.** ``evidence_class`` is
  copied from the readiness report (#307): with every scenario at
  ``SCRIPTED_FIXTURE``, no phase may claim ``gazebo`` or ``physical`` and
  ``release_eligible`` is false for every phase. A phase can be
  ``completed: true`` and still be only software.
* **A human sign-off is never implied by a passing gate.** A phase that requires
  an owner approval records it as ``outstanding``; this module cannot observe a
  human decision, so it refuses to claim one was made.

Everything here is read-only. It starts no simulator, writes no run or event, and
its verdict never claims a phase ran, that evidence is physical, or that a
release is approved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _jsonio import load_json
from _paths import ROOT, enable_local_packages

enable_local_packages()

STATUS_VERSION = "multi-scenario-phase-status-v1"
GENERATED_BY = "tools/scripts/phase_gates.py"

DEFAULT_STATUS_PATH = ROOT / "docs/releases/phase-status-v1.json"
DEFAULT_PAGE_PATH = ROOT / "docs/releases/phase-gates.md"
DEFAULT_NOTES_PATH = ROOT / "docs/releases/release-notes.md"
DEFAULT_README_PATH = ROOT / "README.md"
DEFAULT_README_ZH_PATH = ROOT / "README.zh-CN.md"
DEFAULT_READINESS_PATH = ROOT / "docs/evaluation/readiness-report-v1.json"

README_BEGIN = "<!-- BEGIN GENERATED: phase-status -->"
README_END = "<!-- END GENERATED: phase-status -->"
NOTES_BEGIN = "<!-- BEGIN GENERATED: phase-status -->"
NOTES_END = "<!-- END GENERATED: phase-status -->"

# The evidence axes, weakest first, copied from the readiness report so the two
# documents cannot disagree about what a phase reached.
EVIDENCE_CLASSES: tuple[str, ...] = ("software", "scripted_fixture", "gazebo", "physical")
RELEASE_ELIGIBLE_CLASSES: frozenset[str] = frozenset({"gazebo", "physical"})

PASS = 0
FAIL = 1
INCOMPLETE = 2
NOT_DELIVERED = 3

# The phases, as declared by Issue #314. Each member names the Issues it closes
# and the committed probes that must pass for the phase to be complete. This is
# the only hand-written part of the document; a reader can check it against the
# Issue, and every outcome field below is generated.
PHASES: tuple[dict[str, Any], ...] = (
    {
        "phase": 1,
        "name": "contract and registry",
        "issues": (308, 300),
        "entry_criteria": [
            "the four-phase split is agreed and the parent epic (#309) is open",
            "no scenario manifest is authoritative until the registry contract is frozen",
        ],
        "exit_criteria": [
            "the registry contract is frozen as a reviewed decision record (#308)",
            "the registry loads every committed manifest fail-closed (#300)",
            "a scenario registered without a row in the conformance corpus is refused",
        ],
        "required_evidence": ["contract-valid manifests", "a fail-closed registry load", "generated conformance cases"],
        "probes": (
            ("scenario_contract", "tools/scripts/check_scenario_contract.py"),
            ("scenario_registry", "tools/scripts/check_scenario_contract.py"),
        ),
        "rollback": "revert this phase's commit; no later phase depends on a fixture that changes",
        "sign_off": [
            {
                "role": "Architecture owner",
                "status": "outstanding",
                "detail": "the registry contract is a reviewed ADR",
            },
            {"role": "Release owner", "status": "outstanding", "detail": "the phase is recorded in the release notes"},
        ],
    },
    {
        "phase": 2,
        "name": "task migration, evidence identity and recovery",
        "issues": (301, 302, 305),
        "entry_criteria": [
            "phase 1 is software complete",
            "the shared safety, provenance, replay and evidence boundaries are written down",
        ],
        "exit_criteria": [
            "every legacy task family resolves to a registered identity with a pinned corpus (#301)",
            "a stored run resolves to the scenario definition it was produced from (#302)",
            "recovery is bounded, evidence-aware and shared across scenarios (#305)",
        ],
        "required_evidence": ["a pinned migration digest", "run identity bindings", "bounded recovery decisions"],
        "probes": (
            ("scenario_migration", "tools/scripts/check_scenario_migration.py"),
            ("run_identity", "tools/scripts/check_run_identity.py"),
            ("recovery_policy", "services/agent_runtime/workbench_agent_runtime/recovery.py"),
        ),
        "rollback": "revert this phase's commit; the legacy entry points keep working until v0.4",
        "sign_off": [
            {"role": "World Model owner", "status": "outstanding", "detail": "run identity and evidence policy"},
            {"role": "Agent Runtime owner", "status": "outstanding", "detail": "the bounded recovery policy"},
            {"role": "Release owner", "status": "outstanding", "detail": "the phase is recorded in the release notes"},
        ],
    },
    {
        "phase": 3,
        "name": "first registry-native scenario",
        "issues": (306,),
        "entry_criteria": [
            "phase 2 is software complete",
            "a new scenario can be added without a legacy task_id or a second implementation",
        ],
        "exit_criteria": [
            "cleaning-and-inspection is registered through the shared contract, not a side path (#306)",
            "the scenario passes the same conformance corpus as every migrated family",
        ],
        "required_evidence": [
            "a registry-native manifest",
            "a conformance case set",
            "a reconciled capability matrix row",
        ],
        "probes": (
            ("registry_native_scenario", "sim/registry/*.json"),
            ("scenario_conformance", "tools/scripts/check_scenario_conformance.py"),
        ),
        "blocked_on": [
            "#306 is not delivered: promoting clean_workspace into the shared semantic ActionType "
            "touches libs/contracts and interfaces/json_schema, which AGENTS.md gates behind three "
            "human approvals",
            "the conformance corpus has no case set for a registry-native identity yet",
        ],
        "rollback": (
            "hold the phase; the registry keeps the migrated families and the declared scenario stays unregistered"
        ),
        "sign_off": [
            {"role": "Scenario owner", "status": "outstanding", "detail": "the new manifest and its verifier"},
            {"role": "Release owner", "status": "outstanding", "detail": "the hold is recorded in the release notes"},
        ],
    },
    {
        "phase": 4,
        "name": "dashboard, quality gates and readiness report",
        "issues": (303, 304, 307),
        "entry_criteria": [
            "phases 1 to 3 are software complete",
            "every registered scenario has a scenario identity the surfaces can read",
        ],
        "exit_criteria": [
            "a run and its evidence timeline can be inspected without hand-reading a bundle (#303)",
            "every registered scenario is gated on the shared boundaries (#304)",
            "one generated readiness report states what the software is ready for (#307)",
        ],
        "required_evidence": [
            "a bounded run projection",
            "a conformance corpus over every registered identity",
            "a reproducible readiness artifact",
        ],
        "probes": (
            ("scenario_conformance", "tools/scripts/check_scenario_conformance.py"),
            ("readiness_report", "tools/scripts/check_readiness_report.py"),
        ),
        "rollback": "revert this phase's commit; the readiness page and the dashboard are additive surfaces",
        "sign_off": [
            {
                "role": "Release / QA owner",
                "status": "outstanding",
                "detail": "the readiness report and the phase table",
            },
            {"role": "Project Owner", "status": "outstanding", "detail": "the release decision the notes describe"},
        ],
    },
)

SIGN_OFF_ROLES: tuple[str, ...] = (
    "Architecture owner",
    "World Model owner",
    "Agent Runtime owner",
    "Scenario owner",
    "Release owner",
    "Release / QA owner",
    "Project Owner",
)

EDIT_POLICY = (
    "This file is generated. Do not hand-edit a gate_status, a completed flag, an "
    "evidence_class or release_eligible: regenerate it with "
    f"`python3 {GENERATED_BY}` and let the gate compare the result. Only "
    "generated_at and source_commit change without a change of inputs."
)

AUTHORITY = {
    "grants": [
        "a machine-generated statement of which multi-scenario phase proves itself today",
        "the evidence class each phase reaches and the sign-off it still needs",
    ],
    "does_not_grant": [
        "release approval",
        "physical or Gazebo validation",
        "a completed human sign-off",
        "authority to waive a required check or human approval",
    ],
}


class PhaseGateError(RuntimeError):
    """The phase status cannot be generated from the committed inputs."""


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


# --- the live probes ---------------------------------------------------------- #


def _scenario_contract_probe() -> tuple[int, str]:
    """The frozen contract file is present, well formed and valid against itself."""

    from workbench.kernel.scenario_contract import load_contract

    contract = load_contract(ROOT / "docs/architecture/scenario-contract-v1.json")
    version = contract.get("contract_version")
    if not isinstance(version, str) or not version:
        return FAIL, "the scenario contract declares no contract_version"
    return PASS, f"scenario contract {version} is frozen and loadable"


def _scenario_registry_probe() -> tuple[int, str]:
    """The registry loads fail-closed and every entry is executable under the contract."""

    from workbench.kernel.scenario_registry import load_registry

    registry = load_registry(ROOT / "sim/registry", repo_root=ROOT)
    if not registry.entries:
        return FAIL, "the registry loaded no scenario manifests"
    refused = [entry.identity for entry in registry.entries if not entry.executable]
    if refused:
        return FAIL, f"the registry loaded non-executable manifests: {', '.join(sorted(refused))}"
    return PASS, f"the registry loaded {len(registry.entries)} fail-closed manifest(s)"


def _scenario_migration_probe() -> tuple[int, str]:
    """Every legacy task family still resolves to its registered identity."""

    from workbench.kernel.scenario_migration import migration_report

    report = migration_report(ROOT, registry_root=ROOT / "sim/registry")
    if report["findings"]:
        codes = ", ".join(sorted({finding["code"] for finding in report["findings"]}))
        return FAIL, f"the migration join reports {len(report['findings'])} finding(s): {codes}"
    return PASS, f"{len(report['families'])} legacy task famil(ies) resolve to a registered identity"


def _scenario_conformance_probe() -> tuple[int, str]:
    """Every registered identity proves every required dimension."""

    from workbench.kernel.scenario_conformance import REQUIRED_DIMENSIONS, evaluate, load_corpus
    from workbench.kernel.scenario_registry import load_registry

    registry = load_registry(ROOT / "sim/registry", repo_root=ROOT)
    entries = {entry.identity: entry.as_dict() for entry in registry.entries}
    corpus = load_corpus(ROOT / "tools/qa/scenario-conformance-v1.json")
    code, verdicts = evaluate(
        root=ROOT,
        registry_entries=entries,
        corpus=corpus,
        corpus_path=ROOT / "tools/qa/scenario-conformance-v1.json",
    )
    if code != PASS:
        failed = [verdict.identity for verdict in verdicts if not verdict.ok]
        return FAIL, f"conformance fails for {', '.join(sorted(failed)) or 'the corpus'}"
    return PASS, f"{len(verdicts)} registered scenario(s) prove all {len(REQUIRED_DIMENSIONS)} dimension(s)"


def _run_identity_probe() -> tuple[int, str]:
    """The identity builder is total over its declared inputs and refuses a gap.

    The end-to-end run scan is a separate gate that needs stored runs. What phase
    2 must prove is the binding rule itself, so this probe drives the committed
    builder and checks it refuses a missing input rather than defaulting it.
    """

    from workbench.kernel.scenario_identity import RunIdentity, RunIdentityError, identity_from_entry

    entry = {
        "scenario_id": "probe",
        "scenario_version": "0",
        "evidence_policy": "the reference probe policy text",
        "verifier": "probe.module::verify_probe",
    }
    identity = identity_from_entry(entry, event_stream_hash_value="probe-stream")
    if not isinstance(identity, RunIdentity) or not identity.identity_hash:
        return FAIL, "the run identity builder returned no identity"
    try:
        identity_from_entry({**entry, "scenario_version": ""}, event_stream_hash_value="probe-stream")
    except RunIdentityError:
        return PASS, "the run identity binds scenario, evidence and stream, and refuses a missing version"
    return FAIL, "the run identity builder accepted an entry without a scenario_version"


def _recovery_policy_probe() -> tuple[int, str]:
    """A declared recovery policy parses, a malformed one is refused, and a terminal stop is final.

    The event-driven ledger behaviour is covered by ``tests/unit/test_recovery_policy.py``.
    This probe proves the same module's live rules, which is what a phase gate can
    re-check from a clean checkout without a run.
    """

    from workbench_agent_runtime.recovery import (
        RecoveryAction,
        RecoveryPolicy,
        RecoveryPolicyError,
        RecoveryState,
        transition_is_legal,
    )

    policy = RecoveryPolicy.parse({"allowed": ["re_observe", "retry_action"], "max_attempts": 2})
    if not policy.enabled or RecoveryAction.RETRY_ACTION not in policy.actions:
        return FAIL, "a declared recovery policy parsed but is not enabled"
    try:
        RecoveryPolicy.parse({"allowed": ["not_a_recovery_action"]})
    except RecoveryPolicyError:
        pass
    else:
        return FAIL, "an unknown recovery action was accepted"
    if transition_is_legal(RecoveryState.STOPPING, RecoveryState.ACTING):
        return FAIL, "a stopping recovery is allowed to resume acting"
    if transition_is_legal(RecoveryState.ABORTED, RecoveryState.ACTING):
        return FAIL, "an aborted recovery is allowed to resume acting"
    if RecoveryPolicy.parse(None).enabled:
        return FAIL, "a scenario that declares no policy still enables one"
    return PASS, "a declared policy is bounded, an unknown action and a terminal stop are refused"


def _dashboard_projection_probe() -> tuple[int, str]:
    """A run projects to a bounded envelope carrying scenario identity and evidence."""

    from workbench_backend.read_model import MAX_RUN_PAGE_SIZE, project_runs, scenario_identity_for

    resolved = scenario_identity_for("task-place-red-block")
    if resolved.get("scenario_label") != "pick-place-red-block@1.0":
        return FAIL, f"task-place-red-block resolved to {resolved.get('scenario_label')!r}"
    envelope = project_runs([], page=1, page_size=1)
    if not isinstance(envelope, Mapping) or envelope.get("read_only") is not True:
        return FAIL, "the run projection did not return a read-only bounded envelope"
    if envelope.get("max_page_size") != MAX_RUN_PAGE_SIZE:
        return FAIL, "the run projection did not report the bound it enforces"
    try:
        project_runs([], page_size=MAX_RUN_PAGE_SIZE + 1)
    except ValueError:
        pass
    except Exception as error:  # noqa: BLE001 - the specific class is not the point
        return FAIL, f"an over-large page was refused with an unexpected error: {error}"
    else:
        return FAIL, "an over-large page was accepted"
    return PASS, "a run projects to a bounded, read-only envelope and an over-large page is refused"


def _readiness_probe() -> tuple[int, str]:
    """The committed readiness report is present and carries its own summary.

    The #307 generator is the authority for the report's contents; this probe
    reads the committed artifact so a missing or unreadable report holds phase 4
    at INCOMPLETE rather than letting the phase claim readiness it never
    published.
    """

    if not DEFAULT_READINESS_PATH.is_file():
        return INCOMPLETE, f"{DEFAULT_READINESS_PATH.relative_to(ROOT)} is not committed"
    document = load_json(DEFAULT_READINESS_PATH)
    if not isinstance(document, Mapping) or "scenarios" not in document:
        return FAIL, "the committed readiness report has no scenarios list"
    summary = document.get("summary")
    if not isinstance(summary, Mapping):
        return FAIL, "the committed readiness report has no summary"
    return PASS, f"the readiness report covers {summary.get('registered_count')} identity(ies)"


def _registry_native_scenario_probe() -> tuple[int, str]:
    """Phase 3 is delivered only once a scenario is registered without a legacy task_id.

    Issue #306 adds the first registry-native scenario. A migrated family always
    carries the ``task_id`` it replaced; a registry-native manifest carries none,
    so the absence of a task_id is the committed signal that phase 3 landed. This
    probe reports NOT_DELIVERED while no such manifest exists rather than failing
    the phases around it, and never claims the scenario ran.
    """

    from workbench.kernel.scenario_registry import load_registry

    registry = load_registry(ROOT / "sim/registry", repo_root=ROOT)
    native = sorted(entry.identity for entry in registry.entries if entry.task_id is None)
    if not native:
        return NOT_DELIVERED, "no registry-native scenario is registered yet (issue #306 is not delivered)"
    return PASS, f"registry-native scenario(s) registered: {', '.join(native)}"


PROBES: Mapping[str, Any] = {
    "scenario_contract": _scenario_contract_probe,
    "scenario_registry": _scenario_registry_probe,
    "scenario_migration": _scenario_migration_probe,
    "scenario_conformance": _scenario_conformance_probe,
    "run_identity": _run_identity_probe,
    "recovery_policy": _recovery_policy_probe,
    "registry_native_scenario": _registry_native_scenario_probe,
    "readiness_report": _readiness_probe,
    "dashboard_projection": _dashboard_projection_probe,
}

VERDICTS = {PASS: "PASS", FAIL: "FAIL", INCOMPLETE: "INCOMPLETE", NOT_DELIVERED: "NOT_DELIVERED"}
PROBE_CODES = {label: code for code, label in VERDICTS.items()}


def run_probe(name: str) -> tuple[int, str]:
    """Run one declared probe, converting an unreadable input into INCOMPLETE.

    A probe that raises is not a phase failure; it is a phase we cannot judge, so
    it is recorded as INCOMPLETE with the reason rather than swallowed.
    """

    probe = PROBES.get(name)
    if probe is None:
        return FAIL, f"the probe {name!r} is declared but not implemented"
    try:
        return probe()
    except Exception as error:  # noqa: BLE001 - an unreadable probe is INCOMPLETE
        return INCOMPLETE, f"the probe could not read its input: {type(error).__name__}: {error}"


# --- the generated document --------------------------------------------------- #


def _relative_label(path: Path) -> str:
    """A repository-relative label when possible, so a probe path stays readable."""

    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _readiness_summary() -> dict[str, Any]:
    if not DEFAULT_READINESS_PATH.is_file():
        return {
            "available": False,
            "path": _relative_label(DEFAULT_READINESS_PATH),
            "classes": {name: False for name in EVIDENCE_CLASSES},
            "release_eligible": False,
        }
    document = load_json(DEFAULT_READINESS_PATH)
    summary = document.get("summary") or {}
    return {
        "available": True,
        "path": _relative_label(DEFAULT_READINESS_PATH),
        "registered_count": summary.get("registered_count"),
        "classes": {
            "software": bool(summary.get("software_ready_count")),
            "scripted_fixture": bool(summary.get("scripted_fixture_count")),
            "gazebo": bool(summary.get("gazebo_count")),
            "physical": bool(summary.get("physical_count")),
        },
        "release_eligible": bool(summary.get("release_eligible_count")),
    }


def _evidence_class(classes: Mapping[str, bool]) -> str:
    """The strongest class the readiness report supports, weakest first."""

    reached = "software" if classes.get("software") else "none"
    for name in EVIDENCE_CLASSES:
        if classes.get(name):
            reached = name
    return reached


def _phase_rows(readiness: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for phase in PHASES:
        probes = []
        for name, reference in phase["probes"]:
            code, detail = run_probe(name)
            probes.append(
                {
                    "probe": name,
                    "reference": reference,
                    "gate_status": VERDICTS[code],
                    "detail": detail,
                }
            )
        codes = [PROBE_CODES[probe["gate_status"]] for probe in probes]
        if any(code == FAIL for code in codes):
            phase_status = "FAIL"
        elif any(code == INCOMPLETE for code in codes):
            phase_status = "INCOMPLETE"
        elif any(code == NOT_DELIVERED for code in codes):
            phase_status = "BLOCKED" if phase.get("blocked_on") else "NOT_DELIVERED"
        else:
            phase_status = "COMPLETE"
        classes = dict(readiness.get("classes") or {name: False for name in EVIDENCE_CLASSES})
        # A phase that was never delivered reached no evidence class, so it must
        # not inherit the classes the phases around it earned.
        reached = "none" if phase_status in {"BLOCKED", "NOT_DELIVERED"} else _evidence_class(classes)
        signatures = [
            {
                "role": entry["role"],
                "status": entry["status"],
                "detail": entry["detail"],
            }
            for entry in phase["sign_off"]
        ]
        rows.append(
            {
                "phase": phase["phase"],
                "name": phase["name"],
                "issues": list(phase["issues"]),
                "entry_criteria": list(phase["entry_criteria"]),
                "exit_criteria": list(phase["exit_criteria"]),
                "required_evidence": list(phase["required_evidence"]),
                "probes": probes,
                "gate_status": phase_status,
                "completed": phase_status == "COMPLETE",
                # Software readiness and physical capability are separate claims.
                # A phase is never marked completed from a physical run it did
                # not do, and a completed phase is still not release eligible.
                "evidence_class": reached,
                "release_eligible": bool(readiness.get("release_eligible")) and reached in RELEASE_ELIGIBLE_CLASSES,
                "rollback": phase["rollback"],
                "blocked_on": list(phase.get("blocked_on") or []),
                "sign_off": signatures,
                "sign_off_outstanding": [entry["role"] for entry in signatures if entry["status"] != "approved"],
            }
        )
    return rows


def build_status(
    *,
    generated_at: str | None = None,
    source_commit: str | None = None,
) -> dict[str, Any]:
    """Build the phase status from the live gates, deterministically."""

    readiness = _readiness_summary()
    rows = _phase_rows(readiness)

    inputs = {
        "phases": [
            {
                "phase": phase["phase"],
                "issues": list(phase["issues"]),
                "probes": [[name, reference] for name, reference in phase["probes"]],
            }
            for phase in PHASES
        ],
        "readiness_summary": readiness,
    }

    return {
        "status_version": STATUS_VERSION,
        "generated_by": GENERATED_BY,
        "issue": 314,
        "epic": 309,
        "edit_policy": EDIT_POLICY,
        "authority": AUTHORITY,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "source_commit": source_commit if source_commit is not None else _git_commit(),
        "configuration_hash": _sha256(inputs),
        "evidence_class_vocabulary": {
            "classes": list(EVIDENCE_CLASSES),
            "release_eligible_classes": sorted(RELEASE_ELIGIBLE_CLASSES),
            "rule": (
                "evidence_class is copied from the readiness report; software means the phase's gates pass over "
                "contract-valid, registered and conformance-proven definitions, and only gazebo and physical can "
                "make a phase release eligible, so a completed phase can still be a software-only phase"
            ),
        },
        "generated_fields": [
            "generated_at",
            "source_commit",
            "configuration_hash",
            "phases[].probes[].gate_status",
            "phases[].probes[].detail",
            "phases[].gate_status",
            "phases[].completed",
            "phases[].evidence_class",
            "phases[].release_eligible",
            "summary",
            "phases[].blocked_on",
            "phases[].sign_off_outstanding",
        ],
        "readiness": readiness,
        "summary": {
            "phase_count": len(rows),
            "complete_count": sum(1 for row in rows if row["completed"]),
            "incomplete_count": sum(1 for row in rows if row["gate_status"] == "INCOMPLETE"),
            "failed_count": sum(1 for row in rows if row["gate_status"] == "FAIL"),
            "blocked_count": sum(1 for row in rows if row["gate_status"] == "BLOCKED"),
            "release_eligible_count": sum(1 for row in rows if row["release_eligible"]),
            "sign_off_outstanding_count": sum(len(row["sign_off_outstanding"]) for row in rows),
        },
        "limitations": [
            "every reachable evidence class is software or scripted_fixture: no Gazebo or physical run backs any phase",
            "a phase is complete only in the sense that its declared gates pass; it is not release eligible",
            "each phase records the owner sign-off it still needs, and no gate here can observe a human decision",
            "this status grants no release approval and replaces no required check or human approval",
        ],
        "phases": rows,
    }


def markdown_page(status: Mapping[str, Any]) -> str:
    """Render the committed markdown page from the status, deterministically."""

    summary = status["summary"]
    readiness = status["readiness"]
    lines = [
        "# Multi-scenario phase gates",
        "",
        "<!-- Generated from docs/releases/phase-status-v1.json. Do not edit by hand. -->",
        "",
        "This page is generated by `tools/scripts/phase_gates.py`. It states which",
        "multi-scenario delivery phase proves itself today, and what evidence class each",
        "phase reaches. It grants no release approval and is not physical or Gazebo",
        "evidence.",
        "",
        f"- Code revision: `{status['source_commit']}`",
        f"- Configuration hash: `{status['configuration_hash']}`",
        f"- Phases: {summary['phase_count']}",
        f"- Complete: {summary['complete_count']}",
        f"- Incomplete: {summary['incomplete_count']}",
        f"- Failed: {summary['failed_count']}",
        f"- Blocked: {summary['blocked_count']}",
        f"- Release eligible: {summary['release_eligible_count']}",
        f"- Sign-offs outstanding: {summary['sign_off_outstanding_count']}",
        f"- Readiness report: {'available' if readiness.get('available') else 'missing'} at `{readiness.get('path')}`",
        "",
        "## Phases",
        "",
        "| Phase | Name | Issues | Gate status | Completed | Evidence class | Release eligible |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in status["phases"]:
        issues = ", ".join(f"#{number}" for number in row["issues"])
        lines.append(
            "| {phase} | {name} | {issues} | {gate} | {completed} | {evidence} | {eligible} |".format(
                phase=row["phase"],
                name=row["name"],
                issues=issues,
                gate=row["gate_status"],
                completed="yes" if row["completed"] else "no",
                evidence=row["evidence_class"],
                eligible="yes" if row["release_eligible"] else "no",
            )
        )

    lines.extend(["", "## Gate detail", ""])
    for row in status["phases"]:
        lines.extend(
            [
                f"### Phase {row['phase']}: {row['name']}",
                "",
                f"Status: **{row['gate_status']}** "
                f"(completed: {'yes' if row['completed'] else 'no'}, "
                f"evidence class: `{row['evidence_class']}`, "
                f"release eligible: {'yes' if row['release_eligible'] else 'no'})",
                "",
                "| Probe | Reference | Gate status | Detail |",
                "| --- | --- | --- | --- |",
            ]
        )
        for probe in row["probes"]:
            lines.append(
                f"| `{probe['probe']}` | `{probe['reference']}` | {probe['gate_status']} | {probe['detail']} |"
            )
        lines.extend(["", "Entry criteria:", ""])
        for criterion in row["entry_criteria"]:
            lines.append(f"- {criterion}")
        lines.extend(["", "Exit criteria:", ""])
        for criterion in row["exit_criteria"]:
            lines.append(f"- {criterion}")
        lines.extend(["", "Required evidence:", ""])
        for evidence in row["required_evidence"]:
            lines.append(f"- {evidence}")
        if row.get("blocked_on"):
            lines.extend(["", "Blocked on:", ""])
            for blocker in row["blocked_on"]:
                lines.append(f"- {blocker}")
        lines.extend(["", "Sign-off outstanding:", ""])
        for entry in row["sign_off"]:
            lines.append(f"- {entry['role']}: {entry['status']} - {entry['detail']}")
        lines.extend(["", f"Rollback: {row['rollback']}", ""])

    lines.extend(["## Known limitations", ""])
    for limitation in status["limitations"]:
        lines.append(f"- {limitation}")
    lines.extend(
        [
            "",
            "## Verifying locally",
            "",
            "```bash",
            "python3 tools/scripts/phase_gates.py --output docs/releases/phase-status-v1.json",
            "python3 tools/scripts/check_phase_gates.py",
            "python3 -m pytest tests/unit/test_phase_gates.py -v",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def readme_block(status: Mapping[str, Any], *, language: str = "en") -> str:
    """Render the README phase table from the same status data."""

    if language == "zh":
        lines = [
            README_BEGIN,
            "",
            "### 阶段门禁状态",
            "",
            "由 `tools/scripts/phase_gates.py` 依据"
            " [`docs/releases/phase-status-v1.json`](docs/releases/phase-status-v1.json)"
            " 生成。它说明当前提交对每个多场景交付阶段证明了什么。"
            "它不代表发布批准。它也不是物理或 Gazebo 证据。详见"
            " [阶段门禁](docs/releases/phase-gates.md)。",
            "",
            "| 阶段 | 门禁状态 | 已完成 | 证据类别 | 可否发布 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in status["phases"]:
            lines.append(
                f"| Phase {row['phase']} | {row['gate_status']} | "
                f"{'是' if row['completed'] else '否'} | {row['evidence_class']} | "
                f"{'是' if row['release_eligible'] else '否'} |"
            )
        lines.extend(["", README_END, ""])
        return "\n".join(lines)

    lines = [
        README_BEGIN,
        "",
        "### Phase gate status",
        "",
        "Generated from [`docs/releases/phase-status-v1.json`](docs/releases/phase-status-v1.json)"
        " by `tools/scripts/phase_gates.py`. It states which multi-scenario delivery phase"
        " proves itself; it grants no release approval and is not physical or Gazebo evidence."
        " See [phase gates](docs/releases/phase-gates.md).",
        "",
        "| Phase | Gate status | Completed | Evidence class | Release eligible |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in status["phases"]:
        lines.append(
            f"| Phase {row['phase']} | {row['gate_status']} | "
            f"{'yes' if row['completed'] else 'no'} | {row['evidence_class']} | "
            f"{'yes' if row['release_eligible'] else 'no'} |"
        )
    lines.extend(["", README_END, ""])
    return "\n".join(lines)


def release_notes_block(status: Mapping[str, Any]) -> str:
    """Render the release-notes phase block from the same status data.

    Issue #314 asks for release notes that link the generated status rather than
    restating it, so this block is a link plus the counts a reader scans first.
    """

    summary = status["summary"]
    lines = [
        NOTES_BEGIN,
        "",
        "### Multi-scenario phase status",
        "",
        "Generated by `tools/scripts/phase_gates.py`; do not edit by hand.",
        "Source: [`docs/releases/phase-status-v1.json`](phase-status-v1.json).",
        "Page: [phase gates](phase-gates.md).",
        "",
        f"- Phases: {summary['phase_count']}",
        f"- Complete: {summary['complete_count']}",
        f"- Incomplete: {summary['incomplete_count']}",
        f"- Failed: {summary['failed_count']}",
        f"- Blocked: {summary['blocked_count']}",
        f"- Release eligible: {summary['release_eligible_count']}",
        f"- Sign-offs outstanding: {summary['sign_off_outstanding_count']}",
        f"- Configuration hash: `{status['configuration_hash']}`",
        "",
        "| Phase | Issues | Gate status | Completed | Evidence class | Release eligible |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in status["phases"]:
        issues = ", ".join(f"#{number}" for number in row["issues"])
        lines.append(
            f"| {row['phase']} | {issues} | {row['gate_status']} | "
            f"{'yes' if row['completed'] else 'no'} | {row['evidence_class']} | "
            f"{'yes' if row['release_eligible'] else 'no'} |"
        )
    lines.extend(["", NOTES_END, ""])
    return "\n".join(lines)


def apply_block(text: str, block: str, *, begin: str = README_BEGIN, end: str = README_END) -> str:
    """Replace a generated block, or append it when absent.

    Replacement is idempotent: running the generator twice must produce the same
    bytes, so the separators around the block are normalized rather than appended
    to. Re-running the generator is the normal case, not an edge case.
    """

    block = block.strip("\n")
    if begin in text and end in text:
        head, _, remainder = text.partition(begin)
        _, _, tail = remainder.partition(end)
        parts = [head.rstrip("\n"), "", block]
        suffix = tail.strip("\n")
        if suffix:
            parts.extend(["", suffix])
        return "\n".join(parts) + "\n"
    return text.rstrip("\n") + "\n\n" + block + "\n"


def write_outputs(
    status: Mapping[str, Any],
    *,
    status_path: Path,
    page_path: Path,
    notes_path: Path,
    readme_path: Path,
    readme_zh_path: Path | None = None,
) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    page_path.write_text(markdown_page(status), encoding="utf-8")
    if notes_path.is_file():
        notes_path.write_text(
            apply_block(
                notes_path.read_text(encoding="utf-8"),
                release_notes_block(status),
                begin=NOTES_BEGIN,
                end=NOTES_END,
            )
        )
    readme_path.write_text(
        apply_block(readme_path.read_text(encoding="utf-8"), readme_block(status)),
    )
    if readme_zh_path is not None and readme_zh_path.is_file():
        readme_zh_path.write_text(
            apply_block(
                readme_zh_path.read_text(encoding="utf-8"),
                readme_block(status, language="zh"),
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the multi-scenario phase-gate status.")
    parser.add_argument("--output", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--page", type=Path, default=DEFAULT_PAGE_PATH)
    parser.add_argument("--release-notes", type=Path, default=DEFAULT_NOTES_PATH)
    parser.add_argument("--readme", type=Path, default=DEFAULT_README_PATH)
    parser.add_argument("--readme-zh", type=Path, default=DEFAULT_README_ZH_PATH)
    parser.add_argument("--print", action="store_true", dest="as_stdout", help="print the status instead of writing")
    args = parser.parse_args(argv)

    status = build_status()

    if args.as_stdout:
        print(json.dumps(status, indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    write_outputs(
        status,
        status_path=args.output,
        page_path=args.page,
        notes_path=args.release_notes,
        readme_path=args.readme,
        readme_zh_path=args.readme_zh,
    )
    print(
        f"wrote {args.output.relative_to(ROOT) if args.output.is_relative_to(ROOT) else args.output} "
        f"({status['summary']['phase_count']} phase(s), "
        f"{status['summary']['complete_count']} complete, "
        f"configuration_hash {status['configuration_hash'][:12]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
