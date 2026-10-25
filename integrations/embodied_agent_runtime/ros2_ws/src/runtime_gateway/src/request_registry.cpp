#include "runtime_gateway/request_registry.hpp"

#include <algorithm>
#include <stdexcept>

namespace runtime_gateway
{
namespace
{

bool is_terminal(const RequestRecord & record)
{
  return record.state == "COMPLETED" || record.state == "CANCELED" ||
         record.state == "SAFE_STOP" || record.state == "DEVICE_FAULT" ||
         record.state == "ABORTED" || record.state == "REJECTED";
}

}  // namespace

std::string fingerprint(const RequestRecord & record)
{
  // Length-prefixed fields so no field value can be confused with a separator.
  std::string result;
  const auto append = [&result](const std::string & value) {
      result += std::to_string(value.size());
      result += ':';
      result += value;
    };
  append(record.request_id);
  append(record.task_id);
  append(record.target_id);
  append(record.workflow_id);
  append(std::to_string(record.timeout_ms));
  return result;
}

RequestRegistry::RequestRegistry(const std::size_t capacity)
: capacity_(capacity)
{
  if (capacity_ == 0) {
    throw std::invalid_argument("request registry capacity must be positive");
  }
}

InsertResult RequestRegistry::insert(const RequestRecord & record)
{
  if (record.request_id.empty() || record.task_id.empty() || record.target_id.empty()) {
    return InsertResult::INVALID;
  }
  const std::lock_guard<std::mutex> lock(mutex_);
  const auto existing = records_.find(record.request_id);
  if (existing != records_.end()) {
    return fingerprint(existing->second) == fingerprint(record) ?
           InsertResult::DUPLICATE : InsertResult::CONFLICT;
  }
  for (const auto & [request_id, value] : records_) {
    (void)request_id;
    if (value.task_id == record.task_id) {
      return InsertResult::CONFLICT;
    }
  }
  if (records_.size() >= capacity_) {
    const auto evictable = std::find_if(
      insertion_order_.begin(), insertion_order_.end(), [this](const std::string & request_id) {
        const auto value = records_.find(request_id);
        return value != records_.end() && is_terminal(value->second);
      });
    if (evictable == insertion_order_.end()) {
      return InsertResult::CAPACITY;
    }
    records_.erase(*evictable);
    insertion_order_.erase(evictable);
  }
  records_.emplace(record.request_id, record);
  insertion_order_.push_back(record.request_id);
  return InsertResult::INSERTED;
}

std::optional<RequestRecord> RequestRegistry::get_by_request_id(
  const std::string & request_id) const
{
  const std::lock_guard<std::mutex> lock(mutex_);
  const auto value = records_.find(request_id);
  return value == records_.end() ? std::nullopt : std::optional<RequestRecord>{value->second};
}

std::optional<RequestRecord> RequestRegistry::get_by_task_id(const std::string & task_id) const
{
  const std::lock_guard<std::mutex> lock(mutex_);
  for (const auto & [request_id, record] : records_) {
    (void)request_id;
    if (record.task_id == task_id) {
      return record;
    }
  }
  return std::nullopt;
}

bool RequestRegistry::update_terminal(
  const std::string & request_id, const std::string & state, const std::uint8_t outcome,
  const std::uint16_t error_code, const std::string & message)
{
  const std::lock_guard<std::mutex> lock(mutex_);
  const auto value = records_.find(request_id);
  if (value == records_.end()) {
    return false;
  }
  value->second.state = state;
  value->second.outcome = outcome;
  value->second.error_code = error_code;
  value->second.message = message;
  return true;
}

void RequestRegistry::clear()
{
  const std::lock_guard<std::mutex> lock(mutex_);
  records_.clear();
  insertion_order_.clear();
}

std::size_t RequestRegistry::size() const
{
  const std::lock_guard<std::mutex> lock(mutex_);
  return records_.size();
}

}  // namespace runtime_gateway
