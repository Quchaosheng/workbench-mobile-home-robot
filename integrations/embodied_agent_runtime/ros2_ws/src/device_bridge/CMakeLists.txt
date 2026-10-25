cmake_minimum_required(VERSION 3.8)
project(device_bridge)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_gtest QUIET)
find_package(builtin_interfaces REQUIRED)
find_package(diagnostic_msgs REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_action REQUIRED)
find_package(robot_task_interfaces REQUIRED)
find_package(runtime_can REQUIRED)
find_package(runtime_history REQUIRED)

add_library(device_bridge_policy
  src/command_policy.cpp)
target_compile_features(device_bridge_policy PUBLIC cxx_std_17)
target_include_directories(device_bridge_policy PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
ament_target_dependencies(device_bridge_policy builtin_interfaces)

add_library(device_bridge_diagnostics src/diagnostic_state.cpp)
target_compile_features(device_bridge_diagnostics PUBLIC cxx_std_17)
target_include_directories(device_bridge_diagnostics PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
ament_target_dependencies(device_bridge_diagnostics diagnostic_msgs)

add_executable(device_bridge_node src/device_bridge_node.cpp)
target_compile_features(device_bridge_node PUBLIC cxx_std_17)
target_link_libraries(device_bridge_node
  device_bridge_diagnostics
  device_bridge_policy
  runtime_can::runtime_can
  runtime_history::runtime_history_stage_events)
ament_target_dependencies(device_bridge_node
  builtin_interfaces
  diagnostic_msgs
  rclcpp
  rclcpp_action
  robot_task_interfaces)

add_executable(device_bridge_client src/device_bridge_client.cpp)
target_compile_features(device_bridge_client PUBLIC cxx_std_17)
ament_target_dependencies(device_bridge_client
  builtin_interfaces
  rclcpp
  rclcpp_action
  robot_task_interfaces)

install(TARGETS
  device_bridge_diagnostics
  device_bridge_policy
  device_bridge_node
  device_bridge_client
  EXPORT export_device_bridge
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY include/ DESTINATION include)
install(DIRECTORY config/ DESTINATION share/${PROJECT_NAME}/config)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()

  ament_add_gtest(test_command_policy test/test_command_policy.cpp)
  target_link_libraries(test_command_policy device_bridge_policy)
  ament_target_dependencies(test_command_policy builtin_interfaces)

  ament_add_gtest(test_diagnostic_state test/test_diagnostic_state.cpp)
  target_link_libraries(test_diagnostic_state device_bridge_diagnostics)
  ament_target_dependencies(test_diagnostic_state diagnostic_msgs)
endif()

ament_export_targets(export_device_bridge HAS_LIBRARY_TARGET)
ament_export_dependencies(builtin_interfaces)
ament_export_dependencies(diagnostic_msgs)
ament_package()
