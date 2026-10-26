#include "runtime_history/stage_event_log.hpp"

#include <gtest/gtest.h>

#include <algorithm>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <unistd.h>

namespace
{

std::string read_file(const std::string & path)
{
  std::ifstream input(path);
  std::ostringstream buffer;
  buffer << input.rdbuf();
  return buffer.str();
}

// The offline diagnoser parses each line as one JSON object and requires these
// keys to be present with these types; a regression here silently drops
// evidence instead of failing loudly.
TEST(StageEventLog, WritesParserReadyRuntimeEventLine)
{
  const std::string path = "/tmp/stage_event_log_test_" +
    std::to_string(::getpid()) + ".jsonl";
  std::remove(path.c_str());
  {
    runtime_history::StageEventLog log(path);
    ASSERT_TRUE(log.enabled());
    runtime_history::StageEvent event;
    event.trace_id = "trace-1";
    event.source_node = "task_executor";
    event.stage = "action_execute_start";
    event.event_name = "action_execute_start";
    event.event_type = "action";
    event.extra_json = R"({"task_id":"task-1","command_id":7})";
    log.emit(event);
  }

  const std::string content = read_file(path);
  std::remove(path.c_str());
  ASSERT_FALSE(content.empty());
  EXPECT_EQ(content.back(), '\n');
  for (const char * key : {
      "\"trace_id\":\"trace-1\"", "\"sequence_id\":", "\"source_node\":\"task_executor\"",
      "\"stage\":\"action_execute_start\"", "\"timestamp_ns\":", "\"event_name\":",
      "\"event_type\":\"action\"", "\"pid\":", "\"tid\":", "\"host_id\":\"",
      "\"clock_id\":\"monotonic\"", "\"duration_ns\":0", "\"status\":\"observed\"",
      "\"reason_code\":\"\"", "\"extra_json\":\"{\\\"task_id\\\"",
    })
  {
    EXPECT_NE(content.find(key), std::string::npos) << "missing " << key;
  }
  // host_id must be resolved, never the placeholder the adapter rejects.
  EXPECT_EQ(content.find("\"host_id\":\"unknown\""), std::string::npos);
  EXPECT_EQ(content.find("\"host_id\":\"\""), std::string::npos);
}

TEST(StageEventLog, DisabledLogWritesNothing)
{
  const std::string path = "/tmp/stage_event_log_disabled_" +
    std::to_string(::getpid()) + ".jsonl";
  std::remove(path.c_str());
  runtime_history::StageEventLog log("");
  EXPECT_FALSE(log.enabled());
  runtime_history::StageEvent event;
  event.trace_id = "trace-1";
  log.emit(event);
  EXPECT_TRUE(read_file(path).empty());
}

// Both processes must agree on sequence_id for one trace, otherwise the
// diagnoser rejects the trace as inconsistent identity.
TEST(StageEventLog, SequenceIdIsStableAndIndependentOfProducer)
{
  const auto first = runtime_history::stage_sequence_id("workflow-42");
  EXPECT_EQ(first, runtime_history::stage_sequence_id("workflow-42"));
  EXPECT_GE(first, 1ULL);
  EXPECT_NE(first, runtime_history::stage_sequence_id("workflow-43"));
}

// Identifiers come from the network. A quote in one must not be able to break
// the surrounding record, or the offline adapter rejects the whole capture.
TEST(StageEventLog, EscapesHostileIdentifiersIntoOneValidLine)
{
  const std::string path = "/tmp/stage_event_log_escape_" +
    std::to_string(::getpid()) + ".jsonl";
  std::remove(path.c_str());
  {
    runtime_history::StageEventLog log(path);
    runtime_history::StageEvent event;
    event.trace_id = "trace-1";
    event.source_node = "task_executor";
    event.stage = "action_receive";
    event.event_name = "action_command_received";
    event.event_type = "action";
    event.extra_json = "{\"task_id\":" +
      runtime_history::json_quote("bad\"id\n{") + "}";
    log.emit(event);
  }

  const std::string content = read_file(path);
  std::remove(path.c_str());
  // Exactly one line, so the embedded quote did not start a second record.
  EXPECT_EQ(std::count(content.begin(), content.end(), '\n'), 1);
  EXPECT_NE(content.find("\\\"id"), std::string::npos);
  EXPECT_EQ(content.find("bad\"id"), std::string::npos);
}

}  // namespace
