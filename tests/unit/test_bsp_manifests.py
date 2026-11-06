from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from bsp.validation.check_readiness import BLOCKED_STATUS, READY_STATUS, check, validate_readiness
from bsp.validation.validate_image_inputs import validate_image_inputs
from bsp.validation.validate_manifests import (
    validate,
    validate_camera_deployment,
    validate_camera_manifest,
    validate_manifests,
)

ROOT = Path(__file__).resolve().parents[2]


def load_manifests() -> tuple[dict, dict, dict]:
    board = yaml.safe_load((ROOT / "bsp/board-manifest.yaml").read_text(encoding="utf-8"))
    compatibility = yaml.safe_load((ROOT / "bsp/firmware/compatibility.yaml").read_text(encoding="utf-8"))
    bringup = yaml.safe_load((ROOT / "bsp/validation/bringup-plan.yaml").read_text(encoding="utf-8"))
    return board, compatibility, bringup


def test_bsp_manifests_are_consistent_and_fail_closed() -> None:
    assert validate() == []
    assert check() == []


def test_duplicate_node_id_and_nonindependent_safety_are_rejected() -> None:
    board, compatibility, bringup = load_manifests()
    changed = deepcopy(board)
    changed["controllers"][1]["node_id"] = changed["controllers"][0]["node_id"]
    changed["controllers"][-1]["independent_reset_domain"] = False

    errors = validate_manifests(changed, compatibility, bringup)

    assert "controller node IDs must be unique" in errors
    assert "MCU-SAFETY must have an independent reset domain" in errors


def test_physical_pass_without_evidence_is_rejected() -> None:
    board, compatibility, bringup = load_manifests()
    changed = deepcopy(bringup)
    changed["tests"][0]["result"] = "PASS"

    errors = validate_manifests(board, compatibility, changed)

    assert "physical bring-up results must remain NOT_EXECUTED" in errors


def test_camera_selection_drift_is_rejected() -> None:
    camera = yaml.safe_load((ROOT / "bsp/sensors/camera-head.yaml").read_text(encoding="utf-8"))
    changed = deepcopy(camera)
    changed["quantity_per_robot"] = 2
    changed["selection"]["model"] = "UNREVIEWED_CAMERA"
    changed["selection"]["interface"] = "USB_2"

    errors = validate_camera_manifest(changed)

    assert "prototype BSP requires exactly one D435 head camera" in errors
    assert "head RGB-D camera must use the selected USB 3 boundary" in errors


def test_camera_physical_evidence_cannot_be_claimed_before_bringup() -> None:
    camera = yaml.safe_load((ROOT / "bsp/sensors/camera-head.yaml").read_text(encoding="utf-8"))
    changed = deepcopy(camera)
    changed["boundaries"]["physical_evidence_ready"] = True

    errors = validate_camera_manifest(changed)

    assert "camera physical evidence must remain false before calibration and bring-up" in errors


def test_composite_camera_cannot_use_one_video_device_alias() -> None:
    camera = yaml.safe_load((ROOT / "bsp/sensors/camera-head.yaml").read_text(encoding="utf-8"))
    changed = deepcopy(camera)
    changed["device_identity"]["single_video_device_symlink"] = "/dev/workbench-camera-head"

    errors = validate_camera_manifest(changed)

    assert "D435 must not use one shared video-device symlink" in errors


def test_unresolved_camera_deployment_cannot_be_enabled() -> None:
    deployment = yaml.safe_load((ROOT / "bsp/rootfs/camera-head-deployment.yaml").read_text(encoding="utf-8"))
    changed = deepcopy(deployment)
    changed["installable"] = True
    unit = (ROOT / "bsp/rootfs/systemd/robot-camera-head.service.in").read_text(encoding="utf-8")
    launcher = (ROOT / "bsp/rootfs/libexec/camera-head-launch").read_text(encoding="utf-8")
    environment = (ROOT / "bsp/rootfs/camera-head.env.example").read_text(encoding="utf-8")

    errors = validate_camera_deployment(changed, unit, launcher, environment)

    assert "camera deployment with unresolved selections must not be installable" in errors


def test_camera_service_must_reject_placeholder_serial() -> None:
    deployment = yaml.safe_load((ROOT / "bsp/rootfs/camera-head-deployment.yaml").read_text(encoding="utf-8"))
    unit = (ROOT / "bsp/rootfs/systemd/robot-camera-head.service.in").read_text(encoding="utf-8")
    launcher = (ROOT / "bsp/rootfs/libexec/camera-head-launch").read_text(encoding="utf-8")
    environment = (ROOT / "bsp/rootfs/camera-head.env.example").read_text(encoding="utf-8")

    errors = validate_camera_deployment(
        deployment, unit.replace("ExecStartPre=", "# removed=", 2), launcher, environment
    )

    assert "camera systemd unit must reject the placeholder serial" in errors


def load_readiness() -> dict:
    return yaml.safe_load((ROOT / "bsp/readiness.yaml").read_text(encoding="utf-8"))


def test_readiness_rejects_missing_or_unknown_controlled_gate() -> None:
    changed = deepcopy(load_readiness())
    changed["gates"].pop()
    changed["gates"][0]["id"] = "BSP-GATE-UNCONTROLLED"

    assert "BSP readiness must contain the exact nine controlled gates" in validate_readiness(changed)


def test_readiness_rejects_evidence_outside_repository(tmp_path: Path) -> None:
    changed = deepcopy(load_readiness())
    changed["gates"][0]["status"] = "PASS"
    changed["gates"][0]["evidence"] = "../outside.txt"

    errors = validate_readiness(changed, tmp_path / "repo")

    assert "BSP-GATE-ARCH claims PASS without repository evidence" in errors


def test_readiness_rejects_directory_as_pass_evidence(tmp_path: Path) -> None:
    changed = deepcopy(load_readiness())
    root = tmp_path / "repo"
    (root / "evidence").mkdir(parents=True)
    changed["gates"][0]["status"] = "PASS"
    changed["gates"][0]["evidence"] = "evidence"

    errors = validate_readiness(changed, root)

    assert "BSP-GATE-ARCH claims PASS without repository evidence" in errors


def test_readiness_release_flag_and_package_status_follow_all_gates(tmp_path: Path) -> None:
    changed = deepcopy(load_readiness())
    root = tmp_path / "repo"
    evidence = root / "evidence.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("controlled evidence\n", encoding="ascii")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    for gate in changed["gates"]:
        gate["status"] = "PASS"
        gate["evidence"] = f"{gate['id']}.yaml"
        (root / gate["evidence"]).write_text(
            yaml.safe_dump(
                {
                    "gate": gate["id"],
                    "board_model": "NVIDIA_JETSON_ORIN_NANO_SUPER_DEVELOPER_KIT_8GB",
                    "board_revision": "P3449-A02",
                    "kernel_version": "5.15.148-tegra",
                    "firmware_versions": {"MCU-BASE": "1.2.0"},
                    "timestamp_utc": "2026-10-20T04:05:06Z",
                    "result": "PASS",
                    "source_revision": "a" * 40,
                    "raw_capture": "evidence.txt",
                    "raw_capture_hash": digest,
                }
            ),
            encoding="utf-8",
        )
    changed["physical_release_ready"] = True
    changed["status"] = READY_STATUS

    assert validate_readiness(changed, root) == []

    changed["gates"][0]["status"] = "BLOCKED"
    assert "physical_release_ready must equal the all-gates-PASS result" in validate_readiness(changed, root)
    changed["physical_release_ready"] = False
    assert f"BSP package status must be {BLOCKED_STATUS}" in validate_readiness(changed, root)


def load_image_inputs() -> dict:
    return yaml.safe_load((ROOT / "bsp/image/build-inputs.yaml").read_text(encoding="utf-8"))


def test_unresolved_image_inputs_are_valid_but_not_build_ready() -> None:
    manifest = load_image_inputs()

    assert manifest["build_ready"] is False
    assert manifest["status"] == "inputs_unresolved"
    assert validate_image_inputs(manifest) == []


def test_image_inputs_reject_premature_ready_or_insecure_source() -> None:
    manifest = deepcopy(load_image_inputs())
    manifest["build_ready"] = True
    manifest["status"] = "inputs_locked"
    manifest["inputs"]["jetpack"]["source"] = "http://vendor.invalid/jetpack"

    errors = validate_image_inputs(manifest)

    assert "jetpack source must use HTTPS" in errors
    assert "unresolved image inputs must block build_ready" in errors


def test_image_inputs_reject_path_escape_and_directory(tmp_path: Path) -> None:
    manifest = deepcopy(load_image_inputs())
    manifest["inputs"]["kernel"]["config"] = "../outside.config"
    manifest["inputs"]["rootfs"]["services"] = "bsp"
    (tmp_path / "repo/bsp").mkdir(parents=True)

    errors = validate_image_inputs(manifest, tmp_path / "repo")

    assert "kernel config must reference a repository file" in errors
    assert "rootfs services must reference a repository file" in errors


def test_image_outputs_cannot_exist_before_inputs_are_locked() -> None:
    manifest = deepcopy(load_image_inputs())
    digest = "a" * 64
    manifest["outputs"] = {
        "boot_image_sha256": digest,
        "rootfs_image_sha256": digest,
        "recovery_image_sha256": digest,
    }

    assert "built output hashes require build_ready inputs" in validate_image_inputs(manifest)


class TestVerifiedImageInputDigests:
    """Issue #443: a digest is locked only once the referenced artifact is hashed."""

    def _manifest(self, root: Path) -> dict:
        files = {
            "kernel.config": b"CONFIG_ROBOT_BSP=y\n",
            "rootfs/services.yaml": b"services: {}\n",
            "rootfs/package.lock": b"pkg==1.0 --hash=sha256:abc\n",
            "recovery/recovery.img": b"recovery-image-bytes\n",
            "src/workbench.dts": b"/dts-v1/;\n",
            "out/boot.img": b"boot-image-bytes\n",
            "out/rootfs.img": b"rootfs-image-bytes\n",
            "out/recovery.img": b"recovery-image-bytes\n",
            "out/workbench.dtb": b"dtb-bytes\n",
            "build/build.log": b"build completed\n",
        }
        for relative, payload in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        container_digest = "sha256:" + _digest(files["recovery/recovery.img"])
        return {
            "version": "0.1",
            "target": "JETSON_TEST",
            "build_ready": True,
            "status": "inputs_locked",
            "inputs": {
                "jetpack": {
                    "version": "6.2",
                    "source": "https://vendor.invalid/jetpack.tbz2",
                    "sha256": _digest(files["kernel.config"]),
                    "retrieval": {
                        "url": "https://vendor.invalid/jetpack.tbz2",
                        "size_bytes": 1024,
                        "sha256": _digest(files["kernel.config"]),
                    },
                },
                "l4t": {
                    "version": "35.5",
                    "source": "https://vendor.invalid/l4t.tbz2",
                    "sha256": _digest(files["rootfs/services.yaml"]),
                    "retrieval": {
                        "url": "https://vendor.invalid/l4t.tbz2",
                        "size_bytes": 2048,
                        "sha256": _digest(files["rootfs/services.yaml"]),
                    },
                },
                "kernel": {
                    "version": "5.15",
                    "source": "https://vendor.invalid/linux.tbz2",
                    "sha256": _digest(files["rootfs/package.lock"]),
                    "retrieval": {
                        "url": "https://vendor.invalid/linux.tbz2",
                        "size_bytes": 4096,
                        "sha256": _digest(files["rootfs/package.lock"]),
                    },
                    "config": "kernel.config",
                    "config_sha256": _digest(files["kernel.config"]),
                },
                "device_tree": {
                    "source": "src/workbench.dts",
                    "source_sha256": _digest(files["src/workbench.dts"]),
                    "dtb_sha256": _digest(files["out/workbench.dtb"]),
                },
                "rootfs": {
                    "base_source": "https://vendor.invalid/rootfs.tbz2",
                    "base_sha256": _digest(files["rootfs/services.yaml"]),
                    "retrieval": {
                        "url": "https://vendor.invalid/rootfs.tbz2",
                        "size_bytes": 8192,
                        "sha256": _digest(files["rootfs/services.yaml"]),
                    },
                    "package_lock": "rootfs/package.lock",
                    "package_lock_sha256": _digest(files["rootfs/package.lock"]),
                    "services": "rootfs/services.yaml",
                    "services_sha256": _digest(files["rootfs/services.yaml"]),
                },
                "recovery": {
                    "image_source": "recovery/recovery.img",
                    "image_sha256": _digest(files["recovery/recovery.img"]),
                    "rollback_procedure": "build/build.log",
                },
                "toolchain": {
                    "container_digest": container_digest,
                    "retrieval": {
                        "url": f"registry.invalid/bsp@{container_digest}",
                        "size_bytes": 512,
                        "sha256": container_digest.removeprefix("sha256:"),
                    },
                    "compiler": "gcc 13.2",
                },
            },
            "outputs": {
                "boot_image_sha256": _digest(files["out/boot.img"]),
                "rootfs_image_sha256": _digest(files["out/rootfs.img"]),
                "recovery_image_sha256": _digest(files["out/recovery.img"]),
            },
            "build_record": {
                "command": "make bsp-image",
                "log": "build/build.log",
                "artifacts": {
                    "boot_image_sha256": "out/boot.img",
                    "rootfs_image_sha256": "out/rootfs.img",
                    "recovery_image_sha256": "out/recovery.img",
                    "dtb_sha256": "out/workbench.dtb",
                },
            },
            "rules": {
                "network_sources_require_https": True,
                "all_build_inputs_require_sha256": True,
                "unresolved_inputs_block_build": True,
                "generated_output_hashes_do_not_imply_physical_boot": True,
            },
        }

    def test_fully_verified_inputs_are_the_only_locked_manifest(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"

        assert validate_image_inputs(self._manifest(root), root) == []

    def test_all_zero_digests_never_lock_the_manifest(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        manifest = self._manifest(root)
        zeros = "0" * 64
        manifest["inputs"]["kernel"]["config_sha256"] = zeros
        manifest["inputs"]["rootfs"]["services_sha256"] = zeros

        errors = validate_image_inputs(manifest, root)

        assert f"kernel.config_sha256 does not match kernel.config: expected {zeros}" in " ".join(errors)
        assert "rootfs.services_sha256 does not match rootfs/services.yaml" in " ".join(errors)
        assert "invalid image inputs must block build_ready" in errors
        assert "image build status must be inputs_unresolved" in errors

    def test_wrong_digest_names_the_field_and_the_real_digest(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        manifest = self._manifest(root)
        manifest["inputs"]["kernel"]["config_sha256"] = "b" * 64

        errors = validate_image_inputs(manifest, root)

        assert (
            f"kernel.config_sha256 does not match kernel.config: expected {'b' * 64}, "
            f"actual {_digest(b'CONFIG_ROBOT_BSP=y\n')}" in errors
        )

    def test_missing_and_wrong_type_inputs_fail_closed(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        manifest = self._manifest(root)
        manifest["inputs"]["rootfs"]["services"] = "rootfs/absent.yaml"
        manifest["inputs"]["recovery"]["image_sha256"] = 1234

        errors = validate_image_inputs(manifest, root)

        assert "rootfs services must reference a repository file" in errors
        assert "recovery.image_sha256 must be a string" in errors
        assert "image build status must be inputs_unresolved" in errors

    def test_external_input_needs_size_and_https_retrieval_identity(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        manifest = self._manifest(root)
        manifest["inputs"]["jetpack"]["retrieval"] = {"url": "https://vendor.invalid/jetpack.tbz2"}
        manifest["inputs"]["l4t"]["retrieval"]["url"] = "http://vendor.invalid/l4t.tbz2"
        manifest["inputs"]["kernel"]["retrieval"]["size_bytes"] = 0

        errors = validate_image_inputs(manifest, root)

        assert "jetpack.sha256 requires a retrieval record with exactly url, size_bytes and sha256" in errors
        assert "l4t.sha256 retrieval url must use HTTPS" in errors
        assert "kernel.sha256 retrieval size_bytes must be a positive integer" in errors

    def test_stale_output_artifact_is_detected(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        manifest = self._manifest(root)
        (root / "out/boot.img").write_bytes(b"rebuilt-elsewhere\n")
        expected = manifest["outputs"]["boot_image_sha256"]

        errors = validate_image_inputs(manifest, root)

        assert (
            f"boot_image_sha256 does not match build artifact out/boot.img: expected {expected}, "
            f"actual {_digest(b'rebuilt-elsewhere\n')}" in errors
        )
        assert "invalid image inputs must block build_ready" in errors

    def test_output_digests_without_a_build_record_are_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        manifest = self._manifest(root)
        del manifest["build_record"]

        errors = validate_image_inputs(manifest, root)

        assert "boot_image_sha256 requires a recorded build output" in errors
        assert "dtb_sha256 requires a recorded build output" in errors

    def test_unreadable_local_input_cannot_be_marked_locked(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root ignores mode 000, so an unreadable input is not reproducible")
        root = tmp_path / "repo"
        manifest = self._manifest(root)
        unreadable = root / "rootfs/services.yaml"
        unreadable.chmod(0)
        try:
            errors = validate_image_inputs(manifest, root)
        finally:
            unreadable.chmod(0o600)

        assert "rootfs.services_sha256 cannot be verified: rootfs/services.yaml is unreadable" in errors
        assert "invalid image inputs must block build_ready" in errors

    def test_truncated_local_input_breaks_its_declared_digest(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        manifest = self._manifest(root)
        (root / "recovery/recovery.img").write_bytes(b"recovery")

        errors = validate_image_inputs(manifest, root)

        assert (
            "recovery.image_sha256 does not match recovery/recovery.img: "
            f"expected {_digest(b'recovery-image-bytes\n')}, actual {_digest(b'recovery')}" in errors
        )

    def test_unverifiable_source_keeps_the_manifest_unlocked(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        manifest = self._manifest(root)
        manifest["inputs"]["device_tree"]["source"] = "TBD_CARRIER_DTS_PATH"

        errors = validate_image_inputs(manifest, root)

        assert "unresolved image inputs must block build_ready" in errors
        assert "image build status must be inputs_unresolved" in errors

    def test_committed_manifest_stays_unresolved_and_non_buildable(self) -> None:
        manifest = load_image_inputs()

        assert manifest["build_ready"] is False
        assert manifest["status"] == "inputs_unresolved"
        assert validate_image_inputs(manifest) == []


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class TestReadinessStructuredEvidence:
    """Issue #442: a PASS gate needs a typed record, not any existing file."""

    @staticmethod
    def _root(tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        (root / "bsp/validation/captures").mkdir(parents=True)
        (root / "bsp/validation/captures/arch.log").write_text("cold boot transcript\n", encoding="ascii")
        return root

    @staticmethod
    def _record(root: Path, **overrides: object) -> dict:
        capture = root / "bsp/validation/captures/arch.log"
        record = {
            "gate": "BSP-GATE-ARCH",
            "board_model": "NVIDIA_JETSON_ORIN_NANO_SUPER_DEVELOPER_KIT_8GB",
            "board_revision": "P3449-A02",
            "kernel_version": "5.15.148-tegra",
            "firmware_versions": {"MCU-BASE": "1.2.0"},
            "timestamp_utc": "2026-10-20T04:05:06Z",
            "result": "PASS",
            "source_revision": "a" * 40,
            "raw_capture": "bsp/validation/captures/arch.log",
            "raw_capture_hash": hashlib.sha256(capture.read_bytes()).hexdigest(),
        }
        record.update(overrides)
        return record

    @staticmethod
    def _readiness(root: Path, record: dict | None = None) -> dict:
        readiness = deepcopy(load_readiness())
        for gate in readiness["gates"]:
            gate["status"] = "BLOCKED"
            gate["evidence"] = "NOT_ATTACHED"
        arch = next(gate for gate in readiness["gates"] if gate["id"] == "BSP-GATE-ARCH")
        arch["status"] = "PASS"
        arch["evidence"] = "evidence.yaml"
        if record is not None:
            (root / "evidence.yaml").write_text(yaml.safe_dump(record), encoding="utf-8")
        return readiness

    def test_arbitrary_existing_file_is_not_pass_evidence(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        (root / "README.md").write_text("# docs\n", encoding="ascii")
        readiness = self._readiness(root)
        for gate in readiness["gates"]:
            gate["status"] = "PASS"
            gate["evidence"] = "README.md"
        readiness["physical_release_ready"] = True
        readiness["status"] = READY_STATUS

        errors = validate_readiness(readiness, root)

        assert "BSP-GATE-ARCH evidence must be a structured YAML record" in errors
        assert "physical_release_ready must equal the all-gates-PASS result" in errors
        assert f"BSP package status must be {BLOCKED_STATUS}" in errors

    def test_structured_record_closes_the_gate_without_a_code_special_case(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)

        assert validate_readiness(self._readiness(root, self._record(root)), root) == []

    def test_stale_digest_and_mutated_capture_are_rejected(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        stale = self._record(root, raw_capture_hash="b" * 64)

        errors = validate_readiness(self._readiness(root, stale), root)
        assert "BSP-GATE-ARCH raw capture digest does not match raw_capture_hash" in errors

        original = self._record(root)
        (root / "bsp/validation/captures/arch.log").write_text("mutated\n", encoding="ascii")
        errors = validate_readiness(self._readiness(root, original), root)
        assert "BSP-GATE-ARCH raw capture digest does not match raw_capture_hash" in errors

    def test_record_bound_to_another_gate_is_rejected(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        other = self._record(root, gate="BSP-GATE-CAN")

        errors = validate_readiness(self._readiness(root, other), root)

        assert "BSP-GATE-ARCH evidence is bound to gate 'BSP-GATE-CAN', not BSP-GATE-ARCH" in errors

    def test_missing_blank_and_placeholder_fields_are_rejected(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        partial = self._record(root)
        del partial["firmware_versions"]
        assert "BSP-GATE-ARCH evidence is missing fields: firmware_versions" in validate_readiness(
            self._readiness(root, partial), root
        )

        blank = self._record(root, board_revision="  ", kernel_version="TBD_L4T_KERNEL_VERSION")
        errors = validate_readiness(self._readiness(root, blank), root)
        assert "BSP-GATE-ARCH evidence board_revision is blank or unresolved" in errors
        assert "BSP-GATE-ARCH evidence kernel_version is blank or unresolved" in errors

    def test_result_source_revision_and_timestamp_must_be_valid(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        failed = self._record(root, result="NOT_EXECUTED", source_revision="abc123")
        failed["timestamp_utc"] = "2026-10-20 04:05:06"

        errors = validate_readiness(self._readiness(root, failed), root)

        assert "BSP-GATE-ARCH evidence result NOT_EXECUTED cannot close a PASS gate" in errors
        assert "BSP-GATE-ARCH evidence source_revision must be a full commit hash" in errors
        assert "BSP-GATE-ARCH evidence timestamp_utc must be an ISO-8601 UTC timestamp" in errors

    def test_raw_capture_must_be_a_repository_file(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        escaped = self._record(root, raw_capture="../outside.log")

        errors = validate_readiness(self._readiness(root, escaped), root)

        assert "BSP-GATE-ARCH raw capture must reference a repository file" in errors

    def test_committed_readiness_keeps_physical_release_blocked(self) -> None:
        readiness = load_readiness()

        assert check() == []
        assert readiness["physical_release_ready"] is False
        assert readiness["status"] == BLOCKED_STATUS
        assert not [gate for gate in readiness["gates"] if gate["status"] == "PASS"]
