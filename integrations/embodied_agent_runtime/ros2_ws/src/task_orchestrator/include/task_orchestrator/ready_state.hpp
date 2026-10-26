#pragma once

#include <chrono>
#include <mutex>
#include <optional>

namespace task_orchestrator
{

using SteadyTime = std::chrono::steady_clock::time_point;

class ReadyState
{
public:
  void update(bool ready, SteadyTime received_at);
  bool usable(SteadyTime now, std::chrono::milliseconds stale_after) const;

private:
  mutable std::mutex mutex_;
  std::optional<SteadyTime> received_at_;
  bool ready_{false};
};

}  // namespace task_orchestrator
