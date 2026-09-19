import json
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - host-only Python 3.10 fallback
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[2]
IMMUTABLE_CUDA_BASE = re.compile(r"^FROM nvidia/cuda:12\.8\.1-runtime-ubuntu24\.04@sha256:[0-9a-f]{64}$")


def test_runtime_and_devcontainer_use_the_same_immutable_base() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    base = next(line for line in dockerfile.splitlines() if line.startswith("FROM "))

    assert IMMUTABLE_CUDA_BASE.fullmatch(base)
    assert not (ROOT / ".devcontainer/Dockerfile").exists()
    assert '"dockerfile": "../Dockerfile"' in (ROOT / ".devcontainer/devcontainer.json").read_text(encoding="utf-8")


def test_cuda_base_matches_exported_inventory_and_gpu_compatibility_matrix() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    image = next(line.removeprefix("FROM ") for line in dockerfile.splitlines() if line.startswith("FROM "))
    name, digest = image.split("@", 1)
    cuda_version = name.removeprefix("nvidia/cuda:").split("-", 1)[0]
    matrix = json.loads((ROOT / "docker/gpu-arch-matrix.json").read_text(encoding="utf-8"))

    assert re.findall(r'org\.opencontainers\.image\.base\.name="([^"]+)"', dockerfile) == [name]
    assert re.findall(r'org\.opencontainers\.image\.base\.digest="([^"]+)"', dockerfile) == [digest]
    assert re.findall(r"'base=([^']+)'", dockerfile) == [name]
    assert re.findall(r"'base_digest=([^']+)'", dockerfile) == [digest]
    assert re.findall(r"'cuda=([^']+)'", dockerfile) == [cuda_version]
    assert matrix["cuda_baseline"] == ".".join(cuda_version.split(".")[:2])


def test_runtime_container_copies_installable_workbench_packages_before_install() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    install_offset = dockerfile.index("python -m pip install")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_directories = pyproject["tool"]["setuptools"]["package-dir"].values()
    source_roots = {"/".join(Path(directory).parts[:2]) for directory in package_directories}

    for source_root in sorted(source_roots):
        package_copy = f"COPY {source_root} ./{source_root}"
        assert dockerfile.index(package_copy) < install_offset


def test_image_records_build_inventory_and_uses_system_site_packages() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.count("--system-site-packages") == 2
    # The image installs its third-party dependencies from the committed
    # hash-checked lock, then the project itself without dependency resolution.
    # pip cannot hash an editable local project, so the two steps stay separate;
    # see docs/security/reproducible-python-lock.md.
    assert "--require-hashes -r /tmp/requirements-dev.lock" in dockerfile
    assert "-m pip install --no-compile --no-deps -e ." in dockerfile
    assert "/usr/share/workbench/container/apt-packages.tsv" in dockerfile
    assert "/usr/share/workbench/container/python-packages.txt" in dockerfile
    assert 'org.opencontainers.image.base.digest="sha256:' in dockerfile
    assert 'test "${TARGETARCH}" = "amd64"' in dockerfile
    for host in ("archive.ubuntu.com", "security.ubuntu.com", "packages.ros.org"):
        assert f'Acquire::http::Proxy::{host} "DIRECT"' in dockerfile
        assert f'Acquire::https::Proxy::{host} "DIRECT"' in dockerfile
    # The image runs checks that need a runtime, so the runtime is part of the
    # image contract: ruby for docker/erb-regression-test.rb, and node for the
    # dashboard modules that tests/unit/test_dashboard_accessibility.py
    # evaluates in-image and deliberately does not skip when it is missing.
    packages = (ROOT / "docker/apt-packages.txt").read_text(encoding="utf-8")
    for package in (
        "ros-jazzy-ros-base",
        "ros-jazzy-moveit",
        "ros-jazzy-gz-ros2-control",
        "ros-jazzy-ros-gz-bridge",
        "liburdfdom-tools",
        "ruby-full",
        "nodejs",
    ):
        assert package in packages
    assert "ros-jazzy-desktop" not in packages.splitlines()
    assert "urdfdom-tools" not in packages.splitlines()
