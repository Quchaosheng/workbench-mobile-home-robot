from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release-image.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _step_block(workflow: str, marker: str) -> str:
    start = workflow.index(marker)
    next_step = workflow.find("\n      - ", start + len(marker))
    return workflow[start:] if next_step == -1 else workflow[start:next_step]


def test_sbom_generation_keeps_release_workflow_least_privileged() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    permissions = workflow.split("jobs:", maxsplit=1)[0]
    assert "contents: read" in permissions
    assert "contents: write" not in permissions

    sbom_step = _step_block(workflow, "uses: anchore/sbom-action@")
    assert "upload-artifact: false" in sbom_step
    assert "upload-release-assets: false" in sbom_step

    artifact_step = _step_block(workflow, "uses: actions/upload-artifact@")
    assert "name: workbench-1-sbom" in artifact_step
    assert "if: always()" in artifact_step
    assert "path: |" in artifact_step
    assert "workbench-1.spdx.json" in artifact_step
    assert "workbench-1-grype.json" in artifact_step


def test_kernel_fault_suite_cannot_be_skipped_as_green() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    kernel_job = workflow.split("  kernel-module:", maxsplit=1)[1].split("\n  mcu-qemu:", maxsplit=1)[0]
    summary_step = _step_block(kernel_job, "name: Report runtime fault result")

    assert "continue-on-error" not in kernel_job
    assert "id: module_load" in kernel_job
    assert "id: fault_suite" in kernel_job
    assert "if: always()" in summary_step
    assert "PASS" in summary_step
    assert "NOT_EXECUTED" in summary_step
    assert "FAIL" in summary_step
    assert "Fault-mode coverage" in summary_step
    assert "PASS (7/7 advertised modes)" in summary_step
    assert "Fault-plane readiness" in summary_step
    assert "NOT_EXECUTED" in summary_step
    assert 'test "$status" = PASS' in summary_step
    assert "GITHUB_STEP_SUMMARY" in summary_step


def test_kernel_fault_suite_publishes_and_validates_structured_report() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    kernel_job = workflow.split("  kernel-module:", maxsplit=1)[1].split("\n  mcu-qemu:", maxsplit=1)[0]
    fault_step = _step_block(kernel_job, "name: Run the fault suite")
    validate_step = _step_block(kernel_job, "name: Validate structured driver test report")
    artifact_step = _step_block(kernel_job, "name: Upload driver test report")

    assert "WBCAN_TEST_REPORT" in fault_step
    assert "sudo env WBCAN_TEST_REPORT" in fault_step
    assert "kernel/wbcan/validate_test_report.py" in validate_step
    assert "id: fault_report" in validate_step
    assert "if: always()" in validate_step
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in artifact_step
    assert "wbcan-driver-test-report" in artifact_step
    assert "if-no-files-found: error" in artifact_step
    assert "REPORT_VALIDATION_OUTCOME" in kernel_job
    assert "success:success)" in kernel_job
    assert "status=PASS" in kernel_job


def test_kernel_job_publishes_and_requires_socketcan_ingress_evidence() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    kernel_job = workflow.split("  kernel-module:", maxsplit=1)[1].split("\n  mcu-qemu:", maxsplit=1)[0]
    probe_step = _step_block(kernel_job, "name: Run SocketCAN ingress boundary probe")
    validate_step = _step_block(kernel_job, "name: Validate SocketCAN ingress evidence")
    artifact_step = _step_block(kernel_job, "name: Upload SocketCAN ingress evidence")

    assert "id: socketcan_ingress" in probe_step
    assert "if: steps.module_load.outcome == 'success'" in probe_step
    assert "kernel/wbcan/test_socketcan_ingress.py wbcan0" in probe_step
    assert "--require-pass" in probe_step
    assert "--write-not-executed-report" in probe_step
    assert "if: always()" in validate_step
    assert "--validate-report" in validate_step
    assert "--require-pass" in validate_step
    assert "if: always()" in artifact_step
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in artifact_step
    assert "wbcan-socketcan-ingress-report" in artifact_step
    assert "if-no-files-found: error" in artifact_step


def test_kernel_module_builds_against_lts_and_runner_headers() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    kernel_job = workflow.split("  kernel-module:", maxsplit=1)[1].split("\n  mcu-qemu:", maxsplit=1)[0]
    build_step = _step_block(kernel_job, "name: Build and check wbcan.ko")

    assert "linux-headers-generic" in kernel_job
    assert "/usr/src/linux-headers-*-generic" in build_step
    assert 'KDIR="$headers"' in build_step
    assert "make -C kernel/wbcan clean" in build_step
    assert "make -C kernel/wbcan checkpatch" in build_step


def test_kernel_fault_suite_covers_ethtool_statistics() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    kernel_source = (ROOT / "kernel" / "wbcan" / "wbcan.c").read_text(encoding="utf-8")
    fault_suite = (ROOT / "kernel" / "wbcan" / "test_wbcan.sh").read_text(encoding="utf-8")
    kernel_job = workflow.split("  kernel-module:", maxsplit=1)[1].split("\n  mcu-qemu:", maxsplit=1)[0]

    assert "can-utils ethtool build-essential" in kernel_job
    assert "get_ethtool_stats" in kernel_source
    assert "get_sset_count" in kernel_source
    for name in (
        "tx_frames",
        "rx_frames",
        "fault_candidates",
        "fault_injected",
        "bus_errors",
        "arbitration_lost",
        "restart_attempts",
        "stop_attempts",
    ):
        assert f'"{name}"' in kernel_source
    assert "ethtool tx_frames advances" in fault_suite


def test_kernel_module_unload_verifies_singleton_cleanup() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    load_step = _step_block(workflow, "name: Load the module and bring the interface up")
    unload_step = _step_block(workflow, "name: Unload")

    assert "sudo ip link add wbcan0 type vcan" in load_step
    assert "name collision" in load_step
    assert "duplicate module insertion" in load_step
    assert "fail_debugfs=1" in load_step
    assert "test ! -e /sys/class/net/wbcan0" in unload_step
    assert "test ! -e /sys/kernel/debug/wbcan" in unload_step


def _step_index(workflow: str, name: str) -> int:
    return workflow.index(f"- name: {name}")


def test_no_release_tag_is_pushed_before_provenance_and_promotion_pass() -> None:
    """Issue #434: publishing first leaves a tag behind when a later step fails."""

    workflow = WORKFLOW.read_text(encoding="utf-8")

    staging = _step_index(workflow, "Push the staging image")
    manifest = _step_index(workflow, "Write release provenance manifest")
    verify = _step_index(workflow, "Verify release provenance")
    attest = _step_index(workflow, "Attest published image")
    promotion = _step_index(workflow, "Verify promotion readiness")
    publish = _step_index(workflow, "Publish the verified digest under the release tags")

    assert staging < manifest < verify < attest < promotion < publish, (
        "the release tags must not be published before the evidence that authorizes them"
    )


def test_the_staging_push_does_not_use_a_release_tag() -> None:
    """The staging tag has to be one no consumer would resolve as a release."""

    workflow = WORKFLOW.read_text(encoding="utf-8")
    staging_step = _step_block(workflow, "- name: Push the staging image")

    assert "staging-${{ github.sha }}" in staging_step
    # The only tags this step touches are the staging one; the release tag list is
    # not read here at all.
    assert "IMAGE_TAGS" not in staging_step


def test_the_final_publish_moves_every_release_tag_onto_the_verified_digest() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    publish_step = _step_block(workflow, "- name: Publish the verified digest under the release tags")

    # Pinned to the digest that was verified, not to a local image id that could
    # be retagged onto different bytes.
    assert "DIGEST: ${{ steps.stage.outputs.digest }}" in publish_step
    assert "@${DIGEST}" in publish_step
    assert "IMAGE_TAGS" in publish_step
    # A run that publishes nothing must fail rather than pass quietly.
    assert 'if [ "$published" -eq 0 ]' in publish_step


def test_attestation_targets_the_staged_digest() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    attest_step = _step_block(workflow, "uses: actions/attest-build-provenance@")

    assert "subject-digest: ${{ steps.stage.outputs.digest }}" in attest_step
    assert "steps.publish.outputs.digest" not in attest_step
