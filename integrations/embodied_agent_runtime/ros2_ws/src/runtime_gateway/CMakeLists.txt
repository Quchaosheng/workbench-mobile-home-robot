cmake_minimum_required(VERSION 3.8)
project(runtime_gateway)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(Protobuf REQUIRED)
find_package(gRPC CONFIG QUIET)
if(NOT (TARGET gRPC::grpc++ AND TARGET gRPC::grpc_cpp_plugin))
  find_package(PkgConfig REQUIRED)
  pkg_check_modules(GRPC REQUIRED IMPORTED_TARGET grpc++)

  if(NOT TARGET gRPC::grpc++)
    add_library(gRPC::grpc++ ALIAS PkgConfig::GRPC)
  endif()

  if(NOT TARGET gRPC::grpc_cpp_plugin)
    find_program(GRPC_CPP_PLUGIN_EXECUTABLE NAMES grpc_cpp_plugin REQUIRED)
    add_executable(gRPC::grpc_cpp_plugin IMPORTED GLOBAL)
    set_target_properties(gRPC::grpc_cpp_plugin PROPERTIES
      IMPORTED_LOCATION "${GRPC_CPP_PLUGIN_EXECUTABLE}")
  endif()
endif()
find_package(rclcpp REQUIRED)
find_package(rclcpp_action REQUIRED)
find_package(robot_task_interfaces REQUIRED)
find_package(runtime_history REQUIRED)

set(PROTO_FILE ${CMAKE_CURRENT_SOURCE_DIR}/proto/robot_runtime.proto)
set(PROTO_DIR ${CMAKE_CURRENT_BINARY_DIR}/generated)
set(PROTO_SRCS ${PROTO_DIR}/robot_runtime.pb.cc ${PROTO_DIR}/robot_runtime.grpc.pb.cc)
set(PROTO_HDRS ${PROTO_DIR}/robot_runtime.pb.h ${PROTO_DIR}/robot_runtime.grpc.pb.h)
file(MAKE_DIRECTORY ${PROTO_DIR})
add_custom_command(
  OUTPUT ${PROTO_SRCS} ${PROTO_HDRS}
  COMMAND protobuf::protoc
  ARGS --cpp_out=${PROTO_DIR} --grpc_out=${PROTO_DIR}
    --plugin=protoc-gen-grpc=$<TARGET_FILE:gRPC::grpc_cpp_plugin>
    -I ${CMAKE_CURRENT_SOURCE_DIR}/proto ${PROTO_FILE}
  DEPENDS ${PROTO_FILE} protobuf::protoc gRPC::grpc_cpp_plugin
  VERBATIM)

add_library(runtime_gateway_proto ${PROTO_SRCS})
target_include_directories(runtime_gateway_proto PUBLIC ${PROTO_DIR})
target_link_libraries(runtime_gateway_proto PUBLIC protobuf::libprotobuf gRPC::grpc++)

add_library(request_registry src/request_registry.cpp)
target_compile_features(request_registry PUBLIC cxx_std_17)
target_include_directories(request_registry PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)

add_library(durable_idempotency src/durable_idempotency.cpp)
target_compile_features(durable_idempotency PUBLIC cxx_std_17)
target_include_directories(durable_idempotency PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_link_libraries(durable_idempotency
  request_registry runtime_history::runtime_history_store)

add_library(gateway_service src/gateway_service.cpp)
target_compile_features(gateway_service PUBLIC cxx_std_17)
target_include_directories(gateway_service PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_link_libraries(gateway_service
  request_registry durable_idempotency runtime_gateway_proto
  runtime_history::runtime_history_store)
ament_target_dependencies(gateway_service rclcpp rclcpp_action robot_task_interfaces)

add_library(loopback_server src/loopback_server.cpp)
target_compile_features(loopback_server PUBLIC cxx_std_17)
target_include_directories(loopback_server PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_link_libraries(loopback_server gateway_service runtime_gateway_proto)

add_executable(runtime_gateway_node src/runtime_gateway_node.cpp)
target_compile_features(runtime_gateway_node PUBLIC cxx_std_17)
target_link_libraries(runtime_gateway_node gateway_service loopback_server)
ament_target_dependencies(runtime_gateway_node rclcpp)

add_executable(runtime_gateway_client src/runtime_gateway_client.cpp)
target_compile_features(runtime_gateway_client PUBLIC cxx_std_17)
target_link_libraries(runtime_gateway_client runtime_gateway_proto)

install(TARGETS runtime_gateway_node runtime_gateway_client
  RUNTIME DESTINATION lib/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(ament_cmake_pytest REQUIRED)
  find_package(ament_lint_auto REQUIRED)
  # The project has no license headers yet.
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
  ament_add_gtest(test_request_registry test/test_request_registry.cpp)
  target_link_libraries(test_request_registry request_registry)
  ament_add_gtest(test_durable_idempotency test/test_durable_idempotency.cpp)
  target_link_libraries(test_durable_idempotency durable_idempotency)
  ament_add_gtest(test_grpc_validation test/test_grpc_validation.cpp)
  target_link_libraries(test_grpc_validation gateway_service)
  target_compile_definitions(test_grpc_validation PRIVATE
    RUNTIME_GATEWAY_CLIENT_PATH="$<TARGET_FILE:runtime_gateway_client>")
  add_dependencies(test_grpc_validation runtime_gateway_client)
  ament_add_gtest(test_grpc_action_bridge test/test_grpc_action_bridge.cpp)
  target_link_libraries(test_grpc_action_bridge gateway_service loopback_server)
  ament_add_pytest_test(test_signal_shutdown test/test_signal_shutdown.py TIMEOUT 20)
  ament_add_pytest_test(test_cli test/test_cli.py TIMEOUT 20)
  set_tests_properties(test_grpc_validation PROPERTIES
    ENVIRONMENT "ROS_DOMAIN_ID=191;ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST")
  set_tests_properties(test_grpc_action_bridge PROPERTIES
    ENVIRONMENT "ROS_DOMAIN_ID=192;ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST")
  set_tests_properties(test_signal_shutdown PROPERTIES
    ENVIRONMENT "ROS_DOMAIN_ID=193;ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST")
  set_tests_properties(test_cli PROPERTIES
    ENVIRONMENT "ROS_DOMAIN_ID=194;ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST")
endif()

ament_package()
