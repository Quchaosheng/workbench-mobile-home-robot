import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import unittest

from ament_index_python.packages import get_package_prefix
import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from robot_task_interfaces.action import ExecuteWorkflow


class FakeOrchestrator(Node):

    def __init__(self):
        super().__init__('signal_shutdown_fake_orchestrator')
        self.goal_received = threading.Event()
        self.cancel_received = threading.Event()
        self.terminal_release = threading.Event()
        self.terminal_published = threading.Event()
        self.server = ActionServer(
            self,
            ExecuteWorkflow,
            'execute_workflow',
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
        result = ExecuteWorkflow.Result()
        result.outcome = ExecuteWorkflow.Result.CANCELED
        result.message = 'canceled'
        self.terminal_published.set()
        return result


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait_for_listener(port, expected, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as client:
            client.settimeout(0.05)
            listening = client.connect_ex(('127.0.0.1', port)) == 0
        if listening == expected:
            return True
        time.sleep(0.02)
    return False


class SignalShutdownTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.fake = FakeOrchestrator()
        self.executor = MultiThreadedExecutor(num_threads=2)
        self.executor.add_node(self.fake)
        self.spin_thread = threading.Thread(target=self.executor.spin)
        self.spin_thread.start()
        prefix = Path(get_package_prefix('runtime_gateway'))
        self.node_binary = prefix / 'lib/runtime_gateway/runtime_gateway_node'
        self.client_binary = prefix / 'lib/runtime_gateway/runtime_gateway_client'
        self.port = free_port()
        self.database = Path(f'/tmp/runtime_gateway_signal_{os.getpid()}.sqlite3')
        self.process = subprocess.Popen(
            [
                str(self.node_binary),
                '--ros-args',
                '-p',
                f'port:={self.port}',
                '-p',
                f'database_path:={self.database}',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertTrue(wait_for_listener(self.port, True))

    def tearDown(self):
        self.fake.terminal_release.set()
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
        self.executor.shutdown(timeout_sec=2)
        self.spin_thread.join(timeout=2)
        self.fake.destroy_node()
        for suffix in ('', '-wal', '-shm'):
            try:
                Path(str(self.database) + suffix).unlink()
            except FileNotFoundError:
                pass

    def test_sigint_cancels_workflow_before_ros_shutdown(self):
        environment = os.environ.copy()
        environment['RUNTIME_GATEWAY_ADDRESS'] = f'127.0.0.1:{self.port}'
        deadline = time.monotonic() + 3
        reply = None
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            command = [
                str(self.client_binary),
                'submit',
                '--request-id',
                f'signal-request-{attempt}',
                '--workflow',
                'single_task',
                '--task-id',
                f'signal-task-{attempt}',
                '--target',
                'dock_a',
                '--timeout-ms',
                '3000',
            ]
            completed = subprocess.run(
                command, env=environment, capture_output=True, text=True
            )
            reply = json.loads(completed.stdout)
            if completed.returncode == 0 and reply.get('accepted'):
                break
            time.sleep(0.05)
        self.assertTrue(reply.get('accepted'), reply)
        self.assertTrue(self.fake.goal_received.wait(2))

        self.process.send_signal(signal.SIGINT)
        self.assertTrue(self.fake.cancel_received.wait(2))
        time.sleep(0.5)
        self.assertIsNone(self.process.poll())
        self.assertFalse(self.fake.terminal_published.is_set())
        self.fake.terminal_release.set()
        self.assertTrue(self.fake.terminal_published.wait(2))
        self.process.wait(timeout=3)
        self.assertEqual(self.process.returncode, 0)
        self.assertTrue(wait_for_listener(self.port, False))
        output = self.process.stdout.read()
        self.assertNotIn('signal_handler(SIGINT/SIGTERM)', output)


if __name__ == '__main__':
    unittest.main()
