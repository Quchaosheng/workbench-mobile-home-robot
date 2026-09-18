#!/usr/bin/env python3
"""Generate and verify the root Python dependency lock (Issue #81).

The root ``pyproject.toml`` declares ranges such as ``pydantic>=2.8,<3``. A range
resolves differently as the index changes, so a rebuild from ranges is not
reproducible and a hash cannot be attached to it. This tool resolves those ranges
once, records every distribution as ``name==version --hash=sha256:...`` in
``docker/requirements-dev.lock``, and then verifies the committed result without
network access.

Three subcommands, and the exit codes are the contract used by every other gate
in this repository:

* ``verify``  -- 0 the lock is complete, well-formed and satisfied; 1 it is not;
  2 the lock could not be judged at all.
* ``drift``   -- 0 the lock still reproduces; 1 a pin would move or has been
  withdrawn from the index, so re-resolution would change the environment.
* ``generate``-- resolve and write the lock (the only subcommand that needs the
  network).

Scope is deliberate. This lock covers third-party distributions only. The project
itself is installed as an editable local project, and pip refuses to hash an
editable requirement because there is no single artifact to hash, so the editable
install is a separate ``--no-deps`` step everywhere. ROS, apt and the container
only MuJoCo environment stay outside this lock and keep their own inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from _paths import ROOT

PASS = 0
FAIL = 1
INCOMPLETE = 2

PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "docker/requirements-dev.lock"
LOCK_SCHEMA_VERSION = "workbench-python-lock-v1"

# Extras whose requirements are locked. ``docs`` builds the strict mkdocs site in
# CI and ``dev`` runs the tests, so both are installed in the same environments
# this lock describes.
LOCKED_EXTRAS = ("dev", "docs")

HEADER_PREFIX = "#"
_META = re.compile(r"^#\s*(?P<key>[a-z_]+):\s*(?P<value>.+?)\s*$")
_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9._+-]*)"
    r"(?:\s+--hash=sha256:(?P<hash>[0-9a-f]{64}))+$"
)
_HASH = re.compile(r"--hash=sha256:(?P<hash>[0-9a-f]{64})")


class LockError(RuntimeError):
    """The lock cannot be judged, or an input required to judge it is missing."""


@dataclass
class Finding:
    """One reason the lock is not usable, with the entry that caused it."""

    code: str
    detail: str


@dataclass
class Lock:
    """A parsed lock file: its header metadata and its pinned distributions."""

    path: Path
    metadata: dict[str, str] = field(default_factory=dict)
    pins: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.findings


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def target_platform() -> str:
    """Return the lock's declared target, refusing an unsupported interpreter."""

    if sys.version_info[:2] != (3, 12):
        raise LockError(f"the lock is resolved for CPython 3.12, not {platform.python_version()}")
    machine = platform.machine().lower()
    if machine not in {"x86_64", "amd64", "aarch64", "arm64"}:
        raise LockError(f"unsupported lock target architecture: {machine}")
    manylinux = "x86_64" if machine in {"x86_64", "amd64"} else "aarch64"
    return f"cp312-manylinux_{manylinux}"


def locked_requirements(pyproject: Path = PYPROJECT) -> list[str]:
    """Return the requirement specifiers the lock must cover."""

    try:
        payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise LockError(f"cannot read {pyproject}: {error}") from error

    project = payload.get("project")
    if not isinstance(project, dict):
        raise LockError(f"{pyproject} declares no [project] table")

    requirements = list(project.get("dependencies") or [])
    extras = project.get("optional-dependencies") or {}
    for extra in LOCKED_EXTRAS:
        if extra not in extras:
            raise LockError(f"{pyproject} declares no '{extra}' extra")
        requirements.extend(extras[extra])
    if not requirements:
        raise LockError(f"{pyproject} declares no requirements")
    return requirements


def requirement_names(requirements: list[str]) -> list[str]:
    """Return the normalised distribution name of each requirement specifier."""

    names = []
    for requirement in requirements:
        match = re.match(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        if match is None:
            raise LockError(f"cannot read a distribution name from {requirement!r}")
        names.append(normalise(match.group("name")))
    return names


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> Lock:
    lock = Lock(path=path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LockError(f"cannot read the lock: {error}") from error

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(HEADER_PREFIX):
            meta = _META.match(line)
            if meta is not None:
                lock.metadata[meta.group("key")] = meta.group("value")
            continue

        # A line without a hash is the failure mode this gate exists to catch: it
        # installs, but nothing verifies the artifact it installs from.
        if "--hash=" not in line:
            lock.findings.append(Finding("missing_hash", f"line {line_number} carries no --hash: {line!r}"))
            continue

        match = _PIN.match(line)
        if match is None:
            lock.findings.append(
                Finding("malformed_pin", f"line {line_number} is not 'name==version --hash=sha256:<hex>': {line!r}")
            )
            continue

        name = normalise(match.group("name"))
        version = match.group("version")
        hashes = tuple(sorted(set(_HASH.findall(line))))
        if name in lock.pins:
            previous_version, previous_hashes = lock.pins[name]
            if previous_version != version:
                lock.findings.append(
                    Finding("duplicate_pin", f"{name} is pinned twice: {previous_version} and {version}")
                )
                continue
            lock.pins[name] = (version, tuple(sorted(set(previous_hashes) | set(hashes))))
            continue
        lock.pins[name] = (version, hashes)

    if not lock.pins:
        lock.findings.append(Finding("empty_lock", "the lock pins no distributions"))

    for key in ("schema", "python", "platform", "requirements_hash"):
        if key not in lock.metadata:
            lock.findings.append(Finding("missing_metadata", f"the lock header does not declare '{key}:'"))

    return lock


def check_lock(lock: Lock, requirements: list[str], *, expected_platform: str | None = None) -> list[Finding]:
    """Return every reason the lock does not describe the declared requirements."""

    findings = list(lock.findings)

    schema = lock.metadata.get("schema")
    if schema is not None and schema != LOCK_SCHEMA_VERSION:
        findings.append(Finding("unknown_schema", f"the lock schema is {schema!r}, expected {LOCK_SCHEMA_VERSION!r}"))

    declared_python = lock.metadata.get("python")
    if declared_python is not None and declared_python != f"{sys.version_info[0]}.{sys.version_info[1]}":
        findings.append(
            Finding(
                "python_mismatch",
                f"the lock targets Python {declared_python}, the interpreter is {platform.python_version()}",
            )
        )

    declared_platform = lock.metadata.get("platform")
    if expected_platform is not None and declared_platform is not None and declared_platform != expected_platform:
        findings.append(
            Finding("platform_mismatch", f"the lock targets {declared_platform}, this host is {expected_platform}")
        )

    # An editable or local-path requirement cannot be installed with
    # --require-hashes, so its presence here would break the documented install
    # order rather than reproduce it.
    for name, (version, _hashes) in lock.pins.items():
        if version.startswith(("0.0.0", "dev")):
            findings.append(Finding("editable_pin", f"{name}=={version} looks like an editable local project"))

    covered = set(lock.pins)
    for requirement, name in zip(requirements, requirement_names(requirements), strict=True):
        if name not in covered:
            findings.append(Finding("uncovered_requirement", f"no pin covers the declared requirement {requirement!r}"))

    for name, (_version, hashes) in lock.pins.items():
        if not hashes:
            findings.append(Finding("missing_hash", f"{name} carries no hash"))

    return findings


def _range_check(lock: Lock, requirements: list[str]) -> list[Finding]:
    """Return the pins that do not satisfy the range declared in pyproject."""

    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.version import InvalidVersion, Version

    findings: list[Finding] = []
    by_name = {name: specifier for specifier, name in zip(requirements, requirement_names(requirements), strict=True)}
    for name, (version, _hashes) in sorted(lock.pins.items()):
        specifier = by_name.get(name)
        if specifier is None:
            continue
        try:
            requirement = Requirement(specifier)
            parsed = Version(version)
        except (InvalidRequirement, InvalidVersion) as error:
            findings.append(
                Finding("unreadable_requirement", f"cannot check {specifier!r} against {version!r}: {error}")
            )
            continue
        if parsed not in requirement.specifier:
            findings.append(
                Finding("out_of_range", f"{name}=={version} does not satisfy the declared range {specifier!r}")
            )
    return findings


def render_lock(pins: dict[str, tuple[str, tuple[str, ...]]], requirements: list[str]) -> str:
    platform_tag = target_platform()
    requirements_hash = hashlib.sha256("\n".join(requirements).encode()).hexdigest()
    lines = [
        "# Generated by tools/scripts/lock_python_dependencies.py -- do not edit by hand.",
        "# Regenerate with: make lock-python",
        "#",
        "# The lock is the resolved form of the ranges in pyproject.toml. It is",
        "# installed with --require-hashes; because pip cannot hash an editable local",
        "# project, the project itself is installed separately with --no-deps. See",
        "# docs/security/reproducible-python-lock.md.",
        f"# schema: {LOCK_SCHEMA_VERSION}",
        f"# python: {sys.version_info[0]}.{sys.version_info[1]}",
        f"# platform: {platform_tag}",
        f"# requirements_hash: {requirements_hash}",
        f"# package_count: {len(pins)}",
        "",
    ]
    for name in sorted(pins):
        version, hashes = pins[name]
        for digest in hashes:
            lines.append(f"{name}=={version} --hash=sha256:{digest}")
    return "\n".join(lines) + "\n"


def resolve(requirements: list[str], destination: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Download one wheel per resolved distribution and hash it."""

    destination.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--only-binary=:all:",
        "--dest",
        str(destination),
        *requirements,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise LockError(f"pip download failed with exit code {result.returncode}:\n{result.stderr.strip()}")

    pins: defaultdict[str, tuple[str, list[str]]] = defaultdict(lambda: ("", []))
    for wheel in sorted(destination.glob("*.whl")):
        parts = wheel.name.split("-")
        if len(parts) < 5:
            raise LockError(f"cannot read a name and version from {wheel.name!r}")
        name, version = normalise(parts[0]), parts[1]
        current_version, hashes = pins[name]
        if current_version and current_version != version:
            raise LockError(f"resolution offered two versions of {name}: {current_version} and {version}")
        hashes.append(sha256_of(wheel))
        pins[name] = (version, hashes)
    if not pins:
        raise LockError("resolution produced no wheels")
    return {name: (version, tuple(sorted(set(hashes)))) for name, (version, hashes) in pins.items()}


def _report(findings: list[Finding], *, label: str) -> int:
    if not findings:
        print(f"[PASS] {label}")
        return PASS
    print(f"[FAIL] {label}")
    for finding in findings:
        print(f"  {finding.code}: {finding.detail}")
    return FAIL


def verify(*, lock_path: Path = LOCK, pyproject: Path = PYPROJECT) -> int:
    """Judge the committed lock offline."""

    requirements = locked_requirements(pyproject)
    lock = parse_lock(lock_path)
    findings = check_lock(lock, requirements, expected_platform=target_platform())
    findings += _range_check(lock, requirements)
    return _report(
        findings,
        label=f"{lock_path} pins {len(lock.pins)} distributions for {len(requirements)} declared requirements",
    )


def drift(*, lock_path: Path = LOCK, pyproject: Path = PYPROJECT, destination: Path | None = None) -> int:
    """Re-resolve and report whether the committed pins still reproduce."""

    requirements = locked_requirements(pyproject)
    lock = parse_lock(lock_path)
    if not lock.valid:
        return _report(lock.findings, label=f"{lock_path} is not well-formed, so drift cannot be judged")

    findings: list[Finding] = []
    workspace = destination or Path(tempfile.mkdtemp(prefix="workbench-lock-drift."))
    try:
        resolved = resolve(requirements, workspace)
    except LockError as error:
        print(f"[INCOMPLETE] could not re-resolve: {error}")
        return INCOMPLETE

    for name, (version, hashes) in sorted(resolved.items()):
        if name not in lock.pins:
            findings.append(Finding("unlocked_dependency", f"{name}=={version} resolves but is not locked"))
            continue
        locked_version, locked_hashes = lock.pins[name]
        if locked_version != version:
            findings.append(Finding("version_drift", f"{name} is locked at {locked_version} but resolves to {version}"))
            continue
        if not set(hashes) & set(locked_hashes):
            findings.append(Finding("hash_drift", f"{name}=={version} resolved to an artifact with no locked hash"))
    for name, (version, _hashes) in sorted(lock.pins.items()):
        if name not in resolved:
            findings.append(Finding("withdrawn_pin", f"{name}=={version} is locked but the index no longer offers it"))

    return _report(findings, label=f"re-resolution {'matches' if not findings else 'differs from'} {lock_path}")


def generate(*, lock_path: Path = LOCK, pyproject: Path = PYPROJECT, destination: Path | None = None) -> int:
    """Resolve the declared ranges and write the lock."""

    requirements = locked_requirements(pyproject)
    workspace = destination or Path(tempfile.mkdtemp(prefix="workbench-lock-generate."))
    try:
        pins = resolve(requirements, workspace)
    except LockError as error:
        print(f"[INCOMPLETE] {error}")
        return INCOMPLETE

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(render_lock(pins, requirements), encoding="utf-8")
    print(f"[PASS] wrote {lock_path} with {len(pins)} pinned distributions")
    return PASS


def summary(*, lock_path: Path = LOCK, pyproject: Path = PYPROJECT) -> dict[str, object]:
    """Return the machine-readable lock revision recorded in release provenance."""

    requirements = locked_requirements(pyproject)
    lock = parse_lock(lock_path)
    return {
        "path": str(lock_path.relative_to(ROOT)) if lock_path.is_relative_to(ROOT) else str(lock_path),
        "sha256": sha256_of(lock_path),
        "schema": lock.metadata.get("schema"),
        "platform": lock.metadata.get("platform"),
        "python": lock.metadata.get("python"),
        "package_count": len(lock.pins),
        "hashed_package_count": sum(1 for _version, hashes in lock.pins.values() if hashes),
        "requirements": len(requirements),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate or verify the root Python dependency lock.")
    parser.add_argument("action", choices=("verify", "drift", "generate", "summary"))
    parser.add_argument("--lock", type=Path, default=LOCK, help="path to the lock file")
    parser.add_argument("--pyproject", type=Path, default=PYPROJECT, help="path to pyproject.toml")
    parser.add_argument("--destination", type=Path, help="workspace for downloaded wheels (drift and generate)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.action == "verify":
            return verify(lock_path=args.lock, pyproject=args.pyproject)
        if args.action == "drift":
            return drift(lock_path=args.lock, pyproject=args.pyproject, destination=args.destination)
        if args.action == "generate":
            return generate(lock_path=args.lock, pyproject=args.pyproject, destination=args.destination)
        print(json.dumps(summary(lock_path=args.lock, pyproject=args.pyproject), indent=2))
        return PASS
    except LockError as error:
        print(f"[INCOMPLETE] {error}", file=sys.stderr)
        return INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())
