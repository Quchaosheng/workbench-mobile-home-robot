"""Issue #92: a credential must not survive into an artifact.

The tests are written against the leak path, not against the regexes: each one
asserts that a secret is absent from the bytes a log line, a report or a record
would actually contain.  A rule that stops matching therefore fails here instead
of silently shipping a secret to a CI artifact.
"""

import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "application"))
sys.path.insert(0, str(ROOT / "services" / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "scripts"))

from run_evaluation import (
    EvaluationInputError,
    ExternalRunnerTimeout,
    redacted_summary,
    run_external,
)
from workbench.application.redaction import (
    CREDENTIAL_FORMAT_PATTERNS,
    EVIDENCE_REDACTED,
    REDACTED,
    REDACTION_MARKER_KEY,
    REDACTION_RULES_VERSION,
    redact_exception,
    redact_mapping,
    redact_text,
)
from workbench_backend.logging import StructuredLogger

# Distinctive, obviously synthetic credentials.  Each value is assembled from
# fragments so the repository stores no commit-blocking literal, and each is
# asserted absent from the scrubbed output, so a rule that stops matching still
# fails the suite.  The assertion below keeps this file honest: no line here may
# match the redactor's own format rules.
GITHUB_TOKEN = "ghp_" + "AAAABBBBCCCCDDDDEEEEFFFF111122223333"
FINE_GRAINED_TOKEN = "github" + "_pat_" + "11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCD"
MODEL_KEY = "sk" + "-live-" + "abcdefghijklmnopqrstuvwxyz"
SLACK_TOKEN = "xox" + "b-" + "1234567890-abcdefghijklmnop"
AWS_KEY = "AK" + "IA" + "IOSFODNN7EXAMPLE"
JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJvcGVyYXRvciJ9" + "." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PASSWORD = "sup3r" + "-secret-" + "passphrase"
PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
    "-----END OPENSSH PRIVATE KEY-----"
)

SECRETS = {
    "github": GITHUB_TOKEN,
    "github_pat": FINE_GRAINED_TOKEN,
    "model": MODEL_KEY,
    "slack": SLACK_TOKEN,
    "aws": AWS_KEY,
    "jwt": JWT,
    "password": PASSWORD,
    "private_key": "b3BlbnNzaC1rZXktdjEAAAAA",
}


class RedactionRuleTests(unittest.TestCase):
    def test_rules_are_versioned_and_exported(self) -> None:
        self.assertRegex(REDACTION_RULES_VERSION, r"^redaction-rules-v\d+$")
        self.assertEqual(REDACTION_MARKER_KEY, "redaction")
        self.assertNotEqual(REDACTED, EVIDENCE_REDACTED)

    def test_this_file_holds_no_literal_that_matches_a_format_rule(self) -> None:
        """A fixture that looks like a live credential is a commit-blocking finding.

        Push protection scans committed bytes, so the synthetic values above are
        assembled from fragments.  Refusing here - using the redactor's own rule
        table - is what stops a future edit from reintroducing a blocking literal.
        """
        source = Path(__file__).read_text(encoding="utf-8")
        for index, pattern in enumerate(CREDENTIAL_FORMAT_PATTERNS):
            with self.subTest(rule=index):
                self.assertIsNone(pattern.search(source), f"rule {index} matches a literal in this file")

    def test_format_rule_table_covers_every_skill_credential_shape(self) -> None:
        for secret in (GITHUB_TOKEN, FINE_GRAINED_TOKEN, MODEL_KEY, SLACK_TOKEN, AWS_KEY, JWT):
            with self.subTest(secret_prefix=secret[:4]):
                self.assertNotEqual(redact_text(f"value {secret}"), f"value {secret}")
                self.assertNotIn(secret, redact_text(f"value {secret}"))
        self.assertIn(REDACTED, redact_text(f"value {PRIVATE_KEY}"))
        self.assertNotIn("b3BlbnNzaC1rZXktdjEAAAAA", redact_text(f"value {PRIVATE_KEY}"))

    def test_known_secret_formats_are_scrubbed_from_free_text(self) -> None:
        cases = {
            "github token": f"clone failed for {GITHUB_TOKEN}",
            "fine-grained token": f"pat {FINE_GRAINED_TOKEN} rejected",
            "model key": f"provider returned 401 for {MODEL_KEY}",
            "slack token": f"notify failed with {SLACK_TOKEN}",
            "aws key": f"credentials {AWS_KEY} are expired",
            "jwt": f"Authorization used {JWT}",
            "private key": f"captured {PRIVATE_KEY}",
            "bearer header": "Authorization: Bearer abcdefghijklmnopqrst",
            "cookie header": "Cookie: session=deadbeefcafebabe; Path=/",
            "url userinfo": f"fetch https://operator:{PASSWORD}@example.test/repo",
            "query parameter": f"GET https://api.example.test/v1?access_token={PASSWORD}&page=2",
            "form parameter": f"body password={PASSWORD}&user=operator",
            "cli flag": f"runner --api-key {MODEL_KEY} --scenario normal-001",
            "environment assignment": f"GITHUB_TOKEN={GITHUB_TOKEN}",
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                scrubbed = redact_text(text)
                self.assertIn(REDACTED, scrubbed)
                for secret in SECRETS.values():
                    self.assertNotIn(secret, scrubbed)

    def test_ordinary_engineering_text_is_left_alone(self) -> None:
        for text in (
            "stage planning completed in 12.5ms",
            "run_id=run-001 sequence_no=4",
            "robot moved to in:pickup_shelf",
            "credentials at C:/Users/operator/.aws/credentials",
            "keyboard shortcut and keyframe count",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), text)

    def test_arbitrary_credential_keys_are_scrubbed_by_name(self) -> None:
        payload = {
            "run_id": "run-1",
            "sequence_no": 3,
            "details": {
                "event": "model_call",
                "X-Api-Key": MODEL_KEY,
                "client_secret": PASSWORD,
                "db_password": PASSWORD,
                "auth": {"github_token": GITHUB_TOKEN},
                "registry_credentials": {"user": "operator", "password": PASSWORD},
                "prompt_sha256": "a" * 64,
                "input_tokens": 32,
                "output_tokens": 12,
                "token_count": 44,
            },
        }
        scrubbed, findings = redact_mapping(payload)
        rendered = json.dumps(scrubbed, sort_keys=True)
        for secret in SECRETS.values():
            self.assertNotIn(secret, rendered)
        # Five credential-named keys plus the whole ``registry_credentials``
        # object, whose scalar contents are untrusted by association.
        self.assertEqual(findings, 6)
        details = scrubbed["details"]
        self.assertEqual(details["X-Api-Key"], REDACTED)
        self.assertEqual(details["auth"]["github_token"], REDACTED)
        self.assertEqual(details["registry_credentials"]["password"], REDACTED)
        self.assertEqual(details["registry_credentials"]["user"], REDACTED)
        # A hash is the safe substitute for a prompt, and a count is not a secret.
        self.assertEqual(details["prompt_sha256"], "a" * 64)
        self.assertEqual(details["input_tokens"], 32)
        self.assertEqual(details["output_tokens"], 12)
        self.assertEqual(details["token_count"], 44)

    def test_redaction_preserves_correlation_fields_and_shape(self) -> None:
        record = {
            "timestamp": "2026-09-17T00:00:00+00:00",
            "level": "INFO",
            "service": "scripted-pipeline",
            "source": "simulation",
            "run_id": "dry-run-001",
            "sequence_no": 7,
            "event": "stage_completed",
            "message": f"uploaded with {GITHUB_TOKEN}",
            "details": {"stage": "planning", "duration_ms": 12.5, "list": [f"x {MODEL_KEY}"]},
        }
        scrubbed, findings = redact_mapping(record)
        self.assertEqual(findings, 2)
        for field in ("run_id", "sequence_no", "timestamp", "event", "service", "source", "level"):
            self.assertEqual(scrubbed[field], record[field])
        self.assertEqual(scrubbed["details"]["stage"], "planning")
        self.assertEqual(scrubbed["details"]["duration_ms"], 12.5)
        self.assertNotIn(MODEL_KEY, json.dumps(scrubbed))

    def test_raw_evidence_is_replaced_by_a_reference_marker(self) -> None:
        payload = {
            "evidence_refs": ["camera-frame-0007"],
            "frames": [b"\x89PNG raw bytes"],
            "recording": {"bytes": b"raw"},
            "camera_id": "camera-0",
            "snapshot_sha256": "b" * 64,
        }
        scrubbed, findings = redact_mapping(payload)
        self.assertEqual(findings, 2)
        self.assertEqual(scrubbed["frames"], EVIDENCE_REDACTED)
        self.assertEqual(scrubbed["recording"], EVIDENCE_REDACTED)
        # The reference and the hash survive; only the content is withheld.
        self.assertEqual(scrubbed["evidence_refs"], ["camera-frame-0007"])
        self.assertEqual(scrubbed["camera_id"], "camera-0")
        self.assertEqual(scrubbed["snapshot_sha256"], "b" * 64)

    def test_exception_text_is_scrubbed(self) -> None:
        error = RuntimeError(f"runner failed with {GITHUB_TOKEN}: {MODEL_KEY}")
        rendered = redact_exception(error)
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn(GITHUB_TOKEN, rendered)
        self.assertNotIn(MODEL_KEY, rendered)


class StructuredLogTests(unittest.TestCase):
    def _records(self, logger: StructuredLogger, stream: io.StringIO) -> list:
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    def test_logged_secret_is_scrubbed_and_the_line_stays_valid_json(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger("test-service", stream)
        logger.emit(
            "model_call",
            f"sent {GITHUB_TOKEN}",
            run_id="run-1",
            details={"endpoint": f"https://operator:{PASSWORD}@example.test", "api_key": MODEL_KEY},
        )
        records = self._records(logger, stream)
        self.assertEqual(len(records), 1)
        record = records[0]
        for secret in SECRETS.values():
            self.assertNotIn(secret, stream.getvalue())
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(record["sequence_no"], 0)
        self.assertEqual(record["details"]["api_key"], REDACTED)
        self.assertEqual(record[REDACTION_MARKER_KEY], {"rules": REDACTION_RULES_VERSION, "redacted_values": 3})

    def test_clean_log_line_gains_no_marker_and_keeps_its_sequence(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger("test-service", stream)
        first = logger.emit("stage_completed", "planning done", run_id="run-2", details={"duration_ms": 1.5})
        second = logger.emit("stage_completed", "dispatch done", run_id="run-2", details={"duration_ms": 2.5})
        self.assertNotIn(REDACTION_MARKER_KEY, first)
        self.assertEqual([first["sequence_no"], second["sequence_no"]], [0, 1])
        self.assertEqual(self._records(logger, stream)[1]["details"]["duration_ms"], 2.5)

    def test_emit_failure_never_formats_the_exception_at_the_call_site(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger("test-service", stream)
        try:
            raise ValueError(f"bad credential {MODEL_KEY}")
        except ValueError as exc:
            record = logger.emit_failure("read_model_error", exc, run_id="run-3")
        self.assertEqual(record["level"], "ERROR")
        self.assertIn("ValueError", record["message"])
        self.assertNotIn(MODEL_KEY, stream.getvalue())
        self.assertNotIn(MODEL_KEY, json.dumps(record))

    def test_backend_access_log_keeps_the_client_field(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger("workbench-backend", stream)
        logger.emit(
            "http_access",
            f'GET /api/v1/runs?token={GITHUB_TOKEN} HTTP/1.1" 200 -',
            details={"client": "127.0.0.1"},
        )
        record = self._records(logger, stream)[0]
        self.assertEqual(record["details"]["client"], "127.0.0.1")
        self.assertNotIn(GITHUB_TOKEN, stream.getvalue())


class InjectedFailureReportTests(unittest.TestCase):
    """A secret injected into a failing runner must not reach the report."""

    def _manifest(self, directory: Path) -> Path:
        path = directory / "scenario.json"
        path.write_text(
            json.dumps(
                {
                    "scenario_id": "secret-probe",
                    "seed": 5,
                    "task_id": "task-place-red-block",
                    "world_version": "WorkbenchSim-v0",
                    "fault_type": "none",
                    "timeout_s": 1,
                    "oracle_allowed": False,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_failing_runner_stderr_is_scrubbed_from_the_exception(self) -> None:
        directory = Path(tempfile.mkdtemp())
        runner = directory / "failing.py"
        runner.write_text(
            textwrap.dedent(
                f"""
                import sys
                sys.stderr.write("auth failed for {GITHUB_TOKEN}\\n")
                sys.exit(3)
                """
            ),
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeError) as caught:
            run_external(
                f"{sys.executable} {runner} --api-key {MODEL_KEY}",
                self._manifest(directory),
                directory / "out.jsonl",
                1005,
                "v-test",
                timeout_s=5,
                scenario_id="secret-probe",
            )
        message = str(caught.exception)
        self.assertNotIn(GITHUB_TOKEN, message)
        self.assertNotIn(MODEL_KEY, message)
        self.assertIn(REDACTED, message)

    def test_timeout_message_scrubs_the_runner_command(self) -> None:
        directory = Path(tempfile.mkdtemp())
        runner = directory / "sleeper.py"
        runner.write_text("import time; time.sleep(600)", encoding="utf-8")
        with self.assertRaises(ExternalRunnerTimeout) as caught:
            run_external(
                f"{sys.executable} {runner} --access-token {GITHUB_TOKEN}",
                self._manifest(directory),
                directory / "out.jsonl",
                1005,
                "v-test",
                timeout_s=1,
                scenario_id="secret-probe",
            )
        self.assertNotIn(GITHUB_TOKEN, str(caught.exception))
        self.assertIn("secret-probe", str(caught.exception))

    def test_published_summary_never_contains_an_injected_secret(self) -> None:
        summary = {
            "generated_at": "2026-09-17T00:00:00+00:00",
            "commit": "abc123",
            "runner": "external",
            "release_eligible": False,
            "run_count": 1,
            "blocked_count": 1,
            "runs": [
                {
                    "run_id": "v-test--secret-probe",
                    "event_log": None,
                    "verification_status": "blocked",
                    "blocked_reason": "runner_timeout",
                    "blocked_detail": f"command: runner --token {GITHUB_TOKEN} --password {PASSWORD}",
                    "release_eligible": False,
                }
            ],
        }
        rendered = json.dumps(redacted_summary(summary), sort_keys=True)
        for secret in SECRETS.values():
            self.assertNotIn(secret, rendered)
        published = redacted_summary(summary)
        self.assertEqual(published["runs"][0]["run_id"], "v-test--secret-probe")
        self.assertEqual(published[REDACTION_MARKER_KEY]["rules"], REDACTION_RULES_VERSION)
        self.assertTrue(published["runs"][0]["blocked_detail"].startswith("command:"))

    def test_summary_scrubbing_is_a_copy_not_a_mutation(self) -> None:
        summary = {"run_id": "r", "details": {"api_key": MODEL_KEY}}
        redacted_summary(summary)
        self.assertEqual(summary["details"]["api_key"], MODEL_KEY)

    def test_invalid_scenario_timeout_is_still_rejected(self) -> None:
        directory = Path(tempfile.mkdtemp())
        manifest = self._manifest(directory)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["timeout_s"] = 0
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(EvaluationInputError):
            from run_evaluation import external_timeout_budget, load_scenario_manifests

            scenario = load_scenario_manifests([manifest])[0][1]
            external_timeout_budget(scenario)


if __name__ == "__main__":
    unittest.main()
