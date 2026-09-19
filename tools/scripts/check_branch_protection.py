#!/usr/bin/env python3
"""Compare the declared main-branch safety matrix against the live protection.

Issue #224 found that `main` required only `foundation-checks`, that
`strict` and `enforce_admins` were both false, and that the release workflow
accepted any `v*` tag. The gap was not that the rule was unknown; it was that
nothing compared the intended rule with the configured one, so the protection
could be narrowed without any command noticing.

This checker reads the declaration in `.github/rulesets/main-protection.json`
and compares it against the protection the GitHub API actually reports.

Exit codes are the contract, matching `quality_gates.py`:

* ``0`` PASS -- every declared requirement was observed on the live repository;
* ``1`` FAIL -- the live configuration contradicts the declaration;
* ``2`` INCOMPLETE -- the live configuration could not be observed at all.

A missing token, a rate-limited API or a network failure is INCOMPLETE and never
PASS. "We could not look" and "we looked and it was right" are different
findings, and only the second one may be reported as a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from _paths import ROOT

PASS = 0
FAIL = 1
INCOMPLETE = 2

DEFAULT_DECLARATION = ROOT / ".github" / "rulesets" / "main-protection.json"
API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"


class DeclarationError(RuntimeError):
    """The declaration itself is malformed, which is always a FAIL."""


class UnobservableError(RuntimeError):
    """The live configuration could not be read, which is INCOMPLETE."""


def load_declaration(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeclarationError(f"cannot read declaration: {path}") from exc
    if not isinstance(payload, dict):
        raise DeclarationError("declaration must be a JSON object")

    checks = payload.get("required_status_checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(not isinstance(item, str) or not item.strip() for item in checks)
    ):
        raise DeclarationError("required_status_checks must be a non-empty list of check names")
    if len(set(checks)) != len(checks):
        raise DeclarationError("required_status_checks contains a duplicate check name")
    return payload


def _request(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": _API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UnobservableError(f"the protection API returned {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise UnobservableError(f"the protection API could not be reached: {exc}") from exc


def fetch_live_protection(repository: str, branch: str, token: str) -> dict[str, Any]:
    payload = _request(f"{API_ROOT}/repos/{repository}/branches/{branch}/protection", token)
    if not isinstance(payload, dict):
        raise UnobservableError("the protection API returned an unexpected payload")
    return payload


def compare(declaration: dict[str, Any], live: dict[str, Any]) -> tuple[str, ...]:
    """Return the reasons the live configuration contradicts the declaration."""
    reasons: list[str] = []
    declared = declaration["required_status_checks"]

    status_checks = live.get("required_status_checks")
    if not isinstance(status_checks, dict):
        return ("the live configuration requires no status checks at all",)

    live_checks = status_checks.get("contexts")
    if not isinstance(live_checks, list):
        live_checks = []
    checks = status_checks.get("checks")
    if isinstance(checks, list):
        for entry in checks:
            if isinstance(entry, dict) and isinstance(entry.get("context"), str):
                live_checks.append(entry["context"])

    missing = [name for name in declared if name not in live_checks]
    if missing:
        reasons.append("the live configuration does not require: " + ", ".join(sorted(missing)))

    declared_strict = declaration.get("strict_required_status_checks") is True
    if declared_strict and status_checks.get("strict") is not True:
        reasons.append("the declaration requires strict up-to-date branches but the live configuration does not")

    if declaration.get("require_pull_request") is True:
        reviews = live.get("required_pull_request_reviews")
        if not isinstance(reviews, dict):
            reasons.append("the declaration requires a pull request but the live configuration does not")
        else:
            required = declaration.get("required_approving_review_count")
            live_required = reviews.get("required_approving_review_count")
            if isinstance(required, int) and live_required != required:
                reasons.append(
                    f"the declaration requires {required} approving review(s) "
                    f"but the live configuration reports {live_required!r}"
                )
            if (
                declaration.get("dismiss_stale_reviews_on_push") is True
                and reviews.get("dismiss_stale_reviews") is not True
            ):
                reasons.append("the declaration dismisses stale reviews on push but the live configuration does not")

    if declaration.get("require_conversation_resolution") is True:
        if live.get("required_conversation_resolution", {}).get("enabled") is not True:
            reasons.append("the declaration requires conversation resolution but the live configuration does not")

    bypass = declaration.get("administrator_bypass")
    if isinstance(bypass, dict) and bypass.get("allowed") is False:
        if live.get("enforce_admins", {}).get("enabled") is not True:
            reasons.append("the declaration disables administrator bypass but the live configuration still allows it")

    return tuple(reasons)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the main-branch protection against the declaration")
    parser.add_argument("--declaration", type=Path, default=DEFAULT_DECLARATION)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--branch", default=None, help="defaults to the declaration's target_branch")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--live", type=Path, help="read a recorded protection payload instead of calling the API")
    args = parser.parse_args(argv)

    try:
        declaration = load_declaration(args.declaration)
    except DeclarationError as exc:
        print(f"declaration error: {exc}", file=sys.stderr)
        return FAIL

    branch = args.branch or declaration.get("target_branch") or "main"

    if args.live is not None:
        try:
            live = json.loads(Path(args.live).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(f"cannot read recorded protection payload: {exc}", file=sys.stderr)
            return INCOMPLETE
        if not isinstance(live, dict):
            print("recorded protection payload must be a JSON object", file=sys.stderr)
            return INCOMPLETE
    else:
        if not args.repository:
            print(
                "INCOMPLETE: set GITHUB_REPOSITORY or pass --repository to observe the live protection", file=sys.stderr
            )
            return INCOMPLETE
        if not args.token:
            print("INCOMPLETE: set GITHUB_TOKEN to observe the live protection", file=sys.stderr)
            return INCOMPLETE
        try:
            live = fetch_live_protection(args.repository, branch, args.token)
        except UnobservableError as exc:
            print(f"INCOMPLETE: {exc}", file=sys.stderr)
            return INCOMPLETE

    reasons = compare(declaration, live)
    if reasons:
        print(f"branch protection for {branch} does not match the declaration:", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return FAIL

    check_count = len(declaration["required_status_checks"])
    print(f"branch protection for {branch} matches the declared safety matrix ({check_count} checks)")
    return PASS


if __name__ == "__main__":
    raise SystemExit(main())
