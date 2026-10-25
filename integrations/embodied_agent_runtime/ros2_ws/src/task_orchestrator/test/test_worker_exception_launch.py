from pathlib import Path
import time
import unittest

from action_msgs.msg import GoalStatus
from ament_index_python.packages import get_package_share_directory
import launch
import launch_ros.actions
import launch_testing.actions
import launch_testing.asserts
import launch_testing.markers
import rclpy
from rclpy.action import ActionClient
from robot_task_interfaces.action import ExecuteWorkflow


def generate_test_description():
    orchestrator = launch_ros.actions.Node(
        package='task_orchestrator',
        executable='task_orchestrator_node',
        output='screen',
    )
    return (
        launch.LaunchDescription(
            [orchestrator, launch_testing.actions.ReadyToTest()]
        ),
        {'orchestrator': orchestrator},
    )


class TestWorkerException(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.node = rclpy.create_node('worker_exception_test')
        self.client = ActionClient(self.node, ExecuteWorkflow, 'execute_workflow')
        self.assertTrue(self.client.wait_for_server(timeout_sec=5.0))

    def tearDown(self):
        self.client.destroy()
        self.node.destroy_node()

    def _wait(self, future, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not future.done():
            rclpy.spin_once(self.node, timeout_sec=0.02)
        self.assertTrue(future.done(), 'worker did not return a terminal result')
        return future.result()

    @staticmethod
    def _goal(workflow_id='single_task'):
        goal = ExecuteWorkflow.Goal()
        goal.request_id = 'worker-exception'
        goal.workflow_id = workflow_id
        goal.task_id = 'worker-exception'
        goal.target_id = 'dock_a'
        goal.allowed_duration.sec = 5
        return goal

    def test_malformed_fixed_xml_aborts_goal_without_killing_node(self):
        xml_path = Path(
            get_package_share_directory('task_orchestrator')
        ) / 'config' / 'workflows.xml'
        original = xml_path.read_text(encoding='utf-8')
        try:
            xml_path.write_text('<root BTCPP_format="4">', encoding='utf-8')
            goal_handle = self._wait(self.client.send_goal_async(self._goal()))
            self.assertTrue(goal_handle.accepted)
            wrapped = self._wait(goal_handle.get_result_async())
            self.assertEqual(wrapped.status, GoalStatus.STATUS_ABORTED)
            self.assertEqual(wrapped.result.error_code, 208)
        finally:
            xml_path.write_text(original, encoding='utf-8')

        rejected = self._wait(
            self.client.send_goal_async(self._goal('../still-not-xml'))
        )
        self.assertFalse(rejected.accepted)


@launch_testing.post_shutdown_test()
class TestProcessCleanup(unittest.TestCase):

    def test_clean_shutdown(self, proc_info, orchestrator):
        launch_testing.asserts.assertExitCodes(proc_info, process=orchestrator)
