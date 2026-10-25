#include "runtime_history/store.hpp"

#include <sqlite3.h>

#include <array>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

namespace
{

class TemporaryDatabase
{
public:
  TemporaryDatabase()
  : path_(std::filesystem::temp_directory_path() /
      ("runtime_history_" + std::to_string(counter_++) + ".sqlite3"))
  {
    std::filesystem::remove(path_);
  }

  ~TemporaryDatabase()
  {
    std::filesystem::remove(path_);
    std::filesystem::remove(path_.string() + "-wal");
    std::filesystem::remove(path_.string() + "-shm");
  }

  std::string path() const {return path_.string();}

private:
  static inline unsigned counter_{};
  std::filesystem::path path_;
};

runtime_history::TaskRecord record(
  std::string task_id = "task-1", std::uint64_t duration_ms = 25,
  std::uint8_t outcome = 0)
{
  return {
    std::move(task_id), "arm", 1, outcome, 302, duration_ms,
    "completed", 123456789};
}

std::string query_text(const std::string & path, const char * sql)
{
  sqlite3 * db{};
  if (sqlite3_open(path.c_str(), &db) != SQLITE_OK) {
    throw std::runtime_error("failed to open test database");
  }
  sqlite3_stmt * statement{};
  if (sqlite3_prepare_v2(db, sql, -1, &statement, nullptr) != SQLITE_OK ||
    sqlite3_step(statement) != SQLITE_ROW)
  {
    sqlite3_finalize(statement);
    sqlite3_close(db);
    throw std::runtime_error("failed to query test database");
  }
  const auto * value = sqlite3_column_text(statement, 0);
  const std::string result = value == nullptr ? "" : reinterpret_cast<const char *>(value);
  sqlite3_finalize(statement);
  sqlite3_close(db);
  return result;
}

TEST(StoreTest, CreatesExactSchema)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());

  EXPECT_EQ(
    query_text(
      database.path(),
      "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_runs'"),
    "CREATE TABLE task_runs ("
    "task_id TEXT PRIMARY KEY,"
    "target_id TEXT NOT NULL,"
    "action_status INTEGER NOT NULL,"
    "outcome INTEGER NOT NULL,"
    "error_code INTEGER NOT NULL,"
    "duration_ms INTEGER NOT NULL,"
    "message TEXT NOT NULL,"
    "completed_at_ns INTEGER NOT NULL)"
  );
}

TEST(StoreTest, EnablesWalAndBusyTimeout)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());

  EXPECT_EQ(query_text(database.path(), "PRAGMA journal_mode"), "wal");

  sqlite3 * lock{};
  ASSERT_EQ(sqlite3_open(database.path().c_str(), &lock), SQLITE_OK);
  ASSERT_EQ(sqlite3_exec(lock, "BEGIN IMMEDIATE", nullptr, nullptr, nullptr), SQLITE_OK);
  const auto started_at = std::chrono::steady_clock::now();
  EXPECT_FALSE(store.insert(record("locked")));
  const auto elapsed = std::chrono::steady_clock::now() - started_at;
  EXPECT_GE(elapsed, std::chrono::milliseconds(900));
  EXPECT_LT(elapsed, std::chrono::seconds(3));
  EXPECT_EQ(sqlite3_exec(lock, "ROLLBACK", nullptr, nullptr, nullptr), SQLITE_OK);
  sqlite3_close(lock);
}

TEST(StoreTest, InsertsAndGetsEveryField)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());
  const runtime_history::TaskRecord expected{
    "task-7", "welder", 3, 2, 65535, 9876, "operator's note", -42};

  ASSERT_TRUE(store.insert(expected));
  const auto actual = store.get(expected.task_id);

  ASSERT_TRUE(actual.has_value());
  EXPECT_EQ(actual->task_id, expected.task_id);
  EXPECT_EQ(actual->target_id, expected.target_id);
  EXPECT_EQ(actual->action_status, expected.action_status);
  EXPECT_EQ(actual->outcome, expected.outcome);
  EXPECT_EQ(actual->error_code, expected.error_code);
  EXPECT_EQ(actual->duration_ms, expected.duration_ms);
  EXPECT_EQ(actual->message, expected.message);
  EXPECT_EQ(actual->completed_at_ns, expected.completed_at_ns);
  EXPECT_FALSE(store.get("missing").has_value());
}

TEST(StoreTest, TreatsExactDuplicateAsSuccessfulNoOp)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());
  const auto expected = record();

  ASSERT_TRUE(store.insert(expected));
  EXPECT_TRUE(store.insert(expected));
  EXPECT_EQ(query_text(database.path(), "SELECT count(*) FROM task_runs"), "1");
}

TEST(StoreTest, RejectsConflictingDuplicateWithoutOverwrite)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());
  const auto expected = record();
  auto conflicting = expected;
  conflicting.message = "different";

  ASSERT_TRUE(store.insert(expected));
  EXPECT_FALSE(store.insert(conflicting));

  const auto actual = store.get(expected.task_id);
  ASSERT_TRUE(actual.has_value());
  EXPECT_EQ(actual->message, expected.message);
  EXPECT_EQ(query_text(database.path(), "SELECT count(*) FROM task_runs"), "1");
}

TEST(StoreTest, ReportsNoDataForEmptyTable)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());

  const auto stats = store.stats();

  EXPECT_FALSE(stats.has_data);
  EXPECT_EQ(stats.sample_count, 0U);
  EXPECT_EQ(stats.outcome_counts, (std::array<std::uint64_t, 4>{}));
}

TEST(StoreTest, ComputesNearestRankStatistics)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());
  const std::array<std::uint64_t, 5> durations{10, 20, 30, 40, 100};

  for (std::size_t i = 0; i < durations.size(); ++i) {
    ASSERT_TRUE(store.insert(record("task-" + std::to_string(i), durations[i], i % 4)));
  }

  const auto stats = store.stats();
  EXPECT_TRUE(stats.has_data);
  EXPECT_EQ(stats.sample_count, 5U);
  EXPECT_EQ(stats.outcome_counts, (std::array<std::uint64_t, 4>{2, 1, 1, 1}));
  EXPECT_EQ(stats.p50_ms, 30U);
  EXPECT_EQ(stats.p95_ms, 100U);
  EXPECT_EQ(stats.p99_ms, 100U);
  EXPECT_EQ(stats.max_ms, 100U);
}

TEST(StoreTest, SerializesConcurrentInsertsOnOneStore)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());
  constexpr std::size_t thread_count = 32;
  std::atomic<std::size_t> ready{};
  std::atomic<bool> start{};
  std::vector<int> results(thread_count);
  std::vector<std::thread> threads;

  for (std::size_t i = 0; i < thread_count; ++i) {
    threads.emplace_back([&, i]() {
        ++ready;
        while (!start.load()) {
          std::this_thread::yield();
        }
        results[i] = store.insert(record("concurrent-" + std::to_string(i), i));
    });
  }
  while (ready.load() != thread_count) {
    std::this_thread::yield();
  }
  start = true;
  for (auto & thread : threads) {
    thread.join();
  }

  EXPECT_EQ(results, std::vector<int>(thread_count, 1));
  EXPECT_EQ(store.stats().sample_count, thread_count);
}

TEST(StoreTest, ThrowsWhenTaskRunsSchemaIsMissing)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());
  sqlite3 * db{};
  ASSERT_EQ(sqlite3_open(database.path().c_str(), &db), SQLITE_OK);
  ASSERT_EQ(sqlite3_exec(db, "DROP TABLE task_runs", nullptr, nullptr, nullptr), SQLITE_OK);
  sqlite3_close(db);

  EXPECT_THROW(store.get("task-1"), std::runtime_error);
  EXPECT_THROW(store.stats(), std::runtime_error);
}

runtime_history::IdempotencyRecord idempotency(
  std::string request_id = "request-1", std::string fingerprint = "fp-1",
  std::string state = "COMPLETED", std::int64_t updated_at_ns = 1000)
{
  return {std::move(request_id), std::move(fingerprint), std::move(state), 0, 0, "done",
    updated_at_ns};
}

TEST(StoreTest, PersistsIdempotencyAcrossRestart)
{
  TemporaryDatabase database;
  {
    runtime_history::Store store(database.path());
    ASSERT_TRUE(store.put_idempotency(idempotency()));
  }

  // A new Store instance models a process restart on the same database file.
  runtime_history::Store reopened(database.path());
  const auto actual = reopened.get_idempotency("request-1");

  ASSERT_TRUE(actual.has_value());
  EXPECT_EQ(actual->fingerprint, "fp-1");
  EXPECT_EQ(actual->state, "COMPLETED");
  EXPECT_EQ(actual->message, "done");
  EXPECT_EQ(actual->updated_at_ns, 1000);
  EXPECT_FALSE(reopened.get_idempotency("missing").has_value());
}

TEST(StoreTest, UpdatesSameFingerprintAndRejectsChangedFingerprint)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());
  ASSERT_TRUE(store.put_idempotency(idempotency("request-1", "fp-1", "ADMITTED")));

  ASSERT_TRUE(store.put_idempotency(idempotency("request-1", "fp-1", "COMPLETED")));
  EXPECT_EQ(store.get_idempotency("request-1")->state, "COMPLETED");

  // The admitted identity is immutable: a changed payload cannot overwrite it.
  EXPECT_TRUE(store.put_idempotency(idempotency("request-1", "fp-2", "COMPLETED")));
  EXPECT_EQ(store.get_idempotency("request-1")->fingerprint, "fp-1");
  EXPECT_EQ(
    query_text(
      database.path(),
      "SELECT count(*) FROM request_idempotency WHERE request_id='request-1'"), "1");
}

TEST(StoreTest, RejectsIdempotencyWithoutIdentity)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());

  EXPECT_FALSE(store.put_idempotency(idempotency("", "fp-1")));
  EXPECT_FALSE(store.put_idempotency(idempotency("request-1", "")));
}

TEST(StoreTest, PrunesExpiredIdempotencyButKeepsRecentEntries)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());
  for (int i = 0; i < 5; ++i) {
    ASSERT_TRUE(store.put_idempotency(idempotency(
        "request-" + std::to_string(i), "fp", "COMPLETED", 100 * (i + 1))));
  }

  // Horizon 250 expires request-0 (100) and request-1 (200) only.
  EXPECT_EQ(store.prune_idempotency(250, 0), 2U);
  EXPECT_FALSE(store.get_idempotency("request-0").has_value());
  EXPECT_TRUE(store.get_idempotency("request-2").has_value());

  // The keep floor protects unexpired rows, not expired ones: a horizon above
  // every timestamp reclaims the rest even with a floor of two.
  EXPECT_EQ(store.prune_idempotency(10000, 2), 3U);
  EXPECT_FALSE(store.get_idempotency("request-4").has_value());

  // A fresh row inside the horizon survives any prune.
  ASSERT_TRUE(store.put_idempotency(idempotency("fresh", "fp", "COMPLETED", 20000)));
  EXPECT_EQ(store.prune_idempotency(10000, 0), 0U);
  EXPECT_TRUE(store.get_idempotency("fresh").has_value());
}

TEST(StoreTest, ThrowsWhenIdempotencySchemaIsMissing)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());
  sqlite3 * db{};
  ASSERT_EQ(sqlite3_open(database.path().c_str(), &db), SQLITE_OK);
  ASSERT_EQ(
    sqlite3_exec(db, "DROP TABLE request_idempotency", nullptr, nullptr, nullptr), SQLITE_OK);
  sqlite3_close(db);

  EXPECT_THROW(store.get_idempotency("request-1"), std::runtime_error);
  // Storage failure is reported, not silently treated as "never seen".
  EXPECT_FALSE(store.put_idempotency(idempotency()));
  EXPECT_EQ(store.prune_idempotency(10000, 0), 0U);
}

TEST(StoreTest, RejectsDatabaseThatCannotUseWal)
{
  EXPECT_THROW(runtime_history::Store(":memory:"), std::runtime_error);
}

}  // namespace
