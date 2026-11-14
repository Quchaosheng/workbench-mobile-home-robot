cmake_minimum_required(VERSION 3.16)
project(workbench_mobile_base_hardware CXX)

# Issue #541: the SocketCAN ros2_control system interface for Revision D.
#
# The plugin itself needs `hardware_interface`, which only the full-stack image
# carries. The frame codec does not: it is ROS-free on purpose, so its tests run with
# a C++ compiler alone. Both are built here, and the codec tests are built
# unconditionally so `colcon test` has something to run wherever the package builds.

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(CODEC_SOURCE src/joint_frame_codec.cpp)
add_library(joint_frame_codec STATIC ${CODEC_SOURCE})
target_include_directories(joint_frame_codec PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)

# ---------------------------------------------------------------------- tests
# The codec test needs only gtest. It is built without ROS so a host with a
# compiler can verify the refusals, which is where the value is: an out-of-range
# command that gets clipped is a silent wrong motion.
find_package(GTest QUIET)
option(WORKBENCH_CODEC_TESTS "Build the ROS-free codec tests" ON)

if(WORKBENCH_CODEC_TESTS AND GTest_FOUND)
  enable_testing()
  add_executable(test_joint_frame_codec test/test_joint_frame_codec.cpp)
  target_link_libraries(test_joint_frame_codec joint_frame_codec GTest::gtest GTest::gtest_main)
  add_test(NAME joint_frame_codec COMMAND test_joint_frame_codec)
elseif(WORKBENCH_CODEC_TESTS)
  message(STATUS "GTest not found; the ROS-free codec tests are not built")
endif()

# --------------------------------------------------------------------- plugin
# The `hardware_interface` plugin is only built where ROS 2 ros2_control is
# installed, which is the full-stack image. Refusing softly here keeps the codec
# testable on a host without ROS instead of making the whole package unbuildable.
find_package(ament_cmake QUIET)
find_package(hardware_interface QUIET)
find_package(pluginlib QUIET)
find_package(rclcpp QUIET)
find_package(rclcpp_lifecycle QUIET)

if(ament_cmake_FOUND AND hardware_interface_FOUND AND pluginlib_FOUND)
  find_package(rclcpp_lifecycle REQUIRED)

  add_library(workbench_mobile_base_hardware SHARED
    src/can_system_interface.cpp
    src/joint_frame_codec.cpp)
  target_include_directories(workbench_mobile_base_hardware PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>)
  target_link_libraries(workbench_mobile_base_hardware
    hardware_interface::hardware_interface
    pluginlib::pluginlib
    rclcpp_lifecycle::rclcpp_lifecycle)

  # The plugin description is what makes the class reachable by name from a URDF.
  pluginlib_export_plugin_description_file(
    hardware_interface workbench_mobile_base_hardware.xml)

  install(DIRECTORY include/ DESTINATION include)
  # The export set needs a name distinct from the target: `install(TARGETS ...
  # EXPORT <set>)` creates <set>, and `ament_export_targets(<set>)` names it. Using
  # the target name for both is the shape that produced
  # "INSTALL(EXPORT) given unknown export" on the first CI build.
  install(TARGETS workbench_mobile_base_hardware
    EXPORT workbench_mobile_base_hardware_targets
    ARCHIVE DESTINATION lib
    LIBRARY DESTINATION lib
    RUNTIME DESTINATION bin)

  install(EXPORT workbench_mobile_base_hardware_targets
    DESTINATION share/${PROJECT_NAME}/cmake)

  ament_export_targets(workbench_mobile_base_hardware_targets HAS_LIBRARY_TARGET)
  ament_export_dependencies(hardware_interface pluginlib rclcpp_lifecycle)
  ament_package()
else()
  message(STATUS
    "ros2_control or ament_cmake not found; the plugin is not built, only the codec. "
    "The full-stack image builds both.")
  install(DIRECTORY include/ DESTINATION include)
  install(TARGETS joint_frame_codec ARCHIVE DESTINATION lib)
  if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/package.xml")
    find_package(ament_cmake QUIET)
    if(ament_cmake_FOUND)
      ament_package()
    endif()
  endif()
endif()
