"""Runtime-assurance invariants and the fault acceptance matrix (Assurance Track v1).

The repository already proves individual safety boundaries with individual tests.
What it did not have is one place that answers the question a reviewer asks before
trusting a fault-injection run: *which* invariants are claimed, *where* each one is
enforced, *which* committed test proves it, and *what terminal state* each injected
fault must reach.

This module owns the rules for two committed artifacts:

* ``docs/assurance/invariants-v1.json`` -- the invariants, each naming an
  enforcement symbol and the tests that demonstrate it;
* ``docs/assurance/acceptance-matrix-v1.json`` -- one row per fault case, each
  naming the terminal state it must reach and the invariants it exercises.

Three properties are deliberate, and each has a test:

* **A claim must resolve.** An ``ENFORCED`` invariant names a real
  ``path.py::symbol`` and at least one test function that exists in that file.
  Prose cannot satisfy either field, so a renamed symbol or a deleted test fails
  the gate instead of silently weakening the claim.
* **Status never inflates.** ``NOT_IMPLEMENTED`` rows carry no evidence and must
  state the gap; ``ENFORCED``/``SIMULATED`` rows must carry evidence. A row
  cannot be both unproven and evidenced, and an invariant nobody references is a
  failure rather than an orphan.
* **A fault never becomes a completion.** Only the nominal case may expect a
  ``confirmed`` verdict. Every injected fault must expect ``insufficient_evidence``
  (or ``refuted``), which is the written form of the rule the fault injectors
  already follow.

This module is read-only: it imports no runtime module, starts no simulator and
writes no run. A passing verdict says the register is internally consistent and
its evidence resolves; it never says a fault was injected on hardware, and it is
not a functional-safety certification.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scenario_contract import ERROR, FAIL, PASS, Finding, Verdict, safe_repo_path

DEFAULT_ROOT = Path(__file__).resolve().parents[4]
INVARIANTS_PATH = DEFAULT_ROOT / "docs/assurance/invariants-v1.json"
MATRIX_PATH = DEFAULT_ROOT / "docs/assurance/acceptance-matrix-v1.json"
GENERATED_PATH = DEFAULT_ROOT / "docs/architecture/assurance-track.md"

INVARIANTS_VERSION = "assurance-invariants-v1"
MATRIX_VERSION = "assurance-acceptance-matrix-v1"

INVARIANT_STATUSES: tuple[str, ...] = ("ENFORCED", "NOT_IMPLEMENTED")
COVERAGE_CLASSES: tuple[str, ...] = ("SIMULATED", "HARDWARE_TESTED", "NOT_EXECUTED")
VERDICTS: tuple[str, ...] = ("confirmed", "refuted", "insufficient_evidence")

INVARIANT_KEYS = frozenset({"id", "statement", "boundary", "enforced_by", "evidence", "gap", "status"})
INVARIANT_REQUIRED = frozenset({"id", "statement", "boundary", "status"})
INVARIANTS_DOC_KEYS = frozenset(
    {"invariants_version", "status", "issue", "decision_record", "verdicts", "statuses", "invariants"}
)

ROW_KEYS = frozenset(
    {
        "case_id",
        "fault",
        "trigger",
        "expected_terminal_state",
        "expected_verdict",
        "invariants",
        "evidence",
        "coverage",
        "gap",
    }
)
ROW_REQUIRED = frozenset(
    {"case_id", "fault", "trigger", "expected_terminal_state", "expected_verdict", "invariants", "evidence", "coverage"}
)
MATRIX_DOC_KEYS = frozenset(
    {
        "matrix_version",
        "status",
        "issue",
        "decision_record",
        "invariants_version",
        "terminal_states",
        "evidence_classes",
        "required_faults",
        "rows",
    }
)

# Every diagnostic this module can emit. Each has a test that asserts it against
# an otherwise-valid artifact, so a validator that rejects everything cannot pass.
EMITTED_CODES = (
    "ASSURANCE_DOC_MALFORMED",
    "ASSURANCE_MISSING_FIELD",
    "ASSURANCE_UNKNOWN_FIELD",
    "ASSURANCE_DUPLICATE_ID",
    "ASSURANCE_INVALID_STATUS",
    "ASSURANCE_UNRESOLVED_SYMBOL",
    "ASSURANCE_UNRESOLVED_EVIDENCE",
    "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS",
    "ASSURANCE_UNKNOWN_INVARIANT",
    "ASSURANCE_UNKNOWN_FAULT",
    "ASSURANCE_INVALID_TERMINAL_STATE",
    "ASSURANCE_INVALID_VERDICT",
    "ASSURANCE_FAULT_CLAIMS_COMPLETION",
    "ASSURANCE_UNCOVERED_INVARIANT",
)


class AssuranceError(RuntimeError):
    """An assurance artifact cannot be read or judged."""


@dataclass(frozen=True)
class AssuranceDocuments:
    invariants: dict[str, Any]
    matrix: dict[str, Any]


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise AssuranceError(f"cannot read the {label} at {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise AssuranceError(f"the {label} at {path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise AssuranceError(f"the {label} at {path} must be a JSON object")
    return payload


def load_documents(invariants_path: Path = INVARIANTS_PATH, matrix_path: Path = MATRIX_PATH) -> AssuranceDocuments:
    """Read both artifacts and check the keys a reader depends on."""

    invariants = _read_json(invariants_path, "invariant register")
    matrix = _read_json(matrix_path, "acceptance matrix")
    _check_doc_keys(invariants, INVARIANTS_DOC_KEYS, "invariant register")
    _check_doc_keys(matrix, MATRIX_DOC_KEYS, "acceptance matrix")
    if invariants["invariants_version"] != INVARIANTS_VERSION:
        raise AssuranceError(
            f"the invariant register declares {invariants['invariants_version']!r}; this reader implements "
            f"{INVARIANTS_VERSION!r}"
        )
    if matrix["matrix_version"] != MATRIX_VERSION:
        raise AssuranceError(
            f"the acceptance matrix declares {matrix['matrix_version']!r}; this reader implements {MATRIX_VERSION!r}"
        )
    if matrix["invariants_version"] != INVARIANTS_VERSION:
        raise AssuranceError(
            f"the acceptance matrix joins {matrix['invariants_version']!r}, not {INVARIANTS_VERSION!r}"
        )
    if tuple(invariants["statuses"]) != INVARIANT_STATUSES:
        raise AssuranceError(f"the invariant register must declare the statuses {INVARIANT_STATUSES}")
    if tuple(invariants["verdicts"]) != VERDICTS:
        raise AssuranceError(f"the invariant register must declare the verdicts {VERDICTS}")
    if tuple(matrix["evidence_classes"]) != COVERAGE_CLASSES:
        raise AssuranceError(f"the acceptance matrix must declare the evidence classes {COVERAGE_CLASSES}")
    return AssuranceDocuments(invariants=invariants, matrix=matrix)


def _check_doc_keys(payload: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    missing = sorted(allowed - set(payload))
    if missing:
        raise AssuranceError(f"the {label} is missing keys: {', '.join(missing)}")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise AssuranceError(f"the {label} has unknown keys: {', '.join(unknown)}")


def _symbol_names(tree: ast.Module) -> set[str]:
    """Every top-level name plus every ``Class.method``, read without importing."""

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(f"{node.name}.{child.name}")
    return names


def resolves(reference: Any, root: Path = DEFAULT_ROOT) -> bool:
    """True when ``reference`` is ``path.py::symbol`` and that symbol is defined."""

    if not isinstance(reference, str):
        return False
    path_part, separator, symbol = reference.partition("::")
    # Both `path.py::Class::method` (pytest node ids) and `path.py::Class.method`
    # are accepted, because the two spellings already appear in this repository.
    symbol = symbol.strip().replace("::", ".")
    if separator != "::" or not symbol or not path_part.endswith(".py"):
        return False
    if not safe_repo_path(path_part, root):
        return False
    candidate = root / path_part
    if not candidate.is_file():
        return False
    try:
        tree = ast.parse(candidate.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    return symbol in _symbol_names(tree)


def _add(findings: list[Finding], code: str, path: str, detail: str) -> None:
    findings.append(Finding(code, path, detail, ERROR))


def _check_shape(
    entry: dict[str, Any],
    *,
    name: str,
    keys: frozenset[str],
    required: frozenset[str],
    findings: list[Finding],
) -> bool:
    if not isinstance(entry, dict):
        _add(findings, "ASSURANCE_DOC_MALFORMED", name, "entry must be a JSON object")
        return False
    missing = sorted(required - set(entry))
    if missing:
        _add(findings, "ASSURANCE_MISSING_FIELD", name, f"missing required fields: {', '.join(missing)}")
    unknown = sorted(set(entry) - keys)
    if unknown:
        _add(findings, "ASSURANCE_UNKNOWN_FIELD", name, f"unknown fields: {', '.join(unknown)}")
    return not missing


def _check_evidence(entry: dict[str, Any], *, name: str, root: Path, findings: list[Finding]) -> list[str]:
    evidence = entry.get("evidence")
    if not isinstance(evidence, list):
        _add(findings, "ASSURANCE_DOC_MALFORMED", f"{name}.evidence", "must be a list of test references")
        return []
    unresolved = [item for item in evidence if not resolves(item, root)]
    for item in unresolved:
        _add(
            findings,
            "ASSURANCE_UNRESOLVED_EVIDENCE",
            f"{name}.evidence",
            f"{item!r} does not resolve to a test function in this repository",
        )
    return [item for item in evidence if item not in unresolved]


def validate_invariants(document: dict[str, Any], *, root: Path = DEFAULT_ROOT) -> list[Finding]:
    """Judge one invariant register. Every finding names the field it is about."""

    findings: list[Finding] = []
    entries = document.get("invariants")
    if not isinstance(entries, list) or not entries:
        _add(findings, "ASSURANCE_DOC_MALFORMED", "invariants", "must be a non-empty list")
        return findings

    seen: set[str] = set()
    for position, entry in enumerate(entries):
        name = f"invariants[{position}]"
        if not _check_shape(entry, name=name, keys=INVARIANT_KEYS, required=INVARIANT_REQUIRED, findings=findings):
            continue
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier.startswith("INV-"):
            _add(findings, "ASSURANCE_DOC_MALFORMED", f"{name}.id", f"{identifier!r} is not an INV-* identifier")
            continue
        if identifier in seen:
            _add(findings, "ASSURANCE_DUPLICATE_ID", f"{name}.id", f"{identifier} is declared twice")
            continue
        seen.add(identifier)

        status = entry.get("status")
        if status not in INVARIANT_STATUSES:
            _add(
                findings, "ASSURANCE_INVALID_STATUS", f"{name}.status", f"{status!r} is not one of {INVARIANT_STATUSES}"
            )
            continue
        evidence = _check_evidence(entry, name=name, root=root, findings=findings)
        enforced_by = entry.get("enforced_by")
        gap = entry.get("gap")

        if status == "ENFORCED":
            if gap is not None:
                _add(
                    findings,
                    "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS",
                    f"{name}.gap",
                    "an ENFORCED invariant cannot carry a gap",
                )
            if not resolves(enforced_by, root):
                _add(
                    findings,
                    "ASSURANCE_UNRESOLVED_SYMBOL",
                    f"{name}.enforced_by",
                    f"{enforced_by!r} is not a resolving path.py::symbol enforcement point",
                )
            if not evidence:
                _add(
                    findings,
                    "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS",
                    f"{name}.evidence",
                    "an ENFORCED invariant needs at least one resolving test",
                )
        else:
            if enforced_by is not None:
                _add(
                    findings,
                    "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS",
                    f"{name}.enforced_by",
                    "a NOT_IMPLEMENTED invariant cannot name an enforcement point",
                )
            if evidence:
                _add(
                    findings,
                    "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS",
                    f"{name}.evidence",
                    "a NOT_IMPLEMENTED invariant cannot cite evidence",
                )
            if not isinstance(gap, str) or not gap.strip():
                _add(
                    findings,
                    "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS",
                    f"{name}.gap",
                    "a NOT_IMPLEMENTED invariant must state its gap",
                )
    return findings


def validate_matrix(
    document: dict[str, Any], invariants: dict[str, Any], *, root: Path = DEFAULT_ROOT
) -> list[Finding]:
    """Judge the acceptance matrix against the invariant register it joins."""

    findings: list[Finding] = []
    declared = {entry.get("id") for entry in invariants.get("invariants", []) if isinstance(entry, dict)}
    terminal_states = document.get("terminal_states")
    if not isinstance(terminal_states, dict) or not terminal_states:
        _add(findings, "ASSURANCE_DOC_MALFORMED", "terminal_states", "must be a non-empty domain map")
        terminal_states = {}
    for domain, values in terminal_states.items():
        if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
            _add(findings, "ASSURANCE_DOC_MALFORMED", f"terminal_states.{domain}", "must be a non-empty list of names")

    required_faults = document.get("required_faults")
    if not isinstance(required_faults, list) or not required_faults:
        _add(findings, "ASSURANCE_DOC_MALFORMED", "required_faults", "must be a non-empty list")
        required_faults = []
    rows = document.get("rows")
    if not isinstance(rows, list) or not rows:
        _add(findings, "ASSURANCE_DOC_MALFORMED", "rows", "must be a non-empty list")
        return findings

    seen_cases: set[str] = set()
    seen_faults: set[str] = set()
    referenced: set[str] = set()
    for position, row in enumerate(rows):
        name = f"rows[{position}]"
        if not _check_shape(row, name=name, keys=ROW_KEYS, required=ROW_REQUIRED, findings=findings):
            continue
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            _add(findings, "ASSURANCE_DOC_MALFORMED", f"{name}.case_id", "must be a non-empty string")
            continue
        if case_id in seen_cases:
            _add(findings, "ASSURANCE_DUPLICATE_ID", f"{name}.case_id", f"{case_id} is declared twice")
            continue
        seen_cases.add(case_id)

        fault = row.get("fault")
        if fault not in required_faults:
            _add(findings, "ASSURANCE_UNKNOWN_FAULT", f"{name}.fault", f"{fault!r} is not a declared fault class")
        elif fault in seen_faults:
            _add(findings, "ASSURANCE_DUPLICATE_ID", f"{name}.fault", f"fault {fault!r} already has an acceptance case")
        else:
            seen_faults.add(fault)

        state = row.get("expected_terminal_state")
        if not isinstance(state, dict) or set(state) != {"domain", "state"}:
            _add(
                findings,
                "ASSURANCE_INVALID_TERMINAL_STATE",
                f"{name}.expected_terminal_state",
                "must be {domain, state}",
            )
        else:
            domain, value = state["domain"], state["state"]
            if domain not in terminal_states or value not in terminal_states.get(domain, []):
                _add(
                    findings,
                    "ASSURANCE_INVALID_TERMINAL_STATE",
                    f"{name}.expected_terminal_state",
                    f"{value!r} is not a declared {domain!r} terminal state",
                )

        verdict = row.get("expected_verdict")
        if verdict not in VERDICTS:
            _add(
                findings,
                "ASSURANCE_INVALID_VERDICT",
                f"{name}.expected_verdict",
                f"{verdict!r} is not one of {VERDICTS}",
            )
        elif verdict == "confirmed" and fault != "none":
            _add(
                findings,
                "ASSURANCE_FAULT_CLAIMS_COMPLETION",
                f"{name}.expected_verdict",
                f"fault {fault!r} may not expect a confirmed goal verdict",
            )

        row_invariants = row.get("invariants")
        if not isinstance(row_invariants, list) or not row_invariants:
            _add(findings, "ASSURANCE_DOC_MALFORMED", f"{name}.invariants", "must name at least one invariant")
        else:
            for identifier in row_invariants:
                if identifier not in declared:
                    _add(
                        findings, "ASSURANCE_UNKNOWN_INVARIANT", f"{name}.invariants", f"{identifier!r} is not declared"
                    )
                else:
                    referenced.add(identifier)

        coverage = row.get("coverage")
        evidence = _check_evidence(row, name=name, root=root, findings=findings)
        gap = row.get("gap")
        if coverage not in COVERAGE_CLASSES:
            _add(
                findings,
                "ASSURANCE_INVALID_STATUS",
                f"{name}.coverage",
                f"{coverage!r} is not one of {COVERAGE_CLASSES}",
            )
        elif coverage == "NOT_EXECUTED":
            if evidence:
                _add(
                    findings,
                    "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS",
                    f"{name}.evidence",
                    "a NOT_EXECUTED case cannot cite evidence",
                )
            if not isinstance(gap, str) or not gap.strip():
                _add(
                    findings,
                    "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS",
                    f"{name}.gap",
                    "a NOT_EXECUTED case must state its gap",
                )
        else:
            if gap is not None:
                _add(
                    findings,
                    "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS",
                    f"{name}.gap",
                    "an executed case cannot carry a gap",
                )
            if not evidence:
                _add(
                    findings,
                    "ASSURANCE_EVIDENCE_CONTRADICTS_STATUS",
                    f"{name}.evidence",
                    "an executed case needs at least one resolving test",
                )

    missing_faults = sorted(set(required_faults) - seen_faults)
    if missing_faults:
        _add(findings, "ASSURANCE_UNKNOWN_FAULT", "rows", f"no acceptance case covers: {', '.join(missing_faults)}")
    uncovered = sorted(
        identifier for identifier in declared if isinstance(identifier, str) and identifier not in referenced
    )
    if uncovered:
        _add(
            findings,
            "ASSURANCE_UNCOVERED_INVARIANT",
            "rows",
            f"no acceptance case exercises: {', '.join(uncovered)}",
        )
    return findings


def validate(documents: AssuranceDocuments, *, root: Path = DEFAULT_ROOT) -> Verdict:
    """Judge both artifacts as one register."""

    findings = validate_invariants(documents.invariants, root=root)
    findings += validate_matrix(documents.matrix, documents.invariants, root=root)
    if any(finding.severity == ERROR for finding in findings):
        return Verdict("FAIL", FAIL, "assurance-track", findings)
    return Verdict("PASS", PASS, "assurance-track", findings)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    return str(value)


def rows_to_markdown(documents: AssuranceDocuments) -> str:
    """Render the committed page from the two artifacts, deterministically."""

    invariants = documents.invariants["invariants"]
    rows = documents.matrix["rows"]
    lines = [
        "<!-- Generated from docs/assurance/*.json. Do not edit by hand. -->",
        "",
        "## Invariants",
        "",
        "| ID | Statement | Boundary | Status | Enforced at | Evidence |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in sorted(invariants, key=lambda item: item["id"]):
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{entry['id']}`",
                    entry["statement"],
                    entry["boundary"],
                    entry["status"],
                    f"`{entry['enforced_by']}`" if entry.get("enforced_by") else f"gap: {entry.get('gap', '-')}",
                    "<br>".join(f"`{item}`" for item in entry.get("evidence") or []) or "-",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## Acceptance matrix",
        "",
        "| Case | Fault | Expected terminal state | Expected verdict | Invariants | Coverage | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda item: item["case_id"]):
        state = row["expected_terminal_state"]
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row['case_id']}`",
                    row["fault"],
                    f"`{state['domain']}:{state['state']}`",
                    f"`{row['expected_verdict']}`",
                    _cell(row["invariants"]),
                    row["coverage"],
                    "<br>".join(f"`{item}`" for item in row.get("evidence") or []) or f"gap: {row.get('gap', '-')}",
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def generated_document(documents: AssuranceDocuments) -> str:
    """The full committed page: the claim boundary, then the generated tables."""

    return "\n".join(
        [
            "# Assurance Track",
            "",
            "The runtime-assurance register for this repository: the invariants the runtime is",
            "claimed to hold, the fault cases those invariants must survive, and the committed",
            "test that proves each claim. The tables are **generated** from",
            "`docs/assurance/invariants-v1.json` and `docs/assurance/acceptance-matrix-v1.json`.",
            "",
            "Regenerate and verify with:",
            "",
            "```bash",
            "python3 tools/scripts/check_assurance.py --check-generated",
            "```",
            "",
            "## What this track claims, and what it does not",
            "",
            "It claims **runtime assurance**: bounded actions, fail-closed cancellation, correlated",
            "device acknowledgements, declared terminal states under fault, and three-valued",
            "verification where missing evidence yields `insufficient_evidence` rather than a guess.",
            "",
            "It does **not** claim a functional-safety certification. No row here is a SIL, ASIL or",
            "ISO 26262 claim, and no row is physical evidence: every current row is `SIMULATED`.",
            "A scripted or unit-level fault proves the rule, not the machine.",
            "",
            "## How to read a row",
            "",
            "- **Status** is `ENFORCED` (a resolving enforcement symbol and at least one resolving test)",
            "  or `NOT_IMPLEMENTED` (a stated gap, no evidence). A gap is visible on purpose.",
            "- **Expected terminal state** names one state from a declared vocabulary. A fault that",
            "  reaches no declared state is a failure, not a pass.",
            "- **Expected verdict** is three-valued. Only the nominal case may expect `confirmed`;",
            "  every injected fault must expect `insufficient_evidence` or `refuted`.",
            "- **Coverage** is `SIMULATED`, `HARDWARE_TESTED` or `NOT_EXECUTED`. It never inflates:",
            "  an executed case must cite a resolving test, an unexecuted case must state its gap.",
            "",
            "## Register",
            "",
            rows_to_markdown(documents),
        ]
    )


__all__ = [
    "COVERAGE_CLASSES",
    "EMITTED_CODES",
    "GENERATED_PATH",
    "INVARIANTS_PATH",
    "INVARIANT_STATUSES",
    "MATRIX_PATH",
    "VERDICTS",
    "AssuranceDocuments",
    "AssuranceError",
    "generated_document",
    "load_documents",
    "resolves",
    "rows_to_markdown",
    "validate",
    "validate_invariants",
    "validate_matrix",
]
