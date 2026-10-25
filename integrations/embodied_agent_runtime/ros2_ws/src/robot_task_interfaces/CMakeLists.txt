cmake_minimum_required(VERSION 3.8)
project(robot_task_interfaces)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "action/ExecuteTask.action"
  "action/ExecuteDeviceCommand.action"
  "action/ExecuteWorkflow.action"
  "msg/DeviceState.msg"
  "msg/TaskEvent.msg"
  "srv/ResetFault.srv"
  DEPENDENCIES builtin_interfaces
)
if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  # The project has no license headers yet.
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_export_dependencies(rosidl_default_runtime)
ament_package()
