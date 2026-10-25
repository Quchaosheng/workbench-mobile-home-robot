import time
import unittest

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
import launch
import launch_ros.actions
import launch_testing.actions
import rclpy
from std_msgs.msg import Bool


def generate_test_description():
    monitor = launch_ros.actions.Node(
        package='runtime_monitor',
        executable='runtime_monitor_node',
        parameters=[{'aggregate_period_ms': 50, 'stale_timeout_ms': 300}],
        output='screen',
    )
    return (
        launch.LaunchDescription([monitor, launch_testing.actions.ReadyToTest()]),
        {'monitor': monitor},
    )


class TestRuntimeMonitor(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.node = rclpy.create_node('runtime_monitor_test')
        self.publisher = self.node.create_publisher(DiagnosticArray, '/diagnostics', 10)
        self.ready = None
        self.system_level = None
        self.node.create_subscription(Bool, '/runtime/ready', self._ready_callback, 10)
        self.node.create_subscription(
            DiagnosticArray, '/diagnostics', self._diagnostics_callback, 10
        )

    def tearDown(self):
        self.node.destroy_node()

    def _ready_callback(self, message):
        self.ready = message.data

    def _diagnostics_callback(self, message):
        for status in message.status:
            if status.name == 'runtime/system':
                self.system_level = status.level

    def _publish_core(self):
        message = DiagnosticArray()
        for name in ('runtime/device_bridge', 'runtime/task_executor'):
            status = DiagnosticStatus()
            status.name = name
            status.level = DiagnosticStatus.OK
            status.message = 'healthy'
            message.status.append(status)
        self.publisher.publish(message)

    def _wait_for(self, predicate, timeout=2.0, tick=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if tick is not None:
                tick()
            rclpy.spin_once(self.node, timeout_sec=0.05)
            if predicate():
                return
        self.fail('condition was not reached before timeout')

    def test_ready_stale_and_recovery(self):
        self._wait_for(
            lambda: self.ready is True and self.system_level == DiagnosticStatus.OK,
            tick=self._publish_core,
        )
        self.assertEqual(self.system_level, DiagnosticStatus.OK)

        self._wait_for(
            lambda: self.ready is False
            and self.system_level == DiagnosticStatus.STALE
        )
        self.assertEqual(self.system_level, DiagnosticStatus.STALE)

        self._wait_for(
            lambda: self.ready is True and self.system_level == DiagnosticStatus.OK,
            tick=self._publish_core,
        )
        self.assertEqual(self.system_level, DiagnosticStatus.OK)
