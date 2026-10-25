#include "runtime_gateway/durable_idempotency.hpp"

#include "runtime_history/store.hpp"

#include <stdexcept>

namespace runtime_gateway
{
namespace
{

// Bounded retry horizon: how long a completed request stays replayable.
constexpr std::int64_t kRetryHorizonNs = 24LL * 60 * 60 * 1000 * 1000 * 1000;

constexpr char kAdmitted[] = "ADMITTED";

std::int64_t horizon_start(const std::int64_t now_ns)
{
  return now_ns - kRetryHorizonNs;
}

}  // namespace

bool is_terminal_state(const std::string & state)
{
  // Same set the in-memory registry treats as terminal. CANCEL_FAILED and
  // TIMED_OUT are deliberately absent: the device may still be running, so
  // those stay uncertain and are never auto-replayed.
  return state == "COMPLETED" || state == "CANCELED" || state == "SAFE_STOP" ||
         state == "DEVICE_FAULT" || state == "ABORTED" || state == "REJECTED";
}

bool is_uncertain_state(const std::string & state)
{
  // Anything not a known terminal outcome means the process may have died
  // mid-flight, so the result is unknown rather than safely replayable.
  return !is_terminal_state(state);
}

DurableIdempotency::DurableIdempotency(
  runtime_history::Store & store, const std::size_t capacity)
: store_(store), capacity_(capacity)
{
  if (capacity_ == 0) {
    throw std::invalid_argument("durable idempotency capacity must be positive");
  }
}

IdempotencyOutcome DurableIdempotency::admit(
  const RequestRecord & record, const std::int64_t now_ns)
{
  const std::string value = fingerprint(record);
  std::optional<runtime_history::IdempotencyRecord> existing;
  try {
    existing = store_.get_idempotency(record.request_id);
  } catch (const std::exception &) {
    // Fail closed: without the ledger we cannot prove this is not a retry.
    return {IdempotencyDecision::UNAVAILABLE, std::string{}, 0, 503,
      "durable idempotency ledger unavailable"};
  }
  if (existing && existing->fingerprint != value) {
    return {IdempotencyDecision::CONFLICT, std::string{}, 0, 409,
      "request_id conflicts with a durable request"};
  }
  if (existing && is_uncertain_state(existing->state)) {
    return {IdempotencyDecision::UNCERTAIN, existing->state, existing->outcome,
      existing->error_code,
      existing->message.empty() ? "prior dispatch outcome unknown" : existing->message};
  }
  if (existing) {
    return {IdempotencyDecision::REPLAY, existing->state, existing->outcome,
      existing->error_code, existing->message};
  }

  try {
    store_.prune_idempotency(horizon_start(now_ns), capacity_);
  } catch (const std::exception &) {
    // Retention is best effort; admission below still fails closed.
  }
  const runtime_history::IdempotencyRecord claimed{
    record.request_id, value, kAdmitted, 0, 0, "admitted", now_ns};
  if (!store_.put_idempotency(claimed)) {
    return {IdempotencyDecision::UNAVAILABLE, std::string{}, 0, 503,
      "failed to persist idempotency admission"};
  }
  return {IdempotencyDecision::PROCEED, std::string{}, 0, 0, std::string{}};
}

bool DurableIdempotency::complete(
  const RequestRecord & record, const std::string & state, const std::uint8_t outcome,
  const std::uint16_t error_code, const std::string & message, const std::int64_t now_ns)
{
  const runtime_history::IdempotencyRecord value{
    record.request_id, fingerprint(record), state, outcome, error_code, message, now_ns};
  try {
    return store_.put_idempotency(value);
  } catch (const std::exception &) {
    return false;
  }
}

bool DurableIdempotency::resolve_uncertain(
  const RequestRecord & record, const std::string & state, const std::uint8_t outcome,
  const std::uint16_t error_code, const std::string & message, const std::int64_t now_ns)
{
  // Only an explicit operator/monitor decision may clear an uncertain row;
  // a plain retry never reaches here.
  if (!is_terminal_state(state)) {
    return false;
  }
  try {
    const auto existing = store_.get_idempotency(record.request_id);
    if (!existing || !is_uncertain_state(existing->state)) {
      return false;
    }
  } catch (const std::exception &) {
    return false;
  }
  return complete(record, state, outcome, error_code, message, now_ns);
}

std::optional<IdempotencyOutcome> DurableIdempotency::lookup(
  const std::string & request_id) const
{
  const auto existing = store_.get_idempotency(request_id);
  if (!existing) {
    return std::nullopt;
  }
  if (is_uncertain_state(existing->state)) {
    return IdempotencyOutcome{IdempotencyDecision::UNCERTAIN, existing->state,
      existing->outcome, existing->error_code, existing->message};
  }
  return IdempotencyOutcome{IdempotencyDecision::REPLAY, existing->state, existing->outcome,
    existing->error_code, existing->message};
}

std::size_t DurableIdempotency::prune(const std::int64_t now_ns)
{
  try {
    return store_.prune_idempotency(horizon_start(now_ns), capacity_);
  } catch (const std::exception &) {
    return 0;
  }
}

}  // namespace runtime_gateway
