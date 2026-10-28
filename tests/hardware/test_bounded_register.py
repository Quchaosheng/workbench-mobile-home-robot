"""Issue #454: the evidence register is a bounded, strictly parsed input."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "hardware/validation/tools")]

from evidence import (
    MAX_REGISTER_BYTES,
    MAX_REGISTER_LINE_BYTES,
    MAX_REGISTER_LINES,
    EvidenceError,
    load_register,
)

RECORD = '{"evidence_id": "E1", "result": "PASS"}\n'


class BoundedRegisterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = Path(self._directory.name)

    def _load(self, content: bytes | str, name: str = "register.jsonl") -> list[dict]:
        path = self.directory / name
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)
        return load_register(path)

    def test_a_normal_register_is_read(self) -> None:
        self.assertEqual(len(self._load(RECORD * 3)), 3)

    def test_a_missing_register_is_empty(self) -> None:
        self.assertEqual(load_register(self.directory / "absent.jsonl"), [])

    def test_duplicate_keys_are_refused(self) -> None:
        """Plain json.loads would keep the last value and never say so."""

        with self.assertRaisesRegex(EvidenceError, "duplicate key"):
            self._load('{"a": 1, "a": 2}\n')

    def test_nan_is_refused(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "non-finite number"):
            self._load('{"a": NaN}\n')

    def test_infinity_is_refused(self) -> None:
        for token in ("Infinity", "-Infinity"):
            with self.subTest(token=token), self.assertRaisesRegex(EvidenceError, "non-finite number"):
                self._load(f'{{"a": {token}}}\n')

    def test_invalid_utf8_is_refused(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "not valid UTF-8"):
            self._load(b"\xff\xfe not utf8\n")

    def test_an_over_long_line_is_refused(self) -> None:
        payload = '{"a": "' + "x" * (MAX_REGISTER_LINE_BYTES + 1) + '"}\n'
        with self.assertRaisesRegex(EvidenceError, "over the .*-byte line limit"):
            self._load(payload)

    def test_too_many_lines_are_refused(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "over the .*-line limit"):
            self._load(RECORD * (MAX_REGISTER_LINES + 1))

    def test_an_oversized_register_is_refused(self) -> None:
        payload = '{"a": "' + "x" * MAX_REGISTER_BYTES + '"}\n'
        with self.assertRaisesRegex(EvidenceError, "over the .*-byte limit"):
            self._load(payload)

    def test_a_non_object_line_is_refused(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "must be an object"):
            self._load("[1, 2, 3]\n")

    def test_a_blank_line_is_skipped(self) -> None:
        self.assertEqual(len(self._load(RECORD + "\n" + RECORD)), 2)

    def test_the_error_names_the_line(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "line 2"):
            self._load(RECORD + '{"a": NaN}\n')


if __name__ == "__main__":
    unittest.main()


class AppendStaysAtomicTests(unittest.TestCase):
    """A refused append must not leave the register changed."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.register = Path(self._directory.name) / "register.jsonl"
        self.kwargs = {
            "root": ROOT,
            "scenarios": {"F-01"},
            "units": {"UNIT-001": ("REV-A", "a" * 64)},
        }

    def _record(self) -> dict:
        return {
            "evidence_id": "E2",
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
            "raw_files": {"x.txt": "0" * 64},
        }

    def test_an_ambiguous_register_refuses_the_append_and_leaves_it_alone(self) -> None:
        from evidence import register

        self.register.write_text('{"a": 1, "a": 2}\n', encoding="utf-8")
        before = self.register.read_bytes()

        with self.assertRaisesRegex(EvidenceError, "duplicate key"):
            register(self.register, self._record(), **self.kwargs)

        self.assertEqual(self.register.read_bytes(), before)
