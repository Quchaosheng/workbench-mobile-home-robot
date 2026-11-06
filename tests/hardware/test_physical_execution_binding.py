"""Issue #429: the release gate must not trust a hand-written physical summary."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "hardware/release/tools")]

from check_release_readiness import _binding_observed_ready

REGISTER = "hardware/validation/evidence-register.jsonl"


def report(**overrides: object) -> dict:
    """A summary that is consistent with the register it names."""

    base = {
        "physical_results": "PASS",
        "evidence_counts": {"simulation": 0, "bench": 2, "physical": 4},
        "evidence_register": REGISTER,
        "evidence_register_is_valid": True,
    }
    base.update(overrides)
    return base


def satisfied(candidate: dict) -> bool:
    return _binding_observed_ready("PHYSICAL_EXECUTED", candidate)


class PhysicalExecutionBindingTests(unittest.TestCase):
    def test_a_consistent_summary_satisfies_the_binding(self) -> None:
        self.assertTrue(satisfied(report()))

    def test_the_issue_attack_is_refused(self) -> None:
        """`{"physical_results": "PASS"}` used to satisfy a release gate."""

        self.assertFalse(satisfied({"physical_results": "PASS"}))

    def test_zero_physical_evidence_is_refused(self) -> None:
        self.assertFalse(satisfied(report(evidence_counts={"simulation": 9, "bench": 1, "physical": 0})))

    def test_an_invalid_register_is_refused(self) -> None:
        self.assertFalse(satisfied(report(evidence_register_is_valid=False)))

    def test_a_missing_register_validity_flag_is_refused(self) -> None:
        candidate = report()
        del candidate["evidence_register_is_valid"]
        self.assertFalse(satisfied(candidate))

    def test_a_summary_that_does_not_name_its_register_is_refused(self) -> None:
        candidate = report()
        del candidate["evidence_register"]
        self.assertFalse(satisfied(candidate))

    def test_a_blank_register_name_is_refused(self) -> None:
        self.assertFalse(satisfied(report(evidence_register="   ")))

    def test_a_missing_count_map_is_refused(self) -> None:
        candidate = report()
        del candidate["evidence_counts"]
        self.assertFalse(satisfied(candidate))

    def test_a_non_integer_count_is_refused(self) -> None:
        for value in ("4", 4.0, True, None):
            with self.subTest(value=value):
                self.assertFalse(satisfied(report(evidence_counts={"physical": value})))

    def test_unfinished_results_are_refused(self) -> None:
        for value in ("NOT_EXECUTED", "HOLD", "FAIL", "", None):
            with self.subTest(value=value):
                self.assertFalse(satisfied(report(physical_results=value)))

    def test_the_committed_validation_report_is_derived_and_honest(self) -> None:
        """The real report says not executed, and names the register it read."""

        import json

        committed = json.loads(
            (ROOT / "hardware/validation/generated/validation_report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(committed["physical_results"], "NOT_EXECUTED")
        self.assertEqual(committed["evidence_counts"]["physical"], 0)
        self.assertEqual(committed["evidence_register"], REGISTER)
        self.assertTrue(committed["evidence_register_is_valid"])
        self.assertFalse(satisfied(committed))


if __name__ == "__main__":
    unittest.main()
