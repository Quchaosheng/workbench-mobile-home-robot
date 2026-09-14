"""Build and verify the pinned simulation clock fix inside the project image.

The installed Jazzy package supplies all other runtime assets. Only the ABI-
matched controller_manager library is overlaid; system packages are untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile
from pathlib import Path

REVISION = "4324cabf03a1371951f0a039d239fcf09f563e54"
VERSION = "4.45.2"


def prepare(source: Path, workspace: Path, patch: Path) -> Path:
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if revision != REVISION:
        raise ValueError(f"expected ros2_control {REVISION}, found {revision}")
    archive = subprocess.check_output(
        ["git", "archive", REVISION, "controller_manager", "ros2_control_test_assets"], cwd=source
    )
    workspace.mkdir(parents=True, exist_ok=False)
    extracted = workspace / "source"
    extracted.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(extracted, filter="data")
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=extracted, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=extracted, check=True)
    return extracted / "controller_manager"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="checkout of pinned ros2_control tag 4.45.2")
    parser.add_argument("--workspace", required=True, type=Path, help="new writable build directory")
    args = parser.parse_args(argv)
    from xml.etree import ElementTree

    from ament_index_python.packages import get_package_prefix

    if os.environ.get("ROS_DISTRO") != "jazzy":
        raise RuntimeError("source ROS 2 Jazzy before building")
    prefix = Path(get_package_prefix("controller_manager"))
    installed = ElementTree.parse(prefix / "share/controller_manager/package.xml").findtext("version")
    if installed != VERSION:
        raise RuntimeError(f"installed controller_manager {installed} is not ABI baseline {VERSION}")
    patch = Path(__file__).resolve().parent / "patches/controller-manager-sim-time.patch"
    workspace = args.workspace.resolve()
    package = prepare(args.source.resolve(), workspace, patch)
    build = workspace / "build"
    assets_build = workspace / "assets-build"
    assets_prefix = workspace / "assets-install"
    subprocess.run(
        [
            "cmake",
            "-S",
            str(package.parent / "ros2_control_test_assets"),
            "-B",
            str(assets_build),
            f"-DCMAKE_INSTALL_PREFIX={assets_prefix}",
        ],
        check=True,
    )
    subprocess.run(["cmake", "--install", str(assets_build)], check=True)
    subprocess.run(
        [
            "cmake",
            "-S",
            str(package),
            "-B",
            str(build),
            "-DBUILD_TESTING=ON",
            f"-Dros2_control_test_assets_DIR={assets_prefix}/share/ros2_control_test_assets/cmake",
            "-DCMAKE_BUILD_TYPE=Release",
        ],
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", str(build), "--target", "test_controller_manager", "--parallel", "2"], check=True
    )
    test = [
        str(build / "test_controller_manager"),
        "--gtest_filter=WorkbenchSimulationClock.supplied_simulation_time_survives_delayed_ros_clock",
    ]
    outcomes = {}
    for label, library_path in (("baseline", f"{prefix / 'lib'}:{build}"), ("patched", str(build))):
        env = {
            **os.environ,
            "LD_LIBRARY_PATH": f"{library_path}:{os.environ.get('LD_LIBRARY_PATH', '')}",
            "ROS_LOG_DIR": str(workspace / "ros-log"),
            "ROS_DOMAIN_ID": "73",
        }
        with (workspace / f"{label}-regression.log").open("w") as log:
            result = subprocess.run(test, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
        outcomes[label] = result.returncode
    if outcomes != {"baseline": 1, "patched": 0}:
        raise RuntimeError(f"clock regression must fail before and pass after: {outcomes}; inspect regression logs")
    library = build / "libcontroller_manager.so"
    manifest = {
        "schema_version": "motion-runtime-v1",
        "upstream_revision": REVISION,
        "upstream_version": VERSION,
        "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
        "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
        "regression_exit_codes": outcomes,
        "simulation_clock": "supplied_update_time",
        "hardware_clock": "unchanged",
    }
    (workspace / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    setup = f"export LD_LIBRARY_PATH={shlex.quote(str(build))}:${{LD_LIBRARY_PATH:-}}\n"
    setup += f"export WORKBENCH_MOTION_RUNTIME_MANIFEST={shlex.quote(str(workspace / 'manifest.json'))}\n"
    (workspace / "setup.bash").write_text(setup)
    print(f"Verified motion runtime: source {workspace / 'setup.bash'} before starting Gazebo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
