#include "robot_runtime.grpc.pb.h"

#include <charconv>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <map>
#include <sstream>
#include <string>

#include <grpcpp/grpcpp.h>

namespace
{

void set_deadline(grpc::ClientContext & context)
{
  context.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(2));
}

std::string quote(const std::string & value)
{
  constexpr char hex[] = "0123456789abcdef";
  std::string result{"\""};
  for (const unsigned char character : value) {
    switch (character) {
      case '"': result += "\\\""; break;
      case '\\': result += "\\\\"; break;
      case '\b': result += "\\b"; break;
      case '\f': result += "\\f"; break;
      case '\n': result += "\\n"; break;
      case '\r': result += "\\r"; break;
      case '\t': result += "\\t"; break;
      default:
        if (character < 0x20) {
          result += "\\u00";
          result += hex[character >> 4];
          result += hex[character & 0x0f];
        } else {
          result += static_cast<char>(character);
        }
    }
  }
  return result + '"';
}

std::map<std::string, std::string> options(const int argc, char ** argv, const int first)
{
  if ((argc - first) % 2 != 0) {
    throw std::invalid_argument("options require values");
  }
  std::map<std::string, std::string> result;
  for (int index = first; index < argc; index += 2) {
    if (std::string(argv[index]).rfind("--", 0) != 0 || !result.emplace(
        argv[index],
        argv[index + 1]).second)
    {
      throw std::invalid_argument("invalid or duplicate option");
    }
  }
  return result;
}

std::string required(const std::map<std::string, std::string> & values, const std::string & key)
{
  const auto value = values.find(key);
  if (value == values.end() || value->second.empty()) {
    throw std::invalid_argument("missing " + key);
  }
  return value->second;
}

std::uint64_t number(const std::string & value)
{
  std::uint64_t result{};
  const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
  if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size()) {
    throw std::invalid_argument("invalid number");
  }
  return result;
}

int rpc_error(const grpc::Status & status)
{
  std::cout << "{\"ok\":false,\"grpc_code\":" << status.error_code()
            << ",\"message\":" << quote(status.error_message()) << "}\n";
  return 1;
}

int submit(runtime_gateway::RobotRuntime::Stub & stub, const int argc, char ** argv)
{
  const auto values = options(argc, argv, 2);
  if (values.size() != 5) {
    throw std::invalid_argument("submit requires five options");
  }
  runtime_gateway::SubmitWorkflowRequest request;
  request.set_request_id(required(values, "--request-id"));
  request.set_workflow_id(required(values, "--workflow"));
  request.set_task_id(required(values, "--task-id"));
  request.set_target_id(required(values, "--target"));
  request.set_timeout_ms(number(required(values, "--timeout-ms")));
  runtime_gateway::SubmitWorkflowReply reply;
  grpc::ClientContext context;
  set_deadline(context);
  const auto status = stub.SubmitWorkflow(&context, request, &reply);
  if (!status.ok()) {
    return rpc_error(status);
  }
  std::cout << "{\"accepted\":" << (reply.accepted() ? "true" : "false")
            << ",\"request_id\":" << quote(reply.request_id())
            << ",\"task_id\":" << quote(reply.task_id())
            << ",\"state\":" << quote(reply.state())
            << ",\"outcome\":" << reply.outcome()
            << ",\"error_code\":" << reply.error_code()
            << ",\"message\":" << quote(reply.message()) << "}\n";
  return 0;
}

int cancel(runtime_gateway::RobotRuntime::Stub & stub, const int argc, char ** argv)
{
  const auto values = options(argc, argv, 2);
  if (values.size() != 1) {
    throw std::invalid_argument("cancel requires --request-id");
  }
  runtime_gateway::CancelWorkflowRequest request;
  request.set_request_id(required(values, "--request-id"));
  runtime_gateway::CancelWorkflowReply reply;
  grpc::ClientContext context;
  set_deadline(context);
  const auto status = stub.CancelWorkflow(&context, request, &reply);
  if (!status.ok()) {
    return rpc_error(status);
  }
  std::cout << "{\"accepted\":" << (reply.accepted() ? "true" : "false")
            << ",\"state\":" << quote(reply.state())
            << ",\"outcome\":" << reply.outcome()
            << ",\"error_code\":" << reply.error_code()
            << ",\"message\":" << quote(reply.message()) << "}\n";
  return 0;
}

int get_task(runtime_gateway::RobotRuntime::Stub & stub, const int argc, char ** argv)
{
  const auto values = options(argc, argv, 2);
  if (values.size() != 1) {
    throw std::invalid_argument("get-task requires --task-id");
  }
  runtime_gateway::GetTaskRequest request;
  request.set_task_id(required(values, "--task-id"));
  runtime_gateway::TaskRecord reply;
  grpc::ClientContext context;
  set_deadline(context);
  const auto status = stub.GetTask(&context, request, &reply);
  if (!status.ok()) {
    return rpc_error(status);
  }
  std::cout << "{\"found\":" << (reply.found() ? "true" : "false")
            << ",\"task_id\":" << quote(reply.task_id())
            << ",\"target_id\":" << quote(reply.target_id())
            << ",\"state\":" << quote(reply.state())
            << ",\"action_status\":" << reply.action_status()
            << ",\"outcome\":" << reply.outcome()
            << ",\"error_code\":" << reply.error_code()
            << ",\"duration_ms\":" << reply.duration_ms()
            << ",\"message\":" << quote(reply.message())
            << ",\"completed_at_ns\":" << reply.completed_at_ns() << "}\n";
  return 0;
}

int get_stats(runtime_gateway::RobotRuntime::Stub & stub, const int argc)
{
  if (argc != 2) {
    throw std::invalid_argument("get-stats takes no options");
  }
  runtime_gateway::GetStatsRequest request;
  runtime_gateway::RuntimeStats reply;
  grpc::ClientContext context;
  set_deadline(context);
  const auto status = stub.GetStats(&context, request, &reply);
  if (!status.ok()) {
    return rpc_error(status);
  }
  std::ostringstream counts;
  for (int index = 0; index < reply.outcome_counts_size(); ++index) {
    if (index != 0) {
      counts << ',';
    }
    counts << reply.outcome_counts(index);
  }
  std::cout << "{\"has_data\":" << (reply.has_data() ? "true" : "false")
            << ",\"sample_count\":" << reply.sample_count()
            << ",\"outcome_counts\":[" << counts.str() << ']'
            << ",\"p50_ms\":" << reply.p50_ms()
            << ",\"p95_ms\":" << reply.p95_ms()
            << ",\"p99_ms\":" << reply.p99_ms()
            << ",\"max_ms\":" << reply.max_ms()
            << ",\"state\":" << quote(reply.state())
            << ",\"error_code\":" << reply.error_code()
            << ",\"message\":" << quote(reply.message()) << "}\n";
  return 0;
}

}  // namespace

int main(int argc, char ** argv)
{
  try {
    if (argc < 2) {
      throw std::invalid_argument("command required: submit, cancel, get-task, or get-stats");
    }
    const char * configured = std::getenv("RUNTIME_GATEWAY_ADDRESS");
    const std::string address = configured == nullptr ? "127.0.0.1:50051" : configured;
    auto stub = runtime_gateway::RobotRuntime::NewStub(
      grpc::CreateChannel(address, grpc::InsecureChannelCredentials()));
    const std::string command = argv[1];
    if (command == "submit") {
      return submit(*stub, argc, argv);
    }
    if (command == "cancel") {
      return cancel(*stub, argc, argv);
    }
    if (command == "get-task") {
      return get_task(*stub, argc, argv);
    }
    if (command == "get-stats") {
      return get_stats(*stub, argc);
    }
    throw std::invalid_argument("unknown command");
  } catch (const std::exception & error) {
    std::cout << "{\"ok\":false,\"message\":" << quote(error.what()) << "}\n";
    return 2;
  }
}
