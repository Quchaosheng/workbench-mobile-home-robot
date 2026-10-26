cmake_minimum_required(VERSION 3.8)
project(runtime_history)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(diagnostic_msgs REQUIRED)
find_package(rclcpp REQUIRED)
find_package(robot_task_interfaces REQUIRED)
find_package(SQLite3 REQUIRED)

# Header-only so any node can emit stage evidence without linking SQLite: the
# diagnoser reads these files offline, never through this process.
add_library(runtime_history_stage_events INTERFACE)
target_compile_features(runtime_history_stage_events INTERFACE cxx_std_17)
target_include_directories(runtime_history_stage_events INTERFACE
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)

add_library(runtime_history_store src/store.cpp)
target_compile_features(runtime_history_store PUBLIC cxx_std_17)
target_include_directories(runtime_history_store PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_link_libraries(runtime_history_store SQLite::SQLite3)

add_library(runtime_history_recorder src/recorder.cpp)
target_compile_features(runtime_history_recorder PUBLIC cxx_std_17)
target_include_directories(runtime_history_recorder PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_link_libraries(runtime_history_recorder PUBLIC runtime_history_store)

add_executable(runtime_history_node src/runtime_history_node.cpp)
target_compile_features(runtime_history_node PUBLIC cxx_std_17)
target_link_libraries(runtime_history_node runtime_history_recorder)
ament_target_dependencies(runtime_history_node diagnostic_msgs rclcpp robot_task_interfaces)

add_executable(runtime_history_report src/runtime_history_report.cpp)
target_compile_features(runtime_history_report PUBLIC cxx_std_17)
target_link_libraries(runtime_history_report runtime_history_store)

install(TARGETS runtime_history_store runtime_history_recorder runtime_history_node runtime_history_report
  EXPORT export_runtime_history
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME})
install(TARGETS runtime_history_stage_events EXPORT export_runtime_history)
install(DIRECTORY include/ DESTINATION include)

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(ament_lint_auto REQUIRED)
  # The project has no license headers yet.
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
  ament_add_gtest(test_store test/test_store.cpp)
  target_link_libraries(test_store runtime_history_store SQLite::SQLite3)
  ament_add_gtest(test_recorder test/test_recorder.cpp)
  target_link_libraries(test_recorder runtime_history_recorder)
  ament_add_gtest(test_report_json test/test_report_json.cpp)
  target_include_directories(test_report_json PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
  target_link_libraries(test_report_json runtime_history_store)
  ament_add_gtest(test_stage_event_log test/test_stage_event_log.cpp)
  target_link_libraries(test_stage_event_log runtime_history_stage_events)
endif()

ament_export_targets(export_runtime_history HAS_LIBRARY_TARGET)
ament_export_dependencies(SQLite3)
ament_package()
