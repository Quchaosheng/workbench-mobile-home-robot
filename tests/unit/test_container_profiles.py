import importlib.util
import json
import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_doctor():
    path = ROOT / "docker/container-doctor.py"
    spec = importlib.util.spec_from_file_location("workbench_container_doctor", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_script(filename: str):
    path = ROOT / "docker" / filename
    spec = importlib.util.spec_from_file_location(f"workbench_{path.stem.replace('-', '_')}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compose() -> dict:
    if shutil.which("docker") is None:
        import pytest

        pytest.skip("Docker CLI is intentionally not installed in the runtime container")
    result = subprocess.run(
        ["docker", "compose", "--profile", "*", "config", "--format", "json"],
        cwd=ROOT,
        env={**os.environ, "WORKBENCH_GPU_TIER": "auto"},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_dockerfile_declares_three_gpu_dependency_layers() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    matrix = json.loads((ROOT / "docker/gpu-arch-matrix.json").read_text(encoding="utf-8"))

    assert set(matrix["dependency_layers"]) == {"gpu-runtime", "gpu-simulation", "gpu-validation"}
    for layer in matrix["dependency_layers"]:
        assert layer in dockerfile
    assert "nvidia/cuda:12.8.1-runtime-ubuntu24.04@sha256:" in dockerfile
    assert "nvidia-driver" not in dockerfile
    assert "nvidia-container-toolkit" not in dockerfile


def test_dockerfile_does_not_upgrade_base_packages_before_installing_runtime_stack() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    update = dockerfile.index("apt-get update;", dockerfile.index("ros2.list"))
    install = dockerfile.index("xargs -r apt-get install", update)
    assert update < install
    assert "apt-get upgrade" not in dockerfile


def test_dockerfile_installs_patched_erb_and_runs_regression_guard() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "gem install --no-document --ignore-dependencies /tmp/erb-4.0.3.1.gem" in dockerfile
    assert "gem contents erb -v 4.0.3.1" in dockerfile
    assert 'test -n "${erb_lib_path}"' in dockerfile
    assert "-name 'erb-4.0.2.gemspec' -delete" in dockerfile
    assert "-name 'erb-4.0.2' -prune -exec rm -rf" in dockerfile
    assert "ruby /usr/share/workbench/container/erb-regression-test.rb" in dockerfile
    assert "ERB_GEM_SHA256=bcaaef8cbaa9c46674487c95636050820262ba61293cf33f10242a90dc80654f" in dockerfile


def test_supply_chain_scan_uses_exact_vex_document() -> None:
    workflow = (ROOT / ".github/workflows/container-full-stack.yml").read_text(encoding="utf-8")
    vex = json.loads((ROOT / "docker/vex.json").read_text(encoding="utf-8"))

    scan_block = workflow.split("uses: anchore/scan-action", 1)[1].split("- uses:", 1)[0]
    assert "vex: docker/vex.json" in scan_block
    assert vex["statements"] == [
        {
            "vulnerability": {"name": "GHSA-f865-m6cq-j9vx"},
            "products": [{"identifiers": {"purl": "pkg:pypi/mpmath@0.0.0"}}],
            "status": "not_affected",
            "timestamp": "2026-09-01T00:00:00Z",
            "justification": "vulnerable_code_not_present",
            "impact_statement": (
                "Ubuntu Noble's python3-mpmath package ships a backported ReDOS fix; "
                "Grype 0.118.0 reports synthetic 0.0.0 egg-info metadata, while the "
                "runtime package is the patched distro build."
            ),
        }
    ]


def test_gpu_matrix_covers_rtx_30_40_50_with_name_and_compute_guards() -> None:
    matrix = json.loads((ROOT / "docker/gpu-arch-matrix.json").read_text(encoding="utf-8"))

    assert matrix["cuda_baseline"] == "12.8"
    assert matrix["minimum_driver_linux"] == "570.26"
    expected = {"rtx30": "8.6", "rtx40": "8.9", "rtx50": "12.0"}
    assert set(matrix["generations"]) == set(expected)
    for tier, capability in expected.items():
        generation = matrix["generations"][tier]
        assert capability in generation["compute_capabilities"]
        assert generation["name_patterns"]
        assert generation["physical_evidence_required"] is True


def test_gpu_detection_does_not_misclassify_datacenter_cards_by_compute_only() -> None:
    doctor = _load_doctor()

    assert doctor.detect_gpu_tier("NVIDIA GeForce RTX 3080", "8.6") == "rtx30"
    assert doctor.detect_gpu_tier("NVIDIA GeForce RTX 4090", "8.9") == "rtx40"
    assert doctor.detect_gpu_tier("NVIDIA GeForce RTX 5090", "12.0") == "rtx50"
    assert doctor.detect_gpu_tier("NVIDIA A10", "8.6") is None
    assert doctor.detect_gpu_tier("NVIDIA L40S", "8.9") is None


def test_nvidia_query_reports_command_failure_and_parses_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    doctor = _load_doctor()
    monkeypatch.setattr(doctor.shutil, "which", lambda command: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="driver unavailable"),
    )
    assert doctor._nvidia_query() == ([], "driver unavailable")

    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="0, GPU-1, NVIDIA GeForce RTX 4060, 570.195.03, 8.9\n",
            stderr="",
        ),
    )
    devices, error = doctor._nvidia_query()
    assert error is None
    assert devices == [
        {
            "index": "0",
            "uuid": "GPU-1",
            "name": "NVIDIA GeForce RTX 4060",
            "driver": "570.195.03",
            "compute_capability": "8.9",
        }
    ]


def test_container_doctor_main_returns_two_for_fail_closed_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    doctor = _load_doctor()
    monkeypatch.setattr(doctor, "check_profile", lambda profile: {"profile": profile, "status": "NOT_EXECUTED"})
    monkeypatch.setattr(sys, "argv", ["container-doctor", "--profile", "ros-sim"])

    assert doctor.main() == 2
    assert json.loads(capsys.readouterr().out) == {"profile": "ros-sim", "status": "NOT_EXECUTED"}


def test_container_doctor_rejects_unknown_profile() -> None:
    doctor = _load_doctor()

    report = doctor.check_profile("typo")

    assert report["status"] == "NOT_EXECUTED"
    assert "unknown container profile: typo" in report["reason"]


def test_dds_config_validates_interface_and_writes_bounded_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dds = _load_script("dds_config.py")
    output = tmp_path / "fastdds.xml"
    monkeypatch.setenv("WORKBENCH_DDS_INTERFACE", "eth0")
    monkeypatch.setattr(dds.socket, "if_nametoindex", lambda interface: 2 if interface == "eth0" else 0)
    monkeypatch.setattr(dds.argparse.ArgumentParser, "parse_args", lambda self: Namespace(output=str(output)))

    assert dds.main() == 0
    rendered = output.read_text(encoding="utf-8")
    assert "<interface>eth0</interface>" in rendered
    assert "<useBuiltinTransports>false</useBuiltinTransports>" in rendered

    monkeypatch.setenv("WORKBENCH_DDS_INTERFACE", "eth0;bad")
    with pytest.raises(SystemExit, match="invalid characters"):
        dds.main()


def test_compose_keeps_dashboard_cpu_safe_and_gpu_profiles_explicit() -> None:
    services = _compose()["services"]
    dashboard = services["dashboard"]

    assert "profiles" not in dashboard
    assert "gpus" not in dashboard
    assert "devices" not in dashboard
    assert "cap_add" not in dashboard
    assert dashboard["read_only"] is True
    assert dashboard["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in dashboard["security_opt"]

    for profile in ("ros-sim", "gz-gui-x11", "gz-gui-wayland", "mujoco-gpu"):
        service = services[profile]
        assert service["profiles"] == [profile]
        assert service["gpus"] == [{"count": -1}]
        assert service["environment"]["NVIDIA_DRIVER_CAPABILITIES"] == "compute,utility,graphics"

    hardware = services["hardware-shell"]
    assert hardware["profiles"] == ["hardware-shell"]
    assert hardware["network_mode"] == "host"
    assert hardware["cap_add"] == ["NET_RAW"]

    compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "privileged" not in compose_text
    assert "NET_ADMIN" not in compose_text
    assert "xhost +" not in compose_text


def test_source_is_read_only_and_colcon_outputs_are_named_volumes() -> None:
    compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    devcontainer = json.loads((ROOT / ".devcontainer/devcontainer.json").read_text(encoding="utf-8"))

    assert ".:/workspace/src:ro" in compose_text
    for name in ("workbench-build", "workbench-install", "workbench-log"):
        assert name in compose_text
    assert devcontainer["build"]["dockerfile"] == "../Dockerfile"
    assert "readonly" in devcontainer["workspaceMount"]
    for name in ("workbench-dev-build", "workbench-dev-install", "workbench-dev-log"):
        assert any(name in mount for mount in devcontainer["mounts"])


def test_runtime_tmpfs_and_simulation_paths_are_writable_for_runtime_user() -> None:
    compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert (
        "/home/workbench/.ros:size=64m,mode=700,uid=${WORKBENCH_UID:-10001},gid=${WORKBENCH_GID:-10001}" in compose_text
    )
    assert (
        "/home/workbench/.cache:size=256m,mode=700,uid=${WORKBENCH_UID:-10001},gid=${WORKBENCH_GID:-10001}"
        in compose_text
    )
    for variable, value in (
        ("HOME", "/home/workbench"),
        ("ROS_LOG_DIR", "/tmp/ros-log"),
        ("GZ_LOG_PATH", "/tmp/gz-log"),
        ("XDG_CACHE_HOME", "/tmp/cache"),
        ("GZ_SIM_RESOURCE_PATH", "/opt/ros/jazzy/share:/opt/workbench_ws/install/share:/workspace/install/share"),
    ):
        assert f"{variable}: {value}" in compose_text


def test_entrypoint_sources_ros_then_image_then_development_install() -> None:
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")

    assert "set +u" in entrypoint
    assert entrypoint.index("set +u") < entrypoint.index('source "$1"') < entrypoint.index("set -u")
    ros = entrypoint.index("/opt/ros/jazzy/setup.bash")
    image_install = entrypoint.index("/opt/workbench_ws/install/setup.bash")
    development_install = entrypoint.index("/workspace/install/setup.bash")
    assert ros < image_install < development_install < entrypoint.index('exec "$@"')
    assert "/opt/workbench_source/robot/control/install/setup.bash" not in entrypoint
    doctor = entrypoint.index("workbench-container-doctor")
    dds = entrypoint.index("workbench-dds-config")
    assert doctor < dds


def test_mujoco_smoke_requires_real_egl_pixels_and_rejects_software_renderer() -> None:
    smoke = (ROOT / "docker/mujoco_smoke.py").read_text(encoding="utf-8")

    assert "mujoco.Renderer" in smoke
    assert "image_sha256" in smoke
    assert "CONTAINER_CAPABILITY_SMOKE" in smoke
    assert "llvmpipe" in smoke
    assert '"nvidia" in renderer_identity' in smoke
    assert "image_digest" in smoke


def test_gazebo_render_smoke_requires_nvidia_egl_vendor() -> None:
    smoke = (ROOT / "docker/gazebo_render_smoke.sh").read_text(encoding="utf-8")

    assert "eglinfo -B -p surfaceless" in smoke
    assert "NVIDIA EGL vendor was not evidenced" in smoke
    assert "llvmpipe" in smoke


def test_sim_smoke_waits_for_all_controllers_and_bounds_probe() -> None:
    smoke = (ROOT / "docker/sim_smoke.sh").read_text(encoding="utf-8")

    assert "controllers_ready=false" in smoke
    for controller in ("joint_state_broadcaster", "arm_trajectory_controller", "gripper_controller"):
        assert f"^{controller}.*active" in smoke
    assert "WORKBENCH_PHASE2_TIMEOUT:-180" in smoke


def test_hardware_profile_keeps_devices_and_sros2_fail_closed() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    doctor = (ROOT / "docker/container-doctor.py").read_text(encoding="utf-8")

    assert "WORKBENCH_SERIAL_DEVICE" in compose
    assert "WORKBENCH_CAN_INTERFACE" in compose
    assert "WORKBENCH_DDS_INTERFACE" in compose
    assert "ROS_SECURITY_STRATEGY: Enforce" in compose
    assert "/run/sros2/keystore:ro" in compose
    assert "/dev/serial/by-id/" in doctor
    assert "is_char_device" in doctor
    assert "interfaceWhiteList" in (ROOT / "docker/dds_config.py").read_text(encoding="utf-8")


def test_host_doctor_requires_at_least_one_gpu_row_for_driver_pass() -> None:
    doctor = (ROOT / "docker/host_doctor.py").read_text(encoding="utf-8")

    assert "bool(gpu_rows)" in doctor


def test_host_doctor_keeps_gpu_optional_for_cpu_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    doctor = _load_script("host_doctor.py")
    responses = {
        "docker": (0, "27.0.0"),
        "compose": (0, "2.30.0"),
        "nvidia-ctk": (127, "not installed"),
        "nvidia-smi": (127, "not installed"),
    }
    monkeypatch.setattr(doctor, "command_output", lambda command: responses[command[0]])
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(sys, "argv", ["host-doctor"])

    assert doctor.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "PASS"
    assert report["core_status"] == "PASS"
    assert report["gpu_status"] == "NOT_EXECUTED"

    monkeypatch.setattr(sys, "argv", ["host-doctor", "--require-gpu"])
    assert doctor.main() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "NOT_EXECUTED"


def test_gpu_doctor_requires_nvidia_egl_vendor_evidence() -> None:
    doctor = (ROOT / "docker/container-doctor.py").read_text(encoding="utf-8")

    assert "egl_vendor_files" in doctor
    assert "NVIDIA EGL vendor JSON is unavailable" in doctor


def test_container_python_test_uses_writable_copy_and_excludes_host_docker_tests() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("container-python-test:\n", 1)[1].split("\n\n", 1)[0]

    assert "mktemp -d /tmp/workbench-python-test.XXXXXX" in target
    assert "cp -a /workspace/src/." in target
    assert "--ignore=tests/unit/test_multi_host_deployment.py" in target
    assert "--cache-dir" not in target


def test_colcon_test_and_result_logs_use_writable_volume() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("container-colcon-test:\n", 1)[1].split("\n\n", 1)[0]

    assert target.count("colcon --log-base /workspace/log") == 2
    assert "test-result --test-result-base /workspace/build" in target


def test_container_acceptance_targets_cover_health_contracts_and_independent_sim_smokes() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8080/healthz" in makefile
    assert "http://127.0.0.1:8080/readyz" in makefile
    assert "mktemp -d /tmp/workbench-project-check.XXXXXX" in makefile
    assert "cp -a /workspace/src/." in makefile
    assert (
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS="--ignore=tests/unit/test_multi_host_deployment.py" make test'
    ) in makefile
    assert 'PYTEST_ADDOPTS="--ignore=tests/unit/test_multi_host_deployment.py"' in makefile
    sim_target = makefile.split("container-sim-check:\n", 1)[1].split("\n\n", 1)[0]
    assert "workbench-gazebo-render-smoke || status=$$?" in sim_target
    assert "workbench-sim-smoke || status=$$?" in sim_target


def test_ci_bare_image_smoke_uses_copied_source_workdir() -> None:
    workflow = (ROOT / ".github/workflows/container-full-stack.yml").read_text(encoding="utf-8")

    assert "--read-only --tmpfs /tmp:size=512m,mode=1777" in workflow
    assert "--workdir /opt/workbench_source workbench-1:container-ci" in workflow
    assert "docker compose up -d --no-build dashboard" in workflow
    assert "docker compose exec -T dashboard curl --fail --silent http://127.0.0.1:8080/healthz" in workflow
    assert "docker compose exec -T dashboard curl --fail --silent http://127.0.0.1:8080/readyz" in workflow
    assert "\n            if curl --fail --silent http://127.0.0.1:8080/healthz" not in workflow


def test_ci_and_release_bare_image_smokes_use_copied_source_workdir() -> None:
    smoke_images = (
        ("ci.yml", "workbench-1:ci"),
        ("release-image.yml", "workbench-1:release-candidate"),
    )
    for workflow_name, tag in smoke_images:
        workflow = (ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8")
        assert f"--workdir /opt/workbench_source {tag}" in workflow


def test_supply_chain_scan_gates_actionable_findings_with_filtered_report() -> None:
    workflow = (ROOT / ".github/workflows/container-full-stack.yml").read_text(encoding="utf-8")

    scan_block = workflow.split("uses: anchore/scan-action", 1)[1].split("- uses:", 1)[0]
    assert "fail-build: true" in scan_block
    assert "only-fixed:" not in scan_block
    assert "severity-cutoff: high" in scan_block
    assert "output-file: workbench-1-grype.json" in scan_block


def test_release_workflow_keeps_the_same_high_vulnerability_gate() -> None:
    workflow = (ROOT / ".github/workflows/release-image.yml").read_text(encoding="utf-8")

    scan_block = workflow.split("uses: anchore/scan-action", 1)[1].split("- uses:", 1)[0]
    assert "fail-build: true" in scan_block
    assert "severity-cutoff: high" in scan_block
    assert "vex: docker/vex.json" in scan_block
    assert "output-file: workbench-1-grype.json" in scan_block
    artifact_block = ""
    for block in workflow.split("- uses: actions/upload-artifact"):
        if "name: workbench-1-sbom" in block:
            artifact_block = block
            break
    assert artifact_block
    assert "if: always()" in artifact_block


@pytest.mark.parametrize("script", ["entrypoint.sh", "gazebo_render_smoke.sh", "sim_smoke.sh"])
def test_shell_entrypoints_are_syntax_valid(script: str) -> None:
    subprocess.run(["bash", "-n", str(ROOT / "docker" / script)], check=True)
