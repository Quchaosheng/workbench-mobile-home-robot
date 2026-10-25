#include "runtime_history/recorder.hpp"
#include "runtime_history/store.hpp"

#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <future>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

namespace
{

runtime_history::TaskRecord record(std::string task_id, std::string message = "completed")
{
  return {
    std::move(task_id), "arm", 1, 0, 0, 25,
    std::move(message), 123456789};
}

class TemporaryDatabase
{
public:
  TemporaryDatabase()
  : path_(std::filesystem::temp_directory_path() /
      ("runtime_history_recorder_" + std::to_string(counter_++) + ".sqlite3"))
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

TEST(RecorderTest, RejectsWhenQueueIsFullAndStopDrainsAcceptedRecords)
{
  std::mutex mutex;
  std::condition_variable condition;
  bool writer_started{};
  bool release_writer{};
  std::vector<std::string> written;
  runtime_history::Recorder recorder(
    [&](const runtime_history::TaskRecord & value) {
      std::unique_lock<std::mutex> lock(mutex);
      writer_started = true;
      condition.notify_all();
      condition.wait(lock, [&]() {return release_writer;});
      written.push_back(value.task_id);
      return true;
    }, 2);

  ASSERT_TRUE(recorder.enqueue(record("one")));
  {
    std::unique_lock<std::mutex> lock(mutex);
    ASSERT_TRUE(condition.wait_for(lock, std::chrono::seconds(2), [&]() {return writer_started;}));
  }
  EXPECT_TRUE(recorder.enqueue(record("two")));
  EXPECT_TRUE(recorder.enqueue(record("three")));
  EXPECT_FALSE(recorder.enqueue(record("four")));

  {
    const std::lock_guard<std::mutex> lock(mutex);
    release_writer = true;
  }
  condition.notify_all();
  recorder.stop();

  EXPECT_EQ(written, (std::vector<std::string>{"one", "two", "three"}));
  EXPECT_EQ(recorder.persisted(), 3U);
  EXPECT_EQ(recorder.write_errors(), 0U);
  EXPECT_FALSE(recorder.enqueue(record("after-stop")));
}

TEST(RecorderTest, CountsStoreDuplicatesAsSuccessAndConflictsAsErrors)
{
  TemporaryDatabase database;
  runtime_history::Store store(database.path());
  runtime_history::Recorder recorder(
    [&](const runtime_history::TaskRecord & value) {return store.insert(value);});
  const auto original = record("same");
  const auto conflict = record("same", "different");

  ASSERT_TRUE(recorder.enqueue(original));
  ASSERT_TRUE(recorder.enqueue(original));
  ASSERT_TRUE(recorder.enqueue(conflict));
  recorder.stop();

  EXPECT_EQ(recorder.persisted(), 2U);
  EXPECT_EQ(recorder.write_errors(), 1U);
  EXPECT_EQ(store.stats().sample_count, 1U);
  ASSERT_TRUE(store.get("same").has_value());
  EXPECT_EQ(store.get("same")->message, "completed");
}

TEST(RecorderTest, ConcurrentStopCallsJoinWorkerOnceWithoutThrowing)
{
  std::mutex mutex;
  std::condition_variable condition;
  bool writer_started{};
  bool release_writer{};
  runtime_history::Recorder recorder(
    [&](const runtime_history::TaskRecord &) {
      std::unique_lock<std::mutex> lock(mutex);
      writer_started = true;
      condition.notify_all();
      condition.wait(lock, [&]() {return release_writer;});
      return true;
    });
  ASSERT_TRUE(recorder.enqueue(record("one")));
  {
    std::unique_lock<std::mutex> lock(mutex);
    ASSERT_TRUE(condition.wait_for(lock, std::chrono::seconds(2), [&]() {return writer_started;}));
  }

  std::promise<void> start;
  const auto ready = start.get_future().share();
  auto first = std::async(std::launch::async, [&]() {ready.wait(); recorder.stop();});
  auto second = std::async(std::launch::async, [&]() {ready.wait(); recorder.stop();});
  start.set_value();
  EXPECT_EQ(first.wait_for(std::chrono::milliseconds(50)), std::future_status::timeout);
  EXPECT_EQ(second.wait_for(std::chrono::milliseconds(50)), std::future_status::timeout);
  {
    const std::lock_guard<std::mutex> lock(mutex);
    release_writer = true;
  }
  condition.notify_all();

  EXPECT_NO_THROW(first.get());
  EXPECT_NO_THROW(second.get());
  EXPECT_EQ(recorder.persisted(), 1U);
}

TEST(RecorderTest, StopFromWriteCallbackDefersJoinToExternalCaller)
{
  runtime_history::Recorder * recorder_pointer{};
  bool callback_returned{};
  runtime_history::Recorder recorder(
    [&](const runtime_history::TaskRecord &) {
      recorder_pointer->stop();
      callback_returned = true;
      return true;
    });
  recorder_pointer = &recorder;

  ASSERT_TRUE(recorder.enqueue(record("self-stop")));
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
  while (recorder.persisted() + recorder.write_errors() == 0 &&
    std::chrono::steady_clock::now() < deadline)
  {
    std::this_thread::yield();
  }
  recorder.stop();

  EXPECT_TRUE(callback_returned);
  EXPECT_EQ(recorder.persisted(), 1U);
  EXPECT_EQ(recorder.write_errors(), 0U);
}

TEST(RecorderTest, OverlappingExternalAndCallbackStopDoesNotDeadlock)
{
  std::mutex mutex;
  std::condition_variable condition;
  bool callback_started{};
  bool allow_callback_stop{};
  bool callback_returned{};
  bool external_stop_called{};
  bool external_stop_returned{};
  runtime_history::Recorder * recorder{};
  recorder = new runtime_history::Recorder(
    [&](const runtime_history::TaskRecord &) {
      {
        std::unique_lock<std::mutex> lock(mutex);
        callback_started = true;
        condition.notify_all();
        condition.wait(lock, [&]() {return allow_callback_stop;});
      }
      recorder->stop();
      {
        const std::lock_guard<std::mutex> lock(mutex);
        callback_returned = true;
      }
      condition.notify_all();
      return true;
    });
  ASSERT_TRUE(recorder->enqueue(record("overlap")));
  {
    std::unique_lock<std::mutex> lock(mutex);
    ASSERT_TRUE(condition.wait_for(lock, std::chrono::seconds(2), [&]() {
        return callback_started;
        }));
  }

  std::thread external_stop([&]() {
      {
        const std::lock_guard<std::mutex> lock(mutex);
        external_stop_called = true;
      }
      condition.notify_all();
      recorder->stop();
      {
        const std::lock_guard<std::mutex> lock(mutex);
        external_stop_returned = true;
      }
      condition.notify_all();
    });
  {
    std::unique_lock<std::mutex> lock(mutex);
    ASSERT_TRUE(
      condition.wait_for(lock, std::chrono::seconds(2), [&]() {return external_stop_called;}));
    EXPECT_FALSE(
      condition.wait_for(
        lock, std::chrono::milliseconds(100), [&]() {return external_stop_returned;}));
    allow_callback_stop = true;
  }
  condition.notify_all();

  bool completed;
  {
    std::unique_lock<std::mutex> lock(mutex);
    completed = condition.wait_for(
      lock, std::chrono::seconds(1),
      [&]() {return callback_returned && external_stop_returned;});
  }
  if (!completed) {
    external_stop.detach();
    ADD_FAILURE() << "overlapping external and callback stop deadlocked";
    return;
  }
  external_stop.join();
  EXPECT_EQ(recorder->persisted(), 1U);
  delete recorder;
}

}  // namespace
