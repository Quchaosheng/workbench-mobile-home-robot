#include "ai_task_adapter/natural_language_planner.hpp"

#include <algorithm>
#include <cctype>
#include <utility>

namespace ai_task_adapter
{

namespace
{

bool duration_is_valid(const builtin_interfaces::msg::Duration & duration)
{
  return duration.sec >= 0 && duration.nanosec < 1000000000U &&
         (duration.sec > 0 || duration.nanosec > 0);
}

bool is_boundary_character(const char character)
{
  const auto value = static_cast<unsigned char>(character);
  return !std::isalnum(value) && character != '_';
}

bool contains_token(const std::string & text, const std::string & token)
{
  auto position = text.find(token);
  while (position != std::string::npos) {
    const auto left_is_boundary = position == 0 || is_boundary_character(text[position - 1]);
    const auto end = position + token.size();
    const auto right_is_boundary = end == text.size() || is_boundary_character(text[end]);
    if (left_is_boundary && right_is_boundary) {
      return true;
    }
    position = text.find(token, position + 1);
  }
  return false;
}

}

NaturalLanguagePlanner::NaturalLanguagePlanner(std::vector<std::string> allowed_targets)
: allowed_targets_(std::move(allowed_targets))
{
  allowed_targets_.erase(
    std::remove(allowed_targets_.begin(), allowed_targets_.end(), ""), allowed_targets_.end());
  std::sort(allowed_targets_.begin(), allowed_targets_.end());
  allowed_targets_.erase(
    std::unique(allowed_targets_.begin(), allowed_targets_.end()), allowed_targets_.end());
}

std::optional<PlannedTask> NaturalLanguagePlanner::plan(
  const std::string & request,
  const builtin_interfaces::msg::Duration & allowed_duration,
  const std::string & task_id,
  PlanError & error) const
{
  if (request.empty()) {
    error = PlanError::kEmptyRequest;
    return std::nullopt;
  }
  if (!duration_is_valid(allowed_duration)) {
    error = PlanError::kInvalidDuration;
    return std::nullopt;
  }

  std::vector<std::string> matches;
  for (const auto & target : allowed_targets_) {
    if (contains_token(request, target)) {
      matches.push_back(target);
    }
  }
  if (matches.empty()) {
    error = PlanError::kUnknownTarget;
    return std::nullopt;
  }
  if (matches.size() > 1) {
    error = PlanError::kAmbiguousTarget;
    return std::nullopt;
  }

  error = PlanError::kNone;
  return PlannedTask{task_id, matches.front(), allowed_duration};
}

std::string_view to_string(const PlanError error)
{
  switch (error) {
    case PlanError::kNone:
      return "none";
    case PlanError::kEmptyRequest:
      return "empty_request";
    case PlanError::kInvalidDuration:
      return "invalid_duration";
    case PlanError::kUnknownTarget:
      return "unknown_target";
    case PlanError::kAmbiguousTarget:
      return "ambiguous_target";
  }
  return "unknown";
}

std::uint16_t error_code(const PlanError error)
{
  switch (error) {
    case PlanError::kEmptyRequest:
      return 1001;
    case PlanError::kInvalidDuration:
      return 1002;
    case PlanError::kUnknownTarget:
      return 1003;
    case PlanError::kAmbiguousTarget:
      return 1004;
    case PlanError::kNone:
      return 0;
  }
  return 1099;
}

}
