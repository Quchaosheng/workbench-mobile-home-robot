cmake_minimum_required(VERSION 3.8)
project(runtime_can)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)

add_library(runtime_can
  src/protocol.cpp
  src/socket_can.cpp)
target_compile_features(runtime_can PUBLIC cxx_std_17)
target_include_directories(runtime_can PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)

install(TARGETS runtime_can
  EXPORT export_runtime_can
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib)
install(DIRECTORY include/ DESTINATION include)

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(ament_lint_auto REQUIRED)
  # The project has no license headers yet.
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()

  ament_add_gtest(test_protocol test/test_protocol.cpp)
  target_link_libraries(test_protocol runtime_can)
endif()

ament_export_targets(export_runtime_can HAS_LIBRARY_TARGET)
ament_package()
