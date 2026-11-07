"""Issue #393: every runtime package in the source tree must be declared.

A package that exists but is not in the setuptools list is invisible in editable
mode and missing from the built wheel, so the gap only shows up on install.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"


def declared_packages() -> set[str]:
    text = PYPROJECT.read_text(encoding="utf-8")
    block = re.search(r"^packages = \[(.*?)^\]", text, re.S | re.M)
    assert block is not None, "pyproject.toml must declare an explicit package list"
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def source_packages() -> set[str]:
    """Top-level importable packages that carry runtime code."""
    found: set[str] = set()
    for base in ("libs", "services", "firmware"):
        for path in (ROOT / base).rglob("__init__.py"):
            rel = path.relative_to(ROOT)
            # .../workbench/x/__init__.py -> workbench.x, .../workbench_pkg/__init__.py -> workbench_pkg
            parts = list(rel.parts[:-1])
            if "workbench" in parts:
                index = parts.index("workbench")
                # The namespace package itself is not declared; its subpackages are.
                if len(parts) > index + 1:
                    found.add(".".join(parts[index : index + 2]))
            elif parts and parts[-1].startswith("workbench_"):
                found.add(parts[-1])
    return found


def test_every_runtime_package_is_declared_for_the_build() -> None:
    declared = declared_packages()
    missing = {name for name in source_packages() if name not in declared}
    assert not missing, f"packages absent from the built wheel: {sorted(missing)}"


def test_the_perception_adapter_is_declared() -> None:
    """The exact gap the issue names."""

    assert "workbench_perception" in declared_packages()
    assert (ROOT / "services" / "perception" / "workbench_perception" / "ingestion.py").is_file()
