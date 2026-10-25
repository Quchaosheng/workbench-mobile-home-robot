#include "runtime_gateway/request_registry.hpp"

#include <algorithm>
#include <atomic>
#include <thread>
#include <vector>

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

TEST(RequestRegistryTest, RejectsEmptyIdentifiers)
{
  runtime_gateway::RequestRegistry registry;
  EXPECT_EQ(registry.insert(record("", "task-1")), runtime_gateway::InsertResult::INVALID);
  EXPECT_EQ(registry.insert(record("request-1", "")), runtime_gateway::InsertResult::INVALID);
  EXPECT_EQ(
    registry.insert(record("request-1", "task-1", "")),
    runtime_gateway::InsertResult::INVALID);
}

TEST(RequestRegistryTest, RejectsNewRecordsAfterCapacityButKeepsDuplicateLookup)
{
  runtime_gateway::RequestRegistry registry(1);
  const auto first = record();
  ASSERT_EQ(registry.insert(first), runtime_gateway::InsertResult::INSERTED);
  EXPECT_EQ(registry.insert(first), runtime_gateway::InsertResult::DUPLICATE);
  EXPECT_EQ(
    registry.insert(record("request-2", "task-2")), runtime_gateway::InsertResult::CAPACITY);
}

TEST(RequestRegistryTest, EvictsOldestTerminalRecordAtCapacity)
{
  runtime_gateway::RequestRegistry registry(2);
  ASSERT_EQ(registry.insert(record("request-1", "task-1")),
    runtime_gateway::InsertResult::INSERTED);
  ASSERT_TRUE(registry.update_terminal("request-1", "COMPLETED", 0, 0, "done"));
  ASSERT_EQ(registry.insert(record("request-2", "task-2")),
    runtime_gateway::InsertResult::INSERTED);

  EXPECT_EQ(registry.insert(record("request-3", "task-3")),
    runtime_gateway::InsertResult::INSERTED);
  EXPECT_FALSE(registry.get_by_request_id("request-1").has_value());
  EXPECT_TRUE(registry.get_by_request_id("request-2").has_value());
  EXPECT_TRUE(registry.get_by_request_id("request-3").has_value());
  EXPECT_EQ(registry.size(), 2U);
}

TEST(RequestRegistryTest, InsertsAndReturnsFirstRequest)
{
  runtime_gateway::RequestRegistry registry;
  const auto expected = record();
  ASSERT_EQ(registry.insert(expected), runtime_gateway::InsertResult::INSERTED);
  ASSERT_TRUE(registry.get_by_request_id(expected.request_id).has_value());
  EXPECT_EQ(registry.get_by_request_id(expected.request_id)->task_id, expected.task_id);
}

TEST(RequestRegistryTest, DistinguishesExactAndConflictingDuplicates)
{
  runtime_gateway::RequestRegistry registry;
  const auto expected = record();
  ASSERT_EQ(registry.insert(expected), runtime_gateway::InsertResult::INSERTED);
  EXPECT_EQ(registry.insert(expected), runtime_gateway::InsertResult::DUPLICATE);
  auto conflicting = expected;
  conflicting.task_id = "task-2";
  EXPECT_EQ(registry.insert(conflicting), runtime_gateway::InsertResult::CONFLICT);
  EXPECT_EQ(registry.size(), 1U);
}

TEST(RequestRegistryTest, RejectsDifferentRequestForExistingTaskId)
{
  runtime_gateway::RequestRegistry registry;
  ASSERT_EQ(
    registry.insert(record("request-1", "task-1")),
    runtime_gateway::InsertResult::INSERTED);
  EXPECT_EQ(
    registry.insert(record("request-2", "task-1")),
    runtime_gateway::InsertResult::CONFLICT);
  ASSERT_TRUE(registry.get_by_task_id("task-1").has_value());
  EXPECT_EQ(registry.get_by_task_id("task-1")->request_id, "request-1");
}

TEST(RequestRegistryTest, TreatsDifferentTargetAsConflictingRequest)
{
  runtime_gateway::RequestRegistry registry;
  ASSERT_EQ(registry.insert(record()), runtime_gateway::InsertResult::INSERTED);
  EXPECT_EQ(
    registry.insert(record("request-1", "task-1", "home")),
    runtime_gateway::InsertResult::CONFLICT);
  EXPECT_EQ(registry.size(), 1U);
}

TEST(RequestRegistryTest, MakesTerminalUpdateVisibleByBothIdentifiers)
{
  runtime_gateway::RequestRegistry registry;
  ASSERT_EQ(registry.insert(record()), runtime_gateway::InsertResult::INSERTED);
  ASSERT_TRUE(registry.update_terminal("request-1", "COMPLETED", 0, 0, "done"));
  const auto by_request = registry.get_by_request_id("request-1");
  const auto by_task = registry.get_by_task_id("task-1");
  ASSERT_TRUE(by_request.has_value());
  ASSERT_TRUE(by_task.has_value());
  EXPECT_EQ(by_request->state, "COMPLETED");
  EXPECT_EQ(by_task->message, "done");
  EXPECT_FALSE(registry.update_terminal("missing", "FAILED", 2, 1, "missing"));
}

TEST(RequestRegistryTest, TreatsDifferentTimeoutAsConflictingRequest)
{
  runtime_gateway::RequestRegistry registry;
  ASSERT_EQ(registry.insert(record()), runtime_gateway::InsertResult::INSERTED);
  // Same request_id and payload but a different valid timeout is a conflict,
  // because ExecuteWorkflow.allowed_duration would differ.
  EXPECT_EQ(
    registry.insert(record("request-1", "task-1", "dock_a", 5000)),
    runtime_gateway::InsertResult::CONFLICT);
  EXPECT_EQ(registry.size(), 1U);
}

TEST(RequestRegistryTest, TreatsSemanticFieldChangeAsConflictAndIgnoresTransportFields)
{
  runtime_gateway::RequestRegistry registry;
  auto original = record();
  ASSERT_EQ(registry.insert(original), runtime_gateway::InsertResult::INSERTED);

  for (const auto & changed : {
      record("request-1", "task-2"), record("request-1", "task-1", "home"),
      record("request-1", "task-1", "dock_a", 1)})
  {
    EXPECT_EQ(registry.insert(changed), runtime_gateway::InsertResult::CONFLICT);
  }
  auto other_workflow = record();
  other_workflow.workflow_id = "ready_then_task";
  EXPECT_EQ(registry.insert(other_workflow), runtime_gateway::InsertResult::CONFLICT);

  // Terminal result fields are transport-only and must not break a retry.
  original.state = "COMPLETED";
  original.outcome = 1;
  original.error_code = 302;
  original.message = "done";
  EXPECT_EQ(registry.insert(original), runtime_gateway::InsertResult::DUPLICATE);
}

TEST(RequestRegistryTest, TerminalRetryStaysDuplicateAndIsNotRedispatched)
{
  runtime_gateway::RequestRegistry registry(1);
  const auto original = record();
  ASSERT_EQ(registry.insert(original), runtime_gateway::InsertResult::INSERTED);
  ASSERT_TRUE(registry.update_terminal("request-1", "COMPLETED", 0, 0, "done"));

  EXPECT_EQ(registry.insert(original), runtime_gateway::InsertResult::DUPLICATE);
  EXPECT_EQ(
    registry.insert(record("request-1", "task-1", "dock_a", 4000)),
    runtime_gateway::InsertResult::CONFLICT);
  EXPECT_EQ(registry.size(), 1U);
}

TEST(RequestRegistryTest, SerializesConcurrentIdenticalAndConflictingInserts)
{
  runtime_gateway::RequestRegistry registry;
  constexpr std::size_t thread_count = 16;
  std::vector<std::thread> threads;
  std::vector<runtime_gateway::InsertResult> identical(thread_count);
  std::vector<runtime_gateway::InsertResult> conflicting(thread_count);
  std::atomic<std::size_t> ready{};
  std::atomic<bool> start{};

  for (std::size_t i = 0; i < thread_count; ++i) {
    threads.emplace_back([&, i]() {
        ++ready;
        while (!start.load()) {
          std::this_thread::yield();
        }
        identical[i] = registry.insert(record());
        conflicting[i] = registry.insert(record("request-1", "task-1", "dock_a", 9000));
    });
  }
  while (ready.load() != thread_count) {
    std::this_thread::yield();
  }
  start = true;
  for (auto & thread : threads) {
    thread.join();
  }

  const auto count = [](const std::vector<runtime_gateway::InsertResult> & results,
    const runtime_gateway::InsertResult value) {
      return std::count(results.begin(), results.end(), value);
    };
  EXPECT_EQ(count(identical, runtime_gateway::InsertResult::INSERTED), 1);
  EXPECT_EQ(
    count(identical, runtime_gateway::InsertResult::DUPLICATE), thread_count - 1);
  EXPECT_EQ(count(conflicting, runtime_gateway::InsertResult::CONFLICT), thread_count);
  EXPECT_EQ(registry.size(), 1U);
}

TEST(RequestRegistryTest, ClearsRecordsForShutdown)
{
  runtime_gateway::RequestRegistry registry;
  ASSERT_EQ(registry.insert(record()), runtime_gateway::InsertResult::INSERTED);
  registry.clear();
  EXPECT_EQ(registry.size(), 0U);
  EXPECT_FALSE(registry.get_by_request_id("request-1").has_value());
}

}  // namespace
