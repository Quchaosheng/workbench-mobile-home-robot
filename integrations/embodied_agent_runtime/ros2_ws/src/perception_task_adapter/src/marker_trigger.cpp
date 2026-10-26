#include "perception_task_adapter/marker_trigger.hpp"

#include <algorithm>
#include <stdexcept>
#include <unordered_set>
#include <utility>

namespace perception_task_adapter
{

MarkerTrigger::MarkerTrigger(
  std::vector<MarkerMapping> mappings, std::size_t confirm_frames,
  std::size_t rearm_missing_frames)
: mappings_(std::move(mappings)),
  confirm_frames_(confirm_frames),
  rearm_missing_frames_(rearm_missing_frames)
{
  const auto error = validate(mappings_, confirm_frames_, rearm_missing_frames_);
  if (!error.empty()) {
    throw std::invalid_argument(error);
  }
}

std::string MarkerTrigger::validate(
  const std::vector<MarkerMapping> & mappings, std::size_t confirm_frames,
  std::size_t rearm_missing_frames)
{
  if (mappings.empty()) {
    return "marker mappings must not be empty";
  }
  if (confirm_frames == 0) {
    return "confirm_frames must be greater than zero";
  }
  if (rearm_missing_frames == 0) {
    return "rearm_missing_frames must be greater than zero";
  }

  std::unordered_set<int> marker_ids;
  for (const auto & mapping : mappings) {
    if (mapping.marker_id < 0 || mapping.marker_id > 49) {
      return "marker_id must be between 0 and 49";
    }
    if (!marker_ids.insert(mapping.marker_id).second) {
      return "marker_id values must be unique";
    }
    if (mapping.workflow_id.empty()) {
      return "workflow_id must not be empty";
    }
    if (mapping.target_id.empty()) {
      return "target_id must not be empty";
    }
  }

  return {};
}

std::optional<TriggerEvent> MarkerTrigger::observe(
  const std::vector<int> & visible_ids, bool workflow_active, bool immediate)
{
  if (workflow_active) {
    streak_id_.reset();
    streak_frames_ = 0;
    missing_frames_ = 0;
    return std::nullopt;
  }

  std::vector<const MarkerMapping *> visible_mappings;
  for (const auto & mapping : mappings_) {
    if (
      std::find(visible_ids.begin(), visible_ids.end(), mapping.marker_id) !=
      visible_ids.end())
    {
      visible_mappings.push_back(&mapping);
    }
  }

  if (visible_mappings.empty()) {
    streak_id_.reset();
    streak_frames_ = 0;
    if (!armed_ && ++missing_frames_ >= rearm_missing_frames_) {
      armed_ = true;
      missing_frames_ = 0;
    }
    return std::nullopt;
  }

  if (visible_mappings.size() > 1) {
    streak_id_.reset();
    streak_frames_ = 0;
    return std::nullopt;
  }

  const auto & mapping = *visible_mappings.front();
  missing_frames_ = 0;
  if (!armed_) {
    return std::nullopt;
  }

  if (streak_id_ == mapping.marker_id) {
    ++streak_frames_;
  } else {
    streak_id_ = mapping.marker_id;
    streak_frames_ = 1;
  }

  if (!immediate && streak_frames_ < confirm_frames_) {
    return std::nullopt;
  }

  armed_ = false;
  streak_id_.reset();
  streak_frames_ = 0;
  return TriggerEvent{mapping};
}

void MarkerTrigger::on_terminal()
{
  armed_ = false;
  streak_id_.reset();
  streak_frames_ = 0;
  missing_frames_ = 0;
}

}  // namespace perception_task_adapter
