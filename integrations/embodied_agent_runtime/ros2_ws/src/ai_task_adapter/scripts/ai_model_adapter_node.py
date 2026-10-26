#!/usr/bin/env python3
"""Submit an allowlisted ExecuteWorkflow goal selected by an OpenAI-compatible model."""

from __future__ import annotations

import math
import os
from pathlib import Path
import sys
import time

module_dir = Path(__file__).resolve().parent
source_module_dir = module_dir.parent / 'ai_task_adapter'
sys.path.insert(0, str(source_module_dir if source_module_dir.is_dir() else module_dir))

from action_msgs.msg import GoalStatus  # noqa: E402
from model_contract import (  # noqa: E402
    ModelPlanError,
    parse_model_plan,
)
from model_runtime import (  # noqa: E402
    BackendResult,
    FaultInjectingBackend,
    make_request_context,
    MockBackend,
    ModelAdmission,
    ModelRecorder,
    ModelRuntimeErrorCode,
    ModelRuntimeMetrics,
    OpenAICompatibleBackend,
    ReplayBackend,
)

import rclpy  # noqa: E402
from rclpy.action import ActionClient  # noqa: E402
from rclpy.node import Node  # noqa: E402
from robot_task_interfaces.action import ExecuteWorkflow  # noqa: E402


class AiModelAdapterNode(Node):

    def __init__(self) -> None:
        super().__init__('ai_model_adapter')
        self._request = self.declare_parameter('request', 'Go to dock_a.').value
        self._request_id = self.declare_parameter('request_id', 'model_demo_1').value
        self._task_id = self.declare_parameter('task_id', 'model_task_1').value
        self._mode = self.declare_parameter('mode', 'disabled').value
        self._api_style = self.declare_parameter('api_style', 'chat_completions').value
        self._endpoint = self.declare_parameter('model_endpoint', '').value
        self._model_name = self.declare_parameter('model_name', '').value
        self._api_key_env = self.declare_parameter(
            'api_key_env', 'OPENAI_API_KEY'
        ).value
        self._timeout_sec = float(
            self.declare_parameter('model_timeout_sec', 10.0).value
        )
        self._observation_timestamp_ns = int(
            self.declare_parameter('observation_timestamp_ns', 0).value
        )
        self._observation_ttl_ms = int(
            self.declare_parameter('observation_ttl_ms', 3000).value
        )
        self._inference_deadline_ms = int(
            self.declare_parameter('inference_deadline_ms', 2500).value
        )
        self._output_version = self.declare_parameter(
            'model_output_version', 'workflow-plan/v1'
        ).value
        self._record_path = self.declare_parameter('model_record_path', '').value
        self._replay_path = self.declare_parameter('model_replay_path', '').value
        self._metrics_path = self.declare_parameter('model_metrics_path', '').value
        self._fault_mode = self.declare_parameter('model_fault_mode', 'none').value
        self._mock_response = self.declare_parameter(
            'mock_response',
            '{"workflow_id":"single_task","target_id":"dock_a","duration_ms":1000}',
        ).value
        self._admission = ModelAdmission(
            dedup_window_ms=int(
                self.declare_parameter('model_dedup_window_ms', 5000).value
            ),
            failure_window_ms=int(
                self.declare_parameter('model_failure_window_ms', 30000).value
            ),
            max_failures=int(
                self.declare_parameter('model_failure_storm_count', 3).value
            ),
            max_future_skew_ms=int(
                self.declare_parameter('observation_max_future_skew_ms', 100).value
            ),
        )
        self._metrics = ModelRuntimeMetrics()
        self._action_timeout_sec = float(
            self.declare_parameter('action_result_timeout_sec', 15.0).value
        )
        self._max_duration_ms = int(
            self.declare_parameter('max_duration_ms', 5000).value
        )
        self._workflows = list(
            self.declare_parameter(
                'workflow_ids', ['single_task', 'ready_then_task']
            ).value
        )
        self._targets = list(
            self.declare_parameter('targets', ['dock_a', 'home']).value
        )
        self._client = ActionClient(self, ExecuteWorkflow, 'execute_workflow')

    def run(self) -> int:
        if self._mode not in {'openai_compatible', 'mock', 'replay'}:
            self.get_logger().error(
                'mode must be openai_compatible, mock, or replay; '
                'model calls are disabled by default'
            )
            return 2
        if not self._request or not self._request_id or not self._task_id:
            self.get_logger().error(
                'request, request_id, and task_id must not be empty'
            )
            return 2
        if (
            not math.isfinite(self._timeout_sec)
            or not math.isfinite(self._action_timeout_sec)
            or self._timeout_sec <= 0
            or self._action_timeout_sec <= 0
        ):
            self.get_logger().error('positive model and action timeouts are required')
            return 2
        if self._mode == 'openai_compatible' and (
            not self._endpoint or not self._model_name
        ):
            self.get_logger().error('model_endpoint and model_name are required')
            return 2
        if self._mode == 'replay' and not self._replay_path:
            self.get_logger().error('model_replay_path is required in replay mode')
            return 2
        if self._max_duration_ms <= 0 or not self._workflows or not self._targets:
            self.get_logger().error(
                'max_duration_ms and non-empty allowlists are required'
            )
            return 2
        if self._observation_ttl_ms <= 0 or self._inference_deadline_ms <= 0:
            self.get_logger().error(
                'observation TTL and inference deadline must be positive'
            )
            return 2

        now_ns = time.monotonic_ns()
        context = make_request_context(
            request_id=self._request_id,
            request=self._request,
            observation_timestamp_ns=self._observation_timestamp_ns,
            observation_ttl_ms=self._observation_ttl_ms,
            inference_deadline_ms=self._inference_deadline_ms,
            output_version=self._output_version,
            now_ns=now_ns,
        )
        self._metrics.begin_request()
        admission_error = self._admission.admit(context, now_ns)
        if admission_error:
            self._metrics.note_rejection(admission_error)
            self.get_logger().error(f'model request rejected: {admission_error}')
            return 2

        try:
            backend = FaultInjectingBackend(self._build_backend(), self._fault_mode)
            result = backend.invoke(context, self._request)
        except (OSError, ValueError) as error:
            self._metrics.note_rejection('backend_configuration_error')
            self.get_logger().error(f'model backend rejected: {error}')
            return 2
        self._metrics.note_backend(result)
        if not result.succeeded:
            reason_code = result.error_code or ModelRuntimeErrorCode.BACKEND_FAILURE
            if self._admission.note_backend_failure(time.monotonic_ns()):
                reason_code = ModelRuntimeErrorCode.FALLBACK_STORM
            self._record(context, result, 'rejected', reason_code, None)
            self._metrics.note_rejection(reason_code)
            self.get_logger().error(f'model plan rejected: {reason_code}')
            return 2
        output_error = self._admission.output_allowed(context, time.monotonic_ns())
        if output_error:
            self._record(context, result, 'rejected', output_error, None)
            self._metrics.note_rejection(output_error)
            self.get_logger().error(f'model plan rejected: {output_error}')
            return 2
        try:
            plan = parse_model_plan(
                result.content, self._workflows, self._targets, self._max_duration_ms
            )
        except ModelPlanError as error:
            invalid_result = BackendResult(
                result.backend,
                None,
                result.latency_ns,
                ModelRuntimeErrorCode.INVALID_PLAN,
                result.replayed,
                result.provider_response_id,
            )
            self._record(
                context,
                invalid_result,
                'rejected',
                ModelRuntimeErrorCode.INVALID_PLAN,
                None,
            )
            self._metrics.note_rejection(ModelRuntimeErrorCode.INVALID_PLAN)
            self.get_logger().error(f'model plan rejected: {error}')
            return 2
        self._record(context, result, 'accepted', '', plan)
        self._metrics.note_acceptance()

        self.get_logger().info(
            'validated model plan '
            f'workflow_id={plan.workflow_id} target_id={plan.target_id} '
            f'duration_ms={plan.duration_ms}'
        )
        if not self._client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error(
                'ExecuteWorkflow server not available after 5 seconds'
            )
            self._metrics.note_task(False)
            return 1

        goal = ExecuteWorkflow.Goal()
        goal.request_id = self._request_id
        goal.task_id = self._task_id
        goal.workflow_id = plan.workflow_id
        goal.target_id = plan.target_id
        goal.allowed_duration.sec = plan.duration_ms // 1000
        goal.allowed_duration.nanosec = (plan.duration_ms % 1000) * 1_000_000
        send_future = self._client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, send_future, timeout_sec=5.0)
        if not send_future.done():
            self.get_logger().error('ExecuteWorkflow goal response timed out')
            self._metrics.note_task(False)
            return 4
        try:
            goal_handle = send_future.result()
        except Exception as error:
            self.get_logger().error(f'ExecuteWorkflow goal request failed: {error}')
            self._metrics.note_task(False)
            return 4
        if goal_handle is None or not goal_handle.accepted:
            self.get_logger().error('ExecuteWorkflow goal rejected')
            self._metrics.note_task(False)
            return 2

        result_future = goal_handle.get_result_async()
        rclpy.spin_until_future_complete(
            self, result_future, timeout_sec=self._action_timeout_sec
        )
        if not result_future.done():
            self.get_logger().error(
                'ExecuteWorkflow result timed out; requesting cancellation'
            )
            cancel_future = goal_handle.cancel_goal_async()
            rclpy.spin_until_future_complete(self, cancel_future, timeout_sec=2.0)
            self._metrics.note_task(False)
            return 4
        try:
            result = result_future.result()
        except Exception as error:
            self.get_logger().error(f'ExecuteWorkflow result failed: {error}')
            self._metrics.note_task(False)
            return 4
        if result is None:
            self.get_logger().error('ExecuteWorkflow returned no result')
            self._metrics.note_task(False)
            return 4
        self.get_logger().info(
            f'ExecuteWorkflow outcome={result.result.outcome} '
            f'error_code={result.result.error_code} message={result.result.message}'
        )
        completed = (
            result.status == GoalStatus.STATUS_SUCCEEDED
            and result.result.outcome == ExecuteWorkflow.Result.COMPLETED
        )
        self._metrics.note_task(completed)
        return 0 if completed else 4

    def _build_backend(self):
        if self._mode == 'mock':
            return MockBackend(self._mock_response)
        if self._mode == 'replay':
            return ReplayBackend(self._replay_path)
        api_key = ''
        if self._api_key_env:
            api_key = os.environ.get(self._api_key_env, '')
            if not api_key:
                raise ValueError(
                    f'required API key environment variable is empty: {self._api_key_env}'
                )
        return OpenAICompatibleBackend(
            endpoint=self._endpoint,
            model_name=self._model_name,
            api_key=api_key,
            workflows=self._workflows,
            targets=self._targets,
            max_duration_ms=self._max_duration_ms,
            timeout_sec=self._timeout_sec,
            api_style=self._api_style,
        )

    def _record(self, context, result, status, reason_code, plan) -> None:
        if self._record_path:
            ModelRecorder(self._record_path).record(
                context,
                result,
                status=status,
                reason_code=reason_code,
                plan=plan,
            )

    def flush_metrics(self) -> None:
        if self._metrics_path:
            self._metrics.write(self._metrics_path)


def main() -> int:
    rclpy.init()
    node = AiModelAdapterNode()
    try:
        return node.run()
    finally:
        node.flush_metrics()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
