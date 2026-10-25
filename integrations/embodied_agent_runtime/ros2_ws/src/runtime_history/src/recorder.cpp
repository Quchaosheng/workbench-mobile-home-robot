#include "runtime_history/recorder.hpp"

#include <utility>

namespace runtime_history
{

Recorder::Recorder(Write write, const std::size_t capacity)
: write_(std::move(write)), capacity_(capacity), worker_([this]() {run();})
{
}

Recorder::~Recorder()
{
  stop();
}

bool Recorder::enqueue(TaskRecord record)
{
  const std::lock_guard<std::mutex> lock(mutex_);
  if (stopping_ || queue_.size() >= capacity_) {
    return false;
  }
  queue_.push_back(std::move(record));
  condition_.notify_one();
  return true;
}

void Recorder::stop()
{
  std::unique_lock<std::mutex> lock(mutex_);
  stopping_ = true;
  condition_.notify_all();
  if (worker_id_ == std::this_thread::get_id()) {
    return;
  }
  condition_.wait(lock, [this]() {return join_state_ != JoinState::joining;});
  if (join_state_ == JoinState::joined) {
    return;
  }
  join_state_ = JoinState::joining;
  lock.unlock();
  worker_.join();
  lock.lock();
  join_state_ = JoinState::joined;
  lock.unlock();
  condition_.notify_all();
}

std::uint64_t Recorder::persisted() const
{
  const std::lock_guard<std::mutex> lock(mutex_);
  return persisted_;
}

std::uint64_t Recorder::write_errors() const
{
  const std::lock_guard<std::mutex> lock(mutex_);
  return write_errors_;
}

void Recorder::run()
{
  std::unique_lock<std::mutex> lock(mutex_);
  worker_id_ = std::this_thread::get_id();
  while (true) {
    condition_.wait(lock, [this]() {return stopping_ || !queue_.empty();});
    if (queue_.empty()) {
      return;
    }
    TaskRecord record = std::move(queue_.front());
    queue_.pop_front();
    lock.unlock();
    bool written{};
    try {
      written = write_(record);
    } catch (...) {
      written = false;
    }
    lock.lock();
    ++(written ? persisted_ : write_errors_);
  }
}

}  // namespace runtime_history
