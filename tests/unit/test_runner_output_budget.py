"""Issue #456: a runner's output is bounded instead of accumulated in memory."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts")]

from run_evaluation import (
    MAX_RUNNER_STDERR_BYTES,
    MAX_RUNNER_STDOUT_BYTES,
    ExternalRunnerOutputOverrun,
    run_external,
)


def flood(stream: str, blocks: int, *, pause: float = 0.0) -> str:
    """A runner command that writes `blocks` x 64 KiB to `stream`."""

    body = (
        f"import sys, time\n"
        f"for _ in range({blocks}):\n"
        f"    sys.{stream}.write(chr(120) * 65536)\n"
        f"    sys.{stream}.flush()\n"
        f"    time.sleep({pause})\n"
    )
    return f'{sys.executable} -c "{body}"'


class RunnerOutputBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = Path(self._directory.name)
        self.manifest = self.directory / "manifest.json"
        self.manifest.write_text("{}", encoding="utf-8")

    def _run(self, command: str, *, timeout_s: int = 20) -> None:
        run_external(
            command,
            self.manifest,
            self.directory / "out.jsonl",
            1,
            "v1",
            timeout_s=timeout_s,
            scenario_id="probe",
        )

    def test_a_quiet_runner_is_accepted(self) -> None:
        self._run(f'{sys.executable} -c "print(1)"')

    def test_stdout_over_the_budget_is_refused(self) -> None:
        blocks = MAX_RUNNER_STDOUT_BYTES // 65536 + 4
        with self.assertRaises(ExternalRunnerOutputOverrun) as caught:
            self._run(flood("stdout", blocks))
        self.assertEqual(caught.exception.stream, "stdout")
        self.assertGreater(caught.exception.observed, caught.exception.limit)

    def test_stderr_over_the_budget_is_refused(self) -> None:
        blocks = MAX_RUNNER_STDERR_BYTES // 65536 + 4
        with self.assertRaises(ExternalRunnerOutputOverrun) as caught:
            self._run(flood("stderr", blocks))
        self.assertEqual(caught.exception.stream, "stderr")

    def test_a_runner_that_floods_and_keeps_running_is_stopped_promptly(self) -> None:
        """The budget has to stop the process, not wait for the wall clock."""

        started = time.monotonic()
        with self.assertRaises(ExternalRunnerOutputOverrun):
            self._run(flood("stdout", 10_000, pause=0.01), timeout_s=120)
        # Ten thousand blocks at 10 ms each would take over a minute.
        self.assertLess(time.monotonic() - started, 20)

    def test_the_refusal_names_the_stream_and_the_numbers(self) -> None:
        blocks = MAX_RUNNER_STDOUT_BYTES // 65536 + 4
        with self.assertRaises(ExternalRunnerOutputOverrun) as caught:
            self._run(flood("stdout", blocks))
        message = str(caught.exception)
        self.assertIn("stdout", message)
        self.assertIn(str(MAX_RUNNER_STDOUT_BYTES), message)

    def test_the_failure_message_stays_bounded(self) -> None:
        """A failing runner's stderr is excerpted, not echoed whole."""

        body = f"import sys\nsys.stderr.write(chr(120) * {MAX_RUNNER_STDERR_BYTES - 1024})\nsys.exit(3)\n"
        with self.assertRaises(RuntimeError) as caught:
            self._run(f'{sys.executable} -c "{body}"')
        message = str(caught.exception)
        self.assertLess(len(message), MAX_RUNNER_STDERR_BYTES + 1024)
        self.assertIn("failed (3)", message)


if __name__ == "__main__":
    unittest.main()
