cmake_minimum_required(VERSION 3.8)
project(task_orchestrator)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(ament_index_cpp REQUIRED)
find_package(behaviortree_cpp REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_action REQUIRED)
find_package(robot_task_interfaces REQUIRED)
find_package(std_msgs REQUIRED)

add_library(ready_state src/ready_state.cpp)
target_compile_features(ready_state PUBLIC cxx_std_17)
target_include_directories(ready_state PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)

add_executable(task_orchestrator_node src/task_orchestrator_node.cpp)
target_compile_features(task_orchestrator_node PUBLIC cxx_std_17)
target_link_libraries(task_orchestrator_node ready_state)
ament_target_dependencies(task_orchestrator_node
  ament_index_cpp
  behaviortree_cpp
  rclcpp
  rclcpp_action
  robot_task_interfaces
  std_msgs)

install(TARGETS ready_state task_orchestrator_node
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY include/ DESTINATION include)
install(DIRECTORY config/ DESTINATION share/${PROJECT_NAME}/config)

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(launch_testing_ament_cmake REQUIRED)
  find_package(ament_lint_auto REQUIRED)
  # The project has no license headers yet.
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
  ament_add_gtest(test_ready_state test/test_ready_state.cpp)
  target_link_libraries(test_ready_state ready_state)

  add_library(task_orchestrator_node_test_lib src/task_orchestrator_node.cpp)
  target_compile_definitions(task_orchestrator_node_test_lib PRIVATE TASK_ORCHESTRATOR_NO_MAIN)
  target_compile_features(task_orchestrator_node_test_lib PUBLIC cxx_std_17)
  target_include_directories(task_orchestrator_node_test_lib PRIVATE src)
  target_link_libraries(task_orchestrator_node_test_lib ready_state)
  ament_target_dependencies(task_orchestrator_node_test_lib
    ament_index_cpp behaviortree_cpp rclcpp rclcpp_action robot_task_interfaces std_msgs)

  ament_add_gtest(test_orchestrator_faults test/test_orchestrator_faults.cpp)
  target_include_directories(test_orchestrator_faults PRIVATE src)
  target_link_libraries(test_orchestrator_faults task_orchestrator_node_test_lib)
  ament_target_dependencies(test_orchestrator_faults
    rclcpp rclcpp_action robot_task_interfaces std_msgs)

  add_launch_test(test/test_workflow_launch.py TIMEOUT 30)
  add_launch_test(test/test_worker_exception_launch.py TIMEOUT 20)
  set_tests_properties(
    test_test_workflow_launch.py
    test_test_worker_exception_launch.py
    PROPERTIES RUN_SERIAL TRUE)
  set_tests_properties(test_test_workflow_launch.py PROPERTIES
    ENVIRONMENT "ROS_DOMAIN_ID=181;ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST")
  set_tests_properties(test_test_worker_exception_launch.py PROPERTIES
    ENVIRONMENT "ROS_DOMAIN_ID=182;ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST")
endif()

ament_package()
