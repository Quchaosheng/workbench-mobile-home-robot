"""The assurance page is a read-only projection, and it must stay honest.

The failure this file guards against is specific: a dashboard is where an honest
register turns into a progress bar. Three rules are tested here, each of which the
rendering would otherwise be free to break quietly:

* a gap stays a gap - it is listed, not folded into a count that reads as progress;
* ``NOT_EXECUTED`` is not "no data"; it is a case that never ran, and a reader must
  be able to tell it from a pass;
* an unreadable register is reported as *unavailable*, never as an empty register.
  "The runtime claims nothing" and "we could not read what it claims" are different
  facts and must not render the same way.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/backend"))
sys.path.insert(0, str(ROOT / "libs/contracts"))

from workbench_backend.assurance_view import (
    CLAIM_SCOPE,
    AssuranceUnavailable,
    project,
)


def test_the_committed_register_projects_without_error() -> None:
    payload = project(ROOT)
    assert payload["read_only"] is True
    assert payload["counts"]["invariants"] == len(payload["invariants"])
    assert payload["counts"]["cases"] == len(payload["cases"])


def test_every_invariant_carries_its_status_label() -> None:
    payload = project(ROOT)
    for entry in payload["invariants"]:
        assert entry["status"] in {"ENFORCED", "NOT_IMPLEMENTED"}
        assert entry["status_label"]
        assert entry["runtime_boundary"]


def test_a_not_implemented_invariant_is_listed_as_a_gap() -> None:
    payload = project(ROOT)
    gaps = {gap["id"] for gap in payload["gaps"] if gap["kind"] == "invariant"}
    not_implemented = {entry["id"] for entry in payload["invariants"] if entry["status"] == "NOT_IMPLEMENTED"}
    assert gaps == not_implemented
    for gap in payload["gaps"]:
        assert gap["detail"], f"{gap['id']} must say what is missing"
        assert gap["label"], f"{gap['id']} needs a readable label"


def test_an_unexecuted_case_is_never_counted_as_executed() -> None:
    payload = project(ROOT)
    for case in payload["cases"]:
        assert case["executed"] is (case["coverage"] != "NOT_EXECUTED")
    not_executed = [case for case in payload["cases"] if not case["executed"]]
    assert payload["counts"]["not_executed"] == len(not_executed)
    assert {gap["id"] for gap in payload["gaps"] if gap["kind"] == "case"} == {
        case["case_id"] for case in not_executed
    }


def test_only_the_nominal_case_expects_a_confirmed_verdict() -> None:
    """The register's own rule, re-checked at the projection so the UI cannot invert it."""

    payload = project(ROOT)
    for case in payload["cases"]:
        if case["expected_verdict"] == "confirmed":
            assert case["fault"] == "none", case["case_id"]


def test_the_claim_scope_is_shown_verbatim() -> None:
    payload = project(ROOT)
    assert payload["claim_scope"] == CLAIM_SCOPE
    assert "非功能安全认证" in payload["claim_scope"]


def test_a_missing_register_is_unavailable_not_empty(tmp_path: Path) -> None:
    with pytest.raises(AssuranceUnavailable):
        project(tmp_path)


def test_a_malformed_register_is_refused(tmp_path: Path) -> None:
    (tmp_path / "docs/assurance").mkdir(parents=True)
    (tmp_path / "docs/assurance/invariants-v1.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "docs/assurance/acceptance-matrix-v1.json").write_text("{}", encoding="utf-8")
    with pytest.raises(AssuranceUnavailable):
        project(tmp_path)


def test_an_unknown_status_is_refused_rather_than_rendered(tmp_path: Path) -> None:
    """A status outside the vocabulary must fail closed; a badge for 'maybe' is worse."""

    base = tmp_path / "docs/assurance"
    base.mkdir(parents=True)
    (base / "invariants-v1.json").write_text(
        json.dumps({"invariants": [{"id": "INV-01", "statement": "x", "runtime_boundary": "b", "status": "PROBABLY"}]}),
        encoding="utf-8",
    )
    (base / "acceptance-matrix-v1.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    with pytest.raises(AssuranceUnavailable):
        project(tmp_path)


def test_an_unknown_coverage_class_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "docs/assurance"
    base.mkdir(parents=True)
    (base / "invariants-v1.json").write_text(json.dumps({"invariants": []}), encoding="utf-8")
    (base / "acceptance-matrix-v1.json").write_text(
        json.dumps({"rows": [{"case_id": "c", "coverage": "PROBABLY_FINE"}]}), encoding="utf-8"
    )
    with pytest.raises(AssuranceUnavailable):
        project(tmp_path)
