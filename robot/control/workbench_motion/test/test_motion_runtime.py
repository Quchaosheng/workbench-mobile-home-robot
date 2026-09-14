"""Offline build provenance tests, not simulation evidence."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location("build_motion_runtime", ROOT / "docker/build_motion_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@pytest.fixture
def source(tmp_path, monkeypatch):
    repo = tmp_path / "upstream"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name in ("controller_manager", "ros2_control_test_assets"):
        (repo / name).mkdir()
        (repo / name / "source.txt").write_text("original\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "source"],
        cwd=repo,
        check=True,
    )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    monkeypatch.setattr(runtime, "REVISION", revision)
    patch = tmp_path / "fix.patch"
    patch.write_text(
        "diff --git a/controller_manager/source.txt b/controller_manager/source.txt\n"
        "--- a/controller_manager/source.txt\n+++ b/controller_manager/source.txt\n"
        "@@ -1 +1 @@\n-original\n+patched\n"
    )
    return repo, patch


def test_pinned_archive_ignores_dirty_checkout_and_includes_matching_test_assets(source, tmp_path):
    repo, patch = source
    (repo / "controller_manager/source.txt").write_text("unreviewed dirty content\n")
    package = runtime.prepare(repo, tmp_path / "build", patch)
    assert (package / "source.txt").read_text() == "patched\n"
    assert (package.parent / "ros2_control_test_assets/source.txt").read_text() == "original\n"


def test_wrong_revision_refuses_before_creating_workspace(source, tmp_path, monkeypatch):
    repo, patch = source
    monkeypatch.setattr(runtime, "REVISION", "0" * 40)
    destination = tmp_path / "build"
    with pytest.raises(ValueError, match="expected ros2_control"):
        runtime.prepare(repo, destination, patch)
    assert not destination.exists()


def test_existing_workspace_is_never_overwritten(source, tmp_path):
    repo, patch = source
    destination = tmp_path / "build"
    destination.mkdir()
    evidence = destination / "evidence.json"
    evidence.write_text("preserve")
    with pytest.raises(FileExistsError):
        runtime.prepare(repo, destination, patch)
    assert evidence.read_text() == "preserve"
