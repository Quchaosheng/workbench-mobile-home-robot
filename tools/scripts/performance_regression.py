#!/usr/bin/env python3
"""Compare local-software performance reports against a baseline and budget."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


class PerformanceGateError(ValueError):
    """A report or policy cannot be used as trustworthy comparison evidence."""


def _reject_constant(value: str) -> None:
    raise PerformanceGateError(f"non-finite JSON constant is not allowed: {value}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PerformanceGateError(f"cannot read JSON report: {path}") from exc
    if not isinstance(payload, dict):
        raise PerformanceGateError(f"report must be a JSON object: {path}")
    return payload


def _number(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise PerformanceGateError(f"{label} must be a finite non-negative number")
    return float(value)


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise PerformanceGateError(f"{label} must be a positive integer")
    return value


def _environment(report: dict[str, Any], kind: str) -> dict[str, str]:
    environment = report.get("environment")
    if not isinstance(environment, dict):
        raise PerformanceGateError(f"{kind} report has no environment object")
    result: dict[str, str] = {}
    for key in ("platform", "python", "machine"):
        value = environment.get(key)
        if not isinstance(value, str) or not value:
            raise PerformanceGateError(f"{kind} environment.{key} must be a non-empty string")
        result[key] = value
    return result


def extract_startup(report: dict[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    if report.get("schema_version") != 1:
        raise PerformanceGateError("startup report schema_version must be 1")
    cache_mode = report.get("cache_mode")
    if cache_mode not in {"standard", "disabled"}:
        raise PerformanceGateError("startup cache_mode must be standard or disabled")
    phases = report.get("phases_s")
    if not isinstance(phases, dict):
        raise PerformanceGateError("startup report has no phases_s object")
    metrics = {
        f"startup.{name}": _number(value, f"startup.phases_s.{name}")
        for name, value in phases.items()
        if value is not None
    }
    required = {
        "startup.container_start_to_health",
        "startup.container_start_to_ready",
        "startup.full_stack_clone_to_ready",
    }
    if not required.issubset(metrics):
        raise PerformanceGateError("startup report is missing required phases")
    environment = _environment(report, "startup")
    environment["cache_mode"] = cache_mode
    return metrics, environment


def extract_resources(report: dict[str, Any], minimum_samples: int) -> tuple[dict[str, float], dict[str, str]]:
    if report.get("schema_version") != 1:
        raise PerformanceGateError("resource report schema_version must be 1")
    if _positive_int(report.get("sample_count"), "resource sample_count") < minimum_samples:
        raise PerformanceGateError(f"resource report requires at least {minimum_samples} samples")
    resources = report.get("resources")
    if not isinstance(resources, dict) or not resources:
        raise PerformanceGateError("resource report has no container resources")
    disk_growth = report.get("event_log")
    if not isinstance(disk_growth, dict):
        raise PerformanceGateError("resource report has no event_log object")
    cpu_total = 0.0
    memory_total = 0.0
    for name, values in resources.items():
        if not isinstance(name, str) or not name or not isinstance(values, dict):
            raise PerformanceGateError("resource entries must map container names to objects")
        samples = _positive_int(values.get("samples"), f"resources.{name}.samples")
        if samples < minimum_samples:
            raise PerformanceGateError(f"resources.{name} requires at least {minimum_samples} samples")
        cpu_p50 = _number(values.get("cpu_percent_p50"), f"resources.{name}.cpu_percent_p50")
        cpu_p95 = _number(values.get("cpu_percent_p95"), f"resources.{name}.cpu_percent_p95")
        cpu_max = _number(values.get("cpu_percent_max"), f"resources.{name}.cpu_percent_max")
        memory_p50 = _number(values.get("memory_bytes_p50"), f"resources.{name}.memory_bytes_p50")
        memory_p95 = _number(values.get("memory_bytes_p95"), f"resources.{name}.memory_bytes_p95")
        memory_max = _number(values.get("memory_bytes_max"), f"resources.{name}.memory_bytes_max")
        if not cpu_p50 <= cpu_p95 <= cpu_max:
            raise PerformanceGateError(f"resources.{name} CPU percentiles are not ordered")
        if not memory_p50 <= memory_p95 <= memory_max:
            raise PerformanceGateError(f"resources.{name} memory percentiles are not ordered")
        cpu_total += cpu_p95
        memory_total += memory_p95
    metrics = {
        "resources.cpu_percent_p95_total": cpu_total,
        "resources.memory_bytes_p95_total": memory_total,
        "resources.event_log_bytes_max": _number(disk_growth.get("max_bytes"), "event_log.max_bytes"),
        "resources.event_log_growth_bytes_per_run": _number(
            disk_growth.get("growth_bytes_per_run"), "event_log.growth_bytes_per_run"
        ),
        "resources.concurrent_runs_max": _number(
            disk_growth.get("concurrent_runs_max"), "event_log.concurrent_runs_max"
        ),
    }
    api = report.get("api")
    if not isinstance(api, dict):
        raise PerformanceGateError("resource report has no api object")
    metrics["api.latency_p95_ms"] = _number(api.get("latency_p95_ms"), "api.latency_p95_ms")
    metrics["api.latency_p99_ms"] = _number(api.get("latency_p99_ms"), "api.latency_p99_ms")
    metrics["api.failure_rate"] = _number(api.get("failure_rate"), "api.failure_rate")
    if metrics["api.latency_p95_ms"] > metrics["api.latency_p99_ms"]:
        raise PerformanceGateError("api latency percentiles are not ordered")
    return metrics, _environment(report, "resource")


def extract_telemetry(report: dict[str, Any], minimum_samples: int) -> tuple[dict[str, float], dict[str, str]]:
    if report.get("schema_version") != 1:
        raise PerformanceGateError("telemetry report schema_version must be 1")
    sources = report.get("sources")
    if not isinstance(sources, dict) or set(sources) != {"simulation"}:
        raise PerformanceGateError("software regression accepts simulation telemetry only")
    simulation = sources["simulation"]
    stages = simulation.get("stages") if isinstance(simulation, dict) else None
    if not isinstance(stages, dict) or not stages:
        raise PerformanceGateError("telemetry report has no simulation stages")
    metrics: dict[str, float] = {}
    for name, values in stages.items():
        if not isinstance(name, str) or not name or not isinstance(values, dict):
            raise PerformanceGateError("telemetry stages must map names to objects")
        samples = _positive_int(values.get("samples"), f"telemetry.{name}.samples")
        if samples < minimum_samples:
            raise PerformanceGateError(f"telemetry.{name} requires at least {minimum_samples} samples")
        if values.get("unit") != "ms":
            raise PerformanceGateError(f"telemetry.{name} must declare its unit as ms")
        p50 = _number(values.get("p50_ms"), f"telemetry.{name}.p50_ms")
        p95 = _number(values.get("p95_ms"), f"telemetry.{name}.p95_ms")
        maximum = _number(values.get("max_ms"), f"telemetry.{name}.max_ms")
        p99 = _number(values.get("p99_ms"), f"telemetry.{name}.p99_ms")
        if not p50 <= p95 <= p99 <= maximum:
            raise PerformanceGateError(f"telemetry.{name} percentiles are not ordered")
        metrics[f"telemetry.{name}.p95_ms"] = p95
        metrics[f"telemetry.{name}.p99_ms"] = p99
    failures = _failure_rate(simulation.get("failures"))
    metrics["telemetry.failure_rate"] = failures
    return metrics, _environment(report, "telemetry")


def _failure_rate(failures: object) -> float:
    """Read one aggregate failure rate from a source's counted failures.

    The count must be internally consistent, because a producer that under-
    reports its own denominator would otherwise lower the measured rate.
    """
    if not isinstance(failures, dict) or not failures:
        raise PerformanceGateError("telemetry report has no counted failures")
    total = 0
    failed = 0
    for name, counts in failures.items():
        if not isinstance(name, str) or not name or not isinstance(counts, dict):
            raise PerformanceGateError("telemetry failures must map sources to counted objects")
        source_total = _positive_int(counts.get("total"), f"telemetry.failures.{name}.total")
        source_failed = _number(counts.get("failed"), f"telemetry.failures.{name}.failed")
        if not float(source_failed).is_integer() or source_failed > source_total:
            raise PerformanceGateError(f"telemetry.failures.{name} failed count is inconsistent")
        rate = _number(counts.get("failure_rate"), f"telemetry.failures.{name}.failure_rate")
        if abs(rate - source_failed / source_total) > 1e-9:
            raise PerformanceGateError(f"telemetry.failures.{name} failure_rate does not match its counts")
        total += source_total
        failed += int(source_failed)
    if total == 0:
        raise PerformanceGateError("telemetry failure counts have a zero denominator")
    return failed / total


def _check_environment(kind: str, baseline: dict[str, str], current: dict[str, str]) -> None:
    if baseline != current:
        raise PerformanceGateError(
            f"{kind} reports are not comparable: baseline environment {baseline!r} != current {current!r}"
        )


def _required_reports(policy: dict[str, Any]) -> list[str]:
    required = policy.get("required_reports")
    supported = {"startup", "resources", "telemetry"}
    if not isinstance(required, list) or not required or any(kind not in supported for kind in required):
        raise PerformanceGateError("policy required_reports must list supported report kinds")
    if len(required) != len(set(required)):
        raise PerformanceGateError("policy required_reports must be unique")
    return required


def evaluate(
    baseline_reports: dict[str, dict[str, Any]],
    current_reports: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    if policy.get("schema_version") != 1 or policy.get("evidence_class") != "local_software":
        raise PerformanceGateError("policy must be schema version 1 for local_software evidence")
    required = _required_reports(policy)
    if set(baseline_reports) != set(required) or set(current_reports) != set(required):
        raise PerformanceGateError("baseline and current reports must exactly match required_reports")
    minimum_samples = _positive_int(policy.get("minimum_samples"), "policy minimum_samples")
    extractors = {
        "startup": extract_startup,
        "resources": lambda report: extract_resources(report, minimum_samples),
        "telemetry": lambda report: extract_telemetry(report, minimum_samples),
    }
    baseline_metrics: dict[str, float] = {}
    current_metrics: dict[str, float] = {}
    for kind in required:
        baseline_values, baseline_environment = extractors[kind](baseline_reports[kind])
        current_values, current_environment = extractors[kind](current_reports[kind])
        _check_environment(kind, baseline_environment, current_environment)
        baseline_metrics.update(baseline_values)
        current_metrics.update(current_values)

    metric_policy = policy.get("metrics")
    if not isinstance(metric_policy, dict) or not metric_policy:
        raise PerformanceGateError("policy metrics must be a non-empty object")
    checks = []
    for name, limits in metric_policy.items():
        if name not in baseline_metrics or name not in current_metrics or not isinstance(limits, dict):
            raise PerformanceGateError(f"policy metric is unavailable: {name}")
        maximum = _number(limits.get("maximum"), f"policy.metrics.{name}.maximum")
        regression_percent = _number(
            limits.get("max_regression_percent"), f"policy.metrics.{name}.max_regression_percent"
        )
        noise_tolerance = _number(limits.get("noise_tolerance"), f"policy.metrics.{name}.noise_tolerance")
        baseline_value = baseline_metrics[name]
        current_value = current_metrics[name]
        relative_limit = baseline_value * (1 + regression_percent / 100) + noise_tolerance
        passed = current_value <= maximum and current_value <= relative_limit
        checks.append(
            {
                "metric": name,
                "baseline": baseline_value,
                "current": current_value,
                "absolute_limit": maximum,
                "regression_limit": relative_limit,
                "status": "PASS" if passed else "FAIL",
            }
        )

    passed = all(check["status"] == "PASS" for check in checks)
    return {
        "schema_version": 1,
        "baseline_required": True,
        "evidence_class": "local_software",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "target_hardware_measurement": "NOT_EXECUTED",
        "physical_source_validation": "NOT_EXECUTED",
    }


def evaluate_budgets(current_reports: dict[str, dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    """Check absolute budgets only, for a host with no committed baseline.

    The relative limit needs a comparable baseline, so it is reported as not
    applied instead of being silently treated as a pass. A metric that exceeds
    its absolute budget still fails, so this mode can gate a scheduled run
    without pretending to have measured a regression.
    """
    if policy.get("schema_version") != 1 or policy.get("evidence_class") != "local_software":
        raise PerformanceGateError("policy must be schema version 1 for local_software evidence")
    required = _required_reports(policy)
    unknown = set(current_reports) - set(required)
    if unknown or not current_reports:
        raise PerformanceGateError("current reports must be a non-empty subset of required_reports")
    minimum_samples = _positive_int(policy.get("minimum_samples"), "policy minimum_samples")
    extractors = {
        "startup": extract_startup,
        "resources": lambda report: extract_resources(report, minimum_samples),
        "telemetry": lambda report: extract_telemetry(report, minimum_samples),
    }
    current_metrics: dict[str, float] = {}
    for kind in sorted(current_reports):
        values, _environment = extractors[kind](current_reports[kind])
        current_metrics.update(values)
    kind_for_prefix = {"startup": "startup", "resources": "resources", "api": "resources", "telemetry": "telemetry"}
    metric_policy = policy.get("metrics")
    if not isinstance(metric_policy, dict) or not metric_policy:
        raise PerformanceGateError("policy metrics must be a non-empty object")
    checks = []
    not_evaluated = []
    for name, limits in metric_policy.items():
        if not isinstance(limits, dict):
            raise PerformanceGateError(f"policy.metrics.{name} must be an object")
        maximum = _number(limits.get("maximum"), f"policy.metrics.{name}.maximum")
        if name not in current_metrics:
            kind = kind_for_prefix.get(name.partition(".")[0])
            if kind is None:
                raise PerformanceGateError(f"policy metric has no report kind: {name}")
            # The report that owns this metric was not supplied. Recording it as
            # not evaluated keeps a partial run from reading as a full pass.
            not_evaluated.append({"metric": name, "report_kind": kind, "reason": "report not supplied"})
            continue
        current_value = current_metrics[name]
        checks.append(
            {
                "metric": name,
                "current": current_value,
                "absolute_limit": maximum,
                "regression_limit": None,
                "status": "PASS" if current_value <= maximum else "FAIL",
            }
        )
    failed = any(check["status"] == "FAIL" for check in checks)
    status = "FAIL" if failed else ("INCOMPLETE" if not_evaluated else "PASS")
    return {
        "schema_version": 1,
        "baseline_required": False,
        "evidence_class": "local_software",
        "status": status,
        "checks": checks,
        "not_evaluated": not_evaluated,
        "evaluated_reports": sorted(current_reports),
        "target_hardware_measurement": "NOT_EXECUTED",
        "physical_source_validation": "NOT_EXECUTED",
        "note": "Absolute budgets only; no baseline was supplied, so no relative regression was measured.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--budgets-only", action="store_true")
    for kind in ("startup", "resources", "telemetry"):
        parser.add_argument(f"--baseline-{kind}", type=Path)
        parser.add_argument(f"--current-{kind}", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        policy = load_json(args.policy)
        required = _required_reports(policy)
        baseline = {
            kind: load_json(getattr(args, f"baseline_{kind}"))
            for kind in required
            if getattr(args, f"baseline_{kind}") is not None
        }
        current = {
            kind: load_json(getattr(args, f"current_{kind}"))
            for kind in required
            if getattr(args, f"current_{kind}") is not None
        }
        if args.budgets_only:
            if not current:
                raise PerformanceGateError("--budgets-only requires at least one current report")
            report = evaluate_budgets(current, policy)
        else:
            report = evaluate(baseline, current, policy)
    except PerformanceGateError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, allow_nan=False))
    # Three outcomes stay distinguishable at the shell: a full pass, a budget
    # breach, and a partial run whose unsupplied reports were never checked.
    if report["status"] == "PASS":
        return 0
    if report["status"] == "INCOMPLETE":
        print("budget gate is incomplete: some policy metrics had no report", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
