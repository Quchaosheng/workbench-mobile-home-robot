import json
from pathlib import Path
import tempfile
import unittest

from ai_task_adapter.model_contract import parse_model_plan
from ai_task_adapter.model_runtime import (
    BackendResult,
    FaultInjectingBackend,
    make_request_context,
    MockBackend,
    ModelAdmission,
    ModelRecorder,
    ModelRuntimeErrorCode,
    ModelRuntimeMetrics,
    ReplayBackend,
)

REQUEST = 'Go to dock_a.'
RESPONSE = '{"workflow_id":"single_task","target_id":"dock_a","duration_ms":1000}'


def context(request_id='request-1'):
    return make_request_context(
        request_id=request_id,
        request=REQUEST,
        observation_timestamp_ns=1_000_000_000,
        observation_ttl_ms=500,
        inference_deadline_ms=250,
        now_ns=1_010_000_000,
    )


class ModelRuntimeTest(unittest.TestCase):

    def test_context_has_bounded_deadline_and_no_raw_request(self):
        value = context()
        self.assertEqual(value.deadline_ns, 1_260_000_000)
        self.assertFalse(value.expired(1_259_999_999))
        self.assertTrue(value.expired(1_260_000_001))
        self.assertNotIn(REQUEST, json.dumps(value.public_dict()))

    def test_recording_replays_only_one_normalized_plan(self):
        request_context = context()
        result = MockBackend(RESPONSE).invoke(request_context, REQUEST)
        plan = parse_model_plan(RESPONSE, ['single_task'], ['dock_a'], 5000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.jsonl'
            ModelRecorder(path).record(
                request_context,
                result,
                status='accepted',
                reason_code='',
                plan=plan,
            )
            raw = path.read_text(encoding='utf-8')
            self.assertNotIn(REQUEST, raw)
            replay = ReplayBackend(path).invoke(request_context, REQUEST)
        self.assertTrue(replay.succeeded)
        self.assertTrue(replay.replayed)
        self.assertEqual(json.loads(replay.content), json.loads(RESPONSE))

    def test_duplicate_recording_match_fails_closed(self):
        request_context = context()
        result = BackendResult('mock', RESPONSE, 10)
        plan = parse_model_plan(RESPONSE, ['single_task'], ['dock_a'], 5000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.jsonl'
            recorder = ModelRecorder(path)
            for _ in range(2):
                recorder.record(
                    request_context,
                    result,
                    status='accepted',
                    reason_code='',
                    plan=plan,
                )
            replay = ReplayBackend(path).invoke(request_context, REQUEST)
        self.assertFalse(replay.succeeded)
        self.assertEqual(replay.error_code, ModelRuntimeErrorCode.REPLAY_MISS)

    def test_malformed_recording_is_rejected_during_load(self):
        malformed = {
            'schema_version': 'model-runtime-record/v1',
            'request': {'output_version': 'workflow-plan/v1'},
            'result': {'latency_ns': 10},
            'status': 'accepted',
            'plan': json.loads(RESPONSE),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.jsonl'
            path.write_text(json.dumps(malformed) + '\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'invalid model recording fields'):
                ReplayBackend(path)

    def test_fault_modes_never_return_a_command(self):
        expected = {
            'timeout': ModelRuntimeErrorCode.TIMEOUT,
            'duplicate_response': ModelRuntimeErrorCode.DUPLICATE_RESPONSE,
            'service_crash': ModelRuntimeErrorCode.SERVICE_CRASH,
            'stale_output': ModelRuntimeErrorCode.STALE_OUTPUT,
            'fallback_storm': ModelRuntimeErrorCode.FALLBACK_STORM,
        }
        for mode, error_code in expected.items():
            with self.subTest(mode=mode):
                result = FaultInjectingBackend(MockBackend(RESPONSE), mode).invoke(
                    context(), REQUEST
                )
                self.assertFalse(result.succeeded)
                self.assertEqual(result.error_code, error_code)

    def test_admission_rejects_duplicate_stale_future_and_failure_storm(self):
        admission = ModelAdmission(
            dedup_window_ms=100,
            failure_window_ms=50,
            max_failures=3,
            max_future_skew_ms=10,
        )
        request_context = context()
        self.assertEqual(admission.admit(request_context, 1_010_000_000), '')
        self.assertEqual(
            admission.admit(request_context, 1_011_000_000), 'duplicate_request'
        )
        self.assertEqual(
            admission.output_allowed(request_context, 1_270_000_000),
            'model_output_expired',
        )
        future = make_request_context(
            request_id='future',
            request=REQUEST,
            observation_timestamp_ns=2_000_000_000,
            observation_ttl_ms=500,
            inference_deadline_ms=250,
            now_ns=1_000_000_000,
        )
        self.assertEqual(
            admission.admit(future, 1_000_000_000), 'observation_timestamp_future'
        )
        self.assertFalse(admission.note_backend_failure(3_000_000_000))
        self.assertFalse(admission.note_backend_failure(3_010_000_000))
        self.assertTrue(admission.note_backend_failure(3_020_000_000))

    def test_metrics_report_latency_rejection_degradation_and_task_success(self):
        metrics = ModelRuntimeMetrics()
        metrics.begin_request()
        metrics.note_backend(BackendResult('mock', RESPONSE, 1_000_000))
        metrics.note_acceptance()
        metrics.note_task(True)
        metrics.begin_request()
        metrics.note_backend(
            BackendResult('openai_compatible', None, 3_000_000, 'timeout'),
            degraded=True,
        )
        metrics.note_rejection('timeout')
        report = metrics.snapshot()
        self.assertEqual(report['rejection_rate'], 0.5)
        self.assertEqual(report['degradation_rate'], 0.5)
        self.assertEqual(report['task_success_rate'], 1.0)
        self.assertEqual(report['model_latency_ms']['p50'], 2.0)
        self.assertEqual(report['reason_counts'], {'timeout': 1})

    def test_node_wires_explicit_backends_without_implicit_mock_fallback(self):
        node = Path(__file__).resolve().parents[1] / 'scripts/ai_model_adapter_node.py'
        source = node.read_text(encoding='utf-8')
        for parameter in (
            'observation_ttl_ms',
            'inference_deadline_ms',
            'model_output_version',
            'model_record_path',
            'model_replay_path',
            'model_metrics_path',
            'model_fault_mode',
        ):
            self.assertIn(parameter, source)
        self.assertIn("self._mode == 'mock'", source)
        self.assertIn("self._mode == 'replay'", source)
        self.assertNotIn('fallback_to_mock', source)
        self.assertNotIn('MockBackend(', source.split('def _build_backend', 1)[0])


if __name__ == '__main__':
    unittest.main()
