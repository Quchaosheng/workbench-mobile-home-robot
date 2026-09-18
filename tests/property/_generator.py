"""Deterministic case generation, shrinking and corpus accounting for Issue #89.

Dependencies are deliberately absent: Hypothesis and mutmut are not installed in
this environment and this repository does not add a dependency to make a safety
boundary testable.  Every suite therefore builds its own seeded generator, and
the same seed produces the same corpus on every host because the only source of
randomness is a fixed 64-bit mix instead of ``random``.

Three rules shape this module:

* A failing case is shrunk before it is reported.  A 200-element case that fails
  on element 137 is not evidence a reviewer can act on.
* The corpus is accounted for.  Each suite records its seed list, case count and
  corpus digest so the gate can refuse a corpus that silently shrank.
* Nothing is retried.  A case either passes or it does not, and the quarantine
  decision lives in a reviewed JSON file rather than in a retry loop.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GENERATOR_VERSION = "workbench-property-generator-v1"
CASE_SCHEMA_VERSION = "workbench-property-case-v1"

# Three fixed seeds.  Changing a seed changes the corpus digest, which the gate
# treats as a reviewed change rather than an incidental edit.
SEEDS: tuple[int, ...] = (20260817, 20260818, 20260819)

MASK64 = (1 << 64) - 1
MAX_SHRINK_STEPS = 240


class CaseFailure(AssertionError):
    """A genesis case violated the property it was generated to exercise."""


class Rng:
    """A splitmix64 stream: tiny, dependency-free and stable across versions."""

    def __init__(self, seed: int) -> None:
        if type(seed) is not int:
            raise TypeError("seed must be an int")
        self._state = seed & MASK64
        self.draws = 0

    def next_u64(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & MASK64
        self.draws += 1
        value = self._state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK64
        return value ^ (value >> 31)

    def randbelow(self, limit: int) -> int:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        return self.next_u64() % limit

    def integer(self, low: int, high: int) -> int:
        if low > high:
            raise ValueError("low must not exceed high")
        return low + self.randbelow(high - low + 1)

    def choice(self, values: Sequence[Any]) -> Any:
        if not values:
            raise ValueError("cannot choose from an empty sequence")
        return values[self.randbelow(len(values))]

    def sample(self, values: Sequence[Any], count: int) -> list[Any]:
        pool = list(values)
        if count > len(pool):
            raise ValueError("cannot sample more values than were supplied")
        picked: list[Any] = []
        for _ in range(count):
            picked.append(pool.pop(self.randbelow(len(pool))))
        return picked

    def boolean(self) -> bool:
        return bool(self.next_u64() & 1)

    def shuffle(self, values: list[Any]) -> None:
        """Shuffle *values* in place with the same Fisher-Yates source."""

        for index in range(len(values) - 1, 0, -1):
            swap = self.randbelow(index + 1)
            values[index], values[swap] = values[swap], values[index]


def digest(values: Iterable[Any]) -> str:
    """Return the sha256 of the canonical JSON encoding of *values*."""

    material = json.dumps(list(values), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class CorpusReport:
    """Per-suite case accounting, filled in as the corpus runs."""

    suite: str
    seeds: tuple[int, ...]
    case_count: int = 0
    cases: list[Any] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def corpus_digest(self) -> str:
        return digest(self.cases)

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "generator_version": GENERATOR_VERSION,
            "case_schema_version": CASE_SCHEMA_VERSION,
            "seeds": list(self.seeds),
            "case_count": self.case_count,
            "corpus_digest": self.corpus_digest,
            "failures": self.failures,
        }


_REGISTRY: dict[str, CorpusReport] = {}


def report_for(suite: str, *, seeds: Sequence[int] = SEEDS) -> CorpusReport:
    """Return the shared report for *suite*, creating it on first use."""

    report = _REGISTRY.get(suite)
    if report is None:
        report = CorpusReport(suite=suite, seeds=tuple(seeds))
        _REGISTRY[suite] = report
    return report


def collected_reports() -> list[CorpusReport]:
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def reset_reports() -> None:
    _REGISTRY.clear()


def case_id(case: Any) -> str:
    return hashlib.sha256(
        json.dumps(case, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]


_STRING_SHORTENINGS = ("", "x")

_INT_SIMPLIFICATIONS = (0, 1, -1)


def shrink_candidates(value: Any) -> Iterator[Any]:
    """Yield strictly simpler variants of *value*, cheapest first."""

    if isinstance(value, dict):
        for key in list(value):
            smaller = {name: entry for name, entry in value.items() if name != key}
            yield smaller
        for key, entry in value.items():
            for candidate in shrink_candidates(entry):
                yield {**value, key: candidate}
        return

    if isinstance(value, list):
        for index in range(len(value)):
            yield value[:index] + value[index + 1 :]
        for index, entry in enumerate(value):
            for candidate in shrink_candidates(entry):
                yield [*value[:index], candidate, *value[index + 1 :]]
        return

    if isinstance(value, bool):
        if value:
            yield False
        return

    if isinstance(value, int):
        if value != 0:
            yield 0
        if value > 1:
            yield value // 2
        elif value < -1:
            yield value // 2 + 1
        return

    if isinstance(value, float):
        if value != 0.0:
            yield 0.0
        return

    if isinstance(value, str):
        for candidate in _STRING_SHORTENINGS:
            if len(candidate) < len(value):
                yield candidate
        if len(value) > 1:
            yield value[: len(value) // 2]
        return


def shrink(case: Any, still_fails: Callable[[Any], bool], *, max_steps: int = MAX_SHRINK_STEPS) -> tuple[Any, int]:
    """Greedily shrink *case* while *still_fails* stays true."""

    current = case
    steps = 0
    improved = True
    while improved and steps < max_steps:
        improved = False
        for candidate in shrink_candidates(current):
            steps += 1
            if steps >= max_steps:
                break
            if still_fails(candidate):
                current = candidate
                improved = True
                break
    return current, steps


@dataclass(frozen=True)
class PropertyOutcome:
    """The result of one generated case, including its shrunk counterexample."""

    case: Any
    ok: bool
    detail: str | None = None
    shrunk: Any = None
    shrink_steps: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_schema_version": CASE_SCHEMA_VERSION,
            "case_id": case_id(self.case),
            "ok": self.ok,
            "detail": self.detail,
            "shrunk": self.shrunk,
            "shrink_steps": self.shrink_steps,
        }


def run_case(case: Any, check: Callable[[Any], None]) -> PropertyOutcome:
    """Run *check* on *case*, shrinking and reporting the minimal counterexample."""

    try:
        check(case)
    except Exception as error:  # noqa: BLE001 - any exception is a failing case, never a pass
        detail = str(error) or type(error).__name__
        failure_type = type(error)

        def still_fails(candidate: Any) -> bool:
            # The counterexample must fail for the same reason.  Without this a
            # shrink step can strip a required field and report an empty case
            # whose "failure" is a KeyError, which is not evidence about the
            # boundary under test.
            try:
                check(candidate)
            except failure_type:
                return True
            except Exception:  # noqa: BLE001 - a different failure class is not this counterexample
                return False
            return False

        minimal, steps = shrink(case, still_fails)
        return PropertyOutcome(
            case=case,
            ok=False,
            detail=detail,
            shrunk=minimal,
            shrink_steps=steps,
        )
    return PropertyOutcome(case=case, ok=True)


def run_corpus(
    suite: str,
    cases: Iterable[Any],
    check: Callable[[Any], None],
    *,
    seeds: Sequence[int] = SEEDS,
) -> CorpusReport:
    """Run every generated case, recording the corpus and any counterexample."""

    report = report_for(suite, seeds=seeds)
    for case in cases:
        report.case_count += 1
        report.cases.append(case)
        outcome = run_case(case, check)
        if not outcome.ok:
            report.failures.append(outcome.as_dict())
    return report


ARCHIVE_ENVIRONMENT_VARIABLE = "WORKBENCH_QUALITY_ARCHIVE"


def write_archive_if_requested() -> str | None:
    """Write every recorded corpus report when the gate asked for an archive.

    The gate sets the environment variable; a plain ``pytest`` run does not, so
    the suites stay side-effect free when a developer runs them directly.  Each
    suite calls this from its last test, so whichever suite happens to run last
    writes the complete set of reports rather than a partial one.
    """

    import os

    destination = os.environ.get(ARCHIVE_ENVIRONMENT_VARIABLE)
    if not destination:
        return None
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generator_version": GENERATOR_VERSION,
        "case_schema_version": CASE_SCHEMA_VERSION,
        "suites": [report.as_dict() for report in collected_reports()],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def assert_corpus_clean(report: CorpusReport) -> None:
    """Fail the owning test with the shrunk counterexamples, not the raw cases."""

    if not report.failures:
        return
    lines = [f"{report.suite}: {len(report.failures)} of {report.case_count} generated cases failed"]
    for failure in report.failures[:5]:
        lines.append(f"  case_id={failure['case_id']} shrunk={failure['shrunk']!r} detail={failure['detail']}")
    raise CaseFailure("\n".join(lines))
