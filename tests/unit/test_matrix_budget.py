"""Issue #450: the evaluation matrix is bounded before any run starts."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts")]

from run_evaluation import (
    MAX_MATRIX_RUNS,
    MAX_SCENARIOS,
    MAX_VERSIONS,
    EvaluationInputError,
    check_matrix_budget,
    matrix_budget,
)


def paths(count: int) -> list[Path]:
    return [Path(f"scenario-{index}.json") for index in range(count)]


class MatrixBudgetTests(unittest.TestCase):
    def test_a_small_matrix_reports_its_shape(self) -> None:
        budget = check_matrix_budget(["v1", "v2"], paths(3))
        self.assertEqual(budget["versions"], 2)
        self.assertEqual(budget["scenarios"], 3)
        self.assertEqual(budget["runs"], 6)

    def test_the_budget_names_the_limits_it_applies(self) -> None:
        budget = matrix_budget(["v1"], paths(1))
        self.assertEqual(budget["max_versions"], MAX_VERSIONS)
        self.assertEqual(budget["max_scenarios"], MAX_SCENARIOS)
        self.assertEqual(budget["max_runs"], MAX_MATRIX_RUNS)

    def test_the_exact_limits_are_accepted(self) -> None:
        budget = check_matrix_budget([f"v{i}" for i in range(MAX_VERSIONS)], paths(1))
        self.assertEqual(budget["runs"], MAX_VERSIONS)

    def test_one_run_over_the_product_is_refused(self) -> None:
        versions = [f"v{i}" for i in range(11)]
        scenarios = paths(MAX_MATRIX_RUNS // 11 + 1)
        with self.assertRaises(EvaluationInputError) as caught:
            check_matrix_budget(versions, scenarios)
        self.assertIn("over the", str(caught.exception))
        self.assertIn(f"{MAX_MATRIX_RUNS}-run limit", str(caught.exception))

    def test_too_many_versions_is_refused(self) -> None:
        with self.assertRaises(EvaluationInputError) as caught:
            check_matrix_budget([f"v{i}" for i in range(MAX_VERSIONS + 1)], paths(1))
        self.assertIn(f"{MAX_VERSIONS}-version limit", str(caught.exception))

    def test_too_many_scenarios_is_refused(self) -> None:
        with self.assertRaises(EvaluationInputError) as caught:
            check_matrix_budget(["v1"], paths(MAX_SCENARIOS + 1))
        self.assertIn(f"{MAX_SCENARIOS}-scenario limit", str(caught.exception))

    def test_the_refusal_says_how_to_proceed(self) -> None:
        with self.assertRaises(EvaluationInputError) as caught:
            check_matrix_budget([f"v{i}" for i in range(MAX_VERSIONS)], paths(MAX_SCENARIOS))
        message = str(caught.exception)
        self.assertIn("Split the work", message)
        self.assertIn("--output-dir", message)

    def test_every_problem_is_reported_at_once(self) -> None:
        """A caller should not have to fix one limit to discover the next."""

        with self.assertRaises(EvaluationInputError) as caught:
            check_matrix_budget([f"v{i}" for i in range(MAX_VERSIONS + 5)], paths(MAX_SCENARIOS + 5))
        message = str(caught.exception)
        self.assertIn("version limit", message)
        self.assertIn("scenario limit", message)
        self.assertIn("run limit", message)


if __name__ == "__main__":
    unittest.main()


class DryRunTests(unittest.TestCase):
    """`--dry-run` reports the matrix without starting anything."""

    def _main(self, arguments: list[str]) -> tuple[int, str]:
        import contextlib
        import io
        from unittest.mock import patch

        from run_evaluation import main

        buffer = io.StringIO()
        with patch.object(sys, "argv", ["run_evaluation.py", *arguments]), contextlib.redirect_stdout(buffer):
            code = main()
        return code, buffer.getvalue()

    def test_a_dry_run_reports_the_count_and_starts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = Path(directory) / "scenario.json"
            scenario.write_text("{}", encoding="utf-8")
            output = Path(directory) / "out"
            code, printed = self._main(
                [
                    "--versions",
                    "v1,v2",
                    "--scenarios",
                    str(scenario),
                    "--output-dir",
                    str(output),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("2 version(s) x 1 scenario(s) = 2 run(s)", printed)
        self.assertIn("limits are", printed)

    def test_a_dry_run_over_the_limit_still_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenarios = []
            for index in range(MAX_SCENARIOS + 1):
                path = Path(directory) / f"s{index}.json"
                path.write_text("{}", encoding="utf-8")
                scenarios.append(str(path))
            with self.assertRaises(EvaluationInputError):
                self._main(
                    [
                        "--versions",
                        "v1",
                        "--scenarios",
                        *scenarios,
                        "--output-dir",
                        str(Path(directory) / "out"),
                        "--dry-run",
                    ]
                )
