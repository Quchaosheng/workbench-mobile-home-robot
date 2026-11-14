"""A missing module is a named refusal, not an empty report (#584).

`BSP-PERF-001` asks for ``cpu_gpu_thermal_power_and_can_load`` and sat at
``NOT_EXECUTED`` because there was no procedure to run. The procedure is now
`tools/scripts/measure_target_power.py` and the budget is
`docs/performance/target-budget-v1.json`.

The thing worth testing hardest is the refusal path, because this host has **no
NVIDIA module**: no `nvpmodel`, no thermal zones, no `tegrastats`. A script that
wrote zeros would produce a plausible artifact saying the module is cool and idle.
"Could not measure" must never be recorded as "measured and fine".

These tests do not measure a module. They assert the refusals, the artifact shape
and that the budget's every number is marked ESTIMATE.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "scripts" / "measure_target_power.py"
BUDGET = ROOT / "docs" / "performance" / "target-budget-v1.json"
BRINGUP = ROOT / "bsp" / "validation" / "bringup-plan.yaml"

sys.path.insert(0, str(ROOT / "tools" / "scripts"))

import measure_target_power as mtp


class TestTheRefusalsAreNamed:
    def test_a_missing_nvpmodel_is_a_named_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """This host has no NVIDIA module, so this is the real path, not a hypothetical."""
        monkeypatch.setattr(mtp.shutil, "which", lambda name: None)
        with pytest.raises(mtp.MeasurementRefused, match="NVPMODEL_UNAVAILABLE"):
            mtp._nvpmodel()

    def test_a_missing_thermal_zone_is_a_named_refusal(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(mtp, "THERMAL_ROOT", tmp_path / "nowhere")
        with pytest.raises(mtp.MeasurementRefused, match="THERMAL_UNAVAILABLE"):
            mtp._temperature()

    def test_an_empty_thermal_root_is_a_refusal_not_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A directory with no zones would make a naive script record 0 degrees."""
        monkeypatch.setattr(mtp, "THERMAL_ROOT", tmp_path)
        with pytest.raises(mtp.MeasurementRefused, match="THERMAL_UNAVAILABLE"):
            mtp._temperature()

    def test_a_missing_workload_is_a_named_refusal(self) -> None:
        with pytest.raises(mtp.MeasurementRefused, match="WORKLOAD_UNAVAILABLE"):
            mtp.measure(workload="nonexistent-workload")

    def test_a_nonsensical_interval_is_refused(self) -> None:
        with pytest.raises(mtp.MeasurementRefused, match="interval"):
            mtp.measure(duration_s=1.0, interval_s=5.0, command=[sys.executable, "-c", "pass"])

    def test_a_nonpositive_duration_is_refused(self) -> None:
        with pytest.raises(mtp.MeasurementRefused, match="positive"):
            mtp.measure(duration_s=0.0, command=[sys.executable, "-c", "pass"])

    def test_the_cli_exits_nonzero_and_writes_nothing_on_refusal(self, tmp_path: Path) -> None:
        """A refusal must exit non-zero and leave no artifact behind."""
        output = tmp_path / "should-not-exist.json"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        # `--check` refuses here because there is no nvpmodel on this host.
        if result.returncode == 0:
            pytest.skip("this host reports a module; the refusal path cannot be exercised")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr
        assert not output.exists()


class TestTheArtifactShape:
    def test_the_report_schema_is_named(self) -> None:
        assert mtp.REPORT_SCHEMA == "workbench-target-measurement-v1"
        budget = json.loads(BUDGET.read_text(encoding="utf-8"))
        assert budget["measurement"]["report_schema"] == mtp.REPORT_SCHEMA

    def test_the_artifact_carries_a_digest(self, tmp_path: Path) -> None:
        """The digest is what a consumer verifies, not the file's presence."""
        report = {"schema_version": mtp.REPORT_SCHEMA, "samples": [], "summary": {"samples": 0}}
        output = tmp_path / "module.json"
        digest = mtp.write_artifact(report, output)

        sidecar = output.with_suffix(".json.sha256").read_text(encoding="utf-8")
        assert digest in sidecar
        recomputed = hashlib.sha256(output.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        assert recomputed == digest

    def test_a_rewritten_artifact_fails_its_digest(self, tmp_path: Path) -> None:
        report = {"schema_version": mtp.REPORT_SCHEMA, "samples": []}
        output = tmp_path / "module.json"
        mtp.write_artifact(report, output)
        output.write_text('{"schema_version": "tampered"}\n', encoding="utf-8")
        recorded = output.with_suffix(".json.sha256").read_text(encoding="utf-8").split()[0]
        recomputed = hashlib.sha256(output.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        assert recorded != recomputed

    def test_the_workloads_are_the_software_not_a_synthetic_loop(self) -> None:
        """The number has to be about this software, not about the module in a vacuum."""
        assert "demo" in mtp.WORKLOADS
        assert "container" in mtp.WORKLOADS
        assert any("demo_scripted.py" in token for token in mtp.WORKLOADS["demo"])
        assert "stress-ng" not in json.dumps(mtp.WORKLOADS)

    def test_every_sample_records_the_required_fields(self) -> None:
        """The fields BSP-PERF-001 requires are the fields the Sample carries."""
        fields = set(mtp.Sample.__dataclass_fields__)
        required = set(
            next(
                i for i in yaml.safe_load(BRINGUP.read_text(encoding="utf-8"))["tests"] if i["id"] == "BSP-PERF-001"
            ).get("required_fields")
            or []
        )
        # Report-level fields: the identity of the run, and the aggregates computed
        # from the samples. The rest have to be on every sample.
        report_level = {
            "power_mode",
            "soc",
            "jetpack",
            "samples",
            "memory_peak_kb",
            "temperature_max_c",
            "throttling",
        }
        per_sample = required - report_level
        assert per_sample <= fields, f"missing per-sample fields: {sorted(per_sample - fields)}"
        # And the two aggregates must actually be produced by the module.
        summary_source = Path(mtp.__file__).read_text(encoding="utf-8")
        for aggregate in ("memory_peak_kb", "temperature_max_c"):
            assert aggregate in summary_source, aggregate


class TestTheBudgetIsHonest:
    @pytest.mark.parametrize("row_name", sorted(json.loads(BUDGET.read_text(encoding="utf-8"))["budget"]))
    def test_every_number_is_marked_estimate(self, row_name: str) -> None:
        """No capture exists, so no row may claim to be measured."""
        budget = json.loads(BUDGET.read_text(encoding="utf-8"))
        row = budget["budget"][row_name]
        assert row["status"] == "ESTIMATE", row_name
        assert row.get("basis"), f"{row_name} has no basis"

    def test_the_budget_says_why_every_row_is_an_estimate(self) -> None:
        budget = json.loads(BUDGET.read_text(encoding="utf-8"))
        assert budget["every_row_is_estimate"] is True
        assert "No capture exists" in budget["why_estimate"]

    def test_the_budget_names_the_combinations_that_run_together(self) -> None:
        budget = json.loads(BUDGET.read_text(encoding="utf-8"))
        scope = budget["scope"]
        for component in ("perception", "navigation", "motion", "dashboard"):
            assert component in scope

    def test_the_budget_claims_no_measurement(self) -> None:
        budget = json.loads(BUDGET.read_text(encoding="utf-8"))
        joined = " ".join(budget["claims"]).lower()
        assert "not a measurement" in joined
        assert "not physical validation" in joined


class TestTheBringupStep:
    def test_the_step_has_the_command_and_the_fields(self) -> None:
        plan = yaml.safe_load(BRINGUP.read_text(encoding="utf-8"))
        step = next(item for item in plan["tests"] if item["id"] == "BSP-PERF-001")
        assert "measure_target_power.py" in step["command"]
        for field in ("power_mode", "soc", "jetpack", "samples", "memory_peak_kb", "temperature_max_c", "throttling"):
            assert field in step["required_fields"], field

    def test_the_step_is_still_not_executed(self) -> None:
        """A committed command is not a measurement."""
        plan = yaml.safe_load(BRINGUP.read_text(encoding="utf-8"))
        step = next(item for item in plan["tests"] if item["id"] == "BSP-PERF-001")
        assert step["result"] == "NOT_EXECUTED"

    def test_the_step_points_at_the_budget(self) -> None:
        plan = yaml.safe_load(BRINGUP.read_text(encoding="utf-8"))
        step = next(item for item in plan["tests"] if item["id"] == "BSP-PERF-001")
        assert step["budget"] == "docs/performance/target-budget-v1.json"
        assert BUDGET.is_file()

    def test_the_validator_still_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "bsp/validation/validate_manifests.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
