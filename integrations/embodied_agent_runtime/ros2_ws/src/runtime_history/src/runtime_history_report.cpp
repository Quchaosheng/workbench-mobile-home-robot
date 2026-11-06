#include "runtime_history/store.hpp"

#include "report_json.hpp"

#include <exception>
#include <iostream>
#include <string>

namespace
{

void print_task(runtime_history::Store & store, const std::string & task_id)
{
  const auto record = store.get(task_id);
  if (!record.has_value()) {
    std::cout << "{\"found\":false,\"task_id\":" << runtime_history::json_quote(task_id) << "}\n";
    return;
  }
  std::cout << "{\"found\":true,\"task_id\":" << runtime_history::json_quote(record->task_id)
            << ",\"target_id\":" << runtime_history::json_quote(record->target_id)
            << ",\"action_status\":" << static_cast<unsigned>(record->action_status)
            << ",\"outcome\":" << static_cast<unsigned>(record->outcome)
            << ",\"error_code\":" << record->error_code
            << ",\"duration_ms\":" << record->duration_ms
            << ",\"message\":" << runtime_history::json_quote(record->message)
            << ",\"completed_at_ns\":" << record->completed_at_ns << "}\n";
}

void print_stats(runtime_history::Store & store)
{
  const auto stats = store.stats();
  std::cout << "{\"has_data\":" << (stats.has_data ? "true" : "false")
            << ",\"sample_count\":" << stats.sample_count
            << ",\"outcome_counts\":[" << stats.outcome_counts[0] << ','
            << stats.outcome_counts[1] << ',' << stats.outcome_counts[2] << ','
            << stats.outcome_counts[3] << ']'
            << ",\"p50_ms\":" << stats.p50_ms
            << ",\"p95_ms\":" << stats.p95_ms
            << ",\"p99_ms\":" << stats.p99_ms
            << ",\"max_ms\":" << stats.max_ms << "}\n";
}

}  // namespace

int main(int argc, char ** argv)
{
  if (argc < 4 || std::string(argv[1]) != "--db") {
    std::cout << "{\"error\":\"usage: --db PATH (--task TASK_ID | --stats)\"}\n";
    return 2;
  }
  const std::string mode = argv[3];
  if ((mode == "--stats" && argc != 4) || (mode == "--task" && argc != 5) ||
    (mode != "--stats" && mode != "--task"))
  {
    std::cout << "{\"error\":\"usage: --db PATH (--task TASK_ID | --stats)\"}\n";
    return 2;
  }
  try {
    runtime_history::Store store(argv[2]);
    if (mode == "--stats") {
      print_stats(store);
    } else {
      print_task(store, argv[4]);
    }
  } catch (const std::exception & error) {
    std::cout << "{\"error\":" << runtime_history::json_quote(error.what()) << "}\n";
    return 1;
  }
  return 0;
}
