#!/usr/bin/env python3
"""Truthful, bounded control surface for Workbench simulation runs.

This module deliberately separates the runnable scripted fixture from a real
Gazebo execution.  A missing adapter is reported as ``NOT_EXECUTED`` rather
than being converted into a green regression result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _paths import ROOT, enable_local_packages

enable_local_packages()

from scenario_tools import canonical_hash, materialize_scenario, validate_simulation_manifest
from workbench.kernel.scenario_contract import ContractError
from workbench.kernel.scenario_identity import (
    RunIdentityError,
    event_stream_hash,
    hash_events_file,
    identity_from_entry,
)
from workbench.kernel.scenario_migration import (
    ScenarioMigrationError,
    migration_report,
    resolve_task_id,
)
from workbench.kernel.scenario_registry import ScenarioRegistryError, load_registry

SCENARIO_ROOT = ROOT / "sim" / "scenarios"
REGISTRY_ROOT = ROOT / "sim" / "registry"
DEFAULT_OUTPUT_DIR = ROOT / "runs" / "sim"
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_COMMAND_TOKENS = 64
MAX_RUNNER_LOG_BYTES = 4 * 1024 * 1024
RUNNER_STATUSES = {
    "SCRIPTED_FIXTURE",
    "EXECUTED",
    "NOT_EXECUTED",
    "FAILED",
    "TIMED_OUT",
    "INVALID_OUTPUT",
    "INTERRUPTED",
}
EXIT_CODES = {
    "SCRIPTED_FIXTURE": 0,
    "EXECUTED": 0,
    "NOT_EXECUTED": 2,
    "FAILED": 1,
    "TIMED_OUT": 1,
    "INVALID_OUTPUT": 1,
    "INTERRUPTED": 130,
}


class SimulationInputError(ValueError):
    """Raised when a scenario or runner input cannot be trusted."""


class _DuplicateJsonKey(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _safe_id(value: str) -> bool:
    return bool(value) and value.isascii() and all(character.isalnum() or character in "._-" for character in value)


def _command_is_available(command: Sequence[str] | None) -> bool:
    if not command or not command[0] or "\x00" in command[0]:
        return False
    executable = Path(command[0])
    if executable.is_absolute() or executable.parent != Path("."):
        return executable.is_file()
    return shutil.which(command[0]) is not None


@dataclass(frozen=True)
class Scenario:
    """A manifest plus its deterministic, non-Gazebo scene projection."""

    path: Path
    manifest: dict[str, Any]
    raw_bytes: bytes
    scene: dict[str, Any]

    @property
    def scenario_id(self) -> str:
        return self.manifest["scenario_id"]

    @property
    def scene_hash(self) -> str:
        return canonical_hash(self.scene)


@dataclass
class RunResult:
    scenario_id: str
    status: str
    exit_code: int
    run_id: str
    artifact_dir: str
    release_eligible: bool
    executed: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "run_id": self.run_id,
            "artifact_dir": self.artifact_dir,
            "release_eligible": self.release_eligible,
            "executed": self.executed,
            "reason": self.reason,
        }


def _read_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SimulationInputError(f"cannot read scenario manifest {path}: {exc}") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise SimulationInputError(f"scenario manifest exceeds {MAX_MANIFEST_BYTES} bytes: {path}")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates)
    except (_DuplicateJsonKey, UnicodeError, json.JSONDecodeError) as exc:
        raise SimulationInputError(f"invalid scenario manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SimulationInputError(f"scenario manifest must be an object: {path}")
    try:
        validate_simulation_manifest(payload)
        materialize_scenario(payload)
    except (ValueError, KeyError) as exc:
        raise SimulationInputError(f"invalid scenario manifest {path}: {exc}") from exc
    scenario_id = payload.get("scenario_id")
    if not isinstance(scenario_id, str) or not _safe_id(scenario_id):
        raise SimulationInputError(f"scenario_id must be filesystem-safe: {scenario_id!r}")
    return payload, raw


def load_scenarios(paths: Iterable[Path] | None = None) -> list[Scenario]:
    """Load and validate manifests, rejecting duplicate IDs and duplicate JSON keys."""

    candidates = sorted(paths if paths is not None else SCENARIO_ROOT.rglob("*.json"))
    if not candidates:
        raise SimulationInputError(f"no scenario manifests found under {SCENARIO_ROOT}")
    scenarios: list[Scenario] = []
    seen_ids: dict[str, Path] = {}
    for path in candidates:
        manifest, raw = _read_manifest(path)
        scenario_id = manifest["scenario_id"]
        if scenario_id in seen_ids:
            raise SimulationInputError(f"duplicate scenario_id {scenario_id!r} in {seen_ids[scenario_id]} and {path}")
        seen_ids[scenario_id] = path
        scenarios.append(Scenario(path, manifest, raw, materialize_scenario(manifest)))
    return scenarios


def scenario_catalog(scenarios: Iterable[Scenario]) -> list[dict[str, Any]]:
    return [
        {
            "scenario_id": scenario.scenario_id,
            "path": str(scenario.path.relative_to(ROOT)),
            "seed": scenario.manifest["seed"],
            "task_id": scenario.manifest["task_id"],
            "world_version": scenario.manifest["world_version"],
            "fault_type": scenario.manifest["fault_type"],
            "timeout_s": scenario.manifest["timeout_s"],
            "scene_hash": scenario.scene_hash,
        }
        for scenario in scenarios
    ]


def doctor(*, require_gazebo: bool = False) -> tuple[dict[str, Any], int]:
    """Return dependency diagnostics without starting a simulator."""

    scenarios: list[Scenario] = []
    manifest_error: str | None = None
    try:
        scenarios = load_scenarios()
    except SimulationInputError as exc:
        manifest_error = str(exc)
    configured = os.environ.get("WORKBENCH_GAZEBO_COMMAND", "").strip()
    configured_command: list[str] | None = None
    command_error: str | None = None
    if configured:
        try:
            configured_command = shlex.split(configured, posix=os.name != "nt")
        except ValueError as exc:
            command_error = str(exc)
    command_available = _command_is_available(configured_command)
    simulator_ready = manifest_error is None and command_error is None and command_available
    report = {
        "status": "invalid" if manifest_error or command_error else "ready" if simulator_ready else "not_ready",
        "python": sys.executable,
        "ros2": shutil.which("ros2"),
        "gz": shutil.which("gz"),
        "gazebo_command_configured": bool(configured),
        "gazebo_command_available": command_available,
        "process_tree_cleanup": "windows-taskkill" if os.name == "nt" else "posix-process-group",
        "scenario_count": len(scenarios),
        "manifest_error": manifest_error or command_error,
        "simulator_ready": simulator_ready,
        "note": "doctor is diagnostic only; it never launches Gazebo",
    }
    if require_gazebo and not report["simulator_ready"]:
        return report, 2
    return report, 0


def _format_command(
    command: Sequence[str], *, manifest: Path, output: Path, seed: int, version: str, run_dir: Path
) -> list[str]:
    if (
        not command
        or len(command) > MAX_COMMAND_TOKENS
        or any(not isinstance(token, str) or not token for token in command)
    ):
        raise SimulationInputError("runner command must contain 1-64 argv tokens")
    substitutions = {
        "manifest": str(manifest.resolve()),
        "output": str(output.resolve()),
        "seed": str(seed),
        "version": version,
        "run_dir": str(run_dir.resolve()),
    }
    formatted: list[str] = []
    for token in command:
        try:
            formatted.append(token.format(**substitutions))
        except (KeyError, ValueError) as exc:
            raise SimulationInputError(f"invalid runner command token {token!r}: {exc}") from exc
    return formatted


def _default_command(runner: str) -> list[str] | None:
    configured = os.environ.get("WORKBENCH_GAZEBO_COMMAND", "").strip()
    if not configured:
        return None
    try:
        return shlex.split(configured, posix=os.name != "nt")
    except ValueError as exc:
        raise SimulationInputError(f"invalid WORKBENCH_GAZEBO_COMMAND: {exc}") from exc


def _terminate_process_tree(process: subprocess.Popen[Any], grace_s: float = 2.0) -> None:
    """Stop a launched process and its descendants, best effort but bounded."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            text=True,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.terminate()
            except OSError:
                pass
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                text=True,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            pass


def _wait_bounded(
    process: subprocess.Popen[Any],
    *,
    timeout_s: int,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[int | None, bool, bool]:
    """Wait while bounding wall time and captured log growth."""

    deadline = time.monotonic() + timeout_s
    while process.poll() is None:
        if stdout_path.stat().st_size > MAX_RUNNER_LOG_BYTES or stderr_path.stat().st_size > MAX_RUNNER_LOG_BYTES:
            _terminate_process_tree(process)
            _truncate_logs(stdout_path, stderr_path)
            return process.poll(), False, True
        if time.monotonic() >= deadline:
            _terminate_process_tree(process)
            return process.poll(), True, False
        time.sleep(0.05)
    exceeded = stdout_path.stat().st_size > MAX_RUNNER_LOG_BYTES or stderr_path.stat().st_size > MAX_RUNNER_LOG_BYTES
    if exceeded:
        _truncate_logs(stdout_path, stderr_path)
    return process.returncode, False, exceeded


def _truncate_logs(stdout_path: Path, stderr_path: Path) -> None:
    for path in (stdout_path, stderr_path):
        if path.stat().st_size <= MAX_RUNNER_LOG_BYTES:
            continue
        with path.open("r+b") as stream:
            stream.truncate(MAX_RUNNER_LOG_BYTES)


def _write_checksums(directory: Path) -> None:
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.name != "checksums.sha256")
    content = "".join(f"{_sha256(path)}  {path.name}\n" for path in files)
    (directory / "checksums.sha256").write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.write_bytes(_json_bytes(payload))


def _publish_artifact(staging: Path, final: Path) -> None:
    if final.exists():
        raise SimulationInputError(f"refusing to overwrite existing run artifact: {final}")
    try:
        os.replace(staging, final)
    except OSError as exc:
        raise SimulationInputError(f"could not atomically publish run artifact {final}: {exc}") from exc


def _result_from_metadata(metadata: dict[str, Any], final: Path) -> RunResult:
    status = metadata["status"]
    return RunResult(
        scenario_id=metadata["scenario_id"],
        status=status,
        exit_code=EXIT_CODES[status],
        run_id=metadata["run_id"],
        artifact_dir=str(final),
        release_eligible=bool(metadata["release_eligible"]),
        executed=bool(metadata["executed"]),
        reason=metadata.get("reason"),
    )


def _identity_entry(scenario: Scenario) -> dict[str, Any]:
    """Resolve the registry definition a scenario belongs to (Issue #302).

    A manifest under ``sim/registry`` is already registry-native. A legacy
    fixture under ``sim/scenarios`` names a ``task_id`` that Issue #301 mapped to
    an identity. Either way the definition comes from the registry reader, so the
    identity is a property of the scenario, not of the caller's path.
    """

    registry = load_registry(REGISTRY_ROOT, repo_root=ROOT)
    by_identity = {entry.identity: entry.as_dict() for entry in registry.entries}
    by_scenario_id: dict[str, list[dict[str, Any]]] = {}
    for entry in by_identity.values():
        by_scenario_id.setdefault(entry["scenario_id"], []).append(entry)

    version = scenario.manifest.get("scenario_version")
    if isinstance(version, str) and version.strip():
        identity = f"{scenario.scenario_id}@{version}"
        if identity in by_identity:
            return by_identity[identity]
        raise SimulationInputError(f"{identity} is not a registered scenario; runs require a registry definition")

    task_id = scenario.manifest.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        try:
            family = resolve_task_id(task_id)
        except ScenarioMigrationError as error:
            raise SimulationInputError(f"task_id {task_id!r} does not resolve to a registry identity") from error
        if family.identity in by_identity:
            return by_identity[family.identity]

    candidates = by_scenario_id.get(scenario.scenario_id, [])
    if len(candidates) == 1:
        return candidates[0]
    raise SimulationInputError(
        f"{scenario.scenario_id} does not resolve to exactly one registered scenario; "
        "runs require a registry definition"
    )


def _record_identity(metadata: dict[str, Any], scenario: Scenario, events_path: Path) -> None:
    """Attach the Issue #302 identity block to a run's metadata."""

    entry = _identity_entry(scenario)
    stream_hash = hash_events_file(events_path) if events_path.is_file() else event_stream_hash(())
    identity = identity_from_entry(
        entry,
        event_stream_hash_value=stream_hash,
        config_hash=scenario.scene_hash or "none",
        commit=metadata.get("commit") or "unspecified",
    )
    metadata["identity"] = identity.as_dict()
    metadata["registry_identity"] = identity.identity
    metadata["evidence_policy_version"] = identity.material["evidence_policy_version"]
    metadata["verifier_rule_version"] = identity.material["verifier_rule_version"]
    metadata["event_stream_hash"] = identity.material["event_stream_hash"]


def run_scenario(
    scenario: Scenario,
    *,
    runner: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    version: str = "sim-cli",
    command: Sequence[str] | None = None,
    seed_base: int = 1000,
) -> RunResult:
    """Run one scenario and atomically publish a bounded evidence directory."""

    if runner not in {"scripted", "gazebo", "external"}:
        raise SimulationInputError(f"unsupported runner: {runner}")
    if not _safe_id(version):
        raise SimulationInputError(f"version must be ASCII and filesystem-safe: {version!r}")
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{scenario.scenario_id}-{uuid.uuid4().hex[:12]}"
    staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.", suffix=".partial", dir=output_dir))
    final = output_dir / run_id
    stdout_path = staging / "stdout.log"
    stderr_path = staging / "stderr.log"
    events_path = staging / "events.jsonl"
    effective_seed = seed_base + scenario.manifest["seed"]
    metadata: dict[str, Any] = {
        "schema_version": "sim-run-v1",
        "run_id": run_id,
        "scenario_id": scenario.scenario_id,
        "runner": runner,
        "version": version,
        "seed": effective_seed,
        "manifest_seed": scenario.manifest["seed"],
        "world_version": scenario.manifest["world_version"],
        "fault_type": scenario.manifest["fault_type"],
        "timeout_s": scenario.manifest["timeout_s"],
        "scene_hash": scenario.scene_hash,
        "commit": _git_commit(),
        "release_eligible": False,
        "executed": False,
        "status": "NOT_EXECUTED",
        "exit_code": EXIT_CODES["NOT_EXECUTED"],
        "process_exit_code": None,
        "timed_out": False,
        "started_at": datetime.now(UTC).isoformat(),
        "command": None,
        "reason": None,
    }
    try:
        (staging / "source-manifest.json").write_bytes(scenario.raw_bytes)
        _write_json(staging / "scene.json", scenario.scene)
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        if runner == "scripted":
            from run_evaluation import scripted_events, validate_event_log, write_jsonl

            events = scripted_events(version, scenario.manifest, metadata["commit"], seed_base)
            write_jsonl(events_path, events)
            validate_event_log(
                events_path,
                f"{version}--{scenario.scenario_id}",
                scenario_id=scenario.scenario_id,
                seed=effective_seed,
                commit=metadata["commit"],
            )
            metadata.update(
                {
                    "status": "SCRIPTED_FIXTURE",
                    "exit_code": EXIT_CODES["SCRIPTED_FIXTURE"],
                    "executed": False,
                    "reason": "scripted fixture; not Gazebo or hardware evidence",
                }
            )
        else:
            selected_command = list(command) if command else _default_command(runner)
            if not selected_command:
                metadata["reason"] = (
                    "no Gazebo adapter configured; set WORKBENCH_GAZEBO_COMMAND or pass --command"
                    if runner == "gazebo"
                    else "external runner command is required"
                )
            else:
                formatted = _format_command(
                    selected_command,
                    manifest=staging / "source-manifest.json",
                    output=events_path,
                    seed=effective_seed,
                    version=version,
                    run_dir=staging,
                )
                metadata["command"] = formatted
                creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
                with (
                    stdout_path.open("w", encoding="utf-8") as stdout,
                    stderr_path.open("w", encoding="utf-8") as stderr,
                ):
                    try:
                        process = subprocess.Popen(
                            formatted,
                            cwd=ROOT,
                            stdin=subprocess.DEVNULL,
                            stdout=stdout,
                            stderr=stderr,
                            text=True,
                            start_new_session=os.name != "nt",
                            creationflags=creation_flags,
                        )
                    except (FileNotFoundError, OSError) as exc:
                        metadata["reason"] = f"runner could not start: {exc}"
                    else:
                        metadata["executed"] = True
                        try:
                            return_code, timed_out, output_exceeded = _wait_bounded(
                                process,
                                timeout_s=scenario.manifest["timeout_s"],
                                stdout_path=stdout_path,
                                stderr_path=stderr_path,
                            )
                        except KeyboardInterrupt:
                            _terminate_process_tree(process)
                            metadata.update(
                                {
                                    "status": "INTERRUPTED",
                                    "exit_code": EXIT_CODES["INTERRUPTED"],
                                    "reason": "runner interrupted; process tree terminated",
                                }
                            )
                        else:
                            metadata["process_exit_code"] = return_code
                            if output_exceeded:
                                metadata.update(
                                    {
                                        "status": "FAILED",
                                        "reason": f"runner logs exceeded {MAX_RUNNER_LOG_BYTES} bytes",
                                    }
                                )
                            elif timed_out:
                                metadata["timed_out"] = True
                                metadata.update(
                                    {
                                        "status": "TIMED_OUT",
                                        "reason": (
                                            f"runner exceeded manifest timeout of {scenario.manifest['timeout_s']}s"
                                        ),
                                    }
                                )
                            elif return_code != 0:
                                metadata.update(
                                    {
                                        "status": "FAILED",
                                        "reason": f"runner exited with status {return_code}",
                                    }
                                )
                            elif not events_path.is_file() or events_path.stat().st_size == 0:
                                metadata.update(
                                    {
                                        "status": "INVALID_OUTPUT",
                                        "reason": "runner exited successfully but did not produce events.jsonl",
                                    }
                                )
                            else:
                                try:
                                    from run_evaluation import validate_event_log

                                    validate_event_log(
                                        events_path,
                                        f"{version}--{scenario.scenario_id}",
                                        scenario_id=scenario.scenario_id,
                                        seed=effective_seed,
                                        commit=metadata["commit"],
                                    )
                                except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
                                    metadata.update(
                                        {
                                            "status": "INVALID_OUTPUT",
                                            "reason": f"event log failed validation: {exc}",
                                        }
                                    )
                                else:
                                    metadata.update(
                                        {
                                            "status": "EXECUTED",
                                            "reason": "external runner completed and event log validated",
                                        }
                                    )
        try:
            _record_identity(metadata, scenario, events_path)
        except RunIdentityError as error:
            raise SimulationInputError(f"run identity could not be recorded: {error}") from error
        metadata["exit_code"] = EXIT_CODES[metadata["status"]]
        metadata["evidence_paths"] = [
            "source-manifest.json",
            "scene.json",
            "events.jsonl" if events_path.is_file() else None,
            "stdout.log",
            "stderr.log",
            "identity.json",
            "metadata.json",
            "checksums.sha256",
        ]
        metadata["evidence_paths"] = [path for path in metadata["evidence_paths"] if path is not None]
        metadata["finished_at"] = datetime.now(UTC).isoformat()
        _write_json(staging / "metadata.json", metadata)
        _write_json(staging / "identity.json", metadata["identity"])
        _write_checksums(staging)
        _publish_artifact(staging, final)
        return _result_from_metadata(metadata, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def run_scenarios(
    scenarios: Sequence[Scenario],
    *,
    runner: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    version: str = "sim-cli",
    command: Sequence[str] | None = None,
) -> dict[str, Any]:
    if not scenarios:
        raise SimulationInputError("at least one scenario is required")
    results = [
        run_scenario(scenario, runner=runner, output_dir=output_dir, version=version, command=command)
        for scenario in scenarios
    ]
    counts = {status: sum(result.status == status for result in results) for status in sorted(RUNNER_STATUSES)}
    executed_count = counts["EXECUTED"]
    summary = {
        "schema_version": "sim-summary-v1",
        "runner": runner,
        "version": version,
        "scenario_count": len(results),
        "executed_count": executed_count,
        "scripted_count": counts["SCRIPTED_FIXTURE"],
        "not_executed_count": counts["NOT_EXECUTED"],
        "failed_count": counts["FAILED"] + counts["TIMED_OUT"] + counts["INVALID_OUTPUT"] + counts["INTERRUPTED"],
        "release_eligible": False,
        "counts": counts,
        "results": [result.as_dict() for result in results],
    }
    return summary


def _print_json_or_text(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif isinstance(payload, dict) and "results" in payload:
        print(
            f"runner={payload['runner']} scenarios={payload['scenario_count']} "
            f"executed={payload['executed_count']} scripted={payload['scripted_count']} "
            f"not_executed={payload['not_executed_count']} failed={payload['failed_count']}"
        )
        for result in payload["results"]:
            reason = f" - {result['reason']}" if result.get("reason") else ""
            print(f"[{result['status']}] {result['scenario_id']} ({result['artifact_dir']}){reason}")
    elif isinstance(payload, dict) and "scenario_count" in payload:
        print(
            f"scenarios={payload['scenario_count']} simulator_ready={payload['simulator_ready']} "
            f"gazebo_command_configured={payload['gazebo_command_configured']}"
        )
        if payload.get("manifest_error"):
            print(f"manifest_error: {payload['manifest_error']}")
    else:
        for item in payload if isinstance(payload, list) else [payload]:
            print(
                "{scenario_id} task={task_id} fault={fault_type} timeout={timeout_s}s seed={seed} "
                "world={world_version} scene_hash={scene_hash}".format(**item)
            )


def _parse_registry_identity(token: str) -> tuple[str, str | None]:
    """Split ``id@version``. A bare ID leaves the version to the registry."""

    scenario_id, separator, version = token.partition("@")
    if not scenario_id:
        raise SimulationInputError(f"invalid scenario identity: {token!r}")
    if separator and not version:
        raise SimulationInputError(f"invalid scenario identity: {token!r}")
    return scenario_id, version or None


def _registry_command(args: argparse.Namespace) -> int:
    """List or describe registry manifests without creating a run.

    A registry failure is reported as NOT_EXECUTED with the stable diagnostic
    code rather than as a traceback, because an empty or partial catalog is a
    refusal, not a result.
    """

    try:
        registry = load_registry(REGISTRY_ROOT, repo_root=ROOT)
    except (ScenarioRegistryError, ContractError) as exc:
        code = getattr(exc, "code", "SCENARIO_REGISTRY_INVALID")
        print(f"NOT_EXECUTED: {code}: {exc}", file=sys.stderr)
        return 2

    if args.subcommand == "registry-list":
        payload = {
            "contract_version": registry.contract_version,
            "scenario_count": len(registry),
            "scenarios": registry.catalog(),
        }
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
            return 0
        for entry in registry.entries:
            print(
                f"{entry.identity} evidence={entry.evidence_status} "
                f"release_eligible={str(entry.release_eligible).lower()} "
                f"executable={str(entry.executable).lower()} actions={','.join(entry.semantic_actions)}"
            )
        return 0

    scenario_id, version = _parse_registry_identity(args.scenario)
    try:
        payload = registry.describe(scenario_id, version)
    except ScenarioRegistryError as exc:
        print(f"NOT_EXECUTED: {exc.code}: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    print(f"{payload['identity']} ({payload['path']})")
    print(f"goal: {payload['goal']}")
    print(f"actions: {', '.join(payload['semantic_actions'])}")
    print(f"adapters: {', '.join(payload['required_adapters'])}")
    print(f"verifier: {payload['verifier']}")
    print(f"evidence_status: {payload['evidence_status']}")
    print(f"release_eligible: {str(payload['release_eligible']).lower()}")
    print(f"executable: {str(payload['executable']).lower()}")
    for notice in payload["notices"]:
        print(f"notice: {notice}")
    return 0


def _migration_command(args: argparse.Namespace) -> int:
    """Report the legacy task_id -> registry identity migration without creating a run."""

    try:
        report = migration_report(ROOT, registry_root=REGISTRY_ROOT)
    except (ScenarioMigrationError, ScenarioRegistryError, ContractError) as exc:
        code = getattr(exc, "code", "SCENARIO_MIGRATION_MISMATCH")
        print(f"NOT_EXECUTED: {code}: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return 0 if report["ok"] else 1

    for family in report["families"]:
        print(
            f"{family['task_id']} -> {family['identity']} "
            f"registered={str(family['registered']).lower()} "
            f"corpus={family['actual_corpus_count']} fixtures"
        )
    for finding in report["findings"]:
        print(f"{finding['code']} {finding['path']}: {finding['detail']}", file=sys.stderr)
    print(
        f"{'PASS' if report['ok'] else 'FAIL'}: {len(report['families'])} famil(ies); "
        f"authoritative from {report['authoritative_release']}, "
        f"task_id entry points removed in {report['deprecation_release']}"
    )
    return 0 if report["ok"] else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run truthful Workbench simulation probes")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="diagnose simulator readiness without launching it")
    doctor_parser.add_argument("--require-gazebo", action="store_true")
    doctor_parser.add_argument("--json", action="store_true", dest="as_json")

    registry_list = subparsers.add_parser("registry-list", help="list registry manifests by stable ID and version")
    registry_list.add_argument("--json", action="store_true", dest="as_json")

    registry_describe = subparsers.add_parser("describe", help="describe one registered scenario by ID and version")
    registry_describe.add_argument("scenario", help="scenario_id, or scenario_id@scenario_version")
    registry_describe.add_argument("--json", action="store_true", dest="as_json")

    migration_parser = subparsers.add_parser(
        "migration", help="show the legacy task_id to registry identity migration (#301)"
    )
    migration_parser.add_argument("--json", action="store_true", dest="as_json")

    list_parser = subparsers.add_parser("list", help="list validated scenarios and deterministic scene hashes")
    list_parser.add_argument("--json", action="store_true", dest="as_json")
    list_parser.add_argument(
        "--expanded", action="store_true", help="include expanded scenarios (default includes all)"
    )

    run_parser = subparsers.add_parser("run", help="run one or more scenarios")
    run_parser.add_argument("scenario_ids", nargs="*", help="scenario IDs; omit with --all")
    run_parser.add_argument("--all", action="store_true", help="run every frozen and expanded scenario")
    run_parser.add_argument("--runner", choices=("gazebo", "external", "scripted"), default="gazebo")
    run_parser.add_argument(
        "--command", nargs="+", help="argv tokens; placeholders: {manifest} {output} {seed} {version} {run_dir}"
    )
    run_parser.add_argument("--version", default="sim-cli")
    run_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    run_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.subcommand == "doctor":
            report, exit_code = doctor(require_gazebo=args.require_gazebo)
            _print_json_or_text(report, args.as_json)
            return exit_code
        if args.subcommand in {"registry-list", "describe"}:
            return _registry_command(args)
        if args.subcommand == "migration":
            return _migration_command(args)
        scenarios = load_scenarios()
        if args.subcommand == "list":
            if args.expanded:
                scenarios = [scenario for scenario in scenarios if scenario.path.parent.name == "expanded"]
            _print_json_or_text(scenario_catalog(scenarios), args.as_json)
            return 0
        if args.scenario_ids and args.all:
            raise SimulationInputError("choose scenario IDs or --all, not both")
        if not args.scenario_ids and not args.all:
            raise SimulationInputError("provide at least one scenario ID or --all")
        selected = (
            scenarios if args.all else [scenario for scenario in scenarios if scenario.scenario_id in args.scenario_ids]
        )
        selected_ids = {scenario.scenario_id for scenario in selected}
        missing = [scenario_id for scenario_id in args.scenario_ids if scenario_id not in selected_ids]
        if missing:
            raise SimulationInputError(f"unknown scenario ID(s): {', '.join(missing)}")
        summary = run_scenarios(
            selected,
            runner=args.runner,
            output_dir=args.output_dir,
            version=args.version,
            command=args.command,
        )
        _print_json_or_text(summary, args.as_json)
        if summary["failed_count"]:
            return 1
        if summary["not_executed_count"]:
            return 2
        return 0
    except SimulationInputError as exc:
        print(f"NOT_EXECUTED: {exc}", file=sys.stderr)
        return 2
    except (ScenarioRegistryError, ContractError) as exc:
        code = getattr(exc, "code", "SCENARIO_REGISTRY_INVALID")
        print(f"NOT_EXECUTED: {code}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
