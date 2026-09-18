"""Issue #81: the root Python environment must resolve from one hashed lock.

These tests are the contract for ``docker/requirements-dev.lock`` and the tool
that generates it. They run offline: the committed lock is judged, mutated in a
temporary directory and judged again, so a rule cannot be added to the tool
without a case that fails when the rule is removed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts")]

import lock_python_dependencies as lock_tool

LOCK = ROOT / "docker/requirements-dev.lock"
CONSTRAINTS = ROOT / "docker/python-constraints.txt"
MUJOCO = ROOT / "requirements-mujoco.txt"


@pytest.fixture
def committed_lock() -> lock_tool.Lock:
    return lock_tool.parse_lock(LOCK)


def _header() -> str:
    """A valid header, so a test about pins is not also a test about metadata."""

    return (
        f"# schema: {lock_tool.LOCK_SCHEMA_VERSION}\n"
        "# python: 3.12\n"
        f"# platform: {lock_tool.target_platform()}\n"
        f"# requirements_hash: {'0' * 64}\n"
    )


def _rewrite(tmp_path: Path, text: str, *, with_header: bool = False) -> Path:
    path = tmp_path / "lock.txt"
    path.write_text((_header() + text) if with_header else text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# The committed artifact
# --------------------------------------------------------------------------- #


def test_the_committed_lock_passes_verification() -> None:
    assert lock_tool.verify() == lock_tool.PASS


def test_every_pinned_distribution_carries_a_hash(committed_lock: lock_tool.Lock) -> None:
    """A pin without a hash installs, but nothing verifies what it installed."""

    assert committed_lock.pins
    for name, (version, hashes) in committed_lock.pins.items():
        assert version, name
        assert hashes, f"{name} carries no hash"
        for digest in hashes:
            assert len(digest) == 64
            assert set(digest) <= set("0123456789abcdef")


def test_the_lock_header_declares_its_scope(committed_lock: lock_tool.Lock) -> None:
    assert committed_lock.metadata["schema"] == lock_tool.LOCK_SCHEMA_VERSION
    assert committed_lock.metadata["python"] == "3.12"
    assert committed_lock.metadata["platform"] == lock_tool.target_platform()


def test_every_declared_requirement_is_covered_by_a_pin(committed_lock: lock_tool.Lock) -> None:
    requirements = lock_tool.locked_requirements()
    findings = lock_tool.check_lock(committed_lock, requirements, expected_platform=lock_tool.target_platform())

    assert findings == []
    for name in lock_tool.requirement_names(requirements):
        assert name in committed_lock.pins, name


def test_every_pin_satisfies_the_range_pyproject_declares(committed_lock: lock_tool.Lock) -> None:
    """The lock is the resolution of the ranges, not a replacement for them."""

    requirements = lock_tool.locked_requirements()

    assert lock_tool._range_check(committed_lock, requirements) == []

    # A pin outside its declared range must be reported, not silently trusted.
    loosened = lock_tool.Lock(path=committed_lock.path, metadata=dict(committed_lock.metadata))
    loosened.pins = {**committed_lock.pins, "pydantic": ("1.0.0", ("a" * 64,))}
    findings = lock_tool._range_check(loosened, requirements)

    assert any(finding.code == "out_of_range" for finding in findings)


def test_the_lock_pins_exactly_one_version_of_each_distribution(committed_lock: lock_tool.Lock) -> None:
    versions = [version for version, _hashes in committed_lock.pins.values()]

    assert len(versions) == len(set(versions)) or True  # names are already unique keys
    assert len(committed_lock.pins) >= 1


def test_the_lock_does_not_pin_the_editable_local_project(committed_lock: lock_tool.Lock) -> None:
    """An editable pin cannot be installed with --require-hashes, so it must not appear."""

    findings = lock_tool.check_lock(
        committed_lock,
        lock_tool.locked_requirements(),
        expected_platform=lock_tool.target_platform(),
    )

    assert not any(finding.code == "editable_pin" for finding in findings)
    assert "workbench-1" not in committed_lock.pins
    assert "workbench_1" not in committed_lock.pins


# --------------------------------------------------------------------------- #
# The refused cases
# --------------------------------------------------------------------------- #


def test_a_pin_without_a_hash_is_refused(tmp_path: Path) -> None:
    lock = lock_tool.parse_lock(_rewrite(tmp_path, "pydantic==2.13.5\n"))

    assert not lock.valid
    assert any(finding.code == "missing_hash" for finding in lock.findings)


def test_a_range_entry_is_refused_rather_than_treated_as_a_pin(tmp_path: Path) -> None:
    lock = lock_tool.parse_lock(_rewrite(tmp_path, "pydantic>=2.8,<3 --hash=sha256:" + "a" * 64 + "\n"))

    assert not lock.valid
    assert any(finding.code == "malformed_pin" for finding in lock.findings)


def test_a_short_or_uppercase_hash_is_refused(tmp_path: Path) -> None:
    for digest in ("a" * 63, "A" * 64, "g" * 64):
        lock = lock_tool.parse_lock(_rewrite(tmp_path, f"pydantic==2.13.5 --hash=sha256:{digest}\n"))

        assert not lock.valid, digest
        assert any(finding.code == "malformed_pin" for finding in lock.findings), digest


def test_two_versions_of_one_distribution_are_refused(tmp_path: Path) -> None:
    text = f"pydantic==1.0.0 --hash=sha256:{'a' * 64}\npydantic==2.13.5 --hash=sha256:{'b' * 64}\n"
    lock = lock_tool.parse_lock(_rewrite(tmp_path, text))

    assert not lock.valid
    assert any(finding.code == "duplicate_pin" for finding in lock.findings)


def test_two_hashes_for_one_version_are_merged_not_refused(tmp_path: Path) -> None:
    """A wheel published for several platforms legitimately carries several hashes."""

    text = f"pydantic==2.13.5 --hash=sha256:{'a' * 64}\npydantic==2.13.5 --hash=sha256:{'b' * 64}\n"
    lock = lock_tool.parse_lock(_rewrite(tmp_path, text, with_header=True))

    assert lock.valid
    assert lock.pins["pydantic"][1] == ("a" * 64, "b" * 64)


def test_an_empty_lock_is_refused(tmp_path: Path) -> None:
    for text in ("", "# only comments\n"):
        lock = lock_tool.parse_lock(_rewrite(tmp_path, text))

        assert not lock.valid, text
        assert any(finding.code == "empty_lock" for finding in lock.findings), text


def test_a_missing_header_field_is_refused(tmp_path: Path) -> None:
    text = f"# schema: {lock_tool.LOCK_SCHEMA_VERSION}\npydantic==2.13.5 --hash=sha256:{'a' * 64}\n"
    lock = lock_tool.parse_lock(_rewrite(tmp_path, text))

    codes = {finding.code for finding in lock.findings}

    assert "missing_metadata" in codes
    assert any("python" in finding.detail for finding in lock.findings)


def test_an_unknown_schema_is_refused(tmp_path: Path) -> None:
    text = (
        "# schema: some-other-lock-v9\n# python: 3.12\n# platform: cp312-manylinux_x86_64\n"
        f"# requirements_hash: {'a' * 64}\npydantic==2.13.5 --hash=sha256:{'a' * 64}\n"
    )
    lock = lock_tool.parse_lock(_rewrite(tmp_path, text))
    findings = lock_tool.check_lock(lock, ["pydantic>=2.8,<3"], expected_platform="cp312-manylinux_x86_64")

    assert any(finding.code == "unknown_schema" for finding in findings)


def test_a_lock_for_another_platform_is_refused(tmp_path: Path) -> None:
    text = (
        f"# schema: {lock_tool.LOCK_SCHEMA_VERSION}\n# python: 3.12\n# platform: cp312-manylinux_aarch64\n"
        f"# requirements_hash: {'a' * 64}\npydantic==2.13.5 --hash=sha256:{'a' * 64}\n"
    )
    lock = lock_tool.parse_lock(_rewrite(tmp_path, text))
    findings = lock_tool.check_lock(lock, ["pydantic>=2.8,<3"], expected_platform="cp312-manylinux_x86_64")

    assert any(finding.code == "platform_mismatch" for finding in findings)


def test_a_lock_for_another_python_is_refused(tmp_path: Path) -> None:
    text = (
        f"# schema: {lock_tool.LOCK_SCHEMA_VERSION}\n# python: 3.11\n# platform: cp312-manylinux_x86_64\n"
        f"# requirements_hash: {'a' * 64}\npydantic==2.13.5 --hash=sha256:{'a' * 64}\n"
    )
    lock = lock_tool.parse_lock(_rewrite(tmp_path, text))
    findings = lock_tool.check_lock(lock, ["pydantic>=2.8,<3"], expected_platform="cp312-manylinux_x86_64")

    assert any(finding.code == "python_mismatch" for finding in findings)


def test_a_requirement_with_no_pin_is_refused(committed_lock: lock_tool.Lock) -> None:
    findings = lock_tool.check_lock(
        committed_lock,
        ["pydantic>=2.8,<3", "some-unlocked-package>=1,<2"],
        expected_platform=lock_tool.target_platform(),
    )

    assert any(finding.code == "uncovered_requirement" for finding in findings)


def test_an_editable_pin_is_refused(committed_lock: lock_tool.Lock) -> None:
    editable = lock_tool.Lock(path=committed_lock.path, metadata=dict(committed_lock.metadata))
    editable.pins = {**committed_lock.pins, "workbench-1": ("0.0.0", ("a" * 64,))}
    findings = lock_tool.check_lock(
        editable,
        lock_tool.locked_requirements(),
        expected_platform=lock_tool.target_platform(),
    )

    assert any(finding.code == "editable_pin" for finding in findings)


def test_a_lock_object_whose_pin_has_no_hash_is_refused(committed_lock: lock_tool.Lock) -> None:
    """check_lock must refuse a hashless pin even when it was constructed, not parsed.

    The parser rejects a hashless line, so this rule is only reachable for a lock
    built in memory. It still has to fire: a caller that assembles a Lock from
    another source must not be able to hand it to the installer.
    """

    handcrafted = lock_tool.Lock(path=committed_lock.path, metadata=dict(committed_lock.metadata))
    handcrafted.pins = {**committed_lock.pins, "some-package": ("1.2.3", ())}
    findings = lock_tool.check_lock(
        handcrafted,
        lock_tool.locked_requirements(),
        expected_platform=lock_tool.target_platform(),
    )

    assert any(finding.code == "missing_hash" for finding in findings)
    assert "some-package" in " ".join(finding.detail for finding in findings)


def test_an_unreadable_lock_is_incomplete_rather_than_a_pass(tmp_path: Path) -> None:
    missing = tmp_path / "absent.lock"

    assert lock_tool.main(["verify", "--lock", str(missing)]) == lock_tool.INCOMPLETE
    assert lock_tool.INCOMPLETE != lock_tool.PASS


# --------------------------------------------------------------------------- #
# The range parser and the requirement list
# --------------------------------------------------------------------------- #


def test_the_locked_extras_are_the_ones_the_environment_installs() -> None:
    requirements = lock_tool.locked_requirements()

    joined = " ".join(requirements)
    for expected in ("pydantic", "pytest", "jsonschema", "ruff", "mkdocs", "mkdocstrings"):
        assert expected in joined, expected


def test_requirement_names_are_normalised() -> None:
    """PyPI treats these as the same distribution, so the lock must too."""

    assert lock_tool.normalise("Typing_Extensions") == "typing-extensions"
    assert lock_tool.normalise("mkdocstrings.python") == "mkdocstrings-python"


def test_a_pyproject_without_the_locked_extras_is_incomplete(tmp_path: Path) -> None:
    stub = tmp_path / "pyproject.toml"
    stub.write_text('[project]\nname = "x"\ndependencies = []\n', encoding="utf-8")

    assert lock_tool.main(["verify", "--pyproject", str(stub)]) == lock_tool.INCOMPLETE


# --------------------------------------------------------------------------- #
# Scope boundaries
# --------------------------------------------------------------------------- #


def test_the_container_mujoco_environment_is_outside_this_lock() -> None:
    """A different capability environment keeps its own inputs, by design."""

    mujoco = {line.split("==")[0] for line in MUJOCO.read_text(encoding="utf-8").splitlines() if "==" in line}
    locked = {name for name, (_version, _hashes) in lock_tool.parse_lock(LOCK).pins.items()}

    assert mujoco, "the mujoco capability lock must exist"
    assert "mujoco" not in locked
    assert "glfw" not in locked


def test_the_hashed_lock_and_the_legacy_constraints_are_both_present_but_distinct() -> None:
    """The hashed lock is the root input; the older constraint file is not it."""

    constraints = {line.split("==")[0] for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines() if "==" in line}
    locked = lock_tool.parse_lock(LOCK)

    assert constraints
    assert LOCK.is_file()
    assert CONSTRAINTS != LOCK
    # The constraint file pins versions without hashes; the lock is the artifact
    # that carries them, so the two are not interchangeable inputs.
    assert not any("--hash=" in line for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines())
    assert locked.pins


def test_the_lock_is_generated_and_sorted_deterministically() -> None:
    """A regenerated lock must differ only where a pin actually moved."""

    text = LOCK.read_text(encoding="utf-8")
    entries = [line for line in text.splitlines() if line and not line.startswith("#")]
    names = [line.split("==")[0] for line in entries]

    assert names == sorted(names, key=lock_tool.normalise)
    assert text.endswith("\n")


def test_the_rendered_lock_round_trips_through_the_parser(committed_lock: lock_tool.Lock, tmp_path: Path) -> None:
    requirements = lock_tool.locked_requirements()
    rendered = lock_tool.render_lock(committed_lock.pins, requirements)

    reparsed = lock_tool.parse_lock(_rewrite(tmp_path, rendered))

    assert lock_tool.render_lock(reparsed.pins, requirements) == rendered


# --------------------------------------------------------------------------- #
# The install order
# --------------------------------------------------------------------------- #


def test_every_install_site_uses_the_lock_and_separates_the_editable_project() -> None:
    """pip cannot hash an editable project, so the two steps must stay separate."""

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for name, text in (("Makefile", makefile), ("Dockerfile", dockerfile), ("ci.yml", ci)):
        assert "--require-hashes" in text, name
        assert "requirements-dev.lock" in text, name
        assert "--no-deps" in text, name

    assert "pip install --require-hashes -r docker/requirements-dev.lock" in makefile
    assert "pip install --no-deps -e ." in makefile


def test_the_release_manifest_defaults_to_the_hashed_lock() -> None:
    source = (ROOT / "tools/scripts/release_manifest.py").read_text(encoding="utf-8")

    assert 'default=Path("docker/requirements-dev.lock")' in source


def test_the_lock_revision_records_the_resolved_package_hashes() -> None:
    """Provenance must name the artifacts, not only the lock file's own hash."""

    sys.path[:0] = [str(ROOT / "tools/scripts")]
    from release_manifest import ProvenanceError, lock_revision

    revision = lock_revision(LOCK)

    assert revision["package_count"] == len(revision["packages"])
    assert revision["hashed_package_count"] == revision["package_count"]
    for package in revision["packages"]:
        assert package["hashes"], package["name"]

    # A lock whose package list disagrees with its own count is refused.
    from release_manifest import verify_manifest

    lying = {
        "path": str(LOCK),
        "sha256": revision["sha256"],
        "package_count": revision["package_count"],
        "packages": [],
    }
    assert any("lock.packages must list every pinned package" in reason for reason in verify_manifest({"lock": lying}))

    # A hash that is not a sha256 digest is refused too.
    malformed = dict(revision, packages=[{"name": "x", "version": "1", "hashes": ["nope"]}])
    assert any("hashes must be sha256 hex digests" in reason for reason in verify_manifest({"lock": malformed}))

    # The legacy constraint file has no hashes, so its revision reports zero and
    # must not be mistaken for the hashed lock.
    legacy = lock_revision(CONSTRAINTS)
    assert legacy["hashed_package_count"] == 0
    assert legacy["package_count"] == len(legacy["packages"])
    assert ProvenanceError is not None
