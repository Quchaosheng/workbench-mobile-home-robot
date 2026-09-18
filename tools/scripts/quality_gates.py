"""Property-corpus and mutation gates for the fail-closed boundaries (Issue #89).

Two gates live here, and both are deliberately boring:

``property``
    Runs the seeded property suites and archives a machine-readable summary of the
    generator version, the case schema version, every seed, the per-suite case
    count, the corpus digest and the measured duration.  A corpus smaller than
    the committed minimum is an INCOMPLETE run, not a fast one.

``mutation``
    Copies the repository into a throwaway directory, neuters exactly one
    rejection or threshold inside that copy, and requires the named tests to fail
    there while the unmutated copy passes.  The repository itself is never
    written to, and the gate confirms that by hashing the files it touched before
    and after.

Exit codes are the contract: 0 PASS, 1 FAIL, 2 INCOMPLETE.  Nothing here retries
anything, and a step that could not run at all is never reported as a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from _paths import ROOT

PASS = 0
FAIL = 1
INCOMPLETE = 2

# Each suite file declares one logical SUITE name, and the archive is keyed on
# that name rather than the file name so a rename cannot silently drop a suite.
PROPERTY_SUITES = {
    "tests/property/test_property_event_ordering.py": "event_ordering",
    "tests/property/test_property_schema_validation.py": "schema_validation",
    "tests/property/test_property_policy_rejection.py": "policy_rejection",
    "tests/property/test_property_frame_decoding.py": "frame_decoding",
    "tests/property/test_property_state_transitions.py": "state_transitions",
}

# The suites cover five boundaries.  The registry must not quietly lose one.
REQUIRED_BOUNDARIES = frozenset(
    {"event ordering", "schema validation", "policy rejection", "frame decoding", "state transitions"}
)

MINIMUM_CASES_PER_SUITE = 120
DEFAULT_TIME_BUDGET_S = 900.0
GENERATOR_VERSION = "workbench-property-generator-v1"
CASE_SCHEMA_VERSION = "workbench-property-case-v1"

ARCHIVE_ENVIRONMENT_VARIABLE = "WORKBENCH_QUALITY_ARCHIVE"

# A copy carries everything needed to run the suites and nothing that costs
# minutes to duplicate.  Sockets and the installed environment are excluded on
# purpose: the sandbox supplies its own import paths.
COPY_EXCLUDES = (
    ".git",
    ".venv",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    "site",
    ".benchmarks",
    "node_modules",
    "build",
    "install",
    "log",
)

SANDBOX_IMPORT_LAYOUT = {
    "workbench.application": "libs/application/workbench/application",
    "workbench.hardware": "libs/hardware/workbench/hardware",
    "workbench.kernel": "libs/kernel/workbench/kernel",
}

SANDBOX_PATH_ENTRIES = (
    "",
    "libs/contracts",
    "libs/kernel",
    "libs/hardware",
    "libs/application",
    "libs/task_utils",
    "services/agent_runtime",
    "services/backend",
    "services/world_model",
    "firmware/virtual_mcu",
)

# The sandbox must resolve every gate-relevant package inside the copy.  If a
# package escapes to the real checkout, a probe would test unmutated code and
# report a false pass, so the gate refuses to run in that case.
SANDBOX_GUARD_MODULES = (
    "workbench.kernel.event_store",
    "workbench.kernel.lifecycle",
    "workbench.hardware.can_driver_safe",
    "workbench_contracts",
    "workbench_world_model.reducer",
    "workbench_world_model.event_payloads",
    "workbench_agent_runtime.policy_validator",
    "workbench_virtual_mcu.state_machine",
)

SANDBOX_SITE_CUSTOMIZE = '''\
"""Sandbox-only import isolation written by tools/scripts/quality_gates.py."""

import importlib.util
import os
import sys

_SANDBOX = os.environ["WORKBENCH_SANDBOX_ROOT"]
_LAYOUT = __LAYOUT__


class _SandboxFinder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        base = _LAYOUT.get(fullname)
        if base is not None:
            directory = os.path.join(_SANDBOX, base)
            return importlib.util.spec_from_file_location(
                fullname,
                os.path.join(directory, "__init__.py"),
                submodule_search_locations=[directory],
            )
        parent, _, child = fullname.rpartition(".")
        if parent in _LAYOUT:
            directory = os.path.join(_SANDBOX, _LAYOUT[parent])
            module = os.path.join(directory, f"{child}.py")
            if os.path.exists(module):
                return importlib.util.spec_from_file_location(fullname, module)
            package = os.path.join(directory, child, "__init__.py")
            if os.path.exists(package):
                return importlib.util.spec_from_file_location(
                    fullname, package, submodule_search_locations=[os.path.join(directory, child)]
                )
        return None


sys.meta_path.insert(0, _SandboxFinder)
sys.meta_path[:] = [finder for finder in sys.meta_path if "__editable__" not in type(finder).__module__]
'''.replace("__LAYOUT__", repr(SANDBOX_IMPORT_LAYOUT))

FAILED_LINE = re.compile(r"^FAILED\s+(\S+)")
COUNT_LINE = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")


class GateError(RuntimeError):
    """The gate cannot produce a trustworthy verdict."""


@dataclass
class GateResult:
    gate: str
    status: str
    exit_code: int
    duration_s: float
    details: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3),
            "details": self.details,
            **self.payload,
        }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot read {path}: {error}") from error
    if not isinstance(payload, dict):
        raise GateError(f"{path} must contain a JSON object")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value, label in COUNT_LINE.findall(output):
        normalized = {"error": "errors"}.get(label, label)
        counts[normalized] = int(value)
    return counts


def _parse_failures(output: str) -> list[str]:
    return sorted({match.group(1) for line in output.splitlines() if (match := FAILED_LINE.match(line))})


def _run_pytest(
    targets: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", *targets, "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise GateError(f"pytest exceeded its {timeout_s:g}s budget") from error
    return completed, time.monotonic() - started


def _verify_archive(payload: dict[str, Any], expected_suites: frozenset[str]) -> list[str]:
    """Return the reasons the archived corpus is not acceptable evidence."""

    problems: list[str] = []
    suites = payload.get("suites")
    if not isinstance(suites, list) or not suites:
        return ["the archive contains no suites"]

    names = {suite.get("suite") for suite in suites if isinstance(suite, dict)}
    missing = expected_suites - names
    if missing:
        problems.append(f"the archive is missing suites: {sorted(missing)}")

    for suite in suites:
        if not isinstance(suite, dict):
            problems.append("the archive contains a non-object suite entry")
            continue
        name = suite.get("suite")
        if suite.get("generator_version") != GENERATOR_VERSION:
            problems.append(f"{name}: generator_version is not {GENERATOR_VERSION}")
        if suite.get("case_schema_version") != CASE_SCHEMA_VERSION:
            problems.append(f"{name}: case_schema_version is not {CASE_SCHEMA_VERSION}")
        if not suite.get("seeds"):
            problems.append(f"{name}: no seeds were recorded")
        case_count = suite.get("case_count")
        if not isinstance(case_count, int) or case_count < MINIMUM_CASES_PER_SUITE:
            problems.append(f"{name}: case_count {case_count!r} is below the committed minimum")
        digest = suite.get("corpus_digest")
        if not isinstance(digest, str) or len(digest) != 64:
            problems.append(f"{name}: corpus_digest is not a sha256 hex digest")
        if suite.get("failures"):
            problems.append(f"{name}: the archive records {len(suite['failures'])} failing cases")
    return problems


def run_property_gate(*, archive: Path, time_budget_s: float) -> GateResult:
    """Run the seeded property suites and archive their corpus summary."""

    started = time.monotonic()
    details: list[str] = []

    missing_suites = [suite for suite in PROPERTY_SUITES if not (ROOT / suite).is_file()]
    if missing_suites:
        return GateResult(
            gate="property",
            status="INCOMPLETE",
            exit_code=INCOMPLETE,
            duration_s=time.monotonic() - started,
            details=[f"missing property suites: {missing_suites}"],
        )

    if archive.exists():
        archive.unlink()
    archive.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env[ARCHIVE_ENVIRONMENT_VARIABLE] = str(archive)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        completed, duration = _run_pytest(
            list(PROPERTY_SUITES),
            cwd=ROOT,
            env=env,
            timeout_s=time_budget_s,
        )
    except GateError as error:
        return GateResult(
            gate="property",
            status="INCOMPLETE",
            exit_code=INCOMPLETE,
            duration_s=time.monotonic() - started,
            details=[str(error)],
        )

    counts = _parse_counts(completed.stdout + completed.stderr)
    failures = _parse_failures(completed.stdout)
    if failures:
        details.append(f"{len(failures)} property tests failed")
        details.extend(failures[:10])

    if not archive.exists():
        details.append(f"the suites did not write {archive}")
        return GateResult(
            gate="property",
            status="INCOMPLETE",
            exit_code=INCOMPLETE,
            duration_s=duration,
            details=details,
            payload={"pytest_returncode": completed.returncode, "counts": counts},
        )

    try:
        payload = _load_json(archive)
    except GateError as error:
        return GateResult(
            gate="property",
            status="INCOMPLETE",
            exit_code=INCOMPLETE,
            duration_s=duration,
            details=[*details, str(error)],
        )

    problems = _verify_archive(payload, frozenset(PROPERTY_SUITES.values()))
    details.extend(problems)

    if completed.returncode != 0 or failures or problems:
        if not problems:
            details.append(f"pytest returned {completed.returncode}")
        return GateResult(
            gate="property",
            status="FAIL",
            exit_code=FAIL,
            duration_s=duration,
            details=details,
            payload={"pytest_returncode": completed.returncode, "counts": counts, "archive": str(archive)},
        )

    return GateResult(
        gate="property",
        status="PASS",
        exit_code=PASS,
        duration_s=duration,
        details=[f"archived {len(payload.get('suites', []))} suites to {archive}"],
        payload={"pytest_returncode": 0, "counts": counts, "archive": str(archive)},
    )


def _write_property_archive(archive: Path, result: GateResult) -> None:
    """Add the gate verdict to the corpus archive without discarding the corpus.

    The suites write the corpus summary; the gate adds its own verdict under a
    separate key so a reader can see both what was generated and whether the
    generation was accepted.
    """

    try:
        payload = _load_json(archive)
    except GateError:
        payload = {}
    payload["gate_result"] = result.as_dict()
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _copy_repository(destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in COPY_EXCLUDES}

    shutil.copytree(
        ROOT,
        destination,
        symlinks=False,
        ignore=ignore,
        ignore_dangling_symlinks=True,
    )


def _sandbox_environment(sandbox: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["WORKBENCH_SANDBOX_ROOT"] = str(sandbox)
    env["PYTHONPATH"] = ":".join(str(sandbox / entry) if entry else str(sandbox) for entry in SANDBOX_PATH_ENTRIES)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _sandbox_guard_script() -> str:
    modules = ", ".join(repr(name) for name in SANDBOX_GUARD_MODULES)
    return (
        "import importlib, os, sys\n"
        f"sandbox = os.environ['WORKBENCH_SANDBOX_ROOT']\n"
        f"escaped = []\n"
        f"for name in ({modules},):\n"
        "    module = importlib.import_module(name)\n"
        "    origin = getattr(module, '__file__', None) or ''\n"
        "    if not os.path.realpath(origin).startswith(os.path.realpath(sandbox) + os.sep):\n"
        "        escaped.append((name, origin))\n"
        "if escaped:\n"
        "    print('ESCAPED', escaped)\n"
        "    raise SystemExit(1)\n"
        "print('SANDBOX_OK')\n"
    )


def _protected_path_violation(registry: dict[str, Any], file_value: str) -> str | None:
    """Refuse a probe that names a path AGENTS.md keeps out of AI write tasks.

    A probe edits its target, even though the edit stays inside a throwaway copy.
    The registry therefore must not name firmware/ or robot/control/ at all; the
    declared exclusions are also checked so an entry cannot be smuggled in with a
    differently spelled prefix.
    """

    exclusions = registry.get("excluded_paths")
    if not isinstance(exclusions, dict) or not exclusions:
        return "the registry does not declare excluded_paths"

    for prefix in ("firmware/", "robot/control/"):
        if file_value.startswith(prefix):
            return f"{file_value} is inside {prefix!r}, which AGENTS.md keeps out of AI write tasks"
    return None


def _probe_tests(entry: dict[str, Any]) -> tuple[list[str], str | None]:
    tests = entry.get("tests")
    if not isinstance(tests, list) or not tests or any(not isinstance(item, str) for item in tests):
        return [], "tests must be a non-empty list of repository paths"
    missing = [test for test in tests if not (ROOT / test).is_file()]
    if missing:
        return [], f"tests do not exist: {missing}"
    return tests, None


def _load_quarantine(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return the active quarantine keyed by test id, plus validation problems."""

    problems: list[str] = []
    payload = _load_json(path)
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise GateError(f"{path} must contain an entries list")

    today = date.today()
    active: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"quarantine entry {index} is not an object")
            continue
        test = entry.get("test")
        owner = entry.get("owner")
        reason = entry.get("reason")
        expires = entry.get("expires")

        if not isinstance(test, str) or not test.strip():
            problems.append(f"quarantine entry {index} has no test id")
            continue

        entry_problems: list[str] = []
        if not isinstance(owner, str) or not owner.strip():
            entry_problems.append(f"{test}: quarantine needs an owner")
        if not isinstance(reason, str) or not reason.strip():
            entry_problems.append(f"{test}: quarantine needs a reason")
        try:
            expiry = date.fromisoformat(expires)
        except (TypeError, ValueError):
            entry_problems.append(f"{test}: quarantine expiry must be an ISO date")
        else:
            if expiry < today:
                entry_problems.append(f"{test}: quarantine expired on {expiry.isoformat()}")

        # A malformed entry never takes effect.  It is reported and the gate
        # stops, so a broken escape hatch cannot be mistaken for a working one.
        if entry_problems:
            problems.extend(entry_problems)
            continue
        active[test] = entry
    return active, problems


def _apply_mutation(path: Path, literal: str, replacement: str) -> str | None:
    """Apply one mutation, returning a problem description when it is not exact."""

    source = path.read_text(encoding="utf-8")
    occurrences = source.count(literal)
    if occurrences != 1:
        return f"literal occurs {occurrences} times in {path.name}; expected exactly once"
    mutated = source.replace(literal, replacement, 1)
    if mutated == source:
        return f"the replacement does not change {path.name}"
    path.write_text(mutated, encoding="utf-8")
    return None


def _check_quarantine_entries(
    active: dict[str, dict[str, Any]],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
) -> list[str]:
    """Return the entries that no longer describe a failing test.

    A quarantine is an escape hatch, so it is audited rather than trusted.  An
    entry whose test now passes is stale: it is hiding a green test from the gate
    and would let a real regression be quarantined without anyone noticing.
    """

    problems: list[str] = []
    for test in sorted(active):
        path, _, _node = test.partition("::")
        if not (cwd / path).is_file():
            problems.append(f"{test}: the quarantined test file does not exist")
            continue
        try:
            completed, _ = _run_pytest([test], cwd=cwd, env=env, timeout_s=timeout_s)
        except GateError as error:
            problems.append(f"{test}: {error}")
            continue
        owner = active[test].get("owner")
        if completed.returncode == 0:
            problems.append(f"{test}: quarantined but currently passing; remove the stale entry (owner {owner!r})")
        elif completed.returncode != 1:
            # pytest exits 1 when tests fail.  Any other non-zero code means the
            # run could not decide -- a usage error or an empty selection -- so
            # the entry is unaudited rather than confirmed failing.
            problems.append(
                f"{test}: quarantine could not be audited; pytest returned {completed.returncode} (owner {owner!r})"
            )
    return problems


def run_mutation_gate(
    *,
    registry_path: Path,
    quarantine_path: Path,
    archive: Path,
    time_budget_s: float,
    keep_sandbox: bool = False,
) -> GateResult:
    """Prove each registered rejection is load-bearing, inside a throwaway copy."""

    started = time.monotonic()
    details: list[str] = []

    try:
        registry = _load_json(registry_path)
        active_quarantine, quarantine_problems = _load_quarantine(quarantine_path)
    except GateError as error:
        return GateResult(
            gate="mutation",
            status="INCOMPLETE",
            exit_code=INCOMPLETE,
            duration_s=time.monotonic() - started,
            details=[str(error)],
        )

    mutations = registry.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        return GateResult(
            gate="mutation",
            status="INCOMPLETE",
            exit_code=INCOMPLETE,
            duration_s=time.monotonic() - started,
            details=["the registry contains no mutations"],
        )

    boundaries = {entry.get("boundary") for entry in mutations if isinstance(entry, dict)}
    missing_boundaries = REQUIRED_BOUNDARIES - boundaries
    if missing_boundaries:
        quarantine_problems.append(f"the registry does not cover every boundary: {sorted(missing_boundaries)}")

    records: list[dict[str, Any]] = []
    indeterminate: list[str] = []
    survived: list[str] = []

    # Hash every file the registry names before touching anything, so the gate can
    # prove afterwards that the real checkout was not modified.
    watched = sorted({str(entry["file"]) for entry in mutations if isinstance(entry, dict) and "file" in entry})
    before = {relative: _sha256(ROOT / relative) for relative in watched if (ROOT / relative).is_file()}

    budget_deadline = started + time_budget_s
    sandbox_root = Path(tempfile.mkdtemp(prefix="workbench-mutation-gate-"))
    sandbox = sandbox_root / "sandbox"
    try:
        try:
            _copy_repository(sandbox)
        except OSError as error:
            return GateResult(
                gate="mutation",
                status="INCOMPLETE",
                exit_code=INCOMPLETE,
                duration_s=time.monotonic() - started,
                details=[f"cannot create the sandbox copy: {error}"],
            )

        (sandbox / "sitecustomize.py").write_text(SANDBOX_SITE_CUSTOMIZE, encoding="utf-8")
        env = _sandbox_environment(sandbox)

        guard = subprocess.run(
            [sys.executable, "-c", _sandbox_guard_script()],
            cwd=sandbox,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if guard.returncode != 0 or "SANDBOX_OK" not in guard.stdout:
            return GateResult(
                gate="mutation",
                status="INCOMPLETE",
                exit_code=INCOMPLETE,
                duration_s=time.monotonic() - started,
                details=[
                    "the sandbox does not resolve every module under the copy, so a probe would test unmutated code",
                    guard.stdout.strip(),
                    guard.stderr.strip(),
                ],
            )
        details.append("sandbox import isolation verified for every gate-relevant module")

        if active_quarantine:
            stale = _check_quarantine_entries(
                active_quarantine,
                cwd=sandbox,
                env=env,
                timeout_s=max(30.0, budget_deadline - time.monotonic()),
            )
            quarantine_problems.extend(stale)
            if stale:
                details.append("quarantine contains entries that are no longer failing")

        for entry in mutations:
            if not isinstance(entry, dict):
                indeterminate.append("a registry entry is not an object")
                continue
            identifier = entry.get("id") or "<unnamed>"
            if time.monotonic() > budget_deadline:
                indeterminate.append(f"{identifier}: the time budget was exhausted before this probe ran")
                continue

            file_value = entry.get("file")
            literal = entry.get("literal")
            replacement = entry.get("replacement")
            if not all(isinstance(value, str) and value for value in (file_value, literal, replacement)):
                indeterminate.append(f"{identifier}: file, literal and replacement must all be non-empty strings")
                continue

            tests, problem = _probe_tests(entry)
            if problem is not None:
                indeterminate.append(f"{identifier}: {problem}")
                continue

            protected = _protected_path_violation(registry, file_value)
            if protected is not None:
                indeterminate.append(f"{identifier}: {protected}")
                continue

            quarantined = [test for test in tests if test in active_quarantine]
            if quarantined:
                indeterminate.append(f"{identifier}: named tests are quarantined: {quarantined}")
                continue

            target = sandbox / file_value
            if not target.is_file():
                indeterminate.append(f"{identifier}: {file_value} is not in the sandbox copy")
                continue

            original = target.read_text(encoding="utf-8")
            control, control_s = _run_pytest(
                tests,
                cwd=sandbox,
                env=env,
                timeout_s=max(30.0, budget_deadline - time.monotonic()),
            )
            if control.returncode != 0:
                indeterminate.append(
                    f"{identifier}: the named tests already fail before any mutation: "
                    f"{_parse_failures(control.stdout) or control.returncode}"
                )
                continue

            problem = _apply_mutation(target, literal, replacement)
            if problem is not None:
                indeterminate.append(f"{identifier}: {problem}")
                target.write_text(original, encoding="utf-8")
                continue

            mutated, mutated_s = _run_pytest(
                tests,
                cwd=sandbox,
                env=env,
                timeout_s=max(30.0, budget_deadline - time.monotonic()),
            )
            target.write_text(original, encoding="utf-8")

            caught = mutated.returncode != 0
            records.append(
                {
                    "id": identifier,
                    "boundary": entry.get("boundary"),
                    "file": file_value,
                    "tests": tests,
                    "status": "CAUGHT" if caught else "SURVIVED",
                    "control_duration_s": round(control_s, 3),
                    "mutated_duration_s": round(mutated_s, 3),
                    "mutated_failures": _parse_failures(mutated.stdout)[:10],
                }
            )
            if not caught:
                survived.append(identifier)

            if time.monotonic() > budget_deadline:
                indeterminate.append(f"{identifier}: the probe finished after the time budget was exhausted")
    finally:
        if not keep_sandbox:
            shutil.rmtree(sandbox_root, ignore_errors=True)

    after = {relative: _sha256(ROOT / relative) for relative in watched if (ROOT / relative).is_file()}
    repository_changed = sorted(
        relative for relative in set(before) | set(after) if before.get(relative) != after.get(relative)
    )
    if repository_changed:
        details.append(f"the probe modified the repository: {repository_changed}")
        indeterminate.extend(repository_changed)

    caught_count = sum(1 for record in records if record["status"] == "CAUGHT")
    payload = {
        "registry": str(registry_path),
        "quarantine": str(quarantine_path),
        "mutation_count": len(mutations),
        "caught_count": caught_count,
        "records": records,
        "indeterminate": indeterminate,
        "repository_unchanged": not repository_changed,
        "watched_files": {relative: before.get(relative) for relative in watched},
    }

    if indeterminate or quarantine_problems:
        details.extend(quarantine_problems)
        details.extend(indeterminate)
        result = GateResult(
            gate="mutation",
            status="INCOMPLETE",
            exit_code=INCOMPLETE,
            duration_s=time.monotonic() - started,
            details=details,
            payload=payload,
        )
    elif survived:
        details.append(f"{len(survived)} mutations were not detected: {survived}")
        result = GateResult(
            gate="mutation",
            status="FAIL",
            exit_code=FAIL,
            duration_s=time.monotonic() - started,
            details=details,
            payload=payload,
        )
    else:
        details.append(f"every one of {caught_count} registered mutations was detected by its named tests")
        result = GateResult(
            gate="mutation",
            status="PASS",
            exit_code=PASS,
            duration_s=time.monotonic() - started,
            details=details,
            payload=payload,
        )

    if archive is not None:
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result.payload["archive"] = str(archive)
    return result


def _report(result: GateResult) -> None:
    print(f"[{result.status}] {result.gate} gate in {result.duration_s:.2f}s (exit {result.exit_code})")
    for line in result.details:
        print(f"  {line}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Issue #89 property and mutation gates.")
    subparsers = parser.add_subparsers(dest="gate", required=True)

    property_parser = subparsers.add_parser("property", help="run the seeded property suites")
    property_parser.add_argument("--archive", type=Path, default=ROOT / "runs/qa/property-gate/summary.json")
    property_parser.add_argument("--time-budget", type=float, default=DEFAULT_TIME_BUDGET_S)

    mutation_parser = subparsers.add_parser("mutation", help="prove each registered rejection is load-bearing")
    mutation_parser.add_argument("--registry", type=Path, default=ROOT / "tools/qa/mutations-v1.json")
    mutation_parser.add_argument("--quarantine", type=Path, default=ROOT / "tools/qa/quarantine-v1.json")
    mutation_parser.add_argument("--archive", type=Path, default=ROOT / "runs/qa/mutation-gate/summary.json")
    mutation_parser.add_argument("--time-budget", type=float, default=DEFAULT_TIME_BUDGET_S)
    mutation_parser.add_argument("--keep-sandbox", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    try:
        if args.gate == "property":
            result = run_property_gate(archive=args.archive, time_budget_s=args.time_budget)
            if args.archive is not None and args.archive.exists():
                _write_property_archive(args.archive, result)
        else:
            result = run_mutation_gate(
                registry_path=args.registry,
                quarantine_path=args.quarantine,
                archive=args.archive,
                time_budget_s=args.time_budget,
                keep_sandbox=args.keep_sandbox,
            )
    except GateError as error:
        print(f"[INCOMPLETE] {args.gate} gate: {error}", file=sys.stderr)
        return INCOMPLETE

    _report(result)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
