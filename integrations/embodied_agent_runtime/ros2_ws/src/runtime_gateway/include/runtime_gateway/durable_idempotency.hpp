#pragma once

#include "runtime_gateway/request_registry.hpp"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

namespace runtime_history
{
class Store;
}

namespace runtime_gateway
{

enum class IdempotencyDecision
{
  // No durable row: this request_id was never admitted, so dispatch is allowed.
  PROCEED,
  // Durable row matches the payload and is terminal: replay it, never dispatch.
  REPLAY,
  // Durable row exists with a different payload: reject, never dispatch.
  CONFLICT,
  // Durable row exists but the outcome is unknown (crash or in-flight): the
  // caller must resolve explicitly instead of guessing a redispatch is safe.
  UNCERTAIN,
  // The durable ledger could not be read or written: fail closed.
  UNAVAILABLE,
};

struct IdempotencyOutcome
{
  IdempotencyDecision decision;
  std::string state;
  std::uint8_t outcome;
  std::uint16_t error_code;
  std::string message;
};

// Bounded durable idempotency ledger backed by the SQLite task history store.
// The in-memory RequestRegistry stays as the fast path; this ledger is the
// authority that survives restart and in-memory eviction.
class DurableIdempotency
{
public:
  DurableIdempotency(runtime_history::Store & store, std::size_t capacity = 4096);

  // Claims request_id for the given payload. Must be called before dispatch.
  IdempotencyOutcome admit(const RequestRecord & record, std::int64_t now_ns);
  // Records a terminal result so a later retry replays it.
  bool complete(
    const RequestRecord & record, const std::string & state, std::uint8_t outcome,
    std::uint16_t error_code, const std::string & message, std::int64_t now_ns);
  bool resolve_uncertain(
    const RequestRecord & record, const std::string & state, std::uint8_t outcome,
    std::uint16_t error_code, const std::string & message, std::int64_t now_ns);
  std::optional<IdempotencyOutcome> lookup(const std::string & request_id) const;
  // Drops rows older than the retry horizon, keeping at least keep_at_least so
  // the ledger is bounded without forgetting the most recent requests.
  std::size_t prune(std::int64_t now_ns);

private:
  runtime_history::Store & store_;
  std::size_t capacity_;
};

// True when the durable state is a finished outcome that can be replayed.
bool is_terminal_state(const std::string & state);
// True when the durable state means the result is not known.
bool is_uncertain_state(const std::string & state);

}  // namespace runtime_gateway
