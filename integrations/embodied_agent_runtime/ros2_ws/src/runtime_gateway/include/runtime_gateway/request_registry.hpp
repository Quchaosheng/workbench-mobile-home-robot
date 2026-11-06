#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

namespace runtime_gateway
{

struct RequestRecord
{
  std::string request_id;
  std::string task_id;
  std::string target_id;
  std::string workflow_id;
  std::uint64_t timeout_ms;
  std::string state;
  std::uint8_t outcome;
  std::uint16_t error_code;
  std::string message;
};

// Semantic identity of an execution request: the fields that change what the
// device is asked to do. Transport-only fields (state, outcome, error_code,
// message) are excluded so a retry after a terminal result still matches.
std::string fingerprint(const RequestRecord & record);

enum class InsertResult {INSERTED, DUPLICATE, CONFLICT, INVALID, CAPACITY};

class RequestRegistry
{
public:
  explicit RequestRegistry(std::size_t capacity = 4096);
  InsertResult insert(const RequestRecord & record);
  std::optional<RequestRecord> get_by_request_id(const std::string & request_id) const;
  std::optional<RequestRecord> get_by_task_id(const std::string & task_id) const;
  bool update_terminal(
    const std::string & request_id, const std::string & state, std::uint8_t outcome,
    std::uint16_t error_code, const std::string & message);
  void clear();
  std::size_t size() const;

private:
  mutable std::mutex mutex_;
  std::size_t capacity_;
  std::deque<std::string> insertion_order_;
  std::unordered_map<std::string, RequestRecord> records_;
};

}  // namespace runtime_gateway
