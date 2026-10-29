"""Issue #466: a run id is derived from its inputs, not from a random value."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts")]

from sim_cli import _run_identity, load_scenarios


class RunIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario = load_scenarios()[0]

    def identity(self, **overrides: object) -> str:
        arguments: dict[str, object] = {
            "runner": "scripted",
            "version": "sim-cli",
            "seed_base": 1000,
            "nonce": None,
        }
        arguments.update(overrides)
        return _run_identity(self.scenario, **arguments)  # type: ignore[arg-type]

    def test_the_same_inputs_produce_the_same_id(self) -> None:
        """The issue: every invocation used to get a different uuid4 suffix."""

        self.assertEqual(self.identity(), self.identity())

    def test_the_id_is_reproducible_across_calls_in_a_fresh_object(self) -> None:
        first = self.identity()
        second = self.identity()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(f"{self.scenario.scenario_id}-"))

    def test_each_semantic_input_changes_the_id(self) -> None:
        baseline = self.identity()
        for label, override in (
            ("runner", {"runner": "gazebo"}),
            ("version", {"version": "v2"}),
            ("seed_base", {"seed_base": 2000}),
        ):
            with self.subTest(input=label):
                self.assertNotEqual(self.identity(**override), baseline, label)

    def test_a_nonce_gives_an_intentional_distinct_id(self) -> None:
        baseline = self.identity()
        self.assertNotEqual(self.identity(nonce="retry-1"), baseline)
        self.assertNotEqual(self.identity(nonce="retry-1"), self.identity(nonce="retry-2"))

    def test_the_same_nonce_is_still_reproducible(self) -> None:
        self.assertEqual(self.identity(nonce="retry-1"), self.identity(nonce="retry-1"))

    def test_the_id_is_filesystem_safe(self) -> None:
        identity = self.identity()
        suffix = identity.rsplit("-", 1)[1]
        self.assertEqual(len(suffix), 12)
        self.assertTrue(all(character in "0123456789abcdef" for character in suffix))

    def test_no_uuid_is_used_any_more(self) -> None:
        """A random source cannot produce a stable identity."""

        source = (ROOT / "tools/scripts/sim_cli.py").read_text(encoding="utf-8")
        self.assertNotIn("uuid.uuid4", source)
        self.assertNotIn("import uuid", source)


if __name__ == "__main__":
    unittest.main()
