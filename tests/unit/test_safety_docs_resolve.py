"""The safety-positioning pages must keep pointing at code that exists.

`docs/security/functional-safety-boundary.md` and
`docs/deployment/hazard-analysis-cancel-then-stop.md` are the two documents that
prevent this repository from being read as a functional-safety claim. They are worth
nothing if their evidence links rot: a page that cites a test which no longer exists
reads exactly like a page that cites one that does.

So every backticked `path.py::Symbol` reference in those pages is resolved here,
using the same reader the assurance gate uses, and the sayable/not-sayable table is
checked for the phrases that must never appear on the left.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs/kernel"))
sys.path.insert(0, str(ROOT / "libs/contracts"))

from workbench.kernel.assurance import resolves

BOUNDARY = ROOT / "docs/security/functional-safety-boundary.md"
HAZARD = ROOT / "docs/deployment/hazard-analysis-cancel-then-stop.md"

# `something.py::Symbol`, optionally `Class::method`, inside backticks.
REFERENCE = re.compile(r"`([A-Za-z0-9_./-]+\.py::[A-Za-z0-9_.]+)`")


def test_the_two_pages_exist() -> None:
    assert BOUNDARY.is_file()
    assert HAZARD.is_file()


@pytest.mark.parametrize("page", [BOUNDARY, HAZARD], ids=["boundary", "hazard"])
def test_every_code_reference_resolves(page: Path) -> None:
    references = REFERENCE.findall(page.read_text(encoding="utf-8"))
    unresolved = [reference for reference in references if not resolves(reference, ROOT)]
    assert not unresolved, f"{page.name} cites code that does not exist: {unresolved}"


def test_every_linked_document_exists() -> None:
    """A cross-link in these pages is a promise that the other page is there."""

    for page in (BOUNDARY, HAZARD):
        text = page.read_text(encoding="utf-8")
        for target in re.findall(r"\]\((\.\.?/[^)]+)\)", text):
            assert (page.parent / target).resolve().is_file(), f"{page.name} -> {target}"


def test_the_forbidden_claims_stay_out_of_the_sayable_column() -> None:
    """The right-hand column is the list of things this repository must not say.

    If one of these phrases ever moves left, the page has started overclaiming, which
    is the exact failure it was written to prevent.
    """

    text = BOUNDARY.read_text(encoding="utf-8")
    forbidden = [
        "the STOP function is PL",
        "safety function is validated",
        "hazards have been controlled",
        "the system is safe",
        "ISO 13849-2 validation performed",
        "residual risk is acceptable",
        "conforms to ISO 13482",
    ]
    table = text.split("## Sayable / not sayable", 1)[1].split("##", 1)[0]
    for line in table.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 2 or cells[0] == "Sayable" or set(cells[0]) == {"-"}:
            continue
        sayable = cells[0].lower()
        for phrase in forbidden:
            assert phrase.lower() not in sayable, f"overclaim in the sayable column: {cells[0]}"


def test_the_boundary_page_names_all_four_standards() -> None:
    text = BOUNDARY.read_text(encoding="utf-8")
    for standard in ("ISO 12100", "ISO 13849-1", "ISO 13849-2", "ISO 13482"):
        assert standard in text, standard


def test_the_hazard_page_records_risk_estimation_as_blocked() -> None:
    """Without machine data a risk number would be invented; the page must say so."""

    assert "BLOCKED" in HAZARD.read_text(encoding="utf-8")
