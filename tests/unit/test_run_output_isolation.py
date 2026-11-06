"""Issue #426: a rerun must not inherit logs from a previous invocation."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts")]

from run_evaluation import EvaluationInputError, _prepare_output_dir


def manifests(*scenario_ids: str) -> list[tuple[Path, dict]]:
    return [(Path("scenario.json"), {"scenario_id": scenario}) for scenario in scenario_ids]


class OutputIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.output = Path(self._directory.name)

    def _write(self, version: str, scenario: str) -> Path:
        directory = self.output / version
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / f"{scenario}.jsonl"
        log.write_text('{"sequence_no": 0}\n', encoding="utf-8")
        return log

    def _logs(self) -> list[str]:
        return sorted(path.relative_to(self.output).as_posix() for path in self.output.rglob("*.jsonl"))

    def test_a_fresh_directory_is_untouched(self) -> None:
        _prepare_output_dir(self.output, ["v1"], manifests("s1"))
        self.assertEqual(self._logs(), [])

    def test_this_invocations_logs_are_cleared_before_it_writes(self) -> None:
        log = self._write("v1", "s1")
        _prepare_output_dir(self.output, ["v1"], manifests("s1"))
        self.assertFalse(log.exists())

    def test_a_smaller_matrix_refuses_the_stale_log_from_the_previous_run(self) -> None:
        """The issue: the leftover scenario would be counted by collect_metrics."""

        self._write("v1", "s2")
        with self.assertRaisesRegex(EvaluationInputError, "would not rewrite"):
            _prepare_output_dir(self.output, ["v1"], manifests("s1"))
        self.assertEqual(self._logs(), ["v1/s2.jsonl"])

    def test_a_version_from_another_invocation_is_refused(self) -> None:
        self._write("v1", "s1")
        self._write("v2", "s1")
        with self.assertRaisesRegex(EvaluationInputError, "would not rewrite"):
            _prepare_output_dir(self.output, ["v1"], manifests("s1"))

    def test_the_error_names_every_stray_log(self) -> None:
        self._write("v1", "s2")
        self._write("v2", "s9")
        with self.assertRaises(EvaluationInputError) as caught:
            _prepare_output_dir(self.output, ["v1"], manifests("s1"))
        message = str(caught.exception)
        self.assertIn("v1/s2.jsonl", message)
        self.assertIn("v2/s9.jsonl", message)

    def test_a_refused_preparation_deletes_nothing(self) -> None:
        kept = self._write("v1", "s1")
        self._write("v1", "s2")
        with self.assertRaises(EvaluationInputError):
            _prepare_output_dir(self.output, ["v1"], manifests("s1"))
        self.assertTrue(kept.exists())
        self.assertEqual(self._logs(), ["v1/s1.jsonl", "v1/s2.jsonl"])

    def test_non_log_files_are_left_alone(self) -> None:
        (self.output / "notes.txt").write_text("keep me\n", encoding="utf-8")
        summary = self.output / "v1" / "summary.txt"
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text("keep me too\n", encoding="utf-8")
        _prepare_output_dir(self.output, ["v1"], manifests("s1"))
        self.assertTrue((self.output / "notes.txt").exists())
        self.assertTrue(summary.exists())


if __name__ == "__main__":
    unittest.main()
