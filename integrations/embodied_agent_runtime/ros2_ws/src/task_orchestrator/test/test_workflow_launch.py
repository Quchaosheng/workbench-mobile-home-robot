import os
import signal
import threading
import time
import unittest

from action_msgs.msg import GoalStatus
import launch
import launch_ros.actions
import launch_testing.actions
import launch_testing.asserts
import launch_testing.markers
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from robot_task_interfaces.action import ExecuteTask, ExecuteWorkflow
from std_msgs.msg import Bool


@launch_testing.markers.keep_alive
def generate_test_description():
    orchestrator = launch_ros.actions.Node(
        package='task_orchestrator',
        executable='task_orchestrator_node',
        parameters=[{'ready_stale_ms': 2000, 'cancel_timeout_ms': 1000}],
        output='screen',
    )
    return (
        launch.LaunchDescription(
            [orchestrator, launch_testing.actions.ReadyToTest()]
        ),
        {'orchestrator': orchestrator},
    )


class TestWorkflowOrchestrator(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.node = rclpy.create_node('workflow_orchestrator_test')
        self.server_node = rclpy.create_node('fake_execute_task_server')
        self.ready_publisher = self.node.create_publisher(Bool, '/runtime/ready', 10)
        self.client = ActionClient(self.node, ExecuteWorkflow, 'execute_workflow')
        self.mode = 'success'
        self.child_goal_count = 0
        self.child_goal_request_count = 0
        self.child_cancel_request_count = 0
        self.child_cancel_count = 0
        self.active_child_count = 0
        self.child_goal_times = []
        self.child_allowed_durations = []
        self.shutdown_goal_release = threading.Event()
        self.child_server = ActionServer(
            self.server_node,
            ExecuteTask,
            'execute_task',
            execute_callback=self._execute_child,
            cancel_callback=self._cancel_child,
            goal_callback=self._accept_child,
        )
        self.server_executor = rclpy.executors.MultiThreadedExecutor(num_threads=2)
        self.server_executor.add_node(self.server_node)
        self.server_thread = threading.Thread(target=self.server_executor.spin)
        self.server_thread.start()
        self.assertTrue(self.client.wait_for_server(timeout_sec=5.0))
        self._spin_until(lambda: self.ready_publisher.get_subscription_count() == 1)
        time.sleep(0.2)

    def tearDown(self):
        self.shutdown_goal_release.set()
        self.client.destroy()
        self.child_server.destroy()
        self.server_executor.shutdown(timeout_sec=2.0)
        self.server_thread.join(timeout=2.0)
        self.server_node.destroy_node()
        self.node.destroy_node()

    def _accept_child(self, request):
        self.child_goal_request_count += 1
        if self.mode in ('shutdown_delayed_goal', 'never_goal_response'):
            self.shutdown_goal_release.wait()
        elif self.mode in ('delayed_goal', 'deadline_delayed_goal'):
            time.sleep(1.2 if self.mode == 'delayed_goal' else 1.5)
        return GoalResponse.ACCEPT

    def _execute_child(self, goal_handle):
        self.active_child_count += 1
        try:
            return self._execute_child_impl(goal_handle)
        finally:
            self.active_child_count -= 1

    def _execute_child_impl(self, goal_handle):
        self.child_goal_count += 1
        self.child_goal_times.append(time.monotonic())
        duration = goal_handle.request.allowed_duration
        self.child_allowed_durations.append(duration.sec + duration.nanosec / 1e9)
        mode = self.mode
        if mode == 'cancel_completed':
            time.sleep(0.15)
            result = ExecuteTask.Result()
            result.outcome = ExecuteTask.Result.COMPLETED
            result.message = 'completed during cancel'
            goal_handle.succeed()
            return result
        if mode in (
            'hold', 'slow_cancel', 'cancel_safe_stop',
            'delayed_goal', 'deadline_delayed_goal', 'shutdown_delayed_goal',
            'never_goal_response',
        ):
            while not goal_handle.is_cancel_requested:
                time.sleep(0.01)
            self.child_cancel_count += 1
            result = ExecuteTask.Result()
            if mode == 'slow_cancel':
                time.sleep(1.2)
                result.outcome = ExecuteTask.Result.SAFE_STOP
                result.error_code = 204
                result.message = 'late stop result'
                goal_handle.abort()
                return result
            if mode == 'cancel_safe_stop':
                result.outcome = ExecuteTask.Result.SAFE_STOP
                result.error_code = 204
                result.message = 'safe stop during cancel'
                goal_handle.abort()
                return result
            result.outcome = ExecuteTask.Result.CANCELED
            result.message = 'child canceled'
            goal_handle.canceled()
            return result

        result = ExecuteTask.Result()
        if mode == 'spontaneous_cancel':
            result.outcome = ExecuteTask.Result.CANCELED
            result.message = 'child canceled without parent cancel'
            goal_handle.abort()
        elif mode == 'safe_stop':
            result.outcome = ExecuteTask.Result.SAFE_STOP
            result.error_code = 204
            result.message = 'stop not confirmed'
            goal_handle.abort()
        else:
            result.outcome = ExecuteTask.Result.COMPLETED
            result.message = 'completed'
            goal_handle.succeed()
        return result

    def _cancel_child(self, _):
        self.child_cancel_request_count += 1
        if self.mode == 'cancel_completed':
            return CancelResponse.REJECT
        return CancelResponse.ACCEPT

    def _spin_until(self, predicate, timeout=3.0, tick=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if tick:
                tick()
            rclpy.spin_once(self.node, timeout_sec=0.02)
            if predicate():
                return
        self.fail('condition was not reached before timeout')

    def _future(self, future, timeout=3.0, tick=None):
        self._spin_until(lambda: future.done(), timeout, tick)
        return future.result()

    def _publish_ready(self, ready):
        message = Bool()
        message.data = ready
        self.ready_publisher.publish(message)

    def _goal(self, workflow_id, task_id, duration_sec=5, duration_nanosec=0):
        goal = ExecuteWorkflow.Goal()
        goal.request_id = 'request-' + task_id
        goal.workflow_id = workflow_id
        goal.task_id = task_id
        goal.target_id = 'dock_a'
        goal.allowed_duration.sec = duration_sec
        goal.allowed_duration.nanosec = duration_nanosec
        return goal

    def _send_ready_goal(self, workflow_id, task_id):
        for _ in range(5):
            self._publish_ready(True)
            rclpy.spin_once(self.node, timeout_sec=0.05)
        return self._future(
            self.client.send_goal_async(self._goal(workflow_id, task_id)),
            tick=lambda: self._publish_ready(True),
        )

    def test_workflow_contract(self, proc_info, orchestrator):
        normal = self._send_ready_goal('single_task', 'normal')
        self.assertTrue(normal.accepted)
        wrapped = self._future(normal.get_result_async())
        self.assertEqual(
            wrapped.status,
            GoalStatus.STATUS_SUCCEEDED,
            f'{wrapped.result.outcome}/{wrapped.result.error_code}: {wrapped.result.message}',
        )
        self.assertEqual(wrapped.result.outcome, ExecuteWorkflow.Result.COMPLETED)
        self.assertEqual(self.child_goal_count, 1)

        invalid = self._future(
            self.client.send_goal_async(self._goal('../uploaded.xml', 'invalid'))
        )
        self.assertFalse(invalid.accepted)

        self._publish_ready(False)
        rclpy.spin_once(self.node, timeout_sec=0.05)
        before = self.child_goal_count
        not_ready = self._future(
            self.client.send_goal_async(self._goal('single_task', 'not-ready'))
        )
        self.assertTrue(not_ready.accepted)
        wrapped = self._future(
            not_ready.get_result_async(), tick=lambda: self._publish_ready(False)
        )
        self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
        self.assertEqual(self.child_goal_count, before)

        start = time.monotonic()
        retry_future = self.client.send_goal_async(
            self._goal('ready_then_task', 'retry-ready')
        )
        while time.monotonic() - start < 0.05:
            self._publish_ready(False)
            rclpy.spin_once(self.node, timeout_sec=0.01)
        self.assertEqual(self.child_goal_count, before)
        retry = self._future(retry_future, tick=lambda: self._publish_ready(True))
        self.assertTrue(retry.accepted)
        wrapped = self._future(
            retry.get_result_async(), tick=lambda: self._publish_ready(True)
        )
        self.assertEqual(wrapped.status, GoalStatus.STATUS_SUCCEEDED)
        self.assertEqual(self.child_goal_count, before + 1)
        self.assertGreaterEqual(self.child_goal_times[-1] - start, 0.09)
        self.assertGreater(self.child_allowed_durations[-1], 0.0)
        self.assertLess(self.child_allowed_durations[-1], 5.0)

        self._publish_ready(False)
        predispatch = self._future(
            self.client.send_goal_async(
                self._goal('ready_then_task', 'cancel-before-dispatch')
            ),
            tick=lambda: self._publish_ready(False),
        )
        self.assertTrue(predispatch.accepted)
        canceled = self._future(predispatch.cancel_goal_async())
        self.assertEqual(len(canceled.goals_canceling), 1)
        wrapped = self._future(
            predispatch.get_result_async(), tick=lambda: self._publish_ready(False)
        )
        self.assertEqual(wrapped.status, GoalStatus.STATUS_CANCELED)
        self.assertEqual(wrapped.result.outcome, ExecuteWorkflow.Result.CANCELED)
        self.assertEqual(wrapped.result.error_code, 0)
        self.assertEqual(self.child_goal_count, before + 1)

        deadline_ready = self._future(
            self.client.send_goal_async(
                self._goal(
                    'ready_then_task', 'deadline-ready',
                    duration_sec=0, duration_nanosec=50_000_000,
                )
            ),
            tick=lambda: self._publish_ready(False),
        )
        wrapped = self._future(
            deadline_ready.get_result_async(), tick=lambda: self._publish_ready(False)
        )
        self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
        self.assertEqual(wrapped.result.outcome, ExecuteWorkflow.Result.SAFE_STOP)
        self.assertEqual(wrapped.result.error_code, 209)
        self.assertEqual(self.child_goal_count, before + 1)

        for task_id, duration_sec, duration_nanosec in (
            ('negative-duration', -1, 0),
            ('invalid-nanosec', 1, 1_000_000_000),
            ('zero-duration', 0, 0),
        ):
            invalid_duration = self._future(
                self.client.send_goal_async(
                    self._goal(
                        'single_task', task_id,
                        duration_sec=duration_sec,
                        duration_nanosec=duration_nanosec,
                    )
                )
            )
            self.assertFalse(invalid_duration.accepted)

        self.mode = 'delayed_goal'
        requests_before = self.child_goal_request_count
        late_cancel = self._send_ready_goal('single_task', 'late-cancel')
        self._spin_until(
            lambda: self.child_goal_request_count == requests_before + 1,
        )
        cancels_before = self.child_cancel_count
        cancel_requests_before = self.child_cancel_request_count
        self._future(late_cancel.cancel_goal_async())
        wrapped = self._future(late_cancel.get_result_async(), timeout=3.0)
        self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
        self.assertEqual(wrapped.result.outcome, ExecuteWorkflow.Result.SAFE_STOP)
        self.assertEqual(wrapped.result.error_code, 207)
        self._spin_until(lambda: self.child_cancel_count == cancels_before + 1)
        time.sleep(0.2)
        self.assertEqual(self.child_cancel_count, cancels_before + 1)
        self.assertEqual(
            self.child_cancel_request_count, cancel_requests_before + 1
        )
        self.assertEqual(self.active_child_count, 0)

        self.mode = 'deadline_delayed_goal'
        deadline_late = self._future(
            self.client.send_goal_async(
                self._goal(
                    'single_task', 'deadline-late-goal',
                    duration_sec=0, duration_nanosec=200_000_000,
                )
            ),
            tick=lambda: self._publish_ready(True),
        )
        cancels_before = self.child_cancel_count
        cancel_requests_before = self.child_cancel_request_count
        wrapped = self._future(deadline_late.get_result_async(), timeout=3.0)
        self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
        self.assertEqual(wrapped.result.outcome, ExecuteWorkflow.Result.SAFE_STOP)
        self.assertEqual(wrapped.result.error_code, 209)
        self._spin_until(lambda: self.child_cancel_count == cancels_before + 1)
        time.sleep(0.2)
        self.assertEqual(
            self.child_cancel_request_count, cancel_requests_before + 1
        )
        self.assertEqual(self.active_child_count, 0)

        self.mode = 'never_goal_response'
        requests_before = self.child_goal_request_count
        never_response = self._send_ready_goal('single_task', 'never-goal-response')
        self._spin_until(lambda: self.child_goal_request_count == requests_before + 1)
        cancels_before = self.child_cancel_count
        self._future(never_response.cancel_goal_async())
        wrapped = self._future(never_response.get_result_async(), timeout=3.0)
        self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
        self.assertEqual(wrapped.result.outcome, ExecuteWorkflow.Result.SAFE_STOP)
        self.assertEqual(wrapped.result.error_code, 207)
        self.shutdown_goal_release.set()
        self._spin_until(lambda: self.child_cancel_count >= cancels_before + 1, timeout=3.0)
        self._spin_until(lambda: self.active_child_count == 0, timeout=2.0)
        self.shutdown_goal_release.clear()

        before = self.child_goal_count - 1
        self.mode = 'spontaneous_cancel'
        spontaneous = self._send_ready_goal('single_task', 'spontaneous-cancel')
        wrapped = self._future(spontaneous.get_result_async())
        self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
        self.assertEqual(wrapped.result.error_code, 208)

        self.mode = 'cancel_completed'
        cancel_completed = self._send_ready_goal('single_task', 'cancel-completed')
        self._spin_until(lambda: self.child_goal_count == before + 3)
        self._future(cancel_completed.cancel_goal_async())
        wrapped = self._future(cancel_completed.get_result_async())
        self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
        self.assertEqual(wrapped.result.error_code, 208)

        self.mode = 'cancel_safe_stop'
        cancel_safe_stop = self._send_ready_goal('single_task', 'cancel-safe-stop')
        self._spin_until(lambda: self.child_goal_count == before + 4)
        self._future(cancel_safe_stop.cancel_goal_async())
        wrapped = self._future(cancel_safe_stop.get_result_async())
        self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
        self.assertEqual(wrapped.result.outcome, ExecuteWorkflow.Result.SAFE_STOP)
        self.assertEqual(wrapped.result.error_code, 204)

        cancels_before = self.child_cancel_count
        self.mode = 'hold'
        active = self._send_ready_goal('single_task', 'cancel')
        self._spin_until(lambda: self.child_goal_count == before + 5)
        concurrent = self._future(
            self.client.send_goal_async(self._goal('single_task', 'concurrent'))
        )
        self.assertFalse(concurrent.accepted)
        cancel = self._future(active.cancel_goal_async())
        self.assertEqual(len(cancel.goals_canceling), 1)
        wrapped = self._future(active.get_result_async(), timeout=4.0)
        self.assertEqual(self.child_cancel_count, cancels_before + 1)
        self.assertEqual(wrapped.status, GoalStatus.STATUS_CANCELED)
        self.assertEqual(wrapped.result.outcome, ExecuteWorkflow.Result.CANCELED)

        self.mode = 'slow_cancel'
        timed_out = self._send_ready_goal('single_task', 'cancel-timeout')
        self._spin_until(lambda: self.child_goal_count == before + 6)
        self._future(timed_out.cancel_goal_async())
        wrapped = self._future(timed_out.get_result_async(), timeout=3.0)
        self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
        self.assertEqual(wrapped.result.outcome, ExecuteWorkflow.Result.SAFE_STOP)
        time.sleep(0.3)

        self.mode = 'safe_stop'
        safe_stop = self._send_ready_goal('single_task', 'safe-stop')
        wrapped = self._future(safe_stop.get_result_async())
        self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
        self.assertEqual(wrapped.result.outcome, ExecuteWorkflow.Result.SAFE_STOP)
        self.assertEqual(wrapped.result.error_code, 204)

        self.mode = 'shutdown_delayed_goal'
        requests_before = self.child_goal_request_count
        self._send_ready_goal('single_task', 'shutdown-late-goal')
        self._spin_until(
            lambda: self.child_goal_request_count == requests_before + 1,
        )
        cancel_requests_before = self.child_cancel_request_count
        shutdown_started = time.monotonic()
        os.kill(proc_info[orchestrator].pid, signal.SIGINT)
        while time.monotonic() - shutdown_started < 0.5:
            os.kill(proc_info[orchestrator].pid, 0)
            rclpy.spin_once(self.node, timeout_sec=0.02)
        self.shutdown_goal_release.set()
        self._spin_until(
            lambda: self.child_cancel_request_count == cancel_requests_before + 1,
            timeout=3.0,
        )
        self._spin_until(lambda: self.active_child_count == 0, timeout=2.0)
        time.sleep(0.2)
        self.assertEqual(
            self.child_cancel_request_count, cancel_requests_before + 1
        )
        proc_info.assertWaitForShutdown(process=orchestrator, timeout=2.0)
        self.assertLess(time.monotonic() - shutdown_started, 3.5)


@launch_testing.post_shutdown_test()
class TestProcessCleanup(unittest.TestCase):

    def test_clean_shutdown(self, proc_info, orchestrator):
        launch_testing.asserts.assertExitCodes(proc_info, process=orchestrator)
