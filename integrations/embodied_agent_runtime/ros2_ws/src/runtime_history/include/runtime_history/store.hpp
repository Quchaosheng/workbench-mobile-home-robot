#pragma once

#include <array>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>

struct sqlite3;

namespace runtime_history
{

struct TaskRecord
{
  std::string task_id;
  std::string target_id;
  std::uint8_t action_status;
  std::uint8_t outcome;
  std::uint16_t error_code;
  std::uint64_t duration_ms;
  std::string message;
  std::int64_t completed_at_ns;
};

struct RuntimeStats
{
  bool has_data;
  std::uint64_t sample_count;
  std::array<std::uint64_t, 4> outcome_counts;
  std::uint64_t p50_ms;
  std::uint64_t p95_ms;
  std::uint64_t p99_ms;
  std::uint64_t max_ms;
};

// Durable idempotency record for one admitted gateway request. `fingerprint`
// is the semantic identity of the execution payload, so a retry with a changed
// payload conflicts even after a restart or eviction.
struct IdempotencyRecord
{
  std::string request_id;
  std::string fingerprint;
  std::string state;
  std::uint8_t outcome;
  std::uint16_t error_code;
  std::string message;
  std::int64_t updated_at_ns;
};

class Store
{
public:
  explicit Store(const std::string & path);
  ~Store();

  Store(const Store &) = delete;
  Store & operator=(const Store &) = delete;

  bool insert(const TaskRecord & record);
  std::optional<TaskRecord> get(const std::string & task_id) const;
  RuntimeStats stats() const;

  // Idempotency rows survive restart. ADMITTED is written before dispatch so a
  // crash mid-dispatch is recoverable as uncertain instead of a silent re-run.
  bool put_idempotency(const IdempotencyRecord & record);
  std::optional<IdempotencyRecord> get_idempotency(const std::string & request_id) const;
  std::size_t prune_idempotency(std::int64_t older_than_ns, std::size_t keep_at_least);

private:
  std::optional<TaskRecord> get_unlocked(const std::string & task_id) const;

  sqlite3 * db_{};
  mutable std::mutex mutex_;
};

}  // namespace runtime_history
