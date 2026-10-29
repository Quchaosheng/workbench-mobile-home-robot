"""Issue #451: the release and governance gates bound what they read."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts")]

from _jsonio import (
    MAX_JSON_BYTES,
    MAX_TEXT_BYTES,
    JsonInputError,
    load_json,
    read_text,
)

GATES = (
    "check_release_commit.py",
    "check_ready_gate.py",
    "check_task_packet.py",
    "check_phase_gates.py",
    "check_readiness_report.py",
)


class BoundedReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = Path(self._directory.name)

    def _write(self, name: str, content: str) -> Path:
        path = self.directory / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_a_normal_document_is_read(self) -> None:
        self.assertEqual(load_json(self._write("ok.json", '{"a": 1}')), {"a": 1})
        self.assertEqual(read_text(self._write("ok.md", "# heading")), "# heading")

    def test_an_oversize_json_document_is_refused(self) -> None:
        path = self._write("big.json", '{"a": "' + "x" * (MAX_JSON_BYTES + 64) + '"}')
        with self.assertRaises(JsonInputError) as caught:
            load_json(path)
        message = str(caught.exception)
        self.assertIn(str(MAX_JSON_BYTES), message)
        self.assertIn("big.json", message)

    def test_an_oversize_text_document_is_refused(self) -> None:
        path = self._write("big.md", "x" * (MAX_TEXT_BYTES + 64))
        with self.assertRaises(JsonInputError) as caught:
            read_text(path)
        self.assertIn(str(MAX_TEXT_BYTES), str(caught.exception))

    def test_a_document_at_the_limit_is_accepted(self) -> None:
        """The limit is a ceiling, not a margin."""

        payload = "x" * (MAX_TEXT_BYTES - 1)
        self.assertEqual(len(read_text(self._write("edge.md", payload))), MAX_TEXT_BYTES - 1)

    def test_a_refused_document_is_not_truncated(self) -> None:
        """A truncated policy document is not the policy, so nothing is returned."""

        path = self._write("big.md", "x" * (MAX_TEXT_BYTES + 64))
        with self.assertRaises(JsonInputError):
            read_text(path)

    def test_duplicate_keys_are_still_refused(self) -> None:
        with self.assertRaises(JsonInputError):
            load_json(self._write("dup.json", '{"a": 1, "a": 2}'))

    def test_a_missing_file_is_reported_with_its_path(self) -> None:
        with self.assertRaises(JsonInputError) as caught:
            load_json(self.directory / "absent.json")
        self.assertIn("absent.json", str(caught.exception))

    def test_invalid_utf8_is_refused(self) -> None:
        path = self.directory / "bad.json"
        path.write_bytes(b"\xff\xfe{}")
        with self.assertRaises(JsonInputError):
            load_json(path)


class GateUsesBoundedReadsTests(unittest.TestCase):
    """The gates have to call the helper, not keep their own unbounded read."""

    def test_no_gate_reads_a_file_without_a_bound(self) -> None:
        for name in GATES:
            with self.subTest(gate=name):
                source = (ROOT / "tools/scripts" / name).read_text(encoding="utf-8")
                self.assertNotIn("read_text(encoding=", source, f"{name} still reads unbounded")
                self.assertIn("_jsonio", source, f"{name} does not import the helper")


if __name__ == "__main__":
    unittest.main()
