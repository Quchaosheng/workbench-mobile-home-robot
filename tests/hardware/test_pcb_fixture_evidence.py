"""Issue #445: a fixture net is only VERIFIED when its evidence is bound to it."""

from __future__ import annotations

import csv
import hashlib
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "hardware/pcb/tools")]

from release_readiness import check_fixture_access_plan

PCB = ROOT / "hardware/pcb"


def _rows(name: str) -> list[dict[str, str]]:
    with (PCB / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class FixtureEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._counter = iter(range(1000))
        self.rows = _rows("fixture-access-plan.csv")
        self.pinout = _rows("connector-pinout.csv")
        self.fixtures = _rows("fixture-budget.csv") if (PCB / "fixture-budget.csv").is_file() else _fixtures()
        self.confirmed = [r for r in self.rows if r["design_state"] == "CONNECTOR_ACCESS_CONFIRMED"]

    def _plan(self, mutate) -> dict:
        rows = [dict(row) for row in self.rows]
        for row in rows:
            if row["design_state"] == "CONNECTOR_ACCESS_CONFIRMED":
                mutate(row)
        return check_fixture_access_plan(rows, self.pinout, self.fixtures)

    def _reasons(self, result: dict, nets: list[str]) -> dict[str, str]:
        return {entry["net"]: entry["reason"] for entry in result["invalid_rows"] if entry["net"] in nets}

    def _write(self, directory: Path, net: str, body: str) -> str:
        path = directory / f"probe-{net}.txt"
        path.write_text(body, encoding="utf-8")
        return f"{path.relative_to(ROOT)}#sha256={hashlib.sha256(path.read_bytes()).hexdigest()}"

    def _scratch(self) -> Path:
        """A writable directory inside the repository, because evidence must live there."""

        directory = ROOT / "hardware/pcb/generated" / f"evidence-{next(self._counter)}"
        directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, directory, True)
        return directory

    def test_a_bare_path_is_not_bound_evidence(self) -> None:
        """The issue's own attack: any existing file used to be enough."""

        nets = [row["net"] for row in self.confirmed]

        def mutate(row: dict[str, str]) -> None:
            row["verification_state"] = "VERIFIED"
            row["evidence_ref"] = "hardware/pcb/README.md"

        reasons = self._reasons(self._plan(mutate), nets)
        self.assertEqual(set(reasons), set(nets))
        self.assertEqual(set(reasons.values()), {"verified_without_bound_evidence"})

    def test_a_hashed_reference_still_needs_to_name_the_net(self) -> None:
        if True:
            tmp = self._scratch()

            def mutate(row: dict[str, str]) -> None:
                row["verification_state"] = "VERIFIED"
                row["evidence_ref"] = self._write(tmp, row["net"], "measured")
                row["evidence_instrument"] = "DMM"
                row["evidence_captured_at"] = "2026-10-01T00:00:00Z"

            reasons = self._reasons(self._plan(mutate), [r["net"] for r in self.confirmed])
            self.assertEqual(set(reasons.values()), {"verified_without_evidence_net"})

    def test_evidence_for_another_net_does_not_verify_this_one(self) -> None:
        if True:
            tmp = self._scratch()

            def mutate(row: dict[str, str]) -> None:
                row["verification_state"] = "VERIFIED"
                row["evidence_ref"] = self._write(tmp, "other", "measured elsewhere")
                row["evidence_net"] = "SOME_OTHER_NET"
                row["evidence_instrument"] = "DMM"
                row["evidence_captured_at"] = "2026-10-01T00:00:00Z"

            reasons = self._reasons(self._plan(mutate), [r["net"] for r in self.confirmed])
            self.assertEqual(set(reasons.values()), {"verified_evidence_covers_another_net"})

    def test_a_wrong_digest_is_refused(self) -> None:
        if True:
            tmp = self._scratch()

            def mutate(row: dict[str, str]) -> None:
                row["verification_state"] = "VERIFIED"
                reference = self._write(tmp, row["net"], "measured")
                row["evidence_ref"] = reference.split("#sha256=")[0] + "#sha256=" + "0" * 64
                row["evidence_net"] = row["net"]
                row["evidence_instrument"] = "DMM"
                row["evidence_captured_at"] = "2026-10-01T00:00:00Z"

            reasons = self._reasons(self._plan(mutate), [r["net"] for r in self.confirmed])
            self.assertEqual(set(reasons.values()), {"verified_evidence_hash_mismatch"})

    def test_a_missing_file_is_refused(self) -> None:
        def mutate(row: dict[str, str]) -> None:
            row["verification_state"] = "VERIFIED"
            row["evidence_ref"] = f"hardware/pcb/generated/absent-{row['net']}.txt#sha256=" + "a" * 64
            row["evidence_net"] = row["net"]
            row["evidence_instrument"] = "DMM"
            row["evidence_captured_at"] = "2026-10-01T00:00:00Z"

        reasons = self._reasons(self._plan(mutate), [r["net"] for r in self.confirmed])
        self.assertEqual(set(reasons.values()), {"verified_evidence_missing"})

    def test_missing_instrument_or_timestamp_is_refused(self) -> None:
        if True:
            tmp = self._scratch()
            for field in ("evidence_instrument", "evidence_captured_at"):
                with self.subTest(field=field):

                    def mutate(row: dict[str, str], blank: str = field) -> None:
                        row["verification_state"] = "VERIFIED"
                        row["evidence_ref"] = self._write(tmp, row["net"], "measured")
                        row["evidence_net"] = row["net"]
                        row["evidence_instrument"] = "DMM"
                        row["evidence_captured_at"] = "2026-10-01T00:00:00Z"
                        row[blank] = ""

                    reasons = self._reasons(self._plan(mutate), [r["net"] for r in self.confirmed])
                    self.assertIn(
                        f"verified_without_{field.removeprefix('evidence_')}",
                        set(reasons.values()),
                    )

    def test_complete_evidence_is_accepted(self) -> None:
        if True:
            tmp = self._scratch()

            def mutate(row: dict[str, str]) -> None:
                row["verification_state"] = "VERIFIED"
                row["evidence_ref"] = self._write(tmp, row["net"], f"measurement {row['net']}")
                row["evidence_net"] = row["net"]
                row["evidence_instrument"] = "DMM Fluke 87V"
                row["evidence_captured_at"] = "2026-10-01T00:00:00Z"

            result = self._plan(mutate)
            nets = [row["net"] for row in self.confirmed]
            self.assertEqual(self._reasons(result, nets), {})

    def test_the_committed_plan_is_not_release_ready(self) -> None:
        """The repository's own plan has no physical verification yet."""

        result = check_fixture_access_plan(self.rows, self.pinout, self.fixtures)
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["design_ready"])
        self.assertTrue(result["pass"])


def _fixtures() -> list[dict[str, str]]:
    path = ROOT / "hardware/manufacturing/fixture-budget.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


if __name__ == "__main__":
    unittest.main()
