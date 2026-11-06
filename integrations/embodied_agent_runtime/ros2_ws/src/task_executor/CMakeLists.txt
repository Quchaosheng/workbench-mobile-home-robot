cmake_minimum_required(VERSION 3.8)
project(task_executor)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(diagnostic_msgs REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_action REQUIRED)
find_package(robot_task_interfaces REQUIRED)
find_package(runtime_history REQUIRED)

add_library(target_validator src/target_validator.cpp)
target_compile_features(target_validator PUBLIC cxx_std_17)
target_include_directories(target_validator PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)

add_library(task_executor_diagnostics src/diagnostic_state.cpp)
target_compile_features(task_executor_diagnostics PUBLIC cxx_std_17)
target_include_directories(task_executor_diagnostics PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
ament_target_dependencies(task_executor_diagnostics diagnostic_msgs)

add_library(task_event src/task_event.cpp)
target_compile_features(task_event PUBLIC cxx_std_17)
target_include_directories(task_event PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
ament_target_dependencies(task_event robot_task_interfaces)

add_library(task_executor_node_lib src/task_executor_node.cpp)
target_include_directories(task_executor_node_lib PRIVATE src)
target_link_libraries(task_executor_node_lib target_validator task_executor_diagnostics task_event
  runtime_history::runtime_history_stage_events)
ament_target_dependencies(task_executor_node_lib
  rclcpp
  rclcpp_action
  robot_task_interfaces)

add_executable(task_executor_node src/task_executor_main.cpp)
target_include_directories(task_executor_node PRIVATE src)
target_link_libraries(task_executor_node task_executor_node_lib)
ament_target_dependencies(task_executor_node rclcpp)

install(TARGETS target_validator task_executor_diagnostics task_event task_executor_node
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY include/ DESTINATION include)
install(DIRECTORY config/ DESTINATION share/${PROJECT_NAME}/config)

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(ament_cmake_pytest REQUIRED)
  find_package(ament_lint_auto REQUIRED)
  # The project has no license headers yet.
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()

  ament_add_gtest(test_target_validator test/test_target_validator.cpp)
  target_link_libraries(test_target_validator target_validator)

  ament_add_gtest(test_diagnostic_state test/test_diagnostic_state.cpp)
  target_link_libraries(test_diagnostic_state task_executor_diagnostics)
  ament_target_dependencies(test_diagnostic_state diagnostic_msgs)

  ament_add_gtest(test_task_event test/test_task_event.cpp)
  target_link_libraries(test_task_event task_event)
  ament_target_dependencies(test_task_event robot_task_interfaces)

  ament_add_gtest(test_executor_safety test/test_executor_safety.cpp)
  target_include_directories(test_executor_safety PRIVATE src)
  target_link_libraries(test_executor_safety task_executor_node_lib)
  ament_target_dependencies(test_executor_safety rclcpp rclcpp_action robot_task_interfaces)

  ament_add_pytest_test(test_signal_shutdown test/test_signal_shutdown.py TIMEOUT 20)
  set_tests_properties(test_signal_shutdown PROPERTIES
    ENVIRONMENT "ROS_DOMAIN_ID=228;ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST")
endif()

ament_package()
