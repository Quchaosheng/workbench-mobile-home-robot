#include "runtime_gateway/durable_idempotency.hpp"

#include "runtime_history/store.hpp"

#include <chrono>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>

#include <sqlite3.h>
#include <gtest/gtest.h>

namespace
{

runtime_gateway::RequestRecord record(
  std::string request_id = "request-1", std::string task_id = "task-1",
  std::string target_id = "dock_a", std::uint64_t timeout_ms = 3000)
{
  return {std::move(request_id), std::move(task_id), std::move(target_id), "single_task",
    timeout_ms, "SUBMITTING", 0, 0, ""};
}

class DurableIdempotencyTest : public testing::Test
{
protected:
  void SetUp() override
  {
    path_ = (std::filesystem::temp_directory_path() /
      ("runtime_gateway_durable_" + std::to_string(counter_++) + ".sqlite3")).string();
    remove_files();
    store_ = std::make_unique<runtime_history::Store>(path_);
  }

  void TearDown() override
  {
    store_.reset();
    remove_files();
  }

  void remove_files()
  {
    std::filesystem::remove(path_);
    std::filesystem::remove(path_ + "-wal");
    std::filesystem::remove(path_ + "-shm");
  }

  static std::int64_t now() {return 1000000000;}

  static inline unsigned counter_{};
  std::string path_;
  std::unique_ptr<runtime_history::Store> store_;
};

TEST_F(DurableIdempotencyTest, AdmitsFirstRequestAndReplaysCompletedResultAfterRestart)
{
  const auto value = record();
  {
    runtime_gateway::DurableIdempotency ledger(*store_);
    EXPECT_EQ(
      ledger.admit(value, now()).decision,
      runtime_gateway::IdempotencyDecision::PROCEED);
    ASSERT_TRUE(ledger.complete(value, "COMPLETED", 0, 0, "done", now()));
  }

  // Restart: new Store and ledger on the same file must not allow a dispatch.
  store_.reset();
  store_ = std::make_unique<runtime_history::Store>(path_);
  runtime_gateway::DurableIdempotency ledger(*store_);
  const auto replay = ledger.admit(value, now());

  EXPECT_EQ(replay.decision, runtime_gateway::IdempotencyDecision::REPLAY);
  EXPECT_EQ(replay.state, "COMPLETED");
  EXPECT_EQ(replay.message, "done");
  EXPECT_EQ(replay.outcome, 0);
}

TEST_F(DurableIdempotencyTest, ChangedPayloadConflictsAfterRestart)
{
  {
    runtime_gateway::DurableIdempotency ledger(*store_);
    ASSERT_EQ(
      ledger.admit(record(), now()).decision,
      runtime_gateway::IdempotencyDecision::PROCEED);
    ASSERT_TRUE(ledger.complete(record(), "COMPLETED", 0, 0, "done", now()));
  }
  store_.reset();
  store_ = std::make_unique<runtime_history::Store>(path_);
  runtime_gateway::DurableIdempotency ledger(*store_);

  // Same request_id, different timeout: still a conflict, still no dispatch.
  EXPECT_EQ(
    ledger.admit(record("request-1", "task-1", "dock_a", 9999), now()).decision,
    runtime_gateway::IdempotencyDecision::CONFLICT);
  EXPECT_EQ(
    ledger.admit(record("request-1", "task-2"), now()).decision,
    runtime_gateway::IdempotencyDecision::CONFLICT);
  EXPECT_EQ(
    ledger.lookup("request-1")->state, "COMPLETED");
}

TEST_F(DurableIdempotencyTest, CrashBetweenAdmissionAndDispatchIsUncertainNotSafeToRedispatch)
{
  const auto value = record();
  {
    runtime_gateway::DurableIdempotency ledger(*store_);
    // admit() persists ADMITTED before the Action goal is sent; simulate a
    // crash before any terminal result is written.
    ASSERT_EQ(
      ledger.admit(value, now()).decision,
      runtime_gateway::IdempotencyDecision::PROCEED);
  }
  store_.reset();
  store_ = std::make_unique<runtime_history::Store>(path_);
  runtime_gateway::DurableIdempotency ledger(*store_);
  const auto retry = ledger.admit(value, now());

  EXPECT_EQ(retry.decision, runtime_gateway::IdempotencyDecision::UNCERTAIN);
  EXPECT_EQ(retry.state, "ADMITTED");

  // Only an explicit resolution clears it; a plain retry never becomes REPLAY.
  EXPECT_FALSE(ledger.resolve_uncertain(value, "RUNNING", 0, 0, "not terminal", now()));
  EXPECT_EQ(
    ledger.admit(value, now()).decision,
    runtime_gateway::IdempotencyDecision::UNCERTAIN);

  ASSERT_TRUE(ledger.resolve_uncertain(value, "ABORTED", 3, 500, "resolved by operator", now()));
  EXPECT_EQ(
    ledger.admit(value, now()).decision,
    runtime_gateway::IdempotencyDecision::REPLAY);
}

TEST_F(DurableIdempotencyTest, StorageFailureFailsClosedInsteadOfAllowingDispatch)
{
  runtime_gateway::DurableIdempotency ledger(*store_);
  sqlite3 * db{};
  ASSERT_EQ(sqlite3_open(path_.c_str(), &db), SQLITE_OK);
  ASSERT_EQ(
    sqlite3_exec(db, "DROP TABLE request_idempotency", nullptr, nullptr, nullptr), SQLITE_OK);
  sqlite3_close(db);

  // A broken ledger must not be read as "never seen this request".
  const auto admission = ledger.admit(record(), now());
  EXPECT_EQ(admission.decision, runtime_gateway::IdempotencyDecision::UNAVAILABLE);
  EXPECT_EQ(admission.error_code, 503);
  EXPECT_FALSE(ledger.complete(record(), "COMPLETED", 0, 0, "done", now()));
  EXPECT_EQ(ledger.prune(now()), 0U);
  EXPECT_THROW(ledger.lookup("request-1"), std::runtime_error);
}

TEST_F(DurableIdempotencyTest, ExpiredEntriesLeaveTheRetryHorizonAfterRetention)
{
  runtime_gateway::DurableIdempotency ledger(*store_, 1);
  ASSERT_EQ(
    ledger.admit(record("request-1"), now()).decision,
    runtime_gateway::IdempotencyDecision::PROCEED);
  ASSERT_TRUE(ledger.complete(record("request-1"), "COMPLETED", 0, 0, "done", now()));

  // Beyond the 24h horizon a retry is a fresh request, not a replay.
  const std::int64_t far_future = now() + 48LL * 60 * 60 * 1000 * 1000 * 1000;
  EXPECT_EQ(ledger.admit(record("request-2"), far_future).decision,
    runtime_gateway::IdempotencyDecision::PROCEED);
  EXPECT_FALSE(ledger.lookup("request-1").has_value());
}

TEST_F(DurableIdempotencyTest, CapacityExhaustionPrunesExpiredRowsAndAdmitsNewRequest)
{
  runtime_gateway::DurableIdempotency ledger(*store_, 2);
  for (int i = 0; i < 2; ++i) {
    const auto value = record("request-" + std::to_string(i));
    ASSERT_EQ(ledger.admit(value, now()).decision,
      runtime_gateway::IdempotencyDecision::PROCEED);
    ASSERT_TRUE(ledger.complete(value, "COMPLETED", 0, 0, "done", now()));
  }

  // Both rows are past the horizon, so the bounded ledger reclaims them
  // instead of growing without bound.
  const std::int64_t far_future = now() + 48LL * 60 * 60 * 1000 * 1000 * 1000;
  EXPECT_EQ(
    ledger.admit(record("request-new"), far_future).decision,
    runtime_gateway::IdempotencyDecision::PROCEED);
  EXPECT_FALSE(ledger.lookup("request-0").has_value());
  EXPECT_FALSE(ledger.lookup("request-1").has_value());
  // Unrelated prune later reports zero removals, proving the ledger shrank.
  EXPECT_EQ(ledger.prune(far_future), 0U);
}

TEST_F(DurableIdempotencyTest, RejectsZeroCapacityAndEmptyIdentity)
{
  EXPECT_THROW(runtime_gateway::DurableIdempotency ledger(*store_, 0), std::invalid_argument);

  runtime_gateway::DurableIdempotency ledger(*store_);
  auto invalid = record();
  invalid.request_id.clear();
  EXPECT_EQ(
    ledger.admit(invalid, now()).decision,
    runtime_gateway::IdempotencyDecision::UNAVAILABLE);
}

TEST_F(DurableIdempotencyTest, TerminalAndUncertainStatesAreClassifiedConsistently)
{
  for (const char * state : {"COMPLETED", "CANCELED", "SAFE_STOP", "DEVICE_FAULT",
      "ABORTED", "REJECTED"})
  {
    EXPECT_TRUE(runtime_gateway::is_terminal_state(state)) << state;
    EXPECT_FALSE(runtime_gateway::is_uncertain_state(state)) << state;
  }
  // The device may still be running after these, so they are never replayed.
  for (const char * state : {"ADMITTED", "DISPATCHING", "UNCERTAIN", "RUNNING",
      "CANCEL_FAILED", "TIMED_OUT", "CANCELING"})
  {
    EXPECT_FALSE(runtime_gateway::is_terminal_state(state)) << state;
    EXPECT_TRUE(runtime_gateway::is_uncertain_state(state)) << state;
  }
}

}  // namespace
