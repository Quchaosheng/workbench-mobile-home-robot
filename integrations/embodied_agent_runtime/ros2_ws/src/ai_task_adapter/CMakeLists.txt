cmake_minimum_required(VERSION 3.8)
project(ai_task_adapter)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(Python3 REQUIRED COMPONENTS Interpreter)
find_package(rclcpp REQUIRED)
find_package(rclcpp_action REQUIRED)
find_package(robot_task_interfaces REQUIRED)

add_library(natural_language_planner
  src/natural_language_planner.cpp)
target_compile_features(natural_language_planner PUBLIC cxx_std_17)
target_include_directories(natural_language_planner PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
ament_target_dependencies(natural_language_planner builtin_interfaces)

add_executable(ai_task_adapter_node src/ai_task_adapter_node.cpp)
target_compile_features(ai_task_adapter_node PUBLIC cxx_std_17)
target_link_libraries(ai_task_adapter_node natural_language_planner)
ament_target_dependencies(ai_task_adapter_node
  builtin_interfaces
  rclcpp
  rclcpp_action
  robot_task_interfaces)

install(TARGETS natural_language_planner ai_task_adapter_node
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY include/ DESTINATION include)
install(DIRECTORY config/ DESTINATION share/${PROJECT_NAME}/config)
install(PROGRAMS scripts/ai_model_adapter_node.py DESTINATION lib/${PROJECT_NAME})
install(FILES
  ai_task_adapter/__init__.py
  ai_task_adapter/model_contract.py
  ai_task_adapter/model_runtime.py
  DESTINATION lib/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(ament_lint_auto REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()

  ament_add_gtest(test_natural_language_planner test/test_natural_language_planner.cpp)
  target_link_libraries(test_natural_language_planner natural_language_planner)
  ament_target_dependencies(test_natural_language_planner builtin_interfaces)

  add_test(
    NAME test_model_contract
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test/test_model_contract.py)
  set_tests_properties(test_model_contract PROPERTIES
    ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}")

  add_test(
    NAME test_model_runtime
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test/test_model_runtime.py)
  set_tests_properties(test_model_runtime PROPERTIES
    ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}")
endif()

ament_package()
