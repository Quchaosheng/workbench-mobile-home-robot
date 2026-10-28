"""Issue #436: a `signed` claim has to be backed by a verified subject digest."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/scripts")]

from attestation_verification import VerificationError, attested_digests, build_record, main

DIGEST = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64


def bundles(digest: str = DIGEST) -> list:
    return [
        {
            "verificationResult": {
                "statement": {
                    "subject": [{"name": "ghcr.io/q/workbench", "digest": {"sha256": digest.split(":", 1)[1]}}]
                }
            }
        }
    ]


class AttestedDigestTests(unittest.TestCase):
    def test_a_bundle_names_its_subject_digest(self) -> None:
        self.assertEqual(attested_digests(bundles()), [DIGEST])

    def test_a_bundle_without_a_subject_names_nothing(self) -> None:
        self.assertEqual(attested_digests([{"verificationResult": {}}]), [])

    def test_a_non_list_is_refused(self) -> None:
        with self.assertRaises(VerificationError):
            attested_digests({"not": "a list"})


class BuildRecordTests(unittest.TestCase):
    def test_a_matching_digest_produces_a_verified_record(self) -> None:
        record = build_record(bundles(), repository="q/workbench", digest=DIGEST)
        self.assertEqual(record["result"], "verified")
        self.assertIn(DIGEST, record["reference"])
        self.assertIn("gh attestation verify", record["verifier"])
        self.assertEqual(record["attested_digests"], [DIGEST])

    def test_an_absent_attestation_is_refused(self) -> None:
        with self.assertRaisesRegex(VerificationError, "no attestation bundle names a subject digest"):
            build_record([], repository="q/workbench", digest=DIGEST)

    def test_a_mismatched_subject_digest_is_refused(self) -> None:
        with self.assertRaisesRegex(VerificationError, "not the published"):
            build_record(bundles(OTHER), repository="q/workbench", digest=DIGEST)

    def test_a_digest_that_is_not_sha256_is_refused(self) -> None:
        with self.assertRaisesRegex(VerificationError, "must be a sha256 digest"):
            build_record(bundles(), repository="q/workbench", digest="md5:abc")

    def test_a_repository_without_an_owner_is_refused(self) -> None:
        with self.assertRaisesRegex(VerificationError, "must be owner/name"):
            build_record(bundles(), repository="workbench", digest=DIGEST)


class CliTests(unittest.TestCase):
    def _run(self, payload: object, digest: str = DIGEST) -> tuple[int, dict | None]:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bundles.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            output = Path(directory) / "verification.json"
            code = main(
                [
                    "--bundles",
                    str(source),
                    "--repository",
                    "q/workbench",
                    "--digest",
                    digest,
                    "--output",
                    str(output),
                ]
            )
            written = json.loads(output.read_text(encoding="utf-8")) if output.is_file() else None
            return code, written

    def test_a_valid_bundle_writes_the_record(self) -> None:
        code, written = self._run(bundles())
        self.assertEqual(code, 0)
        assert written is not None
        self.assertEqual(written["result"], "verified")

    def test_a_wrong_digest_exits_non_zero_and_writes_nothing(self) -> None:
        code, written = self._run(bundles(OTHER))
        self.assertEqual(code, 1)
        self.assertIsNone(written)

    def test_malformed_json_exits_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bundles.json"
            source.write_text("{not json", encoding="utf-8")
            output = Path(directory) / "verification.json"
            code = main(
                [
                    "--bundles",
                    str(source),
                    "--repository",
                    "q/workbench",
                    "--digest",
                    DIGEST,
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(code, 1)
            self.assertFalse(output.exists())
