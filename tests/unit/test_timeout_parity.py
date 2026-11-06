"""Issue #431: both evidence-producing runners enforce one budget for one manifest."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts")]

import run_evaluation
import sim_cli
from scenario_tools import EXTERNAL_STARTUP_GRACE_SECONDS, external_timeout_budget


class TimeoutParityTests(unittest.TestCase):
    def test_both_entry_points_agree(self) -> None:
        """The issue: 1s in sim_cli and 31s in run_evaluation for one manifest."""

        for timeout_s in (1, 5, 30, 120):
            with self.subTest(timeout_s=timeout_s):
                manifest = {"timeout_s": timeout_s}
                self.assertEqual(
                    external_timeout_budget(manifest, error=sim_cli.SimulationInputError),
                    run_evaluation.external_timeout_budget(manifest),
                )

    def test_the_budget_includes_the_startup_grace(self) -> None:
        self.assertEqual(
            run_evaluation.external_timeout_budget({"timeout_s": 1}),
            1 + EXTERNAL_STARTUP_GRACE_SECONDS,
        )

    def test_both_entry_points_share_one_constant(self) -> None:
        self.assertIs(
            run_evaluation.EXTERNAL_STARTUP_GRACE_SECONDS,
            EXTERNAL_STARTUP_GRACE_SECONDS,
        )

    def test_each_entry_point_raises_its_own_error_type(self) -> None:
        # sim_cli calls the shared helper directly, passing its own error type, so
        # the message speaks the vocabulary its callers already handle.
        with self.assertRaises(sim_cli.SimulationInputError):
            external_timeout_budget({"timeout_s": 0}, error=sim_cli.SimulationInputError)
        with self.assertRaises(run_evaluation.EvaluationInputError):
            run_evaluation.external_timeout_budget({"timeout_s": 0})

    def test_a_non_integer_timeout_is_refused_by_both(self) -> None:
        for value in ("1", 1.5, None, True):
            with self.subTest(value=value):
                manifest = {"timeout_s": value}
                with self.assertRaises(sim_cli.SimulationInputError):
                    external_timeout_budget(manifest, error=sim_cli.SimulationInputError)
                with self.assertRaises(run_evaluation.EvaluationInputError):
                    run_evaluation.external_timeout_budget(manifest)

    def test_the_shared_helper_is_the_only_place_that_adds_the_grace(self) -> None:
        """Two copies of the arithmetic is how the two runners drifted apart."""

        for name in ("sim_cli.py", "run_evaluation.py"):
            source = (ROOT / "tools/scripts" / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotIn("+ EXTERNAL_STARTUP_GRACE_SECONDS", source)
                self.assertNotIn("EXTERNAL_STARTUP_GRACE_SECONDS = ", source)


if __name__ == "__main__":
    unittest.main()
