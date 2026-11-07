#!/usr/bin/env python3
"""Refuse a release whose tag commit never passed the declared safety matrix.

Issue #224 observed that the release workflow accepted any ``v*`` tag and ran
``make check``. Neither proves that the tagged commit is the one protected
``main`` reviewed, nor that the container, kernel, MCU, CodeQL and
dependency-review jobs that ``main`` requires ever succeeded on it. A tag can be
pushed onto any commit, including one that never saw a pull request.

This checker answers the two questions the release workflow must ask before it is
allowed to build an image:

* is the tag commit an ancestor of the protected branch, so a tagged commit
  cannot bypass review; and
* did the required checks pass on that commit, so a skipped or failed job cannot
  be published as a release.

Exit codes match ``quality_gates.py`` and ``check_branch_protection.py``:
``0`` PASS, ``1`` FAIL, ``2`` INCOMPLETE. A commit whose checks cannot be read is
INCOMPLETE, never PASS, because "we could not look" is not "it passed".
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

from _jsonio import load_json
from _paths import ROOT

PASS = 0
FAIL = 1
INCOMPLETE = 2

DEFAULT_DECLARATION = ROOT / ".github" / "rulesets" / "main-protection.json"
API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"

# Conclusions that may publish an image. `success` is the only passing
# conclusion; `skipped` and `neutral` are deliberately excluded, because a
# required job that did not run is exactly the gap Issue #224 describes.
_PASSING_CONCLUSION = "success"


class UnobservableError(RuntimeError):
    """The release inputs could not be read, so no verdict can be given."""


def load_required_checks(path: Path) -> tuple[str, ...]:
    try:
        payload = load_json(Path(path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UnobservableError(f"cannot read the branch-protection declaration: {path}") from exc
    checks = payload.get("required_status_checks") if isinstance(payload, dict) else None
    if not isinstance(checks, list) or not checks:
        raise UnobservableError("the declaration has no required_status_checks")
    return tuple(checks)


def _api(url: str, token: str) -> Any:
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
        raise UnobservableError(f"the API returned {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise UnobservableError(f"the API could not be reached: {exc}") from exc


def commit_is_ancestor(repository: str, ancestor: str, descendant: str, token: str) -> bool:
    """Return whether `ancestor` is reachable from `descendant`."""
    payload = _api(f"{API_ROOT}/repos/{repository}/compare/{ancestor}...{descendant}", token)
    if not isinstance(payload, dict) or "status" not in payload:
        raise UnobservableError("the compare API returned an unexpected payload")
    # `identical` and `behind` mean the tagged commit is contained in the branch.
    return payload["status"] in {"identical", "behind"}


_MAX_CHECK_RUN_PAGES = 20


def _all_check_runs(repository: str, commit: str, token: str) -> list[dict[str, Any]]:
    """Every check run for a commit, following pagination.

    One page is 100 entries and a busy commit can exceed it, so a required check
    that fell past the first page used to be reported as missing — which reads
    exactly like a check that never ran (issue #401).
    """
    runs: list[dict[str, Any]] = []
    for page in range(1, _MAX_CHECK_RUN_PAGES + 1):
        payload = _api(
            f"{API_ROOT}/repos/{repository}/commits/{commit}/check-runs?per_page=100&page={page}",
            token,
        )
        if not isinstance(payload, dict):
            raise UnobservableError("the check-runs API returned an unexpected payload")
        page_runs = payload.get("check_runs")
        if not isinstance(page_runs, list):
            raise UnobservableError("the check-runs API returned an unexpected payload")
        runs.extend(run for run in page_runs if isinstance(run, dict))
        total = payload.get("total_count")
        if len(page_runs) < 100:
            return runs
        if isinstance(total, int) and len(runs) >= total:
            return runs
    raise UnobservableError("the check-runs API kept returning full pages past the page limit")


def _run_recency(run: dict[str, Any]) -> str:
    """A sortable key for when a check run finished, falling back to when it started.

    ISO-8601 timestamps in the same format sort lexicographically, which is what
    the API returns. A run with neither timestamp sorts before any run that has
    one, so a well-formed rerun always wins.
    """
    completed = run.get("completed_at")
    if isinstance(completed, str) and completed:
        return completed
    started = run.get("started_at")
    if isinstance(started, str) and started:
        return started
    return ""


def failing_required_checks(
    required: tuple[str, ...],
    check_runs: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Split required checks into those that did not pass and those not reported."""
    # The newest run per name, by the timestamps the API reports rather than by
    # response order. A stale failed run appearing after a successful rerun used
    # to win, which turned a green commit red for the wrong reason (issue #401).
    latest: dict[str, dict[str, Any]] = {}
    for run in check_runs:
        name = run.get("name")
        if not isinstance(name, str) or not name:
            continue
        current = latest.get(name)
        if current is None or _run_recency(run) >= _run_recency(current):
            latest[name] = run

    not_passing: list[str] = []
    missing: list[str] = []
    for name in required:
        run = latest.get(name)
        if run is None:
            missing.append(name)
            continue
        if run.get("status") != "completed":
            not_passing.append(f"{name} ({run.get('status')})")
            continue
        if run.get("conclusion") != _PASSING_CONCLUSION:
            not_passing.append(f"{name} ({run.get('conclusion')})")
    return not_passing, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a release tag commit against the declared safety matrix")
    parser.add_argument("--declaration", type=Path, default=DEFAULT_DECLARATION)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA"), help="the tagged commit")
    parser.add_argument("--protected-branch", default=None, help="defaults to the declaration's target_branch")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--check-runs", type=Path, help="read recorded check runs instead of calling the API")
    parser.add_argument("--recorded-ancestor", action="store_true", help="treat the commit as reachable (offline mode)")
    args = parser.parse_args(argv)

    try:
        required = load_required_checks(args.declaration)
    except UnobservableError as exc:
        print(f"INCOMPLETE: {exc}", file=sys.stderr)
        return INCOMPLETE

    if args.check_runs is not None:
        try:
            payload = load_json(Path(args.check_runs))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(f"INCOMPLETE: cannot read recorded check runs: {exc}", file=sys.stderr)
            return INCOMPLETE
        check_runs = payload.get("check_runs") if isinstance(payload, dict) else None
        if not isinstance(check_runs, list):
            print("INCOMPLETE: recorded payload must contain a check_runs list", file=sys.stderr)
            return INCOMPLETE
    else:
        if not args.repository or not args.commit or not args.token:
            print(
                "INCOMPLETE: set GITHUB_REPOSITORY, GITHUB_SHA and GITHUB_TOKEN to observe the release inputs",
                file=sys.stderr,
            )
            return INCOMPLETE
        try:
            check_runs = _all_check_runs(args.repository, args.commit, args.token)
        except UnobservableError as exc:
            print(f"INCOMPLETE: {exc}", file=sys.stderr)
            return INCOMPLETE

    branch = args.protected_branch
    if branch is None:
        try:
            branch = load_json(Path(args.declaration)).get("target_branch")
        except (OSError, UnicodeError, json.JSONDecodeError):
            branch = None
    branch = branch or "main"

    if not args.recorded_ancestor:
        if args.check_runs is not None:
            # Offline verification of the check matrix is still meaningful; the
            # ancestry question needs the network and is reported as unverified
            # rather than silently assumed.
            print(
                f"note: ancestry of {args.commit} in {branch} was not verified in offline mode",
                file=sys.stderr,
            )
        else:
            try:
                if not commit_is_ancestor(args.repository, args.commit, branch, args.token):
                    print(
                        f"the release commit {args.commit} is not reachable from protected {branch}; "
                        "a tag may not bypass review",
                        file=sys.stderr,
                    )
                    return FAIL
            except UnobservableError as exc:
                print(f"INCOMPLETE: {exc}", file=sys.stderr)
                return INCOMPLETE

    not_passing, missing = failing_required_checks(required, check_runs)
    if missing or not_passing:
        print("the release commit did not pass the declared safety matrix:", file=sys.stderr)
        for name in missing:
            print(f"  - {name}: no check run reported for this commit", file=sys.stderr)
        for name in not_passing:
            print(f"  - {name}: did not succeed", file=sys.stderr)
        return FAIL

    print(f"release commit {args.commit} passed all {len(required)} required checks and is reachable from {branch}")
    return PASS


if __name__ == "__main__":
    raise SystemExit(main())
