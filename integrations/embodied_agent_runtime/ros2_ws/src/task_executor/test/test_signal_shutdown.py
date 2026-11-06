import signal
import subprocess
import threading
import time
import unittest

from ament_index_python.packages import get_package_prefix
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from robot_task_interfaces.action import ExecuteDeviceCommand, ExecuteTask


class FakeDeviceBridge(Node):

    def __init__(self):
        super().__init__('signal_shutdown_fake_device_bridge')
        self.goal_received = threading.Event()
        self.cancel_received = threading.Event()
        self.terminal_release = threading.Event()
        self.server = ActionServer(
            self,
            ExecuteDeviceCommand,
            'execute_device_command',
            execute_callback=self.execute,
            goal_callback=self.goal,
            cancel_callback=self.cancel,
        )

    def goal(self, _request):
        self.goal_received.set()
        return GoalResponse.ACCEPT

    def cancel(self, _goal_handle):
        self.cancel_received.set()
        return CancelResponse.ACCEPT

    def execute(self, goal_handle):
        while not goal_handle.is_cancel_requested:
            time.sleep(0.01)
        self.terminal_release.wait()
        goal_handle.canceled()
        result = ExecuteDeviceCommand.Result()
        result.outcome = ExecuteDeviceCommand.Result.CANCELED
        result.message = 'stopped'
        return result


class SignalShutdownTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.bridge = FakeDeviceBridge()
        self.client_node = Node('signal_shutdown_task_client')
        self.client = ActionClient(self.client_node, ExecuteTask, 'execute_task')
        self.executor = MultiThreadedExecutor(num_threads=4)
        self.executor.add_node(self.bridge)
        self.executor.add_node(self.client_node)
        self.spin_thread = threading.Thread(target=self.executor.spin)
        self.spin_thread.start()
        prefix = get_package_prefix('task_executor')
        self.process = subprocess.Popen(
            [
                f'{prefix}/lib/task_executor/task_executor_node',
                '--ros-args',
                '-p',
                'validation_delay_ms:=5',
                '-p',
                'ack_timeout_ms:=50',
                '-p',
                'cancel_timeout_ms:=1000',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertTrue(self.client.wait_for_server(timeout_sec=3.0))

    def tearDown(self):
        self.bridge.terminal_release.set()
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
        self.client.destroy()
        self.bridge.server.destroy()
        self.executor.shutdown(timeout_sec=2.0)
        self.spin_thread.join(timeout=2.0)
        self.client_node.destroy_node()
        self.bridge.destroy_node()

    @staticmethod
    def wait_future(future, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not future.done():
            time.sleep(0.01)
        if not future.done():
            raise TimeoutError('ROS future did not complete')
        return future.result()

    def assert_safe_signal_shutdown(self, signum):
        goal = ExecuteTask.Goal()
        goal.task_id = f'signal-{signum}'
        goal.target_id = 'dock_a'
        goal.allowed_duration.sec = 3
        handle = self.wait_future(self.client.send_goal_async(goal))
        self.assertTrue(handle.accepted)
        self.assertTrue(self.bridge.goal_received.wait(2.0))

        result_future = handle.get_result_async()
        self.process.send_signal(signum)
        self.assertTrue(self.bridge.cancel_received.wait(2.0))
        second = ExecuteTask.Goal()
        second.task_id = f'signal-{signum}-second'
        second.target_id = 'dock_a'
        second.allowed_duration.sec = 1
        self.assertFalse(self.wait_future(self.client.send_goal_async(second)).accepted)
        time.sleep(0.5)
        self.assertIsNone(self.process.poll())
        self.assertFalse(result_future.done())

        self.bridge.terminal_release.set()
        wrapped = self.wait_future(result_future)
        self.assertEqual(wrapped.result.outcome, ExecuteTask.Result.SAFE_STOP)
        self.assertEqual(wrapped.result.error_code, 111)
        self.process.wait(timeout=3.0)
        self.assertEqual(self.process.returncode, 0, self.process.stdout.read())

    def test_sigint_drains_child_before_exit(self):
        self.assert_safe_signal_shutdown(signal.SIGINT)

    def test_sigterm_drains_child_before_exit(self):
        self.assert_safe_signal_shutdown(signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
