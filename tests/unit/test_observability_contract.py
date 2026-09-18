"""Issue #76: the correlated, bounded, failure-aware observability contract.

These tests exercise the contract directly rather than through the backend, so
the three properties the issue asks for are each pinned to a single assertion:
a line is joinable to its run and retries, a payload is bounded and reports what
it dropped, and each distinct failure is counted under its own class.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "libs" / "application"), str(ROOT / "libs" / "contracts"), str(ROOT / "libs" / "task_utils")]

from workbench.application.observability import (
    MAX_DETAIL_KEYS,
    MAX_LIST_ITEMS,
    MAX_RECORD_BYTES,
    MAX_STRING_BYTES,
    OBSERVABILITY_SCHEMA_VERSION,
    TRUNCATION_MARKER_KEY,
    CorrelationFields,
    FailureClass,
    FailureCounter,
    JsonlSink,
    ObservabilityError,
    bounded_payload,
    classify_failure,
    failure_projection,
    observation_failures,
    trace_fields,
)
from workbench_backend.logging import StructuredLogger


class CorrelationFieldTests(unittest.TestCase):
    def test_a_complete_envelope_survives_round_trip(self) -> None:
        fields = CorrelationFields(
            run_id="run-1",
            component="agent_runtime",
            action_id="action-1",
            attempt_id="attempt-2",
            monotonic_ns=123,
        )
        self.assertEqual(
            fields.as_dict(),
            {
                "run_id": "run-1",
                "component": "agent_runtime",
                "schema_version": OBSERVABILITY_SCHEMA_VERSION,
                "action_id": "action-1",
                "attempt_id": "attempt-2",
                "monotonic_ns": 123,
            },
        )
        self.assertTrue(fields.has_trace)

    def test_missing_correlation_is_refused_rather_than_defaulted(self) -> None:
        with self.assertRaises(ObservabilityError):
            CorrelationFields(run_id="", component="backend")
        with self.assertRaises(ObservabilityError):
            CorrelationFields(run_id="run-1", component="backend", schema_version="observability-v0")

    def test_a_retry_without_an_action_cannot_be_written(self) -> None:
        with self.assertRaisesRegex(ObservabilityError, "attempt_id requires action_id"):
            CorrelationFields(run_id="run-1", component="backend", attempt_id="attempt-1")


class StructuredLoggingTests(unittest.TestCase):
    """The logger integration, selected by the issue's `-k logging` command."""

    def test_logger_lines_carry_run_component_and_schema(self) -> None:
        stream = io.StringIO()
        record = StructuredLogger("workbench-backend", stream).emit("step", "ok", run_id="run-9")
        self.assertEqual(record["run_id"], "run-9")
        self.assertEqual(record["component"], "workbench-backend")
        self.assertEqual(record["schema_version"], OBSERVABILITY_SCHEMA_VERSION)
        self.assertIsInstance(record["monotonic_ns"], int)
        self.assertEqual(json.loads(stream.getvalue().splitlines()[0])["schema_version"], OBSERVABILITY_SCHEMA_VERSION)

    def test_logging_refuses_an_attempt_with_no_action(self) -> None:
        logger = StructuredLogger("workbench-backend", io.StringIO())
        with self.assertRaisesRegex(ObservabilityError, "attempt_id requires action_id"):
            logger.emit("retry", "retrying", run_id="run-1", attempt_id="attempt-1")

    def test_logging_a_traced_record_keeps_the_attempt_joinable(self) -> None:
        stream = io.StringIO()
        record = StructuredLogger("workbench-backend", stream).emit(
            "retry",
            "retrying after timeout",
            run_id="run-1",
            action_id="action-1",
            attempt_id="attempt-2",
        )
        written = json.loads(stream.getvalue().splitlines()[0])
        self.assertEqual(
            (written["run_id"], written["action_id"], written["attempt_id"]), ("run-1", "action-1", "attempt-2")
        )
        self.assertEqual(written["component"], record["component"])

    def test_logging_still_scrubs_a_credential_in_the_envelope(self) -> None:
        from workbench.application.redaction import REDACTED

        stream = io.StringIO()
        record = StructuredLogger("workbench-backend", stream).emit(
            "step", "ok", run_id="run-1", details={"api_key": "exposed-value"}
        )
        self.assertEqual(record["details"]["api_key"], REDACTED)
        self.assertNotIn("exposed-value", stream.getvalue())


class FailureClassificationTests(unittest.TestCase):
    def test_each_distinct_failure_gets_its_own_class(self) -> None:
        cases = {
            FailureClass.TIMEOUT: {"outcome": "timeout"},
            FailureClass.TRANSPORT_LOSS: {"dispatch_state": "send_failed"},
            FailureClass.TRANSPORT_LOSS: {"fault_code": "link_lost"},
            FailureClass.REJECTION: {"device_state": "rejected"},
            FailureClass.REJECTION: {"fault_code": "stop_rejected"},
            FailureClass.STALE_EVIDENCE: {"reason_code": "stale_observation"},
            FailureClass.VERIFICATION_FAILURE: {"verification_status": "refuted"},
            FailureClass.VERIFICATION_FAILURE: {"outcome": "failed"},
        }
        for expected, arguments in cases.items():
            with self.subTest(expected=expected, arguments=arguments):
                self.assertEqual(classify_failure(**arguments), expected)

    def test_a_transport_loss_outranks_the_timeout_it_causes(self) -> None:
        self.assertEqual(classify_failure(outcome="timeout", fault_code="link_lost"), FailureClass.TRANSPORT_LOSS)
        self.assertEqual(classify_failure(dispatch_state="send_failed", outcome="timeout"), FailureClass.TRANSPORT_LOSS)

    def test_a_stale_observation_outranks_the_verification_it_fails(self) -> None:
        self.assertEqual(
            classify_failure(reason_code="stale_observation", verification_status="refuted"),
            FailureClass.STALE_EVIDENCE,
        )

    def test_success_and_deliberate_stops_are_not_failures(self) -> None:
        self.assertIsNone(classify_failure(outcome="completed", verification_status="confirmed"))
        self.assertIsNone(classify_failure(outcome="canceled"))
        self.assertIsNone(classify_failure(outcome="safe_stop", device_state="stopped"))
        self.assertIsNone(classify_failure(fault_code="none"))

    def test_an_unknown_contract_value_raises_instead_of_counting_as_success(self) -> None:
        with self.assertRaises(ObservabilityError):
            classify_failure(outcome="exploded")
        with self.assertRaises(ObservabilityError):
            classify_failure(fault_code="not_a_fault")

    def test_the_counter_separates_classes_and_components(self) -> None:
        counter = FailureCounter()
        counter.record(FailureClass.TIMEOUT, component="motion")
        counter.record(FailureClass.TIMEOUT, component="motion")
        counter.record(FailureClass.TIMEOUT, component="perception")
        self.assertEqual(counter.count(FailureClass.TIMEOUT, component="motion"), 2)
        self.assertEqual(counter.count(FailureClass.TIMEOUT, component="perception"), 1)
        self.assertEqual(counter.classes(), ("timeout",))

    def test_the_counter_is_bounded(self) -> None:
        counter = FailureCounter(max_series=1)
        counter.record(FailureClass.TIMEOUT, component="motion")
        with self.assertRaisesRegex(ObservabilityError, "series limit"):
            counter.record(FailureClass.TIMEOUT, component="perception")

    def test_a_series_is_attributable_to_its_run_and_timestamp(self) -> None:
        counter = FailureCounter()
        counter.record(FailureClass.TIMEOUT, component="motion", run_id="run-1", monotonic_ns=10)
        counter.record(FailureClass.TIMEOUT, component="motion", run_id="run-1", monotonic_ns=20)
        counter.record(FailureClass.TIMEOUT, component="motion", run_id="run-2", monotonic_ns=30)
        self.assertEqual(counter.count(FailureClass.TIMEOUT, component="motion", run_id="run-1"), 2)
        self.assertEqual(counter.count(FailureClass.TIMEOUT, component="motion", run_id="run-2"), 1)
        series = {(item.run_id, item.count, item.last_monotonic_ns) for item in counter.series()}
        self.assertEqual(series, {("run-1", 2, 20), ("run-2", 1, 30)})
        # The per-component total aggregates runs without losing the attribution.
        self.assertEqual(counter.counts(), (("timeout", "motion", 3),))

    def test_a_series_timestamp_never_regresses(self) -> None:
        counter = FailureCounter()
        counter.record(FailureClass.TIMEOUT, component="motion", run_id="run-1", monotonic_ns=50)
        counter.record(FailureClass.TIMEOUT, component="motion", run_id="run-1", monotonic_ns=10)
        self.assertEqual(counter.series()[0].last_monotonic_ns, 50)
        self.assertEqual(counter.series()[0].count, 2)

    def test_a_run_id_must_still_be_a_valid_identifier(self) -> None:
        with self.assertRaises(ObservabilityError):
            FailureCounter().record(FailureClass.TIMEOUT, component="motion", run_id="")

    def test_the_counter_document_names_the_schema_and_every_class(self) -> None:
        counter = FailureCounter()
        counter.record(FailureClass.TRANSPORT_LOSS, component="motion", run_id="run-1")
        document = counter.as_document()
        self.assertEqual(document["schema_version"], OBSERVABILITY_SCHEMA_VERSION)
        self.assertEqual(len(document["failure_classes"]), 5)
        self.assertEqual(document["series"][0]["failure"], "transport_loss")
        self.assertEqual(document["series"][0]["run_id"], "run-1")

    def test_a_failure_projection_carries_the_correlation_envelope(self) -> None:
        record = failure_projection(
            FailureClass.TIMEOUT,
            component="motion",
            run_id="run-1",
            action_id="action-1",
            attempt_id="attempt-2",
            monotonic_ns=99,
        )
        self.assertEqual(record["failure"], "timeout")
        self.assertEqual(record["value"], 1)
        self.assertEqual(record["schema_version"], OBSERVABILITY_SCHEMA_VERSION)
        self.assertEqual(record["action_id"], "action-1")
        self.assertEqual(record["attempt_id"], "attempt-2")
        self.assertEqual(record["monotonic_ns"], 99)
        self.assertNotIn("action_id", failure_projection(FailureClass.TIMEOUT, component="motion"))

    def test_a_failure_projection_refuses_an_unknown_class(self) -> None:
        with self.assertRaises(ObservabilityError):
            failure_projection("unknown_failure", component="motion")

    def test_counting_a_stream_recomputes_rather_than_trusts(self) -> None:
        events = [
            {"component": "motion", "outcome": "timeout"},
            {"component": "motion", "fault_code": "link_lost"},
            {"component": "motion", "outcome": "completed"},
            {"component": "perception", "reason_code": "stale_observation"},
        ]
        self.assertEqual(
            observation_failures(events),
            (("stale_evidence", 1), ("timeout", 1), ("transport_loss", 1)),
        )
        with self.assertRaises(ObservabilityError):
            observation_failures([{"outcome": "timeout"}])


class BoundedPayloadTests(unittest.TestCase):
    def test_an_oversized_key_set_is_cut_and_reported(self) -> None:
        payload = {f"key{index:03d}": index for index in range(MAX_DETAIL_KEYS + 5)}
        projected, report = bounded_payload(payload)
        self.assertEqual(len(projected), MAX_DETAIL_KEYS)
        self.assertTrue(report.omitted)
        self.assertEqual(len(report.omitted_paths), 5)

    def test_an_oversized_list_is_cut_and_reported(self) -> None:
        projected, report = bounded_payload({"items": list(range(MAX_LIST_ITEMS + 3))})
        self.assertEqual(len(projected["items"]), MAX_LIST_ITEMS)
        self.assertEqual(report.truncated_paths, ("items",))

    def test_an_over_long_string_is_cut_and_reported(self) -> None:
        projected, report = bounded_payload({"message": "x" * (MAX_STRING_BYTES + 10)})
        self.assertEqual(len(projected["message"]), MAX_STRING_BYTES)
        self.assertEqual(report.truncated_paths, ("message",))

    def test_deep_nesting_is_cut_and_reported_rather_than_recursed(self) -> None:
        nested: dict[str, object] = {}
        cursor = nested
        for _ in range(10):
            child: dict[str, object] = {}
            cursor["next"] = child
            cursor = child
        projected, report = bounded_payload(nested)
        self.assertTrue(report.omitted)
        self.assertLessEqual(len(json.dumps(projected)), MAX_RECORD_BYTES)

    def test_a_payload_that_cannot_be_bounded_raises(self) -> None:
        with self.assertRaisesRegex(ObservabilityError, "not finite JSON"):
            bounded_payload({"value": float("nan")})
        with self.assertRaisesRegex(ObservabilityError, "not finite JSON"):
            bounded_payload({"value": {1, 2, 3}})

    def test_a_truncated_log_line_reports_the_omission_in_band(self) -> None:
        stream = io.StringIO()
        details = {f"key{index:03d}": index for index in range(MAX_DETAIL_KEYS + 2)}
        record = StructuredLogger("workbench-backend", stream).emit("step", "ok", details=details)
        self.assertTrue(record[TRUNCATION_MARKER_KEY]["omitted_paths"])
        written = json.loads(stream.getvalue().splitlines()[0])
        self.assertIn(TRUNCATION_MARKER_KEY, written)
        self.assertLessEqual(len(stream.getvalue().encode()), MAX_RECORD_BYTES * 2)


class TraceJoinTests(unittest.TestCase):
    def test_a_record_joins_its_retries_frames_evidence_and_verdict(self) -> None:
        record = _correlation_record(
            attempts=[("frame-command", 0), ("frame-command", 1), ("frame-ack", 0)],
            verification_status="confirmed",
        )
        join = trace_fields(record)
        self.assertEqual(join.run_id, "run-1")
        self.assertEqual(join.action_id, "action-1")
        self.assertEqual(join.attempts, 3)
        self.assertEqual(join.retries, 1)
        # One frame id per attempt: a repeated id is a real duplicate-frame
        # observation, so deduplicating here would hide it.
        self.assertEqual(len(join.frame_ids), join.attempts)
        self.assertEqual(join.frame_ids, ("frame-ack", "frame-command", "frame-command"))
        self.assertEqual(join.evidence_refs, ("camera-frame-1", "result-1"))
        self.assertEqual(join.verification_id, "verification-1")
        self.assertEqual(join.verification_status, "confirmed")
        self.assertTrue(join.verified)

    def test_a_fault_is_carried_into_the_trace(self) -> None:
        join = trace_fields(_correlation_record(faults=["link_lost", "ack_timeout"]))
        self.assertEqual(join.fault_codes, ("ack_timeout", "link_lost"))
        self.assertFalse(join.verified)

    def test_a_record_without_verification_is_reported_as_unverified_not_confirmed(self) -> None:
        join = trace_fields(_correlation_record())
        self.assertIsNone(join.verification_status)
        self.assertFalse(join.verified)

    def test_a_record_missing_a_correlation_field_cannot_be_joined(self) -> None:
        with self.assertRaisesRegex(ObservabilityError, "missing 'run_id'"):
            trace_fields(_correlation_record(drop="run_id"))


class JsonlSinkTests(unittest.TestCase):
    def test_rotation_keeps_only_complete_parseable_lines(self) -> None:
        with TemporaryDirectory() as directory:
            sink = JsonlSink(Path(directory) / "telemetry.jsonl", max_bytes=1024, max_files=3)
            for index in range(400):
                sink.write({"component": "backend", "sequence": index, "message": "y" * 40})
            files = sorted(Path(directory).glob("telemetry.jsonl.*"))
            self.assertLessEqual(len([item for item in files if item.suffix != ".lock"]), 3)
            records = sink.read_all()
            sequences = [record["sequence"] for record in records]
            self.assertEqual(sequences, sorted(set(sequences)))
            self.assertLess(len(records), 400)

    def test_concurrent_writers_do_not_lose_or_interleave_a_line(self) -> None:
        with TemporaryDirectory() as directory:
            sink = JsonlSink(Path(directory) / "concurrent.jsonl")

            def worker(worker_id: int) -> None:
                for index in range(25):
                    sink.write({"component": "backend", "worker": worker_id, "index": index})

            threads = [threading.Thread(target=worker, args=(worker_id,)) for worker_id in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            records = sink.read_all()
            self.assertEqual(len(records), 150)
            self.assertEqual({(record["worker"], record["index"]) for record in records}, set(_expected_pairs()))

    def test_a_malformed_line_is_refused_rather_than_skipped(self) -> None:
        with TemporaryDirectory() as directory:
            sink = JsonlSink(Path(directory) / "broken.jsonl")
            sink.write({"component": "backend"})
            sink.path.write_bytes(sink.path.read_bytes() + b"{not json}\n")
            with self.assertRaisesRegex(ObservabilityError, "not a parseable record"):
                sink.read_all()

    def test_the_sink_redacts_even_when_the_caller_forgot_to(self) -> None:
        """The sink is the last point where text becomes a file, so it must scrub.

        A caller that passes a raw credential has already made the mistake; the
        write is the only place left that can still stop the leak.
        """
        from workbench.application.redaction import REDACTED

        with TemporaryDirectory() as directory:
            sink = JsonlSink(Path(directory) / "evidence.jsonl")
            written = sink.write({"component": "backend", "api_key": "exposed-value", "nested": {"token": "t"}})
            self.assertEqual(written["api_key"], REDACTED)
            self.assertEqual(sink.read_all()[0]["api_key"], REDACTED)
            self.assertEqual(sink.read_all()[0]["nested"]["token"], REDACTED)

    def test_the_sink_keeps_correlation_fields_verbatim(self) -> None:
        with TemporaryDirectory() as directory:
            sink = JsonlSink(Path(directory) / "correlated.jsonl")
            written = sink.write(
                {
                    "run_id": "run-1",
                    "component": "backend",
                    "action_id": "action-1",
                    "schema_version": OBSERVABILITY_SCHEMA_VERSION,
                    "sequence_no": 4,
                }
            )
            for field in ("run_id", "component", "action_id", "schema_version", "sequence_no"):
                self.assertEqual(sink.read_all()[0][field], written[field])
            self.assertNotIn("redaction", written)

    def test_raw_prompts_and_camera_frames_become_references_not_content(self) -> None:
        """Evidence by reference: the artifact keeps the pointer, not the bytes."""
        from workbench.application.redaction import EVIDENCE_REDACTED

        with TemporaryDirectory() as directory:
            sink = JsonlSink(Path(directory) / "evidence.jsonl")
            sink.write(
                {
                    "component": "perception",
                    "prompt": "raw operator instruction",
                    "frames": [b"\x89PNG raw camera bytes"],
                    "camera_id": "camera-0",
                    "snapshot_sha256": "b" * 64,
                    "evidence_refs": ["camera-frame-0007"],
                }
            )
            written = sink.read_all()[0]
            self.assertEqual(written["prompt"], EVIDENCE_REDACTED)
            self.assertEqual(written["frames"], EVIDENCE_REDACTED)
            # The reference and the hash survive; only the content is withheld.
            self.assertEqual(written["camera_id"], "camera-0")
            self.assertEqual(written["snapshot_sha256"], "b" * 64)
            self.assertEqual(written["evidence_refs"], ["camera-frame-0007"])

    def test_a_bounded_sink_record_reports_the_omission_in_the_file(self) -> None:
        """A dropped key must be visible in the artifact, not silently absent."""
        with TemporaryDirectory() as directory:
            sink = JsonlSink(Path(directory) / "bounded.jsonl")
            details = {f"key{index:03d}": index for index in range(MAX_DETAIL_KEYS + 4)}
            sink.write({"component": "backend", "details": details})
            written = sink.read_all()[0]
            self.assertIn(TRUNCATION_MARKER_KEY, written)
            self.assertEqual(len(written[TRUNCATION_MARKER_KEY]["omitted_paths"]), 4)
            self.assertEqual(len(written["details"]), MAX_DETAIL_KEYS)

    def test_a_sink_record_that_cannot_be_written_raises_instead_of_truncating(self) -> None:
        with TemporaryDirectory() as directory:
            sink = JsonlSink(Path(directory) / "bad.jsonl")
            with self.assertRaisesRegex(ObservabilityError, "not finite JSON"):
                sink.write({"component": "backend", "value": float("inf")})
            self.assertFalse(sink.path.exists())


def _expected_pairs() -> list[tuple[int, int]]:
    return [(worker, index) for worker in range(6) for index in range(25)]


class _Attempt:
    def __init__(self, frame_id: str, retry_count: int | None) -> None:
        self.frame_id = frame_id
        self.retry_count = retry_count


class _Fault:
    def __init__(self, fault_code: str) -> None:
        self.fault_code = fault_code


class _Verification:
    def __init__(self, verification_id: str, status: str) -> None:
        self.verification_id = verification_id
        self.status = status


class _Record:
    def __init__(self, **overrides: object) -> None:
        self.correlation_ref = "correlation:run-1:action-1"
        self.run_id = "run-1"
        self.action_id = "action-1"
        self.transport_attempts: tuple[_Attempt, ...] = ()
        self.execution_evidence_refs: tuple[str, ...] = ()
        self.verification_evidence_refs: tuple[str, ...] = ()
        self.faults: tuple[_Fault, ...] = ()
        self.verification: _Verification | None = None
        for name, value in overrides.items():
            if value is None:
                delattr(self, name)
            else:
                setattr(self, name, value)


def _correlation_record(
    *,
    attempts: list[tuple[str, int | None]] | None = None,
    faults: list[str] | None = None,
    verification_status: str | None = None,
    drop: str | None = None,
) -> _Record:
    record = _Record()
    record.transport_attempts = tuple(_Attempt(frame_id, retry) for frame_id, retry in attempts or [])
    record.execution_evidence_refs = ("result-1",)
    record.verification_evidence_refs = ("camera-frame-1",)
    record.faults = tuple(_Fault(code) for code in faults or [])
    if verification_status is not None:
        record.verification = _Verification("verification-1", verification_status)
    if drop is not None:
        delattr(record, drop)
    return record


if __name__ == "__main__":
    unittest.main()
