"""A documented repository path must exist, and a broken one must fail the build.

Issue #585 found ten references in the committed documentation that pointed at
files which were not in the repository. Two were load-bearing: ``ADR-0004``
grounded the arm decision in an unversioned plan, and an arm-driver README sent a
reader to a cross-compilation note that was never written. ``make docs`` passed
anyway, because mkdocs does not resolve a code span.

``tools/scripts/check_doc_references.py`` closes that hole and now runs inside
``make docs``. These tests pin both halves of the contract:

* the cleaned tree passes, so the check is not vacuous on a repository that is
  already correct;
* a deliberately broken reference fails with exit 1, which is the failure path
  the gate exists for -- a check that cannot fail is not a check.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/scripts/check_doc_references.py"

PASS = 0
FAIL = 1
INCOMPLETE = 2


def run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        cwd=SCRIPT.parent,
    )


def test_the_committed_tree_has_no_dangling_reference() -> None:
    """The repository as committed passes; a regression here names the page."""
    result = run(ROOT)
    assert result.returncode == PASS, result.stderr
    assert "documentation references resolve" in result.stdout


def test_a_broken_reference_fails_the_check(tmp_path: Path) -> None:
    """The failure path: one reference to a missing file exits 1 and names it."""
    (tmp_path / "docs").mkdir()
    page = tmp_path / "docs" / "example.md"
    page.write_text(
        "# Example\n\nSee `docs/hardware/cross_compile.md` for the details.\n",
        encoding="utf-8",
    )

    result = run(tmp_path)

    assert result.returncode == FAIL
    assert "docs/hardware/cross_compile.md" in result.stderr
    assert "example.md" in result.stderr


def test_a_resolving_reference_passes(tmp_path: Path) -> None:
    """The happy path: the same shape of reference passes when the file exists."""
    (tmp_path / "docs" / "hardware").mkdir(parents=True)
    (tmp_path / "docs" / "hardware" / "cross_compile.md").write_text("# Cross compilation\n", encoding="utf-8")
    page = tmp_path / "docs" / "example.md"
    page.write_text(
        "# Example\n\nSee `docs/hardware/cross_compile.md` for the details.\n",
        encoding="utf-8",
    )

    result = run(tmp_path)

    assert result.returncode == PASS, result.stderr


def test_a_bare_filename_is_not_checked(tmp_path: Path) -> None:
    """`summary.json` names a run artifact, not a committed file.

    Checking bare names would fail every page that documents a generated report,
    and the tempting fix would be to weaken the check.
    """
    (tmp_path / "docs").mkdir()
    page = tmp_path / "docs" / "example.md"
    page.write_text(
        "# Example\n\n`make report` writes `summary.json` under `runs/`.\n",
        encoding="utf-8",
    )

    result = run(tmp_path)

    assert result.returncode == PASS, result.stderr


def test_a_runtime_artifact_is_not_checked(tmp_path: Path) -> None:
    """`runs/` is git-ignored, so no reference into it can ever resolve."""
    (tmp_path / "docs").mkdir()
    page = tmp_path / "docs" / "example.md"
    page.write_text(
        "# Example\n\n`make perf` writes `runs/performance/startup-cold.json`.\n",
        encoding="utf-8",
    )

    result = run(tmp_path)

    assert result.returncode == PASS, result.stderr


def test_the_deliberately_local_plan_is_allowlisted(tmp_path: Path) -> None:
    """ADR-0004 cites the unpublished Motion plan; that exclusion is recorded.

    If the plan is ever published, the allowlist entry is deleted and the citation
    starts being checked -- which is why the exclusion is a named constant rather
    than a silent skip.
    """
    (tmp_path / "docs").mkdir()
    page = tmp_path / "docs" / "adr.md"
    page.write_text(
        "# ADR\n\nThe plan (`robot/control/PLAN.md` phase 1) requires it.\n",
        encoding="utf-8",
    )

    result = run(tmp_path)

    assert result.returncode == PASS, result.stderr


def test_an_unreadable_root_is_incomplete(tmp_path: Path) -> None:
    """A missing root is INCOMPLETE, never PASS: "could not look" is not "right"."""
    result = run(tmp_path / "does-not-exist")

    assert result.returncode == INCOMPLETE


def test_the_makefile_runs_the_check_as_part_of_docs() -> None:
    """`make docs` must run the check, or foundation-checks never sees it."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    docs_target = makefile.split("\ndocs:\n", 1)[1].split("\n\n", 1)[0]
    assert "check_doc_references.py" in docs_target


@pytest.mark.parametrize(
    "page",
    [
        "docs/decisions/ADR-0004-arm-selection.md",
        "tasks/pick_place/README.md",
        "hardware/arm_drivers/README.md",
    ],
)
def test_the_corrected_pages_no_longer_cite_a_missing_file(page: str) -> None:
    """The three pages this issue corrected keep their promise.

    Each is asserted separately so a future edit that reintroduces one of the
    original dangling references fails with the page name, not just an aggregate.
    """
    text = (ROOT / page).read_text(encoding="utf-8")
    for absent in (
        "`robot/control/PLAN.md`",
        "`tasks/pick_place/verifier.py`",
        "`docs/hardware/cross_compile.md`",
    ):
        if absent.startswith("`robot/control/PLAN.md`") and page.startswith("docs/"):
            # ADR-0004 quotes the plan's gates instead of citing the file; the
            # README says plainly that the plan is local.
            continue
        assert absent not in text, f"{page} cites {absent}"
