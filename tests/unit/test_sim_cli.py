import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "tools" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from run_evaluation import scripted_events
from sim_cli import (
    EXIT_CODES,
    MAX_RUN_NONCE_LENGTH,
    RUN_ID_HEX_LENGTH,
    RUN_ID_INPUTS,
    SimulationInputError,
    _git_commit,
    derive_run_id,
    doctor,
    load_scenarios,
    run_scenario,
    run_scenarios,
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
            sleeper.write_text("import time; time.sleep(10)\n", encoding="utf-8")
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


class RunIdentityTests(unittest.TestCase):
    """The published run ID is a function of the run's inputs (Issue #466)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario = load_scenarios([FROZEN / "normal-001.json"])[0]

    def test_the_same_inputs_derive_the_same_id_in_two_processes(self) -> None:
        """Determinism is a property of the process boundary, not of one call."""

        probe = textwrap.dedent(
            """
            import json
            from pathlib import Path

            from sim_cli import load_scenarios, run_scenario

            scenario = load_scenarios([Path(%(manifest)r)])[0]
            result = run_scenario(scenario, runner="scripted", output_dir=Path(%(output)r), version="cross-process")
            print(json.dumps({"run_id": result.run_id, "artifact_dir": result.artifact_dir}))
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observed = []
            for attempt in ("first", "second"):
                script = root / f"{attempt}.py"
                output = root / attempt
                script.write_text(
                    probe % {"manifest": str(FROZEN / "normal-001.json"), "output": str(output)},
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [sys.executable, str(script)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    env={**os.environ, "PYTHONPATH": str(SCRIPTS) + os.pathsep + os.environ.get("PYTHONPATH", "")},
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                observed.append(json.loads(completed.stdout))

            self.assertEqual(observed[0]["run_id"], observed[1]["run_id"])
            self.assertEqual(Path(observed[0]["artifact_dir"]).name, Path(observed[1]["artifact_dir"]).name)
            # The identity is stable; the two bundles are still separate evidence.
            self.assertNotEqual(observed[0]["artifact_dir"], observed[1]["artifact_dir"])

    def test_a_different_seed_version_or_commit_changes_the_id(self) -> None:
        base = derive_run_id(
            scenario_id="normal-001",
            registry_identity="pick-place-red-block@1.0",
            seed=1101,
            version="sim-cli",
            commit="a" * 40,
        )[0]
        changes = {
            "seed": {"seed": 1102},
            "version": {"version": "sim-cli-2"},
            "commit": {"commit": "b" * 40},
            "registry_identity": {"registry_identity": "pick-place-red-block@1.1"},
            "scenario_id": {"scenario_id": "normal-002"},
        }
        self.assertEqual(
            base,
            derive_run_id(
                scenario_id="normal-001",
                registry_identity="pick-place-red-block@1.0",
                seed=1101,
                version="sim-cli",
                commit="a" * 40,
            )[0],
        )
        for name, change in changes.items():
            arguments = {
                "scenario_id": "normal-001",
                "registry_identity": "pick-place-red-block@1.0",
                "seed": 1101,
                "version": "sim-cli",
                "commit": "a" * 40,
            }
            arguments.update(change)
            with self.subTest(changed=name):
                self.assertNotEqual(base, derive_run_id(**arguments)[0])

    def test_the_nonced_and_default_ids_differ_and_the_nonce_is_recorded(self) -> None:
        arguments = {
            "scenario_id": self.scenario.scenario_id,
            "registry_identity": "pick-place-red-block@1.0",
            "seed": 1000 + self.scenario.manifest["seed"],
            "version": "nonced",
            "commit": _git_commit(),
        }
        default_id, default_material = derive_run_id(**arguments)
        nonced_id, nonced_material = derive_run_id(**arguments, nonce="rerun-2")
        self.assertNotEqual(default_id, nonced_id)
        # An absent nonce is recorded as an explicit value, never as a random one.
        self.assertEqual(default_material["nonce"], "none")
        self.assertEqual(nonced_material["nonce"], "rerun-2")

        with tempfile.TemporaryDirectory() as directory:
            result = run_scenario(
                self.scenario,
                runner="scripted",
                output_dir=Path(directory),
                version="nonced",
                nonce="rerun-2",
            )
            metadata = json.loads((Path(result.artifact_dir) / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["run_nonce"], "rerun-2")
        self.assertEqual(metadata["run_id_derivation"]["material"]["nonce"], "rerun-2")
        self.assertEqual(result.run_id, nonced_id)

    def test_a_nonce_is_bounded_and_cannot_masquerade_as_the_default(self) -> None:
        arguments = {
            "scenario_id": "normal-001",
            "registry_identity": "pick-place-red-block@1.0",
            "seed": 1101,
            "version": "sim-cli",
            "commit": "a" * 40,
        }
        for nonce, reason in (
            ("", "non-blank"),
            ("   ", "non-blank"),
            (" padded", "padded"),
            ("x" * (MAX_RUN_NONCE_LENGTH + 1), "exceeds"),
            ("has/slash", "filesystem-safe"),
            ("répété", "ASCII"),
        ):
            with self.subTest(nonce=nonce), self.assertRaisesRegex(SimulationInputError, reason):
                derive_run_id(**arguments, nonce=nonce)

        # The reserved default is a value, not a random ID, so an explicit
        # request for it is the same request as omitting it.
        self.assertEqual(
            derive_run_id(**arguments)[0],
            derive_run_id(**arguments, nonce="none")[0],
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SimulationInputError, "exceeds"):
                run_scenario(
                    self.scenario,
                    runner="scripted",
                    output_dir=Path(directory),
                    version="test",
                    nonce="x" * (MAX_RUN_NONCE_LENGTH + 1),
                )

    def test_a_second_run_into_the_same_output_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = run_scenario(self.scenario, runner="scripted", output_dir=root, version="collide")
            artifact = Path(first.artifact_dir)
            before = (artifact / "checksums.sha256").read_text(encoding="utf-8")

            with self.assertRaisesRegex(SimulationInputError, "refusing to overwrite existing run artifact"):
                run_scenario(self.scenario, runner="scripted", output_dir=root, version="collide")

            # The refusal leaves the first bundle byte-identical and no debris.
            self.assertEqual((artifact / "checksums.sha256").read_text(encoding="utf-8"), before)
            self.assertEqual([path.name for path in root.iterdir() if path.is_dir()], [artifact.name])
            self.assertFalse(list(root.glob("*.partial")))

            # An explicit nonce is the documented way to publish a second bundle.
            second = run_scenario(self.scenario, runner="scripted", output_dir=root, version="collide", nonce="rerun")
            self.assertNotEqual(second.run_id, first.run_id)

    def test_the_scenario_name_cannot_be_confused_with_the_digest_suffix(self) -> None:
        """A name is ``<scenario_id>-<hex>``; the last '-' always separates them."""

        with tempfile.TemporaryDirectory() as directory:
            result = run_scenario(self.scenario, runner="scripted", output_dir=Path(directory), version="parse")
        scenario_id, separator, suffix = result.run_id.rpartition("-")
        self.assertEqual(separator, "-")
        self.assertEqual(scenario_id, self.scenario.scenario_id)
        self.assertEqual(len(suffix), RUN_ID_HEX_LENGTH)
        self.assertTrue(all(character in "0123456789abcdef" for character in suffix))

        # A second scenario whose ID is the first run's whole name still gets a
        # distinct directory, because the suffix is recomputed over the new ID.
        doppelganger = derive_run_id(
            scenario_id=result.run_id,
            registry_identity="pick-place-red-block@1.0",
            seed=1101,
            version="parse",
            commit=_git_commit(),
        )[0]
        self.assertNotEqual(doppelganger, result.run_id)
        self.assertTrue(doppelganger.startswith(f"{result.run_id}-"))

    def test_the_recorded_derivation_reproduces_the_published_name(self) -> None:
        """A reviewer can recompute the directory name from the metadata alone."""

        with tempfile.TemporaryDirectory() as directory:
            result = run_scenario(self.scenario, runner="scripted", output_dir=Path(directory), version="reflect")
            metadata = json.loads((Path(result.artifact_dir) / "metadata.json").read_text(encoding="utf-8"))
        derivation = metadata["run_id_derivation"]
        self.assertEqual(derivation["inputs"], list(RUN_ID_INPUTS))
        self.assertEqual(derivation["suffix_length"], RUN_ID_HEX_LENGTH)
        self.assertEqual(derivation["material"]["commit"], metadata["commit"])
        self.assertEqual(derivation["material"]["seed"], str(metadata["seed"]))
        recomputed, _ = derive_run_id(**derivation["material"])
        self.assertEqual(recomputed, result.run_id)
        self.assertEqual(metadata["run_id"], result.run_id)

    def test_the_summary_records_the_nonce_and_the_derived_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = run_scenarios(
                [self.scenario],
                runner="scripted",
                output_dir=Path(directory),
                version="summary",
                nonce="rerun-3",
            )
        self.assertEqual(summary["run_nonce"], "rerun-3")
        self.assertEqual(len(summary["results"]), 1)
        self.assertTrue(summary["results"][0]["run_id"].startswith("normal-001-"))

    def test_an_unresolvable_identity_is_refused_before_the_runner_starts(self) -> None:
        """No registry definition means no identity, and never a random name.

        A manifest that loads always resolves today, so the refusal is driven
        through the resolver itself. What is asserted is the ordering: the
        resolution happens before the output root is created, so a run whose
        identity cannot be derived leaves no directory and no staging debris
        behind, which is the property #466 changes.
        """

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "runs"
            refusal = SimulationInputError("scenario does not resolve to exactly one registered scenario")
            with (
                patch("sim_cli._identity_entry", side_effect=refusal),
                self.assertRaisesRegex(SimulationInputError, "does not resolve"),
            ):
                run_scenario(self.scenario, runner="scripted", output_dir=output, version="test")

            self.assertFalse(output.exists())

    def test_the_id_input_list_is_ordered_and_has_no_duplicates(self) -> None:
        self.assertEqual(
            RUN_ID_INPUTS,
            ("scenario_id", "registry_identity", "seed", "version", "commit", "nonce"),
        )


if __name__ == "__main__":
    unittest.main()
