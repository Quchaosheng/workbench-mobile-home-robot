"""Replayable, time-bounded runtime contract for model workflow selection."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Iterable

try:
    from .model_contract import (
        ModelTransportError,
        ModelWorkflowPlan,
        request_chat_completion,
    )
except ImportError:
    from model_contract import (  # type: ignore
        ModelTransportError,
        ModelWorkflowPlan,
        request_chat_completion,
    )


MODEL_OUTPUT_VERSION = 'workflow-plan/v1'
RECORDING_SCHEMA_VERSION = 'model-runtime-record/v1'
FAULT_MODES = {
    'none',
    'timeout',
    'duplicate_response',
    'service_crash',
    'stale_output',
    'fallback_storm',
}


class ModelRuntimeErrorCode:
    BACKEND_FAILURE = 'backend_failure'
    DUPLICATE_RESPONSE = 'duplicate_response'
    FALLBACK_STORM = 'fallback_storm'
    INVALID_PLAN = 'invalid_plan'
    REPLAY_MISS = 'replay_miss'
    SERVICE_CRASH = 'service_crash'
    STALE_OUTPUT = 'stale_output'
    TIMEOUT = 'timeout'
    TRANSPORT_FAILURE = 'transport_failure'


@dataclass(frozen=True)
class ModelRequestContext:
    request_id: str
    observation_timestamp_ns: int
    created_timestamp_ns: int
    deadline_ns: int
    request_fingerprint: str
    output_version: str = MODEL_OUTPUT_VERSION

    def expired(self, now_ns: int | None = None) -> bool:
        current_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
        return current_ns > self.deadline_ns

    def public_dict(self) -> dict[str, object]:
        return {
            'request_id': self.request_id,
            'observation_timestamp_ns': self.observation_timestamp_ns,
            'created_timestamp_ns': self.created_timestamp_ns,
            'deadline_ns': self.deadline_ns,
            'request_fingerprint': self.request_fingerprint,
            'output_version': self.output_version,
        }


@dataclass(frozen=True)
class BackendResult:
    backend: str
    content: str | None
    latency_ns: int
    error_code: str = ''
    replayed: bool = False
    provider_response_id: str = ''

    @property
    def succeeded(self) -> bool:
        return self.content is not None and not self.error_code

    def public_dict(self) -> dict[str, object]:
        return {
            'backend': self.backend,
            'latency_ns': max(int(self.latency_ns), 0),
            'error_code': self.error_code,
            'replayed': self.replayed,
            'provider_response_id': self.provider_response_id,
        }


def make_request_context(
    *,
    request_id: str,
    request: str,
    observation_timestamp_ns: int,
    observation_ttl_ms: int,
    inference_deadline_ms: int,
    output_version: str = MODEL_OUTPUT_VERSION,
    now_ns: int | None = None,
) -> ModelRequestContext:
    if not request_id or not request:
        raise ValueError('request_id and request are required')
    if observation_ttl_ms <= 0 or inference_deadline_ms <= 0:
        raise ValueError('observation TTL and inference deadline must be positive')
    if not output_version:
        raise ValueError('output_version is required')
    created_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
    observed_ns = int(observation_timestamp_ns) or created_ns
    observation_deadline_ns = observed_ns + int(observation_ttl_ms) * 1_000_000
    inference_deadline_ns = created_ns + int(inference_deadline_ms) * 1_000_000
    return ModelRequestContext(
        request_id=request_id,
        observation_timestamp_ns=observed_ns,
        created_timestamp_ns=created_ns,
        deadline_ns=min(observation_deadline_ns, inference_deadline_ns),
        request_fingerprint=hashlib.sha256(request.encode('utf-8')).hexdigest(),
        output_version=output_version,
    )


class ModelBackend(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the stable backend identifier used in evidence records."""

    @abstractmethod
    def invoke(self, context: ModelRequestContext, request: str) -> BackendResult:
        """Return one bounded result without raising provider exceptions."""


class OpenAICompatibleBackend(ModelBackend):

    def __init__(
        self,
        *,
        endpoint: str,
        model_name: str,
        api_key: str,
        workflows: Iterable[str],
        targets: Iterable[str],
        max_duration_ms: int,
        timeout_sec: float,
        api_style: str,
    ) -> None:
        self._endpoint = endpoint
        self._model_name = model_name
        self._api_key = api_key
        self._workflows = tuple(workflows)
        self._targets = tuple(targets)
        self._max_duration_ms = int(max_duration_ms)
        self._timeout_sec = float(timeout_sec)
        self._api_style = api_style

    @property
    def name(self) -> str:
        return 'openai_compatible'

    def invoke(self, context: ModelRequestContext, request: str) -> BackendResult:
        started_ns = time.monotonic_ns()
        if context.expired(started_ns):
            return BackendResult(self.name, None, 0, ModelRuntimeErrorCode.STALE_OUTPUT)
        try:
            content = request_chat_completion(
                self._endpoint,
                self._model_name,
                self._api_key,
                request,
                self._workflows,
                self._targets,
                self._max_duration_ms,
                self._timeout_sec,
                self._api_style,
            )
        except ModelTransportError as error:
            message = str(error).lower()
            error_code = (
                ModelRuntimeErrorCode.TIMEOUT
                if 'timed out' in message or 'timeout' in message
                else ModelRuntimeErrorCode.TRANSPORT_FAILURE
            )
            return BackendResult(
                self.name, None, time.monotonic_ns() - started_ns, error_code
            )
        except Exception:
            return BackendResult(
                self.name,
                None,
                time.monotonic_ns() - started_ns,
                ModelRuntimeErrorCode.BACKEND_FAILURE,
            )
        return BackendResult(self.name, content, time.monotonic_ns() - started_ns)


class MockBackend(ModelBackend):

    def __init__(self, content: str) -> None:
        self._content = content

    @property
    def name(self) -> str:
        return 'mock'

    def invoke(self, context: ModelRequestContext, request: str) -> BackendResult:
        del context, request
        return BackendResult(self.name, self._content, 0)


class ReplayBackend(ModelBackend):

    def __init__(self, path: str | Path) -> None:
        self._records = read_records(path)

    @property
    def name(self) -> str:
        return 'replay'

    def invoke(self, context: ModelRequestContext, request: str) -> BackendResult:
        del request
        matches = [
            row
            for row in self._records
            if row['request']['request_fingerprint'] == context.request_fingerprint
            and row['request']['output_version'] == context.output_version
            and row['status'] == 'accepted'
            and isinstance(row.get('plan'), dict)
        ]
        if len(matches) != 1:
            return BackendResult(
                self.name, None, 0, ModelRuntimeErrorCode.REPLAY_MISS, replayed=True
            )
        return BackendResult(
            self.name,
            json.dumps(matches[0]['plan'], sort_keys=True, separators=(',', ':')),
            int(matches[0]['result']['latency_ns']),
            replayed=True,
        )


class FaultInjectingBackend(ModelBackend):

    def __init__(self, backend: ModelBackend, mode: str) -> None:
        if mode not in FAULT_MODES:
            raise ValueError(f'unsupported model fault mode: {mode}')
        self._backend = backend
        self._mode = mode

    @property
    def name(self) -> str:
        return self._backend.name

    def invoke(self, context: ModelRequestContext, request: str) -> BackendResult:
        errors = {
            'timeout': ModelRuntimeErrorCode.TIMEOUT,
            'duplicate_response': ModelRuntimeErrorCode.DUPLICATE_RESPONSE,
            'service_crash': ModelRuntimeErrorCode.SERVICE_CRASH,
            'stale_output': ModelRuntimeErrorCode.STALE_OUTPUT,
            'fallback_storm': ModelRuntimeErrorCode.FALLBACK_STORM,
        }
        if self._mode in errors:
            return BackendResult(self.name, None, 0, errors[self._mode])
        return self._backend.invoke(context, request)


class ModelAdmission:

    def __init__(
        self,
        *,
        dedup_window_ms: int,
        failure_window_ms: int,
        max_failures: int,
        max_future_skew_ms: int,
    ) -> None:
        if (
            dedup_window_ms <= 0
            or failure_window_ms <= 0
            or max_failures <= 0
            or max_future_skew_ms < 0
        ):
            raise ValueError('invalid model admission configuration')
        self._dedup_window_ns = int(dedup_window_ms) * 1_000_000
        self._failure_window_ns = int(failure_window_ms) * 1_000_000
        self._max_failures = int(max_failures)
        self._max_future_skew_ns = int(max_future_skew_ms) * 1_000_000
        self._admitted_until_ns: dict[str, int] = {}
        self._failures_ns: deque[int] = deque()

    def admit(self, context: ModelRequestContext, now_ns: int) -> str:
        self._admitted_until_ns = {
            request_id: expiry_ns
            for request_id, expiry_ns in self._admitted_until_ns.items()
            if expiry_ns > now_ns
        }
        if context.observation_timestamp_ns > now_ns + self._max_future_skew_ns:
            return 'observation_timestamp_future'
        if context.expired(now_ns):
            return 'observation_expired'
        if context.request_id in self._admitted_until_ns:
            return 'duplicate_request'
        self._admitted_until_ns[context.request_id] = (
            max(context.deadline_ns, now_ns) + self._dedup_window_ns
        )
        return ''

    @staticmethod
    def output_allowed(context: ModelRequestContext, now_ns: int) -> str:
        return 'model_output_expired' if context.expired(now_ns) else ''

    def note_backend_failure(self, now_ns: int) -> bool:
        cutoff_ns = now_ns - self._failure_window_ns
        while self._failures_ns and self._failures_ns[0] <= cutoff_ns:
            self._failures_ns.popleft()
        self._failures_ns.append(now_ns)
        return len(self._failures_ns) >= self._max_failures


class ModelRecorder:

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def record(
        self,
        context: ModelRequestContext,
        result: BackendResult,
        *,
        status: str,
        reason_code: str,
        plan: ModelWorkflowPlan | None,
    ) -> dict[str, object]:
        if status not in {'accepted', 'rejected'}:
            raise ValueError('invalid model record status')
        plan_value = None
        if plan is not None:
            plan_value = {
                'workflow_id': plan.workflow_id,
                'target_id': plan.target_id,
                'duration_ms': plan.duration_ms,
            }
        row: dict[str, object] = {
            'schema_version': RECORDING_SCHEMA_VERSION,
            'request': context.public_dict(),
            'result': result.public_dict(),
            'status': status,
            'reason_code': reason_code,
            'plan': plan_value,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n')
        return row


def read_records(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        source.read_text(encoding='utf-8').splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f'invalid model recording JSON at line {line_number}'
            ) from error
        if (
            not isinstance(row, dict)
            or row.get('schema_version') != RECORDING_SCHEMA_VERSION
            or not isinstance(row.get('request'), dict)
            or not isinstance(row.get('result'), dict)
        ):
            raise ValueError(f'unsupported model recording at line {line_number}')
        request = row['request']
        result = row['result']
        latency_ns = result.get('latency_ns')
        if (
            not isinstance(request.get('request_fingerprint'), str)
            or not request['request_fingerprint']
            or not isinstance(request.get('output_version'), str)
            or not request['output_version']
            or not isinstance(latency_ns, int)
            or isinstance(latency_ns, bool)
            or latency_ns < 0
            or row.get('status') not in {'accepted', 'rejected'}
        ):
            raise ValueError(f'invalid model recording fields at line {line_number}')
        records.append(row)
    return records


@dataclass
class ModelRuntimeMetrics:
    request_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    backend_failure_count: int = 0
    degraded_count: int = 0
    task_success_count: int = 0
    task_failure_count: int = 0
    latency_ns: list[int] = field(default_factory=list)
    reason_counts: dict[str, int] = field(default_factory=dict)

    def begin_request(self) -> None:
        self.request_count += 1

    def note_backend(self, result: BackendResult, *, degraded: bool = False) -> None:
        self.latency_ns.append(max(int(result.latency_ns), 0))
        self.backend_failure_count += int(not result.succeeded)
        self.degraded_count += int(degraded)

    def note_acceptance(self) -> None:
        self.accepted_count += 1

    def note_rejection(self, reason_code: str) -> None:
        self.rejected_count += 1
        self.reason_counts[reason_code] = self.reason_counts.get(reason_code, 0) + 1

    def note_task(self, succeeded: bool) -> None:
        if succeeded:
            self.task_success_count += 1
        else:
            self.task_failure_count += 1

    def snapshot(self) -> dict[str, object]:
        task_count = self.task_success_count + self.task_failure_count
        return {
            'schema_version': 'model-runtime-metrics/v1',
            'request_count': self.request_count,
            'accepted_count': self.accepted_count,
            'rejected_count': self.rejected_count,
            'backend_failure_count': self.backend_failure_count,
            'degraded_count': self.degraded_count,
            'rejection_rate': _divide(self.rejected_count, self.request_count),
            'degradation_rate': _divide(self.degraded_count, self.request_count),
            'task_success_rate': _divide(self.task_success_count, task_count),
            'model_latency_ms': {
                'p50': _percentile_ms(self.latency_ns, 0.50),
                'p95': _percentile_ms(self.latency_ns, 0.95),
                'p99': _percentile_ms(self.latency_ns, 0.99),
            },
            'reason_counts': dict(sorted(self.reason_counts.items())),
        }

    def write(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.snapshot(), indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )


def _divide(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _percentile_ms(values_ns: list[int], percentile: float) -> float | None:
    if not values_ns:
        return None
    if not math.isfinite(percentile) or not 0 <= percentile <= 1:
        raise ValueError('invalid percentile')
    values = sorted(values_ns)
    if len(values) == 1:
        return values[0] / 1_000_000
    position = percentile * (len(values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    value = values[lower] + (values[upper] - values[lower]) * (position - lower)
    return value / 1_000_000
