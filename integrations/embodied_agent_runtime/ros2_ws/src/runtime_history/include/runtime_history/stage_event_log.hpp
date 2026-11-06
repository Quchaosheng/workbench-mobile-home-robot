#pragma once

#include <chrono>
#include <cstdint>
#include <mutex>
#include <string>

#include <fcntl.h>
#include <unistd.h>

namespace runtime_history
{

// One semantic execution stage, written as a RuntimeEvent v2 JSONL line so an
// offline diagnoser can rebuild the timeline without linking this runtime.
// Callers set identity and meaning; pid/tid/host/clock/timestamp are filled in
// here so every producer agrees on them.
struct StageEvent
{
  std::string trace_id;
  std::string source_node;
  std::string stage;
  std::string event_name;
  std::string event_type;
  std::uint64_t duration_ns{0};
  std::string status{"observed"};
  std::string reason_code;
  std::string extra_json{"{}"};
};

// Stage windows are grouped by (trace_id, sequence_id), so every stage of one
// trace must carry the same sequence_id even across processes. A hash of the
// trace identity gives that with no shared allocator or interface field.
inline std::uint64_t stage_sequence_id(const std::string & trace_id)
{
  std::uint64_t hash = 14695981039346656037ULL;
  for (const char character : trace_id) {
    hash = (hash ^ static_cast<unsigned char>(character)) * 1099511628211ULL;
  }
  // Keep the value a positive JSON integer for the Python consumer.
  return hash >> 1;
}

// The offline adapter rejects a record whose extra_json is not a JSON object
// string, so quotes and backslashes must not survive raw.
inline std::string json_escape(const std::string & value)
{
  static const char * kHexDigits = "0123456789abcdef";
  std::string result;
  result.reserve(value.size());
  for (const char character : value) {
    switch (character) {
      case '"': result += "\\\""; break;
      case '\\': result += "\\\\"; break;
      case '\n': result += "\\n"; break;
      case '\r': result += "\\r"; break;
      case '\t': result += "\\t"; break;
      default:
        if (static_cast<unsigned char>(character) < 0x20U) {
          result += "\\u00";
          result += kHexDigits[(character >> 4) & 0x0F];
          result += kHexDigits[character & 0x0F];
        } else {
          result += character;
        }
    }
  }
  return result;
}

// Quote a value for embedding inside extra_json. Task and request identifiers
// arrive from the network, so an unescaped quote there would leave the offline
// parser with invalid JSON and reject the whole capture.
inline std::string json_quote(const std::string & value)
{
  return "\"" + json_escape(value) + "\"";
}

class StageEventLog
{
public:
  // An empty path disables the log, so callers can leave the feature off and
  // pay only one branch per stage.
  explicit StageEventLog(const std::string & path)
  : path_(path)
  {
    if (!path_.empty()) {
      fd_ = ::open(path_.c_str(), O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    }
  }

  ~StageEventLog()
  {
    if (fd_ >= 0) {
      ::close(fd_);
    }
  }

  StageEventLog(const StageEventLog &) = delete;
  StageEventLog & operator=(const StageEventLog &) = delete;

  bool enabled() const {return fd_ >= 0;}

  // Best-effort: evidence collection must never break task execution, so write
  // failures are swallowed and only the descriptor decides.
  void emit(const StageEvent & event)
  {
    if (fd_ < 0) {
      return;
    }
    const auto timestamp_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
    std::string line;
    line.reserve(512);
    line += "{\"trace_id\":\"";
    line += json_escape(event.trace_id);
    line += "\",\"sequence_id\":";
    line += std::to_string(stage_sequence_id(event.trace_id));
    line += ",\"source_node\":\"";
    line += json_escape(event.source_node);
    line += "\",\"stage\":\"";
    line += json_escape(event.stage);
    line += "\",\"timestamp_ns\":";
    line += std::to_string(timestamp_ns);
    line += ",\"event_name\":\"";
    line += json_escape(event.event_name);
    line += "\",\"event_type\":\"";
    line += json_escape(event.event_type);
    line += "\",\"pid\":";
    line += std::to_string(static_cast<long long>(::getpid()));
    line += ",\"tid\":";
    line += std::to_string(static_cast<long long>(::gettid()));
    line += ",\"host_id\":\"";
    line += json_escape(host_id());
    line += "\",\"clock_id\":\"monotonic\",\"duration_ns\":";
    line += std::to_string(event.duration_ns);
    line += ",\"status\":\"";
    line += json_escape(event.status);
    line += "\",\"reason_code\":\"";
    line += json_escape(event.reason_code);
    line += "\",\"extra_json\":\"";
    line += json_escape(event.extra_json);
    line += "\"}\n";

    // One write per line: several runtime processes append to the same file and
    // O_APPEND keeps each line intact without cross-process locking.
    const std::lock_guard<std::mutex> lock(mutex_);
    const char * data = line.data();
    std::size_t remaining = line.size();
    while (remaining > 0) {
      const auto written = ::write(fd_, data, remaining);
      if (written <= 0) {
        return;
      }
      data += written;
      remaining -= static_cast<std::size_t>(written);
    }
  }

private:
  static const std::string & host_id()
  {
    static const std::string value = []() {
        char buffer[256] = {};
        if (::gethostname(buffer, sizeof(buffer) - 1) != 0) {
          return std::string("unknown");
        }
        return std::string(buffer);
      }();
    return value;
  }

  std::string path_;
  int fd_{-1};
  std::mutex mutex_;
};

}  // namespace runtime_history
