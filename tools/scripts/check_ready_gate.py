"""Report a pull request or issue body as READY, BLOCKED or INCOMPLETE (Issue #310).

The definition this reads is committed at ``tools/qa/governance/ready-gate-v1.json``
and validated against ``tools/schemas/ready-gate-v1.schema.json``. That one file is
the source of truth for both halves of the gate: the Definition of Ready that an
issue body must carry before it is assigned, and the Exit Gate that a pull request
body must carry before it is reviewed.

The detector only reports what a body contains: it never executes the commands it
reads, never opens a link, and never contacts GitHub. A passing verdict grants no
merge, release or physical-validation authority.

Exit codes are the contract, matching the other quality gates in this repository:

* 0 READY -- every required section carries content, and every checklist item is
  ticked with the evidence its entry requires;
* 1 BLOCKED -- the body is readable and complete enough to judge, and at least one
  item is missing, unticked, unsupported, or claims authority the gate withholds;
* 2 INCOMPLETE -- the body could not be judged at all, for example an unreadable
  file, an empty body, or a gate definition that does not validate.

INCOMPLETE is deliberately a separate outcome. A gate that reports BLOCKED when it
simply could not read its input trains reviewers to ignore it, and a gate that
reports READY when it could not read its input is worse.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _paths import ROOT

READY = 0
BLOCKED = 1
INCOMPLETE = 2

GATE_DEFINITION = ROOT / "tools/qa/governance/ready-gate-v1.json"
GATE_SCHEMA = ROOT / "tools/schemas/ready-gate-v1.schema.json"
TEMPLATE = ROOT / ".github/pull_request_template.md"

# The two bodies this gate judges. A pull request body carries the Exit Gate; an
# issue body carries the Definition of Ready. They are separate modes rather than
# one lenient mode, because a rule that only applies to one of them must not be
# silently satisfied by the other.
PULL_REQUEST = "pull-request"
ISSUE = "issue"
KINDS = (PULL_REQUEST, ISSUE)

HEADING = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$", re.MULTILINE)
CHECKBOX = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX])\]\s+(?P<label>.*)$", re.MULTILINE)

# What counts as evidence for one ``requires`` entry. These are deliberately
# syntactic: the gate reports presence, and a reviewer judges quality.
EVIDENCE_MARKERS = {
    "command": re.compile(r"(?:^|\s)(?:make|python3?|pytest|ruff|git|docker|npm|bash|sh)\s+\S+", re.MULTILINE),
    # A full or abbreviated git object id, or an issue reference. The two
    # lookaheads keep the hex branch from matching an ordinary English word that
    # happens to be spelled with a-f, such as "defaced".
    "commit_reference": re.compile(
        r"\b(?=[0-9a-f]{7,40}\b)(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b|#\d+",
        re.IGNORECASE,
    ),
    "path_reference": re.compile(r"`[^`]*[/.][^`]*`|\b[\w./-]+\.(?:py|md|json|ya?ml|toml|txt|cfg)\b"),
    "evidence_reference": re.compile(r"`[^`]+`|\bruns/\S+|https?://\S+|\bPASS\b|\bFAIL\b|\bexit code \d+"),
    # The negated outcome words, plus the rollback and disable wording that the
    # Definition of Ready asks for. The alternation is grouped: an ungrouped
    # alternation would let any single branch ignore the anchors around it.
    "failure_path": re.compile(
        r"\b(?:fail|reject|invalid|refus|denied|error|timeout|rollback|roll back|revert|disable|not_executed)",
        re.IGNORECASE,
    ),
    "body_text": re.compile(r"\S"),
}


class GateError(RuntimeError):
    """The gate cannot produce a trustworthy verdict."""


def _write_archive(path: Path, kind: str, verdict: Verdict) -> None:
    """Record the verdict without recording a body that may contain a secret.

    The archive deliberately stores the finding messages and the counts, not the
    body or the raw command output: this script reads untrusted text, and an
    evidence file that echoed it would publish whatever the body contained.
    """

    payload = {
        "gate": "workbench-ready-gate-v1",
        "kind": kind,
        "status": verdict.status,
        "exit_code": verdict.exit_code,
        "findings": verdict.details,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@dataclass
class Section:
    heading: str
    body: str
    level: int


@dataclass
class Verdict:
    status: str
    exit_code: int
    details: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.status == "READY"


def _load_gate() -> dict[str, Any]:
    try:
        definition = json.loads(GATE_DEFINITION.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot read {GATE_DEFINITION}: {error}") from error

    try:
        schema = json.loads(GATE_SCHEMA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot read {GATE_SCHEMA}: {error}") from error

    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - jsonschema is a development dependency
        raise GateError("jsonschema is required to validate the gate definition") from None

    errors = sorted(Draft202012Validator(schema).iter_errors(definition), key=lambda error: list(error.path))
    if errors:
        detail = "; ".join(f"{'.'.join(str(part) for part in error.path)}: {error.message}" for error in errors)
        raise GateError(f"the gate definition does not validate: {detail}")

    _validate_definition(definition)
    return definition


def _validate_definition(definition: dict[str, Any]) -> None:
    """Refuse a definition the detector cannot enforce honestly.

    These checks are separate from the JSON Schema on purpose. The schema proves
    the file is shaped like a gate; this proves the gate can actually fire. A
    rule naming a requirement kind the detector does not implement, or a rule
    whose own sample it does not match, is a defect in the definition, so it is
    reported as INCOMPLETE rather than as a finding against the body under review.
    """

    if definition.get("gate_version") != "workbench-ready-gate-v1":
        raise GateError(f"unexpected gate_version: {definition.get('gate_version')!r}")

    for key in ("required_sections", "required_checklist", "required_issue_sections"):
        for entry in definition[key]:
            for requirement in entry.get("requires", []):
                if requirement not in EVIDENCE_MARKERS:
                    raise GateError(f"{key}: unknown requirement kind {requirement!r}")

    for claim in definition["forbidden_claims"]:
        sample = claim["sample_violation"]
        if re.search(claim["pattern"], sample, re.IGNORECASE) is None:
            raise GateError(f"forbidden_claims: pattern {claim['pattern']!r} does not match its own sample {sample!r}")


def parse_sections(body: str) -> dict[str, Section]:
    """Index the body by its headings, keeping the text under each one."""

    matches = list(HEADING.finditer(body))
    sections: dict[str, Section] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        title = match.group("title").strip()
        sections.setdefault(
            title.casefold(),
            Section(heading=title, body=body[start:end].strip(), level=len(match.group("level"))),
        )
    return sections


def _requirement_satisfied(requirement: str, text: str) -> bool:
    marker = EVIDENCE_MARKERS.get(requirement)
    if marker is None:
        raise GateError(f"unknown requirement kind: {requirement!r}")
    return marker.search(text) is not None


def _strip_placeholders(text: str) -> str:
    """Drop template guidance so a comment cannot satisfy a requirement."""

    without_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return "\n".join(line for line in without_comments.splitlines() if not line.lstrip().startswith(">"))


def _check_sections(sections: dict[str, Section], definition: dict[str, Any], key: str) -> list[str]:
    """Return the findings for one section list, keyed by ``required_sections`` or ``required_issue_sections``."""

    details: list[str] = []
    for entry in definition[key]:
        heading = entry["heading"]
        section = sections.get(heading.casefold())
        if section is None:
            details.append(f"missing section: '{heading}' ({entry['reason']})")
            continue
        content = _strip_placeholders(section.body)
        if not content.strip():
            details.append(f"section '{heading}' is empty ({entry['reason']})")
            continue
        missing = [
            requirement for requirement in entry.get("requires", []) if not _requirement_satisfied(requirement, content)
        ]
        if missing:
            details.append(f"section '{heading}' has no {', '.join(missing)} ({entry['reason']})")
    return details


def _check_forbidden_claims(body: str, definition: dict[str, Any], source: str) -> list[str]:
    """Refuse a body that claims authority this checklist deliberately withholds.

    The reason text goes into the finding, and the message never repeats the
    matched sentence: a gate that echoed a forbidden claim into CI logs would
    reproduce the claim in a place that looks official.
    """

    details = []
    for claim in definition["forbidden_claims"]:
        match = re.search(claim["pattern"], body, re.IGNORECASE)
        if match is not None:
            line = body.count("\n", 0, match.start()) + 1
            details.append(f"forbidden claim in {source} at line {line}: {claim['reason']}")
    return details


def check_issue_body(body: str, definition: dict[str, Any], *, source: str = "<stdin>") -> Verdict:
    """Return the Definition-of-Ready verdict for one issue body."""

    details = _check_sections(parse_sections(body), definition, "required_issue_sections")
    details += _check_forbidden_claims(body, definition, source)

    if details:
        return Verdict(status="BLOCKED", exit_code=BLOCKED, details=details)
    return Verdict(
        status="READY",
        exit_code=READY,
        details=[
            f"{len(definition['required_issue_sections'])} Definition-of-Ready fields are present",
            "this verdict grants no scheduling, merge, release or physical-validation authority",
        ],
    )


def check_pull_request_body(body: str, definition: dict[str, Any], *, source: str = "<stdin>") -> Verdict:
    """Return the Exit Gate verdict for one pull request body."""

    details = _check_sections(parse_sections(body), definition, "required_sections")

    boxes = {label.strip().casefold(): mark for mark, label in CHECKBOX.findall(body)}
    for entry in definition["required_checklist"]:
        item = entry["item"]
        mark = boxes.get(item.strip().casefold())
        if mark is None:
            details.append(f"missing checklist item: '{item}' ({entry['reason']})")
            continue
        if mark.strip().casefold() != "x":
            details.append(f"unticked checklist item: '{item}' ({entry['reason']})")
            continue
        missing = [
            requirement
            for requirement in entry["requires"]
            if requirement != "body_text" and not _requirement_satisfied(requirement, body)
        ]
        if missing:
            details.append(f"checklist item '{item}' claims completion without a {', '.join(missing)}")

    details += _check_forbidden_claims(body, definition, source)

    if details:
        return Verdict(status="BLOCKED", exit_code=BLOCKED, details=details)
    return Verdict(
        status="READY",
        exit_code=READY,
        details=[
            f"{len(definition['required_sections'])} sections and "
            f"{len(definition['required_checklist'])} checklist items are present",
            "this verdict grants review readiness only; it grants no merge, release or physical-validation authority",
        ],
    )


def check_body(body: str, definition: dict[str, Any], *, source: str = "<stdin>", kind: str = PULL_REQUEST) -> Verdict:
    """Return the verdict for one body, dispatching on the kind of body it is.

    An empty body is INCOMPLETE rather than BLOCKED. Listing every missing rule
    against a body that was never written buries the one fact the operator needs,
    and a reviewer who reads thirteen findings for an empty paste learns to skim
    the output.
    """

    if not body.strip():
        raise GateError(f"the body from {source} is empty")
    if kind == PULL_REQUEST:
        return check_pull_request_body(body, definition, source=source)
    if kind == ISSUE:
        return check_issue_body(body, definition, source=source)
    raise GateError(f"unknown body kind: {kind!r}; expected one of {', '.join(KINDS)}")


def check_file(path: Path, definition: dict[str, Any], *, kind: str = PULL_REQUEST) -> Verdict:
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise GateError(f"cannot read {path}: {error}") from error
    return check_body(body, definition, source=str(path), kind=kind)


def _read_template(path: Path) -> tuple[str | None, str | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeError) as error:
        return None, f"cannot read {path}: {error}"


def check_templates_agree_with_the_gate(definition: dict[str, Any]) -> list[str]:
    """Return the reasons the committed templates and the gate disagree.

    A template the gate does not read is a template that silently drifts, and a
    gate whose template lacks a rule is a rule every new pull request inherits as
    a violation. Both directions are checked.
    """

    problems: list[str] = []

    template, error = _read_template(TEMPLATE)
    if error is not None:
        problems.append(error)
    else:
        headings = {section.heading for section in parse_sections(template).values()}
        for entry in definition["required_sections"]:
            if entry["heading"] not in headings:
                problems.append(f"the pull request template is missing the '{entry['heading']}' section")

        labels = {label.strip().casefold() for _, label in CHECKBOX.findall(template)}
        for entry in definition["required_checklist"]:
            if entry["item"].strip().casefold() not in labels:
                problems.append(f"the pull request template is missing the '{entry['item']}' checklist item")

    issue_path = ROOT / definition["issue_template"]
    issue_form, error = _read_template(issue_path)
    if error is not None:
        problems.append(error)
    else:
        issue_headings = {section.heading for section in parse_sections(issue_form).values()}
        for entry in definition["required_issue_sections"]:
            if entry["heading"] not in issue_headings:
                problems.append(f"the issue template is missing the '{entry['heading']}' section")

    # A gate rule that no committed template mentions is a rule nobody sees until
    # their pull request is blocked. The scripts must be named from the templates
    # so a reviewer can reproduce the verdict from the form itself.
    for path, text in ((TEMPLATE, template), (issue_path, issue_form)):
        if text is None:
            continue
        if "check_ready_gate.py" not in text:
            problems.append(f"{path} does not name tools/scripts/check_ready_gate.py")
        if "ready-gate-v1.json" not in text:
            problems.append(f"{path} does not name tools/qa/governance/ready-gate-v1.json")
    return problems


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report a pull request or issue body as READY, BLOCKED or INCOMPLETE.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--body-file", type=Path, help="markdown body to check")
    source.add_argument("--stdin", action="store_true", help="read the body from standard input")
    source.add_argument(
        "--check-template",
        action="store_true",
        help="check that the committed issue and pull request templates still match the gate",
    )
    parser.add_argument(
        "--kind",
        choices=KINDS,
        default=PULL_REQUEST,
        help="which half of the gate to apply: the exit gate (pull-request) or the definition of ready (issue)",
    )
    parser.add_argument("--gate", type=Path, default=GATE_DEFINITION)
    parser.add_argument(
        "--archive",
        type=Path,
        help="write the verdict as JSON to this path, for a CI run to attach as evidence",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    global GATE_DEFINITION
    GATE_DEFINITION = args.gate
    try:
        definition = _load_gate()
    except GateError as error:
        print(f"[INCOMPLETE] {error}", file=sys.stderr)
        return INCOMPLETE

    try:
        if args.check_template:
            problems = check_templates_agree_with_the_gate(definition)
            if problems:
                print("[BLOCKED] the template and the gate disagree:")
                for problem in problems:
                    print(f"  {problem}")
                return BLOCKED
            print("[READY] the committed templates match the gate")
            return READY

        if args.stdin:
            verdict = check_body(sys.stdin.read(), definition, source="<stdin>", kind=args.kind)
        else:
            if not args.body_file.is_file():
                print(f"[INCOMPLETE] no such body file: {args.body_file}", file=sys.stderr)
                return INCOMPLETE
            verdict = check_file(args.body_file, definition, kind=args.kind)
    except GateError as error:
        print(f"[INCOMPLETE] {error}", file=sys.stderr)
        return INCOMPLETE

    label = "note" if verdict.ready else "finding"
    print(f"[{verdict.status}] {len(verdict.details)} {label}(s)")
    for detail in verdict.details:
        print(f"  {detail}")
    if args.archive is not None:
        _write_archive(args.archive, args.kind, verdict)
    return verdict.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
