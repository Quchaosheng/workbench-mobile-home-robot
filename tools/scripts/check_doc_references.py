#!/usr/bin/env python3
"""Fail a build when a documented repository path does not exist (Issue #585).

Some references in the committed documentation pointed at files that were not in
the repository. Two were load-bearing rather than cosmetic: ``ADR-0004`` grounded
the arm decision in an unversioned ``robot/control/PLAN.md``, and
``hardware/arm_drivers/README.md`` sent a reader to a cross-compilation note that
was never committed. ``make docs`` could not see either, because mkdocs does not
resolve a code span: it renders ```PLAN.md``` as text whether or not the file is
there.

This checker reads what a reader reads -- a backticked path in a Markdown page --
and resolves it against the tree.

What counts as a reference
--------------------------

Only a code span that names a file with a known extension *and* contains a path
separator is checked. That boundary is deliberate:

* A bare filename such as ``summary.json`` or ``package.xml`` names an artifact a
  command produces at run time, or a file inside a ROS package the page is not
  pointing at. Checking those would fail every page that documents a generated
  report, and the honest fix would be to weaken the check.
* A path under ``runs/`` is produced by a command the page tells the reader to
  run. ``runs/`` is git-ignored on purpose, so no run artifact is ever committed
  and such a reference can never resolve.

A reference resolves when it names a path from the repository root, from the
directory of the page that cites it, from ``docs/``, or as the tail of a real
path. The last form is what lets ``config/arm.yaml`` in a module README name
``robot/control/workbench_motion/config/arm.yaml`` without repeating the prefix.

Exit codes match the other gates:

* ``0`` PASS -- every checked reference resolves, or is listed as deliberately
  local;
* ``1`` FAIL -- at least one reference names a path that is not in the tree;
* ``2`` INCOMPLETE -- the tree could not be read, so no verdict is honest.

A missing directory is INCOMPLETE and never PASS: "we could not look" and "we
looked and it was right" are different findings.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from _paths import ROOT

PASS = 0
FAIL = 1
INCOMPLETE = 2

# Extensions worth resolving. A code span naming a directory, a symbol, a URL or
# a bare filename is not checked; see the module docstring for why.
EXTENSIONS = (
    "md",
    "py",
    "json",
    "yaml",
    "yml",
    "xacro",
    "tsv",
    "txt",
    "sh",
    "cpp",
    "hpp",
    "h",
    "c",
    "xml",
    "urdf",
    "srdf",
    "cfg",
    "lock",
    "toml",
)

# A backticked code span with at least one path separator and a known extension.
REFERENCE = re.compile(
    r"`(?P<path>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\."
    r"(?:" + "|".join(EXTENSIONS) + r"))`"
)

# Markdown a reader is sent to: the documentation tree and every module README.
PAGE_GLOBS = ("docs/**/*.md", "**/README*.md")

# Documents that are deliberately kept out of the repository. The check names
# them here so the exclusion is a recorded decision rather than a silent skip.
#
# `robot/control/PLAN.md` is the Motion plan. It is not published (see
# `robot/control/README.md`), so a citation of it cannot resolve; ADR-0004 quotes
# the two gates it depends on instead. If the plan is ever published, delete this
# entry and the citation starts being checked.
DELIBERATELY_LOCAL = frozenset({"robot/control/PLAN.md"})

# Paths a page documents as the output of a command rather than as a committed
# file. `runs/` is git-ignored (see .gitignore).
RUNTIME_PREFIXES = ("runs/",)

# A reference may name a path by its tail; this is the separator that makes the
# suffix match unambiguous rather than a substring match.
SUFFIX_MARKER = "/"


@dataclass(frozen=True)
class Finding:
    page: Path
    reference: str
    root: Path

    def __str__(self) -> str:
        # Relative to the root that was actually checked, not the one this script
        # happens to live in: the failure message must name the page a reader of
        # that tree can open.
        try:
            page = self.page.relative_to(self.root)
        except ValueError:
            page = self.page
        return f"{page}: `{self.reference}` does not exist"


def repository_paths(root: Path) -> set[str]:
    """Every committed-looking file, as a POSIX path relative to the root."""
    found: set[str] = set()
    for path in root.rglob("*"):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.is_file():
            found.add(path.relative_to(root).as_posix())
    return found


def resolves(reference: str, page: Path, root: Path, paths: set[str]) -> bool:
    """Resolve from the root, the page's directory, `docs/`, or as a path tail."""
    for candidate in (root, page.parent, root / "docs"):
        if (candidate / reference).exists():
            return True
    tail = SUFFIX_MARKER + reference
    return any(path.endswith(tail) for path in paths)


def pages(root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in PAGE_GLOBS:
        for path in root.glob(pattern):
            if ".git" in path.parts or "node_modules" in path.parts:
                continue
            found.add(path)
    return sorted(found)


def check(root: Path = ROOT) -> list[Finding]:
    paths = repository_paths(root)
    findings: list[Finding] = []
    for page in pages(root):
        try:
            text = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in REFERENCE.finditer(text):
            reference = match.group("path")
            if reference in DELIBERATELY_LOCAL:
                continue
            if reference.startswith(RUNTIME_PREFIXES):
                continue
            # `...` is documentation's abbreviation for "somewhere below here",
            # not a path a reader can open.
            if "..." in reference:
                continue
            if not resolves(reference, page, root, paths):
                findings.append(Finding(page, reference, root))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a documented repository path does not exist.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root to check (default: the one this script lives in)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        print(f"cannot read the repository root: {root}", file=sys.stderr)
        return INCOMPLETE

    checked = pages(root)
    findings = check(root)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        print(
            f"{len(findings)} documented path(s) do not exist",
            file=sys.stderr,
        )
        return FAIL

    print(f"documentation references resolve ({len(checked)} pages)")
    return PASS


if __name__ == "__main__":
    raise SystemExit(main())
