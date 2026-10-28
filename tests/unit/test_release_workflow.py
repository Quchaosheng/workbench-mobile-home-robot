from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release-image.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _step_block(workflow: str, marker: str) -> str:
    start = workflow.index(marker)
    next_step = workflow.find("\n      - ", start + len(marker))
    return workflow[start:] if next_step == -1 else workflow[start:next_step]


def _pinned_action(workflow: str, action: str) -> str:
    """The `action@<sha>` reference a workflow actually uses.

    Read from the workflow rather than repeated here: a Dependabot bump changes
    the pin, and a test that hardcodes it turns every bump into a red build for a
    reason that has nothing to do with the bump (issue #404).
    """

    prefix = f"uses: {action}@"
    start = workflow.index(prefix) + len(prefix)
    end = start
    while end < len(workflow) and workflow[end] not in " \t\r\n#":
        end += 1
    reference = workflow[start:end]
    assert len(reference) == 40 and all(character in "0123456789abcdef" for character in reference), (
        f"{action} is not pinned to a full commit sha: {reference!r}"
    )
    return reference


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
    assert f"actions/upload-artifact@{_pinned_action(workflow, 'actions/upload-artifact')}" in artifact_step
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
    assert f"actions/upload-artifact@{_pinned_action(workflow, 'actions/upload-artifact')}" in artifact_step
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
