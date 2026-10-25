#include "task_orchestrator/ready_state.hpp"

namespace task_orchestrator
{

void ReadyState::update(const bool ready, const SteadyTime received_at)
{
  std::lock_guard<std::mutex> lock(mutex_);
  ready_ = ready;
  received_at_ = received_at;
}

bool ReadyState::usable(
  const SteadyTime now, const std::chrono::milliseconds stale_after) const
{
  std::lock_guard<std::mutex> lock(mutex_);
  return ready_ && received_at_ && now - *received_at_ <= stale_after;
}

}  // namespace task_orchestrator
