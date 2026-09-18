import json
import subprocess
import sys
from pathlib import Path

import pytest
from performance_regression import PerformanceGateError, evaluate, evaluate_budgets, load_json

ROOT = Path(__file__).resolve().parents[2]


ENVIRONMENT = {"platform": "test-linux", "python": "3.12.0", "machine": "x86_64"}


def startup(ready: float = 1.0, full: float = 8.0) -> dict:
    return {
        "schema_version": 1,
        "cache_mode": "standard",
        "environment": ENVIRONMENT,
        "phases_s": {
            "image_build": 7.0,
            "container_start_to_health": ready,
            "container_start_to_ready": ready,
            "full_stack_clone_to_ready": full,
        },
    }


def resources(
    cpu: float = 2.0,
    memory: float = 16_000_000,
    *,
    event_log_bytes: float = 4_000_000,
    growth_per_run: float = 40_000,
    concurrent_runs: float = 2,
    api_p95: float = 12.0,
    api_p99: float = 15.0,
    api_failure_rate: float = 0.0,
) -> dict:
    return {
        "schema_version": 1,
        "sample_count": 5,
        "environment": ENVIRONMENT,
        "resources": {
            "dashboard": {
                "samples": 5,
                "cpu_percent_p50": cpu / 2,
                "cpu_percent_p95": cpu,
                "cpu_percent_max": cpu + 1,
                "memory_bytes_p50": memory - 1_000_000,
                "memory_bytes_p95": memory,
                "memory_bytes_max": memory + 1_000_000,
            }
        },
        "event_log": {
            "max_bytes": event_log_bytes,
            "growth_bytes": growth_per_run * concurrent_runs,
            "growth_bytes_per_run": growth_per_run,
            "completed_runs": concurrent_runs,
            "concurrent_runs_max": concurrent_runs,
        },
        "api": {
            "samples": 5,
            "latency_p50_ms": api_p95 / 2,
            "latency_p95_ms": api_p95,
            "latency_p99_ms": api_p99,
            "failure_rate": api_failure_rate,
            "unit": "ms",
        },
    }


def telemetry(p95: float = 10.0, *, failed: int = 1, total: int = 30) -> dict:
    return {
        "schema_version": 1,
        "environment": ENVIRONMENT,
        "sources": {
            "simulation": {
                "stages": {
                    "end_to_end": {
                        "samples": 30,
                        "p50_ms": p95 / 2,
                        "p95_ms": p95,
                        "p99_ms": p95 + 0.5,
                        "max_ms": p95 + 1,
                        "unit": "ms",
                    }
                },
                "failures": {
                    "simulation": {
                        "total": total,
                        "failed": failed,
                        "failure_rate": failed / total,
                        "by_event": {} if failed == 0 else {"stage_failed": failed},
                    }
                },
            }
        },
    }


def policy() -> dict:
    path = ROOT / "docs" / "performance" / "software-regression-policy-v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def bundle(
    *,
    ready: float = 1.0,
    full: float = 8.0,
    cpu: float = 2.0,
    memory: float = 16_000_000,
    p95: float = 10.0,
    **resource_overrides: float,
) -> dict:
    return {
        "startup": startup(ready, full),
        "resources": resources(cpu, memory, **resource_overrides),
        "telemetry": telemetry(p95),
    }


def test_gate_passes_comparable_reports_within_budgets() -> None:
    current = bundle(ready=1.1, full=9.0, cpu=2.2, memory=17_000_000, p95=11.0)
    report = evaluate(bundle(), current, policy())

    assert report["status"] == "PASS"
    assert all(check["status"] == "PASS" for check in report["checks"])
    assert report["evidence_class"] == "local_software"
    assert report["target_hardware_measurement"] == "NOT_EXECUTED"


def test_gate_fails_relative_regression_even_below_absolute_budget() -> None:
    report = evaluate(bundle(), bundle(p95=20.0), policy())

    failed = {check["metric"] for check in report["checks"] if check["status"] == "FAIL"}
    assert report["status"] == "FAIL"
    # Both latency percentiles regress together: the fixture derives p99 from
    # p95, so neither is below its relative limit even though both stay below
    # their absolute budgets.
    assert failed == {"telemetry.end_to_end.p95_ms", "telemetry.end_to_end.p99_ms"}
    for check in report["checks"]:
        if check["metric"] in failed:
            assert check["current"] < check["absolute_limit"]


def test_gate_fails_absolute_budget_even_with_slow_baseline() -> None:
    report = evaluate(bundle(full=119.0), bundle(full=121.0), policy())

    check = next(item for item in report["checks"] if item["metric"] == "startup.full_stack_clone_to_ready")
    assert check["status"] == "FAIL"
    assert check["current"] > check["absolute_limit"]


def test_gate_rejects_incomparable_environment_and_cache_mode() -> None:
    current = bundle()
    current["startup"]["cache_mode"] = "disabled"
    with pytest.raises(PerformanceGateError, match="not comparable"):
        evaluate(bundle(), current, policy())

    current = bundle()
    current["resources"]["environment"] = {**ENVIRONMENT, "python": "3.13.0"}
    with pytest.raises(PerformanceGateError, match="not comparable"):
        evaluate(bundle(), current, policy())


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, True, "slow"])
def test_gate_rejects_invalid_numeric_evidence(invalid: object) -> None:
    current = bundle()
    current["telemetry"]["sources"]["simulation"]["stages"]["end_to_end"]["p95_ms"] = invalid
    with pytest.raises(PerformanceGateError, match="finite non-negative"):
        evaluate(bundle(), current, policy())


def test_gate_rejects_hardware_telemetry_and_insufficient_samples() -> None:
    current = bundle()
    current["telemetry"]["sources"]["hardware"] = current["telemetry"]["sources"]["simulation"]
    with pytest.raises(PerformanceGateError, match="simulation telemetry only"):
        evaluate(bundle(), current, policy())

    current = bundle()
    current["resources"]["sample_count"] = 4
    with pytest.raises(PerformanceGateError, match="at least 5 samples"):
        evaluate(bundle(), current, policy())


def test_json_loader_rejects_non_finite_constants(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(PerformanceGateError, match="non-finite"):
        load_json(path)


def test_gate_fails_the_new_disk_and_api_budgets() -> None:
    """Issue #79: disk growth, concurrency, API latency and failure rate are budgeted."""
    cases = {
        "resources.event_log_bytes_max": {"event_log_bytes": 2_000_000_000},
        "resources.event_log_growth_bytes_per_run": {"growth_per_run": 11_000_000},
        "resources.concurrent_runs_max": {"concurrent_runs": 65},
        "api.latency_p95_ms": {"api_p95": 300.0, "api_p99": 400.0},
        "api.latency_p99_ms": {"api_p95": 300.0, "api_p99": 600.0},
        "api.failure_rate": {"api_failure_rate": 0.5},
    }
    for metric, overrides in cases.items():
        report = evaluate(bundle(), bundle(**overrides), policy())
        failed = {check["metric"] for check in report["checks"] if check["status"] == "FAIL"}
        assert report["status"] == "FAIL", f"{metric} did not fail the gate"
        assert metric in failed, f"{metric} missing from {sorted(failed)}"


def test_gate_fails_the_telemetry_failure_rate_budget() -> None:
    current = bundle()
    current["telemetry"]["sources"]["simulation"]["failures"]["simulation"].update(
        {"failed": 10, "failure_rate": 10 / 30, "by_event": {"stage_failed": 10}}
    )
    report = evaluate(bundle(), current, policy())
    failed = {check["metric"] for check in report["checks"] if check["status"] == "FAIL"}
    assert report["status"] == "FAIL"
    assert "telemetry.failure_rate" in failed


def test_gate_rejects_a_failure_rate_that_contradicts_its_own_counts() -> None:
    current = bundle()
    current["telemetry"]["sources"]["simulation"]["failures"]["simulation"]["failure_rate"] = 0.0
    with pytest.raises(PerformanceGateError, match="does not match its counts"):
        evaluate(bundle(), current, policy())


def test_gate_rejects_an_unlabelled_stage_and_an_unordered_p99() -> None:
    current = bundle()
    del current["telemetry"]["sources"]["simulation"]["stages"]["end_to_end"]["unit"]
    with pytest.raises(PerformanceGateError, match="unit as ms"):
        evaluate(bundle(), current, policy())

    current = bundle()
    current["telemetry"]["sources"]["simulation"]["stages"]["end_to_end"]["p99_ms"] = 0.1
    with pytest.raises(PerformanceGateError, match="percentiles are not ordered"):
        evaluate(bundle(), current, policy())


def test_gate_rejects_a_report_without_disk_or_api_evidence() -> None:
    for kind in ("event_log", "api"):
        current = bundle()
        del current["resources"][kind]
        with pytest.raises(PerformanceGateError, match=kind):
            evaluate(bundle(), current, policy())


def test_budgets_only_mode_marks_missing_reports_incomplete_not_passing() -> None:
    """A scheduled run without Docker must not read as a full pass."""
    report = evaluate_budgets({"telemetry": telemetry()}, policy())
    assert report["baseline_required"] is False
    assert report["status"] == "INCOMPLETE"
    assert report["evaluated_reports"] == ["telemetry"]
    missing = {entry["metric"] for entry in report["not_evaluated"]}
    assert "resources.event_log_bytes_max" in missing
    assert "api.latency_p95_ms" in missing
    assert "startup.container_start_to_ready" in missing
    # Every metric that was supplied is still checked against its budget.
    assert any(check["metric"] == "telemetry.end_to_end.p95_ms" for check in report["checks"])
    assert all(check["regression_limit"] is None for check in report["checks"])


def test_budgets_only_mode_fails_when_a_supplied_metric_exceeds_its_budget() -> None:
    report = evaluate_budgets({"telemetry": telemetry(p95=60.0)}, policy())
    assert report["status"] == "FAIL"
    failed = {check["metric"] for check in report["checks"] if check["status"] == "FAIL"}
    assert "telemetry.end_to_end.p95_ms" in failed


def test_budgets_only_mode_passes_only_when_every_report_is_supplied() -> None:
    reports = {"startup": startup(), "resources": resources(), "telemetry": telemetry()}
    report = evaluate_budgets(reports, policy())
    assert report["status"] == "PASS"
    assert report["not_evaluated"] == []


def test_budgets_only_mode_rejects_an_unknown_or_empty_report_set() -> None:
    with pytest.raises(PerformanceGateError, match="non-empty subset"):
        evaluate_budgets({}, policy())
    with pytest.raises(PerformanceGateError, match="non-empty subset"):
        evaluate_budgets({"unknown": telemetry()}, policy())


BUDGET_WORKFLOW = ROOT / ".github" / "workflows" / "performance-budget.yml"
SCRIPTED_POLICY = ROOT / "docs" / "performance" / "software-budget-policy-scripted-v1.json"


def test_scheduled_budget_job_runs_the_docker_free_policy_it_declares() -> None:
    """The scheduled gate must invoke the scope it actually claims."""
    workflow = BUDGET_WORKFLOW.read_text(encoding="utf-8")
    policy = json.loads(SCRIPTED_POLICY.read_text(encoding="utf-8"))

    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "make performance-budget-check" in workflow
    assert "if-no-files-found: error" in workflow
    assert "NOT_EXECUTED" in workflow
    # A scheduled runner has no committed baseline, so it can only claim budgets.
    assert policy["required_reports"] == ["telemetry"]
    assert set(policy["metrics"]) == {
        "telemetry.end_to_end.p95_ms",
        "telemetry.end_to_end.p99_ms",
        "telemetry.failure_rate",
    }
    for name, limits in policy["metrics"].items():
        assert limits["maximum"] > 0, name
    assert policy["scope"]


def test_the_make_target_uses_the_scripted_policy_and_budgets_only() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("performance-budget-check:", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]

    assert "software-budget-policy-scripted-v1.json" in target
    assert "--budgets-only" in target
    assert "--current-telemetry" in target
    # A scheduled job must not depend on Docker, which the container targets need.
    assert "docker" not in target


def test_cli_writes_failed_report_and_exits_nonzero(tmp_path: Path) -> None:
    paths = {}
    for prefix, reports in (("baseline", bundle()), ("current", bundle(p95=20.0))):
        for kind, payload in reports.items():
            path = tmp_path / f"{prefix}-{kind}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            paths[f"{prefix}-{kind}"] = path
    output = tmp_path / "gate.json"
    command = [
        sys.executable,
        str(ROOT / "tools" / "scripts" / "performance_regression.py"),
        "--policy",
        str(ROOT / "docs" / "performance" / "software-regression-policy-v1.json"),
    ]
    for kind in ("startup", "resources", "telemetry"):
        command.extend([f"--baseline-{kind}", str(paths[f"baseline-{kind}"])])
        command.extend([f"--current-{kind}", str(paths[f"current-{kind}"])])
    command.extend(["--output", str(output)])

    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    assert completed.returncode == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert any(check["status"] == "FAIL" for check in report["checks"])
