"""Issue #86: refuse a promotion whose input provenance is missing or mutable.

The tool is exercised offline. Image inspection is injected, so no Docker daemon
and no network are needed, and every refusal is a deterministic string compare.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "scripts"))

from release_manifest import (
    MANIFEST_SCHEMA_VERSION,
    ProvenanceError,
    build_manifest,
    git_commit,
    lock_revision,
    main,
    parse_image_reference,
    promotion_reasons,
    record_attestation,
    resolve_base_image,
    verify_manifest,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
HEX = "d" * 64
COMMIT = "e" * 40


def pinned_base() -> object:
    return parse_image_reference(f"nvidia/cuda:12.8.1-runtime-ubuntu24.04@{DIGEST_A}")


def lock_payload() -> dict:
    """A lock revision whose package list matches its own count.

    The list is not decoration: it is where the resolved package hashes live, so a
    count that disagrees with the list is refused by verify_manifest.
    """

    packages = [
        {"name": "pydantic", "version": "2.13.5", "hashes": [HEX]},
        {"name": "pytest", "version": "9.1.1", "hashes": [HEX]},
        {"name": "jsonschema", "version": "4.26.0", "hashes": [HEX]},
    ]
    return {
        "path": "docker/requirements-dev.lock",
        "sha256": HEX,
        "package_count": len(packages),
        "hashed_package_count": len(packages),
        "packages": packages,
    }


def sbom_payload() -> dict:
    return {"format": "spdx-json", "path": "workbench-1.spdx.json", "sha256": HEX}


def image_payload(registry_digest: str | None = DIGEST_B) -> dict:
    return {
        "image": "workbench-1:release-candidate",
        "image_id": DIGEST_A,
        "repo_digests": [f"ghcr.io/q/workbench@{DIGEST_B}"],
        "registry_digest": registry_digest,
    }


def manifest(**overrides: object) -> dict:
    fields = {
        "version": "v0.3.0",
        "source_commit": COMMIT,
        "workflow_ref": "refs/tags/v0.3.0",
        "base_image": pinned_base(),
        "lock": lock_payload(),
        "sbom": sbom_payload(),
        "image": image_payload(),
        "attestation": {"status": "signed"},
    }
    fields.update(overrides)
    return build_manifest(**fields)  # type: ignore[arg-type]


class ImageReferenceTests(unittest.TestCase):
    def test_a_mutable_tag_is_not_an_immutable_identity(self) -> None:
        pin = parse_image_reference("ghcr.io/q/workbench:latest")
        self.assertFalse(pin.pinned)
        self.assertIsNone(pin.digest)
        self.assertEqual(pin.name, "ghcr.io/q/workbench:latest")

    def test_a_digest_reference_is_pinned(self) -> None:
        pin = parse_image_reference(f"ghcr.io/q/workbench@{DIGEST_A}")
        self.assertTrue(pin.pinned)
        self.assertEqual(pin.digest, DIGEST_A)
        self.assertEqual(pin.name, "ghcr.io/q/workbench")

    def test_malformed_references_are_refused(self) -> None:
        for bad in ("", "   ", 7, None, "name@sha256:short", "name@", "@sha256:" + "a" * 64):
            with self.subTest(reference=bad), self.assertRaises(ProvenanceError):
                parse_image_reference(bad)  # type: ignore[arg-type]

    def test_base_image_is_read_from_the_dockerfile(self) -> None:
        pin = resolve_base_image(f"FROM ubuntu:24.04@{DIGEST_A}\nRUN true\n")
        self.assertEqual(pin.digest, DIGEST_A)
        # A later FROM must not shadow the first base image.
        multi = f"FROM alpine:3@{DIGEST_A}\nFROM debian:12@{DIGEST_B}\n"
        self.assertEqual(resolve_base_image(multi).digest, DIGEST_A)

    def test_dockerfile_without_a_base_image_is_refused(self) -> None:
        for bad in ("", "RUN true\n", 5):
            with self.subTest(text=bad), self.assertRaises(ProvenanceError):
                resolve_base_image(bad)  # type: ignore[arg-type]

    def test_the_repository_dockerfile_is_digest_pinned(self) -> None:
        pin = resolve_base_image((ROOT / "Dockerfile").read_text(encoding="utf-8"))
        self.assertTrue(pin.pinned, "the shipped base image must be digest-pinned")


class LockRevisionTests(unittest.TestCase):
    def test_the_repository_lock_is_fully_pinned(self) -> None:
        lock = lock_revision(ROOT / "docker" / "python-constraints.txt")
        self.assertGreaterEqual(lock["package_count"], 1)
        self.assertEqual(len(lock["sha256"]), 64)
        for package in lock["packages"]:
            self.assertNotIn(">", package["version"])
            self.assertNotIn("*", package["version"])

    def test_an_unpinned_or_duplicate_or_empty_lock_is_refused(self) -> None:
        import tempfile

        cases = {
            "range": "pydantic>=2.8,<3\n",
            "duplicate": "pydantic==2.13.5\nPydantic==2.13.5\n",
            "empty": "# only a comment\n",
            "blank": "",
        }
        for label, text in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "lock.txt"
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ProvenanceError):
                    lock_revision(path)

    def test_a_missing_lock_is_refused(self) -> None:
        with self.assertRaises(ProvenanceError):
            lock_revision(ROOT / "does-not-exist.txt")


class VerificationTests(unittest.TestCase):
    def test_a_complete_signed_manifest_is_promotable(self) -> None:
        document = manifest()
        self.assertEqual(verify_manifest(document), ())
        self.assertEqual(promotion_reasons(document), ())
        self.assertTrue(document["release_eligible"])
        self.assertEqual(document["schema_version"], MANIFEST_SCHEMA_VERSION)

    def test_the_build_time_manifest_is_provenance_complete_but_not_promotable(self) -> None:
        """Attestation runs after the manifest is written, so unsigned is honest."""
        document = manifest(attestation={"status": "unsigned"})
        self.assertEqual(verify_manifest(document), ())
        self.assertTrue(document["release_eligible"])
        self.assertIn("signed", " ".join(promotion_reasons(document)))

    def test_missing_provenance_fields_are_named_individually(self) -> None:
        for field in ("source_commit", "workflow_ref", "base_image", "lock", "sbom", "image"):
            with self.subTest(field=field):
                document = manifest()
                document[field] = None
                reasons = verify_manifest(document)
                self.assertTrue(any(field in reason for reason in reasons), reasons)

    def test_a_mutable_tag_cannot_substitute_for_an_image_digest(self) -> None:
        document = manifest(image=image_payload(registry_digest=None))
        reasons = verify_manifest(document)
        self.assertTrue(any("registry_digest" in reason for reason in reasons), reasons)
        self.assertFalse(document["release_eligible"])

    def test_an_unpinned_base_image_is_refused_even_with_other_provenance(self) -> None:
        document = manifest(base_image=parse_image_reference("ubuntu:24.04"))
        self.assertTrue(any("base_image" in reason for reason in verify_manifest(document)))

    def test_a_short_or_non_v_version_is_refused(self) -> None:
        for version in ("0.3.0", "", "release-1", None):
            with self.subTest(version=version):
                document = manifest(version=version)
                self.assertTrue(any("version" in reason for reason in verify_manifest(document)))

    def test_a_short_commit_or_unknown_schema_is_refused(self) -> None:
        short = manifest()
        short["source_commit"] = "abc123"
        self.assertTrue(any("commit" in reason for reason in verify_manifest(short)))

        stale = manifest()
        stale["schema_version"] = 1
        self.assertTrue(any("schema_version" in reason for reason in verify_manifest(stale)))

    def test_non_object_manifests_and_bad_attestations_are_refused(self) -> None:
        for bad in ("text", 5, None, []):
            with self.subTest(value=bad):
                self.assertNotEqual(verify_manifest(bad), ())  # type: ignore[arg-type]
        document = manifest()
        document["attestation"] = {"status": "maybe"}
        self.assertTrue(any("attestation" in reason for reason in verify_manifest(document)))

    def test_verification_is_deterministic(self) -> None:
        document = manifest(image=image_payload(registry_digest=None))
        self.assertEqual(verify_manifest(document), verify_manifest(document))


class CliTests(unittest.TestCase):
    def test_verify_exits_nonzero_on_a_refused_manifest(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            document = manifest(image=image_payload(registry_digest=None))
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(main(["--verify", str(path)]), 1)
            self.assertEqual(main(["--verify", str(path), "--require-promotion"]), 1)

    def test_verify_exits_zero_on_a_provenance_complete_manifest(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest()), encoding="utf-8")
            self.assertEqual(main(["--verify", str(path)]), 0)
            self.assertEqual(main(["--verify", str(path), "--require-promotion"]), 0)

    def test_an_unpromotable_unsigned_manifest_still_passes_plain_verification(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest(attestation={"status": "unsigned"})), encoding="utf-8")
            self.assertEqual(main(["--verify", str(path)]), 0)
            self.assertEqual(main(["--verify", str(path), "--require-promotion"]), 1)

    def test_verify_refuses_an_unreadable_or_non_object_manifest(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaises(ProvenanceError):
                main(["--verify", str(missing)])
            not_object = Path(directory) / "list.json"
            not_object.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(ProvenanceError):
                main(["--verify", str(not_object)])

    def test_dry_run_writes_nothing_and_needs_no_docker(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            sbom = Path(directory) / "workbench-1.spdx.json"
            sbom.write_text("{}", encoding="utf-8")
            output = Path(directory) / "manifest.json"
            code = main(
                [
                    "--image",
                    "workbench-1:release-candidate",
                    "--sbom",
                    str(sbom),
                    "--version",
                    "v0.3.0",
                    "--dry-run",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(code, 0)
            self.assertFalse(output.exists())

    def test_a_missing_sbom_fails_before_any_manifest_is_written(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "manifest.json"
            with self.assertRaises(ProvenanceError):
                main(
                    [
                        "--image",
                        "workbench-1:release-candidate",
                        "--sbom",
                        str(Path(directory) / "absent.spdx.json"),
                        "--version",
                        "v0.3.0",
                        "--output",
                        str(output),
                    ]
                )
            self.assertFalse(output.exists(), "a failed run must not publish a partial manifest")


class GitTests(unittest.TestCase):
    def test_the_repository_head_is_a_full_commit_hash(self) -> None:
        self.assertRegex(git_commit(), r"^[0-9a-f]{40}$")


class AttestationRecordingTests(unittest.TestCase):
    def test_recording_signed_attestation_makes_a_manifest_promotable(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest(attestation={"status": "unsigned"})), encoding="utf-8")
            self.assertNotEqual(promotion_reasons(json.loads(path.read_text())), ())

            document = record_attestation(path, "signed")
            self.assertEqual(document["attestation"]["status"], "signed")
            self.assertEqual(promotion_reasons(document), ())
            self.assertTrue(document["release_eligible"])
            # The recorded outcome is persisted, not just returned.
            self.assertEqual(json.loads(path.read_text())["attestation"]["status"], "signed")

    def test_recording_preserves_every_other_provenance_field(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            original = manifest(attestation={"status": "unsigned"})
            path.write_text(json.dumps(original), encoding="utf-8")
            updated = record_attestation(path, "verified")
            for field in ("source_commit", "workflow_ref", "base_image", "lock", "sbom", "image", "version"):
                with self.subTest(field=field):
                    self.assertEqual(updated[field], original[field])

    def test_an_unknown_attestation_status_is_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest()), encoding="utf-8")
            for bad in ("", "trusted", "SIGNED"):
                with self.subTest(status=bad), self.assertRaises(ProvenanceError):
                    record_attestation(path, bad)

    def test_recording_does_not_hide_a_missing_provenance_field(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            document = manifest()
            document["image"] = image_payload(registry_digest=None)
            path.write_text(json.dumps(document), encoding="utf-8")
            updated = record_attestation(path, "signed")
            self.assertFalse(updated["release_eligible"])
            self.assertTrue(any("registry_digest" in reason for reason in verify_manifest(updated)))


class WorkflowGateTests(unittest.TestCase):
    """The workflow must verify provenance before publishing evidence."""

    def _workflow(self) -> str:
        return (ROOT / ".github" / "workflows" / "release-image.yml").read_text(encoding="utf-8")

    def test_the_manifest_step_records_the_workflow_ref(self) -> None:
        assert "--workflow-ref" in self._workflow()

    def test_provenance_is_verified_before_the_image_is_attested(self) -> None:
        workflow = self._workflow()
        verify_at = workflow.index("--verify release-manifest.json")
        attest_at = workflow.index("actions/attest-build-provenance@")
        self.assertLess(verify_at, attest_at, "provenance must be verified before attestation")

    def test_promotion_readiness_is_checked_before_the_provenance_artifact(self) -> None:
        workflow = self._workflow()
        promotion_at = workflow.index("--require-promotion")
        upload_at = workflow.index("workbench-1-release-provenance")
        self.assertLess(promotion_at, upload_at, "promotion must be gated before the artifact is published")

    def test_the_provenance_artifact_is_retained_even_when_the_job_fails(self) -> None:
        workflow = self._workflow()
        block = workflow[workflow.index("workbench-1-release-provenance") - 400 :]
        self.assertIn("if: always()", block)


if __name__ == "__main__":
    unittest.main()
