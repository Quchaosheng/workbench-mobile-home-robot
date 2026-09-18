#!/usr/bin/env python3
"""Bind a release artifact to the exact inputs that produced it.

A release is only as trustworthy as the provenance that names its inputs. The
manifest written here records the source commit, the workflow ref, the pinned
base-image digest, the dependency-lock revision, the SBOM hash and the image
registry digest, and it reports whether an attestation covers the published
image. ``verify_manifest`` then refuses a promotion whose provenance is missing,
mutable or inconsistent, so a mutable tag can never stand in for a digest.

Nothing here publishes an artifact. It records what was built and decides whether
the record is complete enough to promote.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_VERSION = 2

REQUIRED_PROVENANCE_FIELDS = (
    "source_commit",
    "workflow_ref",
    "base_image",
    "lock",
    "sbom",
    "image",
)

ATTESTATION_STATUSES = frozenset({"unsigned", "signed", "verified"})
SIGNED_ATTESTATION_STATUSES = frozenset({"signed", "verified"})

_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PINNED_PACKAGE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+-]*)$")
_FROM_LINE = re.compile(r"^FROM\s+(?P<reference>\S+)", re.IGNORECASE | re.MULTILINE)


class ProvenanceError(ValueError):
    """A provenance input or manifest cannot be trusted as release evidence."""


@dataclass(frozen=True)
class ImagePin:
    """One image reference, resolved or refused as an immutable digest."""

    reference: str
    name: str
    digest: str | None

    @property
    def pinned(self) -> bool:
        return self.digest is not None


def parse_image_reference(reference: object) -> ImagePin:
    """Split an image reference and require an immutable digest.

    A tag is mutable: the same name can resolve to different bytes tomorrow. Only
    a ``@sha256:`` digest identifies exactly one artifact, so a reference without
    one is reported as unpinned rather than accepted as a weaker identity.
    """
    if type(reference) is not str or not reference.strip():
        raise ProvenanceError("image reference must be a non-empty string")
    text = reference.strip()
    name, separator, digest = text.partition("@")
    if not separator:
        return ImagePin(reference=text, name=text, digest=None)
    if not name.strip():
        raise ProvenanceError(f"image reference has no name: {text!r}")
    if _SHA256_DIGEST.fullmatch(digest) is None:
        raise ProvenanceError(f"image digest must be sha256:<64 hex>: {digest!r}")
    return ImagePin(reference=text, name=name, digest=digest)


def resolve_base_image(dockerfile_text: object) -> ImagePin:
    """Return the digest-pinned base image declared by a Dockerfile's first FROM."""
    if type(dockerfile_text) is not str or not dockerfile_text.strip():
        raise ProvenanceError("Dockerfile text must be a non-empty string")
    match = _FROM_LINE.search(dockerfile_text)
    if match is None:
        raise ProvenanceError("Dockerfile declares no FROM base image")
    return parse_image_reference(match.group("reference"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def lock_revision(path: Path) -> dict[str, Any]:
    """Summarize a fully pinned dependency lock, refusing any unpinned entry.

    A range such as ``pydantic>=2.8,<3`` resolves differently over time, so it is
    not a lock entry. The revision records the file hash and the exact
    name-version pairs, which is what a rebuild has to reproduce.
    """
    lock_path = Path(path)
    if not lock_path.is_file():
        raise ProvenanceError(f"dependency lock does not exist: {lock_path}")
    try:
        text = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ProvenanceError(f"dependency lock is unreadable: {lock_path}") from exc

    packages: list[dict[str, str]] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PINNED_PACKAGE.fullmatch(line)
        if match is None:
            raise ProvenanceError(f"dependency lock line {line_number} is not pinned as name==version: {line!r}")
        name = match.group(1)
        if name.lower() in seen:
            raise ProvenanceError(f"dependency lock pins {name!r} more than once")
        seen.add(name.lower())
        packages.append({"name": name, "version": match.group(2)})
    if not packages:
        raise ProvenanceError(f"dependency lock has no pinned packages: {lock_path}")
    return {
        "path": str(lock_path),
        "sha256": sha256(lock_path),
        "package_count": len(packages),
        "packages": packages,
    }


def _image_metadata(
    image: str,
    registry_image: str | None,
    *,
    inspect: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    runner = inspect if inspect is not None else _docker_image_inspect
    payload = runner(image)
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], Mapping):
        raise ProvenanceError(f"image inspection returned an unexpected payload for {image!r}")
    record = payload[0]
    repo_digests = [value for value in record.get("RepoDigests", []) or [] if isinstance(value, str)]
    registry_digest = None
    if registry_image:
        target = registry_image.lower()
        for value in repo_digests:
            name, separator, digest = value.rpartition("@")
            if separator and name.lower() == target and _SHA256_DIGEST.fullmatch(digest):
                registry_digest = digest
                break
    return {
        "image": image,
        "image_id": record.get("Id"),
        "repo_digests": repo_digests,
        "registry_digest": registry_digest,
    }


def _docker_image_inspect(image: str) -> Any:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def git_commit(repository: Path | None = None) -> str:
    command = ["git", "rev-parse", "HEAD"]
    if repository is not None:
        command = ["git", "-C", str(repository), *command]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    commit = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ProvenanceError(f"git HEAD is not a full commit hash: {commit!r}")
    return commit


def build_manifest(
    *,
    version: str,
    source_commit: str,
    workflow_ref: str,
    base_image: ImagePin,
    lock: Mapping[str, Any],
    sbom: Mapping[str, Any],
    image: Mapping[str, Any],
    attestation: Mapping[str, Any] | None = None,
    registry_image: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Assemble one release manifest. It describes inputs; it grants nothing."""
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "version": version,
        "source_commit": source_commit,
        "workflow_ref": workflow_ref,
        "registry_image": registry_image,
        "base_image": {
            "reference": base_image.reference,
            "name": base_image.name,
            "digest": base_image.digest,
            "pinned": base_image.pinned,
        },
        "lock": dict(lock),
        "sbom": dict(sbom),
        "image": dict(image),
        "attestation": dict(attestation) if attestation is not None else {"status": "unsigned"},
        "environment": {"platform": platform.platform(), "python": platform.python_version()},
    }
    manifest["release_eligible"] = not verify_manifest(manifest)
    return manifest


def verify_manifest(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the reasons this manifest must not be promoted. Empty means promotable."""
    if not isinstance(manifest, Mapping):
        return ("manifest must be a JSON object",)

    reasons: list[str] = []
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        reasons.append(f"schema_version must be {MANIFEST_SCHEMA_VERSION}")

    for field in REQUIRED_PROVENANCE_FIELDS:
        if manifest.get(field) is None:
            reasons.append(f"missing provenance field: {field}")

    commit = manifest.get("source_commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        reasons.append("source_commit must be a full 40-character commit hash")

    workflow_ref = manifest.get("workflow_ref")
    if not isinstance(workflow_ref, str) or not workflow_ref.strip():
        reasons.append("workflow_ref must be a non-empty string")

    version = manifest.get("version")
    if not isinstance(version, str) or not version.startswith("v"):
        reasons.append("version must be a v-prefixed release version")

    base_image = manifest.get("base_image")
    if not isinstance(base_image, Mapping):
        reasons.append("base_image must be an object")
    else:
        digest = base_image.get("digest")
        pinned = base_image.get("pinned") is True
        if not pinned or not isinstance(digest, str) or _SHA256_DIGEST.fullmatch(digest) is None:
            reasons.append("base_image must be pinned to an immutable sha256 digest")

    lock = manifest.get("lock")
    if not isinstance(lock, Mapping):
        reasons.append("lock must be an object")
    else:
        lock_sha = lock.get("sha256")
        if not isinstance(lock_sha, str) or re.fullmatch(r"[0-9a-f]{64}", lock_sha) is None:
            reasons.append("lock.sha256 must be a hex digest")
        count = lock.get("package_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            reasons.append("lock.package_count must be a positive integer")

    sbom = manifest.get("sbom")
    if not isinstance(sbom, Mapping):
        reasons.append("sbom must be an object")
    elif not isinstance(sbom.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", str(sbom.get("sha256"))) is None:
        reasons.append("sbom.sha256 must be a hex digest")

    image = manifest.get("image")
    if not isinstance(image, Mapping):
        reasons.append("image must be an object")
    else:
        digest = image.get("registry_digest")
        if not isinstance(digest, str) or _SHA256_DIGEST.fullmatch(digest) is None:
            # A tag, an empty digest or a local-only image is not a promotable
            # artifact: there is nothing immutable to promote or to attest.
            reasons.append("image.registry_digest must be an immutable sha256 digest from the registry")

    attestation = manifest.get("attestation")
    if not isinstance(attestation, Mapping):
        reasons.append("attestation must be an object")
    elif attestation.get("status") not in ATTESTATION_STATUSES:
        reasons.append(f"attestation.status must be one of {sorted(ATTESTATION_STATUSES)}")

    return tuple(reasons)


def promotion_reasons(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Reasons this manifest cannot be *promoted*, a stricter question.

    Provenance completeness is one thing; whether the published artifact carries
    an attestation is another. The build-time manifest is written before the
    attestation step exists, so it reports ``unsigned`` truthfully and stays
    provenance-complete. Promotion additionally requires a signed or verified
    attestation.
    """
    reasons = list(verify_manifest(manifest))
    attestation = manifest.get("attestation") if isinstance(manifest, Mapping) else None
    status = attestation.get("status") if isinstance(attestation, Mapping) else None
    if status not in SIGNED_ATTESTATION_STATUSES:
        reasons.append("attestation must be signed or verified before promotion")
    return tuple(reasons)


def _default_workflow_ref() -> str:
    import os

    for name in ("GITHUB_WORKFLOW_REF", "GITHUB_REF", "GITHUB_SHA"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or verify a release provenance manifest")
    parser.add_argument("--image", help="tested local image reference")
    parser.add_argument("--sbom", type=Path, help="SPDX SBOM produced for the image")
    parser.add_argument("--version", help="release version, v-prefixed for a promotion")
    parser.add_argument("--registry-image", help="registry repository the image is pushed to")
    parser.add_argument("--dockerfile", type=Path, default=Path("Dockerfile"), help="Dockerfile naming the base image")
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path("docker/python-constraints.txt"),
        help="pinned dependency lock",
    )
    parser.add_argument("--workflow-ref", default=None, help="workflow ref that produced the artifact")
    parser.add_argument("--attestation-status", default="unsigned", choices=("unsigned", "signed", "verified"))
    parser.add_argument("--output", type=Path, help="where to write the manifest")
    parser.add_argument("--dry-run", action="store_true", help="skip image inspection and write nothing")
    parser.add_argument("--verify", type=Path, help="verify an existing manifest instead of writing one")
    parser.add_argument(
        "--mark-attestation",
        choices=sorted(ATTESTATION_STATUSES),
        help="record an attestation outcome on the manifest at --output",
    )
    parser.add_argument("--require-promotion", action="store_true", help="exit non-zero when verification refuses")
    return parser


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"cannot read manifest: {path}") from exc
    if not isinstance(payload, dict):
        raise ProvenanceError(f"manifest must be a JSON object: {path}")
    return payload


def record_attestation(path: Path, status: str) -> dict[str, Any]:
    """Set the attestation status on an existing manifest and return it.

    The build-time manifest is written before the attestation step runs, so this
    records the outcome afterwards instead of leaving the file claiming
    ``unsigned`` forever. Everything else in the manifest is preserved, because
    re-deriving provenance here would let the two runs disagree.
    """
    if status not in ATTESTATION_STATUSES:
        raise ProvenanceError(f"attestation status must be one of {sorted(ATTESTATION_STATUSES)}")
    document = _load_manifest(path)
    document["attestation"] = {"status": status, "recorded_at": datetime.now(UTC).isoformat()}
    document["release_eligible"] = not verify_manifest(document)
    Path(path).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document


def report_verification(manifest: Mapping[str, Any], *, require_promotion: bool = False) -> int:
    reasons = promotion_reasons(manifest) if require_promotion else verify_manifest(manifest)
    label = "promotable" if require_promotion else "provenance-complete"
    if reasons:
        print(f"manifest is NOT {label}:", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    print(f"manifest verification passed ({label})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.verify is not None:
        return report_verification(_load_manifest(args.verify), require_promotion=args.require_promotion)

    if args.mark_attestation is not None:
        if args.output is None:
            parser.error("--mark-attestation requires --output")
        record_attestation(Path(args.output), args.mark_attestation)
        if args.require_promotion:
            return report_verification(_load_manifest(Path(args.output)), require_promotion=True)
        return 0

    missing = [name for name in ("image", "sbom", "version", "output") if getattr(args, name) is None]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(f"--{name}" for name in missing))

    if not args.sbom.is_file():
        raise ProvenanceError(f"SBOM does not exist: {args.sbom}")

    dockerfile = Path(args.dockerfile)
    if not dockerfile.is_file():
        raise ProvenanceError(f"Dockerfile does not exist: {dockerfile}")
    base_image = resolve_base_image(dockerfile.read_text(encoding="utf-8"))
    lock = lock_revision(Path(args.lock))
    sbom = {"format": "spdx-json", "path": str(args.sbom), "sha256": sha256(args.sbom)}

    if args.dry_run:
        image: dict[str, Any] = {
            "image": args.image,
            "image_id": None,
            "repo_digests": [],
            "registry_digest": None,
        }
        print(json.dumps({"dry_run": True, "base_image": base_image.reference, "lock": lock["sha256"]}, indent=2))
        return 0

    image = _image_metadata(args.image, args.registry_image)
    manifest = build_manifest(
        version=args.version,
        source_commit=git_commit(),
        workflow_ref=(args.workflow_ref or _default_workflow_ref() or ""),
        base_image=base_image,
        lock=lock,
        sbom=sbom,
        image=image,
        attestation={"status": args.attestation_status},
        registry_image=args.registry_image,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    if args.require_promotion:
        return report_verification(manifest, require_promotion=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
