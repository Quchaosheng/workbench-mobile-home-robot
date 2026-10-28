"""Issue #446: an upstream report is only structurally valid if it says something true."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "hardware/release/tools")]

from check_release_readiness import (
    UPSTREAM_REPORTS,
    UPSTREAM_STATUSES,
    load_report,
    validate_upstream_report,
    validate_upstream_reports,
)

STAGED_KEYS = (
    "evt_prototype_order",
    "production_release",
    "order_release_ready",
    "blockers",
    "blocker_count",
)


class UpstreamReportStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.package = "pcb"
        self.base = load_report(UPSTREAM_REPORTS[self.package])

    def _mutated(self, **overrides: object) -> dict:
        """A report with the staged blocks removed, so only the field under test fails."""

        report = copy.deepcopy(self.base)
        for key in STAGED_KEYS:
            report.pop(key, None)
        report.update(overrides)
        return report

    def _failing(self, report: dict) -> list[str]:
        checks = validate_upstream_report(report, self.package)
        return [name for name, ok in checks.items() if not ok]

    def test_the_committed_reports_are_structurally_valid(self) -> None:
        for name, info in validate_upstream_reports().items():
            with self.subTest(package=name):
                self.assertTrue(info["pass"], info["checks"])

    def test_an_arbitrary_status_is_refused(self) -> None:
        """The issue's attack: any non-empty string used to validate."""

        self.assertEqual(self._failing(self._mutated(status="JUST_A_STRING")), ["status_is_in_the_package_vocabulary"])

    def test_an_empty_status_is_still_refused(self) -> None:
        self.assertIn("status_is_nonempty_string", self._failing(self._mutated(status="")))

    def test_a_false_pass_is_refused(self) -> None:
        report = self._mutated()
        report["pass"] = False
        self.assertEqual(self._failing(report), ["structural_pass_field_is_true"])

    def test_a_string_pass_is_refused(self) -> None:
        report = self._mutated()
        report["pass"] = "true"
        self.assertIn("structural_pass_field_is_boolean", self._failing(report))

    def test_a_missing_pass_is_refused(self) -> None:
        report = self._mutated()
        report.pop("pass", None)
        report.pop("engineering_package_pass", None)
        self.assertIn("structural_pass_field_is_true", self._failing(report))

    def test_every_package_has_a_declared_vocabulary(self) -> None:
        self.assertEqual(set(UPSTREAM_STATUSES), set(UPSTREAM_REPORTS))

    def test_each_committed_status_is_in_its_own_vocabulary(self) -> None:
        for name, path in UPSTREAM_REPORTS.items():
            with self.subTest(package=name):
                status = load_report(path).get("status")
                self.assertIn(status, UPSTREAM_STATUSES[name])

    def test_a_ready_status_from_another_package_is_refused(self) -> None:
        """Cross-package copy-paste is the realistic way a wrong status arrives."""

        self.assertIn(
            "status_is_in_the_package_vocabulary",
            self._failing(self._mutated(status="ORDER_RELEASE_READY")),
        )


if __name__ == "__main__":
    unittest.main()
