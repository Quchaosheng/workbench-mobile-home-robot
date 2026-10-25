cmake_minimum_required(VERSION 3.8)
project(runtime_monitor)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(diagnostic_msgs REQUIRED)
find_package(rclcpp REQUIRED)
find_package(std_msgs REQUIRED)

add_library(runtime_monitor_core src/health_aggregator.cpp)
target_compile_features(runtime_monitor_core PUBLIC cxx_std_17)
target_include_directories(runtime_monitor_core PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
ament_target_dependencies(runtime_monitor_core diagnostic_msgs)

add_executable(runtime_monitor_node src/runtime_monitor_node.cpp)
target_compile_features(runtime_monitor_node PUBLIC cxx_std_17)
target_link_libraries(runtime_monitor_node runtime_monitor_core)
ament_target_dependencies(runtime_monitor_node
  diagnostic_msgs
  rclcpp
  std_msgs)

install(TARGETS runtime_monitor_node
  RUNTIME DESTINATION lib/${PROJECT_NAME})
install(TARGETS runtime_monitor_core
  EXPORT export_runtime_monitor
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib)
install(DIRECTORY config/ DESTINATION share/${PROJECT_NAME}/config)
install(DIRECTORY include/ DESTINATION include)

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(ament_lint_auto REQUIRED)
  find_package(launch_testing_ament_cmake REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()

  ament_add_gtest(test_health_aggregator test/test_health_aggregator.cpp)
  target_link_libraries(test_health_aggregator runtime_monitor_core)
  ament_target_dependencies(test_health_aggregator diagnostic_msgs)

  add_launch_test(test/test_runtime_monitor_launch.py TIMEOUT 20)
endif()

ament_export_targets(export_runtime_monitor HAS_LIBRARY_TARGET)
ament_export_dependencies(diagnostic_msgs)
ament_package()
