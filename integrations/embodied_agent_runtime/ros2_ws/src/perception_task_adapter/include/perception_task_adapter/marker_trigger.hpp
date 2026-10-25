#pragma once

#include <cstddef>
#include <optional>
#include <string>
#include <vector>

namespace perception_task_adapter
{

struct MarkerMapping
{
  int marker_id;
  std::string workflow_id;
  std::string target_id;
};

struct TriggerEvent
{
  MarkerMapping mapping;
};

class MarkerTrigger
{
public:
  MarkerTrigger(
    std::vector<MarkerMapping> mappings, std::size_t confirm_frames,
    std::size_t rearm_missing_frames);

  static std::string validate(
    const std::vector<MarkerMapping> & mappings, std::size_t confirm_frames,
    std::size_t rearm_missing_frames);

  std::optional<TriggerEvent> observe(
    const std::vector<int> & visible_ids, bool workflow_active, bool immediate = false);

  void on_terminal();

private:
  std::vector<MarkerMapping> mappings_;
  std::size_t confirm_frames_;
  std::size_t rearm_missing_frames_;
  std::optional<int> streak_id_;
  std::size_t streak_frames_{0};
  std::size_t missing_frames_{0};
  bool armed_{true};
};

}  // namespace perception_task_adapter
