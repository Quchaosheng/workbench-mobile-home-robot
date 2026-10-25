#pragma once

#include "runtime_history/store.hpp"

#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <thread>

namespace runtime_history
{

class Recorder
{
public:
  using Write = std::function<bool(const TaskRecord &)>;

  explicit Recorder(Write write, std::size_t capacity = 256);
  ~Recorder();

  Recorder(const Recorder &) = delete;
  Recorder & operator=(const Recorder &) = delete;

  bool enqueue(TaskRecord record);
  void stop();
  std::uint64_t persisted() const;
  std::uint64_t write_errors() const;

private:
  enum class JoinState {idle, joining, joined};

  void run();

  Write write_;
  std::size_t capacity_;
  JoinState join_state_{JoinState::idle};
  mutable std::mutex mutex_;
  std::condition_variable condition_;
  std::deque<TaskRecord> queue_;
  std::thread::id worker_id_;
  bool stopping_{};
  std::uint64_t persisted_{};
  std::uint64_t write_errors_{};
  std::thread worker_;
};

}  // namespace runtime_history
