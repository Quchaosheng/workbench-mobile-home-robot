"""The SocketCAN system interface exists and refuses what it must (#541).

The only `ros2_control` hardware plugin in the repository was the Gazebo one, so on
physical hardware a trajectory command had no route to a joint. This package is that
route.

Two layers, deliberately:

* the **frame codec** is ROS-free, so its behaviour is testable anywhere a C++
  compiler exists. This module compiles and runs those C++ tests for real.
* the **plugin** needs `hardware_interface`, which this environment does not have, so
  it is not compiled here. Its structure and its refusals are asserted statically, and
  the pull request says the plugin itself is unbuilt on this host.

What neither layer proves: that a frame reached an MCU, that a joint moved, or that a
stop was physically achieved. Hardware stays `NOT_EXECUTED`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "robot" / "mobile_base" / "hardware" / "workbench_mobile_base_hardware"
CODEC_SRC = PACKAGE / "src" / "joint_frame_codec.cpp"
CODEC_HPP = PACKAGE / "include" / "workbench_mobile_base_hardware" / "joint_frame_codec.hpp"
CODEC_TEST = PACKAGE / "test" / "test_joint_frame_codec.cpp"
PLUGIN_SRC = PACKAGE / "src" / "can_system_interface.cpp"
PLUGIN_HPP = PACKAGE / "include" / "workbench_mobile_base_hardware" / "can_system_interface.hpp"
PLUGIN_XML = PACKAGE / "workbench_mobile_base_hardware.xml"

HAVE_COMPILER = shutil.which("g++") is not None or shutil.which("clang++") is not None
HAVE_GTEST = Path("/usr/include/gtest/gtest.h").is_file()


class TestThePackageIsComplete:
    @pytest.mark.parametrize(
        "relative",
        [
            "CMakeLists.txt",
            "package.xml",
            "workbench_mobile_base_hardware.xml",
            "src/joint_frame_codec.cpp",
            "src/can_system_interface.cpp",
            "include/workbench_mobile_base_hardware/joint_frame_codec.hpp",
            "include/workbench_mobile_base_hardware/can_system_interface.hpp",
            "test/test_joint_frame_codec.cpp",
        ],
    )
    def test_every_required_file_exists(self, relative: str) -> None:
        assert (PACKAGE / relative).is_file(), relative

    def test_the_package_declares_the_plugin_dependencies(self) -> None:
        root = ET.parse(PACKAGE / "package.xml").getroot()
        depends = {element.text for element in root.findall("depend")}
        assert {"hardware_interface", "pluginlib", "rclcpp_lifecycle"} <= depends

    def test_the_plugin_description_names_the_base_class(self) -> None:
        """A wrong `base_class_type` loads the plugin as a different kind of thing."""
        root = ET.parse(PLUGIN_XML).getroot()
        plugin = root.find("class")
        assert plugin is not None
        assert plugin.get("base_class_type") == "hardware_interface::SystemInterface"
        assert plugin.get("type") == "workbench_mobile_base_hardware::CanSystemInterface"

    def test_the_plugin_is_exported(self) -> None:
        source = PLUGIN_SRC.read_text(encoding="utf-8")
        assert "PLUGINLIB_EXPORT_CLASS" in source
        assert "CanSystemInterface, hardware_interface::SystemInterface" in source

    def test_the_cmake_builds_the_codec_even_without_ros(self) -> None:
        """A package that cannot build without ROS makes its own refusals untestable."""
        cmake = (PACKAGE / "CMakeLists.txt").read_text(encoding="utf-8")
        assert "add_library(joint_frame_codec" in cmake
        # The plugin is guarded, the codec is not.
        codec_index = cmake.index("add_library(joint_frame_codec")
        guard_index = cmake.index("if(ament_cmake_FOUND AND hardware_interface_FOUND")
        assert codec_index < guard_index


class TestTheCodecRefusalsAreRealCpp:
    """These compile and run the C++ tests. A passing Python assertion is not the claim."""

    @pytest.mark.skipif(not HAVE_COMPILER, reason="no C++ compiler here")
    @pytest.mark.skipif(not HAVE_GTEST, reason="gtest is not installed here")
    def test_the_cpp_codec_tests_pass(self, tmp_path: Path) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        binary = tmp_path / "test_codec"
        build = subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                f"-I{CODEC_HPP.parent.parent}",
                "-o",
                str(binary),
                str(CODEC_SRC),
                str(CODEC_TEST),
                "-lgtest",
                "-lgtest_main",
                "-pthread",
            ],
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stdout + build.stderr

        run = subprocess.run([str(binary)], capture_output=True, text=True)
        assert run.returncode == 0, run.stdout + run.stderr
        # The count is asserted so a test file that silently stops being collected
        # (a mangled TEST name, a missing link) fails here rather than passing.
        match = re.search(r"(\d+) tests from \d+ test suites ran", run.stdout)
        assert match, run.stdout
        assert int(match.group(1)) >= 13
        assert "PASSED" in run.stdout

    def test_the_codec_does_not_include_ros(self) -> None:
        """ROS-free is what lets the refusals be tested without a ROS installation."""
        for path in (CODEC_SRC, CODEC_HPP, CODEC_TEST):
            text = path.read_text(encoding="utf-8")
            for header in ("rclcpp", "hardware_interface/", "pluginlib"):
                assert f'#include "{header}' not in text, f"{path.name} includes {header}"
                assert f"#include <{header}" not in text, f"{path.name} includes {header}"


class TestTheFailClosedBehaviourIsInTheSource:
    """The plugin is not compiled here, so its refusals are asserted as text.

    Each assertion names a behaviour the issue requires. They are weaker than a
    passing C++ test and stronger than prose: removing the guard fails this module.
    """

    def test_configure_refuses_a_missing_bus(self) -> None:
        source = PLUGIN_SRC.read_text(encoding="utf-8")
        assert "SIOCGIFINDEX" in source
        assert "does not exist" in source
        # A configure-time failure, not a retry loop.
        assert "CallbackReturn::FAILURE" in source

    def test_configure_refuses_an_unset_arbitration_table(self) -> None:
        source = PLUGIN_SRC.read_text(encoding="utf-8")
        assert "load_arbitration_table" in source
        assert "half-set table is not a table" in source

    def test_a_duplicate_arbitration_id_is_refused(self) -> None:
        source = PLUGIN_SRC.read_text(encoding="utf-8")
        assert "repeats arbitration key" in source
        assert "two meanings for one frame" in source

    def test_a_stale_state_stops_commands_and_does_not_repeat(self) -> None:
        source = PLUGIN_SRC.read_text(encoding="utf-8")
        assert "judge_state_age" in source
        assert "do NOT repeat the last command" in source or "NOT repeated" in source

    def test_an_out_of_range_command_is_refused_not_clipped(self) -> None:
        source = PLUGIN_SRC.read_text(encoding="utf-8")
        assert "encode_joint_command" in source
        assert "silently clipped" in source

    def test_the_read_loop_is_bounded(self) -> None:
        """A read that never returns is a robot that never commands."""
        source = PLUGIN_SRC.read_text(encoding="utf-8")
        assert "kMaxFramesPerRead" in source
        assert "MSG_DONTWAIT" in source

    def test_every_parameter_the_issue_names_is_read(self) -> None:
        source = PLUGIN_SRC.read_text(encoding="utf-8")
        for parameter in (
            "can_interface",
            "arbitration_id_table",
            "state_timeout_s",
            "command_rate_hz",
        ):
            assert f'"{parameter}"' in source, parameter

    def test_the_readme_says_hardware_is_not_executed(self) -> None:
        readme = (ROOT / "robot" / "mobile_base" / "README.md").read_text(encoding="utf-8")
        assert "NOT_EXECUTED" in readme


class TestTheControllersAreNotRewritten:
    def test_the_controller_config_is_untouched(self) -> None:
        """The same controller configuration must drive either plugin."""
        controllers = ROOT / "robot" / "description" / "revision_d" / "revision_d_controllers.yaml"
        assert controllers.is_file()
        # This package adds a plugin; it must not have edited the controller list.
        assert "CanSystemInterface" not in controllers.read_text(encoding="utf-8")

    def test_the_colcon_build_covers_both_packages(self) -> None:
        """CI has to build the new package, or the plugin is unverified there too."""
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        assert "workbench_mobile_base_hardware" in makefile
        assert "COLCON_PACKAGES" in makefile
        assert "robot/mobile_base/hardware" in makefile
