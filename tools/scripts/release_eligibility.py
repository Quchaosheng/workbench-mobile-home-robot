#!/usr/bin/env python3
"""One predicate that decides whether an evaluation run may be release evidence.

Before this module, three places each decided eligibility for themselves:

* ``run_evaluation.py`` stamped ``release_eligible`` from ``--runner external``
  and a non-unknown commit;
* ``collect_metrics.py`` trusted that boolean from ``summary.json`` and ANDed it
  with "an audit file exists";
* ``generate_report.py`` read the resulting metric.

That chain trusted a summary file, so a hand-written ``summary.json`` claiming
``runner: external`` was enough to publish a scripted run, and a copied log whose
events had been edited kept the same eligibility. Provenance that can be typed by
hand is not provenance.

This module recomputes eligibility from the evidence itself:

* the per-event ``evaluation`` metadata inside the log - not the summary - names
  the runner, commit, scenario and seed;
* the manifest is re-validated and must still agree with the log;
* every run's event content is bound to a hash served by a signed-by-hash
  provenance record, so an edited log fails to match;
* the human audit must name a reviewer, a timestamp and a per-run oracle status.

A run is eligible only when all of that holds. Missing, stale or mismatched
provenance fails closed, and a scripted run can never become eligible, because a
scripted runner identity is rejected even when every other field is present.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROVENANCE_FORMAT = "workbench-evaluation-provenance"
PROVENANCE_FORMAT_VERSION = 1
ELIGIBLE_RUNNER = "external"
SCRIPTED_RUNNERS = frozenset({"scripted", "fixture", "fixtures"})
AUDIT_FORMAT = "workbench-false-completion-audit"
AUDIT_FORMAT_VERSION = 1

# Reasons are stable strings so a caller can assert on them and an operator can
# search for them. They are not error messages: eligibility is a verdict, not an
# exception, because a refused run still has to be reported.
REASON_NO_PROVENANCE = "no provenance record binds this run"
REASON_UNKNOWN_RUNNER = "runner identity is missing or not external"
REASON_SCRIPTED_RUNNER = "scripted runs are pipeline fixtures, never release evidence"
REASON_UNKNOWN_COMMIT = "provenance does not name a commit"
REASON_COMMIT_MISMATCH = "provenance commit disagrees with the event log"
REASON_RUNNER_MISMATCH = "provenance runner disagrees with the event log"
REASON_LOG_HASH_MISMATCH = "event log no longer matches the provenance hash"
REASON_MANIFEST_HASH_MISMATCH = "scenario manifest no longer matches the provenance hash"
REASON_AUDIT_MISSING = "no human false-completion audit was supplied"
REASON_AUDIT_INVALID = "human audit is missing reviewer, timestamp or per-run oracle status"
REASON_AUDIT_INCOMPLETE = "human audit does not cover every run"
REASON_FALSE_COMPLETION = "human audit found a false completion"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def event_log_sha256(events: list[dict[str, Any]]) -> str:
    """Hash event content, so an edited log cannot keep a stale eligibility.

    Keys are sorted and separators are fixed, so the hash depends on values
    rather than on formatting. The hash is not a signature: it detects an
    accidentally or casually edited log, and a provenance record that carries it
    must itself be produced by the approval step.
    """
    canonical = json.dumps(events, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return sha256_bytes(canonical.encode("utf-8"))


def discover_manifests(directories: Iterable[Path] | Path) -> dict[str, Path]:
    """Map scenario id to manifest path for every readable scenario manifest.

    Shared by the runner, the metrics collector and the eligibility checker so all
    three bind a run to the same manifest file. A single path is accepted as well
    as an iterable, because a caller often has exactly one directory to search.
    """
    if isinstance(directories, str | Path):
        directories = [Path(directories)]
    manifests: dict[str, Path] = {}
    for directory in directories:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                scenario_id = payload.get("scenario_id")
                if isinstance(scenario_id, str) and scenario_id:
                    manifests.setdefault(scenario_id, path)
    return manifests


def _recorded_path(path: Path | None) -> str | None:
    """Record a path relative to the repository when possible, else absolutely.

    A relative path keeps the provenance record portable across checkouts, and an
    absolute fallback keeps it usable when the manifest lives outside the tree.
    """
    if path is None:
        return None
    path = Path(path)
    try:
        from _paths import ROOT
    except ImportError:  # pragma: no cover - only when run outside the repository
        return str(path.resolve())
    try:
        return path.resolve().relative_to(Path(ROOT).resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def resolve_recorded_path(recorded: str | None) -> Path | None:
    """Resolve a manifest path recorded by :func:`_recorded_path`."""
    if not isinstance(recorded, str) or not recorded:
        return None
    candidate = Path(recorded)
    if candidate.is_absolute():
        return candidate
    try:
        from _paths import ROOT
    except ImportError:  # pragma: no cover - only when run outside the repository
        return candidate
    return Path(ROOT) / candidate


def manifest_search_dirs(run_dir: Path | None = None) -> list[Path]:
    """Directories to search when a provenance entry names no manifest path.

    The run directory is searched so a manifest copied next to its logs is found,
    and the checked-in scenario directories are searched for the normal case. The
    run directory's *parent* is deliberately excluded: that directory can be
    shared, and a recursive search there could bind a run to an unrelated file
    that happens to declare the same ``scenario_id``.
    """
    directories: list[Path] = []
    if run_dir is not None:
        directories.append(Path(run_dir))
    try:
        from _paths import ROOT
    except ImportError:  # pragma: no cover - only when run outside the repository
        return directories
    directories.append(ROOT / "sim" / "scenarios")
    return directories


def _evaluation_metadata(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the first per-event evaluation block, or an empty mapping."""
    return next((event.get("evaluation") for event in events if isinstance(event.get("evaluation"), dict)), {})


def build_provenance(
    *,
    runner: str,
    commit: str,
    environment: dict[str, Any],
    manifests: dict[str, Path],
    logs: dict[str, list[dict[str, Any]]],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the provenance record that ``run_evaluation.py`` publishes.

    ``logs`` maps ``run_id`` to its validated events; each entry records the
    runner identity, the commit, the seed, the scenario, and the content hashes
    of both the log and the manifest that produced it.
    """
    runs: dict[str, Any] = {}
    for run_id, events in sorted(logs.items()):
        evaluation = _evaluation_metadata(events)
        scenario_id = evaluation.get("scenario_id")
        manifest_path = manifests.get(str(scenario_id))
        runs[run_id] = {
            "runner": evaluation.get("runner", runner),
            "commit": evaluation.get("commit", commit),
            "scenario_id": scenario_id,
            "seed": evaluation.get("seed"),
            "event_log_sha256": event_log_sha256(events),
            "manifest_sha256": file_sha256(manifest_path) if manifest_path is not None else None,
            "manifest_path": _recorded_path(manifest_path),
        }
    return {
        "format": PROVENANCE_FORMAT,
        "format_version": PROVENANCE_FORMAT_VERSION,
        "commit": commit,
        "runner": runner,
        "environment": environment,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "runs": runs,
    }


def load_provenance(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("provenance record is not an object")
    if payload.get("format") != PROVENANCE_FORMAT or payload.get("format_version") != PROVENANCE_FORMAT_VERSION:
        raise ValueError("provenance record has an unsupported format or version")
    if not isinstance(payload.get("runs"), dict):
        raise ValueError("provenance record has no per-run entries")
    return payload


def evaluate_eligibility(
    *,
    runs: dict[str, list[dict[str, Any]]],
    provenance: dict[str, Any] | None,
    manifests: dict[str, Path] | None = None,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the eligibility verdict and every reason it was refused.

    ``runs`` must be the validated event logs, ``provenance`` the record bound to
    them, ``manifests`` the scenario manifests by scenario id, and ``audit`` the
    human false-completion audit. Any missing or disagreeing input is a reason,
    never a default.
    """
    reasons: list[str] = []
    if not runs:
        reasons.append("no evaluated runs were supplied")
    if provenance is None:
        reasons.append(REASON_NO_PROVENANCE)
    # The audit is judged even when provenance is missing, so a caller that only
    # asks "was this reviewed?" still gets a truthful answer.
    if audit is None:
        reasons.append(REASON_AUDIT_MISSING)
    else:
        reasons.extend(_audit_reasons(runs, audit))
    if provenance is None:
        return _verdict(False, reasons, {})

    provenance_runs = provenance.get("runs") or {}
    if not provenance_runs:
        reasons.append(REASON_NO_PROVENANCE)

    per_run: dict[str, Any] = {}
    for run_id, events in sorted(runs.items()):
        run_reasons: list[str] = []
        entry = provenance_runs.get(run_id)
        evaluation = _evaluation_metadata(events)
        log_runner = evaluation.get("runner")
        log_commit = evaluation.get("commit")

        if not isinstance(log_runner, str) or not log_runner:
            run_reasons.append(REASON_UNKNOWN_RUNNER)
        elif log_runner.casefold() in SCRIPTED_RUNNERS:
            run_reasons.append(REASON_SCRIPTED_RUNNER)
        elif log_runner != ELIGIBLE_RUNNER:
            run_reasons.append(REASON_UNKNOWN_RUNNER)

        if not isinstance(log_commit, str) or not log_commit or log_commit == "unknown":
            run_reasons.append(REASON_UNKNOWN_COMMIT)

        if entry is None:
            run_reasons.append(REASON_NO_PROVENANCE)
        else:
            if entry.get("runner") != log_runner:
                run_reasons.append(REASON_RUNNER_MISMATCH)
            if entry.get("commit") != log_commit:
                run_reasons.append(REASON_COMMIT_MISMATCH)
            if entry.get("event_log_sha256") != event_log_sha256(events):
                run_reasons.append(REASON_LOG_HASH_MISMATCH)
            expected_manifest = entry.get("manifest_sha256")
            manifest_path = resolve_recorded_path(entry.get("manifest_path"))
            if manifest_path is None and manifests is not None:
                manifest_path = manifests.get(str(evaluation.get("scenario_id")))
            if manifest_path is None:
                if expected_manifest is not None:
                    run_reasons.append(REASON_MANIFEST_HASH_MISMATCH)
            elif not manifest_path.is_file():
                run_reasons.append(REASON_MANIFEST_HASH_MISMATCH)
            elif expected_manifest != file_sha256(manifest_path):
                run_reasons.append(REASON_MANIFEST_HASH_MISMATCH)

        per_run[run_id] = {"eligible": not run_reasons, "reasons": sorted(set(run_reasons))}
        reasons.extend(run_reasons)

    eligible = not reasons
    return _verdict(eligible, reasons, per_run)


def _audit_reasons(runs: dict[str, list[dict[str, Any]]], audit: dict[str, Any]) -> list[str]:
    """Validate a human audit: reviewer, timestamp and per-run oracle status."""
    reasons: list[str] = []
    reviewer = audit.get("reviewed_by")
    timestamp = audit.get("reviewed_at")
    decisions = audit.get("runs")
    if (
        not isinstance(reviewer, str)
        or not reviewer.strip()
        or not isinstance(timestamp, str)
        or not timestamp.strip()
        or not isinstance(decisions, dict)
    ):
        reasons.append(REASON_AUDIT_INVALID)
        return reasons
    missing = sorted(set(runs) - set(decisions))
    if missing:
        reasons.append(REASON_AUDIT_INCOMPLETE)
        return reasons
    for run_id, events in runs.items():
        decision = decisions.get(run_id)
        if not isinstance(decision, dict) or decision.get("oracle_status") not in {"confirmed", "refuted"}:
            reasons.append(REASON_AUDIT_INVALID)
            continue
        statuses = _verification_statuses(events)
        claimed_complete = bool(statuses and statuses[-1] == "confirmed")
        if claimed_complete and decision["oracle_status"] != "confirmed":
            reasons.append(REASON_FALSE_COMPLETION)
    return reasons


def _verification_statuses(events: list[dict[str, Any]]) -> list[Any]:
    return [event.get("payload", {}).get("status") for event in events if event.get("event_type") == "verification"]


def _verdict(eligible: bool, reasons: list[str], per_run: dict[str, Any]) -> dict[str, Any]:
    return {
        "eligible": eligible,
        "reasons": sorted(set(reasons)),
        "runner_eligible": eligible,
        "per_run": per_run,
    }
