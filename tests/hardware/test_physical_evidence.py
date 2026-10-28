"""Issue #444: a physical PASS has to carry checkable measurement metadata."""

from __future__ import annotations

import hashlib
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "hardware/validation/tools")]

from evidence import EvidenceError, validate_record

SCENARIOS = {"F-01"}
UNITS = {"UNIT-001": ("REV-A", "a" * 64)}
SCRATCH = ROOT / "hardware/validation/generated"


class PhysicalEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = SCRATCH / f"evidence-{self._testMethodName}"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.raw = self.directory / "capture.txt"
        self.raw.write_text("offset 1.2 mV\n", encoding="utf-8")

    def record(self, **overrides: object) -> dict:
        reference = self.raw.relative_to(ROOT).as_posix()
        base = {
            "evidence_id": "EV-1",
            "scenario_id": "F-01",
            "unit_id": "UNIT-001",
            "hardware_revision": "REV-A",
            "config_hash": "a" * 64,
            "operator": "operator",
            "reviewer": "reviewer",
            "captured_at": "2026-10-01T00:00:00Z",
            "evidence_kind": "physical",
            "result": "PASS",
            "instrument_refs": ["DMM"],
            "calibration_refs": ["cal-1"],
            "instrument_serials": ["SN-1"],
            "calibration_due": {"SN-1": "2027-01-01T00:00:00Z"},
            "measurement_summary": {"offset_mv": 1.2},
            "raw_files": {reference: hashlib.sha256(self.raw.read_bytes()).hexdigest()},
            "raw_file_kinds": {reference: "capture"},
        }
        base.update(overrides)
        return base

    def _validate(self, record: dict) -> None:
        validate_record(record, root=ROOT, scenarios=SCENARIOS, units=UNITS)

    def test_the_issue_attack_is_refused(self) -> None:
        """Any file plus a self-written instrument name used to be a PASS."""

        record = self.record()
        for field in ("instrument_serials", "calibration_due", "measurement_summary"):
            record.pop(field)
        with self.assertRaisesRegex(EvidenceError, "physical evidence is missing fields"):
            self._validate(record)

    def test_a_complete_physical_record_is_accepted(self) -> None:
        self._validate(self.record())

    def test_a_serial_is_required_per_instrument(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "one serial per instrument_ref"):
            self._validate(self.record(instrument_refs=["DMM", "scope"]))

    def test_repeated_serials_are_refused(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "must not repeat a serial"):
            self._validate(self.record(instrument_serials=["SN-1", "SN-1"], instrument_refs=["DMM", "scope"]))

    def test_calibration_has_to_cover_every_serial(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "expiry for every instrument serial"):
            self._validate(self.record(calibration_due={"SN-2": "2027-01-01T00:00:00Z"}))

    def test_expired_calibration_is_refused(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "had expired before the measurement"):
            self._validate(self.record(calibration_due={"SN-1": "2025-01-01T00:00:00Z"}))

    def test_calibration_without_a_timezone_is_refused(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "must state a timezone"):
            self._validate(self.record(calibration_due={"SN-1": "2027-01-01T00:00:00"}))

    def test_a_summary_value_has_to_be_a_number(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "must be a number"):
            self._validate(self.record(measurement_summary={"offset_mv": "about a milli"}))

    def test_an_empty_summary_is_refused(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "must record what was measured"):
            self._validate(self.record(measurement_summary={}))

    def test_every_raw_file_has_to_be_classified(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "classify every raw file"):
            self._validate(self.record(raw_file_kinds={}))

    def test_a_record_without_a_capture_is_refused(self) -> None:
        reference = self.raw.relative_to(ROOT).as_posix()
        with self.assertRaisesRegex(EvidenceError, "at least one captured measurement"):
            self._validate(self.record(raw_file_kinds={reference: "log"}))

    def test_a_simulation_record_is_unaffected(self) -> None:
        """The stricter fields are about physical claims only."""

        reference = self.raw.relative_to(ROOT).as_posix()
        record = {
            "evidence_id": "EV-2",
            "scenario_id": "F-01",
            "unit_id": "UNIT-001",
            "hardware_revision": "REV-A",
            "config_hash": "a" * 64,
            "operator": "operator",
            "reviewer": "reviewer",
            "captured_at": "2026-10-01T00:00:00Z",
            "evidence_kind": "simulation",
            "result": "PASS",
            "instrument_refs": ["sim"],
            "calibration_refs": ["n/a"],
            "raw_files": {reference: hashlib.sha256(self.raw.read_bytes()).hexdigest()},
        }
        self._validate(record)


if __name__ == "__main__":
    unittest.main()
