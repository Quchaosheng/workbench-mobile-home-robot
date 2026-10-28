#!/usr/bin/env python3
"""Turn `gh attestation verify` output into the record the manifest stores.

`gh attestation verify --format json` answers "is there a bundle that this
identity signed". It does not tell the release workflow whether the bundle covers
*the digest that was pushed*, and a manifest that says `signed` without that
check is a workflow assertion rather than evidence (issue #436).

This reads the bundles, finds the subject digests they attest, and refuses when
the pushed digest is not among them. The output is the `verification` object the
manifest records, so what a reviewer reads is what was actually checked.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class VerificationError(Exception):
    """The attestation does not cover what the workflow published."""


def attested_digests(bundles: object) -> list[str]:
    """Every subject digest the bundles name, in `sha256:` form."""

    digests: list[str] = []
    if not isinstance(bundles, list):
        raise VerificationError("attestation verification did not produce a list of bundles")
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        result = bundle.get("verificationResult")
        statement = result.get("statement") if isinstance(result, dict) else None
        subjects = statement.get("subject") if isinstance(statement, dict) else None
        if not isinstance(subjects, list):
            continue
        for subject in subjects:
            if not isinstance(subject, dict):
                continue
            digest = subject.get("digest")
            sha256 = digest.get("sha256") if isinstance(digest, dict) else None
            if isinstance(sha256, str) and sha256:
                digests.append(f"sha256:{sha256}")
    return digests


def build_record(bundles: object, *, repository: str, digest: str) -> dict[str, Any]:
    if not repository or "/" not in repository:
        raise VerificationError(f"repository must be owner/name, got {repository!r}")
    if not digest.startswith("sha256:"):
        raise VerificationError(f"digest must be a sha256 digest, got {digest!r}")

    digests = attested_digests(bundles)
    if not digests:
        raise VerificationError("no attestation bundle names a subject digest")
    if digest not in digests:
        raise VerificationError(f"attestation covers {', '.join(sorted(set(digests)))}, not the published {digest}")

    owner = repository.split("/", 1)[0]
    return {
        "result": "verified",
        "reference": f"oci://ghcr.io/{repository}@{digest}",
        "verifier": f"gh attestation verify --owner {owner}",
        "attested_digests": sorted(set(digests)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", type=Path, required=True, help="gh attestation verify JSON output")
    parser.add_argument("--repository", required=True, help="owner/name of the published image")
    parser.add_argument("--digest", required=True, help="sha256 digest that was published")
    parser.add_argument("--output", type=Path, required=True, help="where to write the verification record")
    args = parser.parse_args(argv)

    try:
        bundles = json.loads(args.bundles.read_text(encoding="utf-8"))
        record = build_record(bundles, repository=args.repository, digest=args.digest)
    except (OSError, ValueError, VerificationError) as error:
        print(f"attestation verification failed: {error}", file=sys.stderr)
        return 1

    args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"attestation verified for {args.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
