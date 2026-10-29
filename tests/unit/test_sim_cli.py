import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "tools" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from run_evaluation import scripted_events
from scenario_tools import EXTERNAL_STARTUP_GRACE_SECONDS
from sim_cli import (
    EXIT_CODES,
    SimulationInputError,
    _git_commit,
    doctor,
    load_scenarios,
    run_scenario,
)

FROZEN = ROOT / "sim" / "scenarios" / "frozen"


class SimulationCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario_path = FROZEN / "normal-001.json"
        cls.scenario = load_scenarios([cls.scenario_path])[0]

    def test_doctor_is_diagnostic_and_require_mode_is_fail_closed(self) -> None:
        report, exit_code = doctor()
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["scenario_count"], 36)
        self.assertFalse(report["simulator_ready"])
        _, required_exit = doctor(require_gazebo=True)
        self.assertEqual(required_exit, 2)
        with patch.dict(os.environ, {"WORKBENCH_GAZEBO_COMMAND": "missing-workbench-gazebo"}):
            configured_report, configured_exit = doctor(require_gazebo=True)
        self.assertTrue(configured_report["gazebo_command_configured"])
        self.assertFalse(configured_report["gazebo_command_available"])
        self.assertEqual(configured_exit, 2)

    def test_duplicate_manifest_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text(
                '{"scenario_id":"normal-001","scenario_id":"normal-002",'
                '"seed":1,"task_id":"task-place-red-block","world_version":"v",'
                '"fault_type":"none","timeout_s":1,"oracle_allowed":false}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SimulationInputError, "duplicate JSON key"):
                load_scenarios([path])

    def test_simulation_manifest_adapter_rejects_unknown_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unknown-extension.json"
            payload = dict(self.scenario.manifest)
            payload["unreviewed_extension"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(SimulationInputError, "unknown simulation manifest fields"):
                load_scenarios([path])

    def test_scripted_fixture_publishes_complete_non_release_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_scenario(self.scenario, runner="scripted", output_dir=Path(directory), version="test")
            artifact = Path(result.artifact_dir)
            self.assertEqual(result.status, "SCRIPTED_FIXTURE")
            self.assertEqual(result.exit_code, EXIT_CODES["SCRIPTED_FIXTURE"])
            self.assertFalse(result.release_eligible)
            self.assertFalse(result.executed)
            for name in (
                "source-manifest.json",
                "scene.json",
                "events.jsonl",
                "stdout.log",
                "stderr.log",
                "metadata.json",
                "checksums.sha256",
            ):
                self.assertTrue((artifact / name).is_file(), name)
            self.assertEqual((artifact / "source-manifest.json").read_bytes(), self.scenario.raw_bytes)
            metadata = json.loads((artifact / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["scene_hash"], self.scenario.scene_hash)
            self.assertEqual(metadata["status"], "SCRIPTED_FIXTURE")
            self.assertIn("events.jsonl", metadata["evidence_paths"])
            self.assertIn("checksums.sha256", metadata["evidence_paths"])
            checksums = (artifact / "checksums.sha256").read_text(encoding="utf-8")
            self.assertNotIn("checksums.sha256", checksums)
            self.assertFalse(list(Path(directory).glob("*.partial")))

    def test_missing_gazebo_adapter_is_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_scenario(self.scenario, runner="gazebo", output_dir=Path(directory), version="test")
            self.assertEqual(result.status, "NOT_EXECUTED")
            self.assertEqual(result.exit_code, 2)
            self.assertFalse(result.executed)
            metadata = json.loads((Path(result.artifact_dir) / "metadata.json").read_text(encoding="utf-8"))
            self.assertFalse(metadata["release_eligible"])
            self.assertIn("no Gazebo adapter", metadata["reason"])

    def test_external_nonzero_exit_is_failed_and_logs_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fail.py"
            script.write_text(
                "import sys; print('runner failed'); print('bad', file=sys.stderr); raise SystemExit(7)\n",
                encoding="utf-8",
            )
            result = run_scenario(
                self.scenario,
                runner="external",
                output_dir=Path(directory) / "runs",
                version="test",
                command=[sys.executable, str(script)],
            )
            self.assertEqual(result.status, "FAILED")
            artifact = Path(result.artifact_dir)
            self.assertIn("runner failed", (artifact / "stdout.log").read_text(encoding="utf-8"))
            self.assertIn("bad", (artifact / "stderr.log").read_text(encoding="utf-8"))
            metadata = json.loads((artifact / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["process_exit_code"], 7)

    def test_external_valid_event_log_is_executed_but_not_release_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "events.jsonl"
            events = scripted_events("test", self.scenario.manifest, _git_commit(), 1000)
            source.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            copier = root / "copy.py"
            copier.write_text(
                "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])\n",
                encoding="utf-8",
            )
            result = run_scenario(
                self.scenario,
                runner="external",
                output_dir=root / "runs",
                version="test",
                command=[sys.executable, str(copier), str(source), "{output}"],
            )
            self.assertEqual(result.status, "EXECUTED")
            self.assertTrue(result.executed)
            self.assertFalse(result.release_eligible)

    def test_timeout_records_status_and_does_not_leave_partial_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "timeout.json"
            manifest = dict(self.scenario.manifest)
            manifest["scenario_id"] = "timeout-test"
            manifest["timeout_s"] = 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            scenario = load_scenarios([manifest_path])[0]
            sleeper = root / "sleep.py"
            # The budget is the manifest timeout plus the startup grace, which
            # both entry points now share (issue #431), so the sleeper has to
            # outlast the grace for this to be a timeout at all.
            sleeper.write_text(f"import time; time.sleep({EXTERNAL_STARTUP_GRACE_SECONDS + 60})\n", encoding="utf-8")
            result = run_scenario(
                scenario,
                runner="external",
                output_dir=root / "runs",
                version="test",
                command=[sys.executable, str(sleeper)],
            )
            self.assertEqual(result.status, "TIMED_OUT")
            metadata = json.loads((Path(result.artifact_dir) / "metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(metadata["timed_out"])
            self.assertFalse(list((root / "runs").glob("*.partial")))

    def test_runner_logs_are_bounded_and_marked_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            noisy = root / "noisy.py"
            noisy.write_text("print('x' * 5_000_000)\n", encoding="utf-8")
            result = run_scenario(
                self.scenario,
                runner="external",
                output_dir=root / "runs",
                version="test",
                command=[sys.executable, str(noisy)],
            )
            self.assertEqual(result.status, "FAILED")
            self.assertLessEqual((Path(result.artifact_dir) / "stdout.log").stat().st_size, 4 * 1024 * 1024)

    def test_empty_run_matrix_is_rejected(self) -> None:
        from sim_cli import run_scenarios

        with self.assertRaisesRegex(SimulationInputError, "at least one scenario"):
            run_scenarios([], runner="gazebo")

    def test_frozen_regression_without_gazebo_exits_nonzero_without_placeholder_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tests" / "regression" / "run_frozen_scenarios.py"),
                    "--version",
                    "test",
                    "--runner",
                    "gazebo",
                    "--output-dir",
                    directory,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("NOT_EXECUTED", completed.stdout)
            self.assertNotIn("占位", completed.stdout)
            self.assertNotIn("VTCR", completed.stdout)


if __name__ == "__main__":
    unittest.main()
