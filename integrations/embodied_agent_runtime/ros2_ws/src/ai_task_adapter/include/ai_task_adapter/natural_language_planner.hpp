#pragma once

#include <builtin_interfaces/msg/duration.hpp>

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace ai_task_adapter
{

struct PlannedTask
{
  std::string task_id;
  std::string target_id;
  builtin_interfaces::msg::Duration allowed_duration;
};

enum class PlanError
{
  kNone,
  kEmptyRequest,
  kInvalidDuration,
  kUnknownTarget,
  kAmbiguousTarget
};

class NaturalLanguagePlanner
{
public:
  explicit NaturalLanguagePlanner(std::vector<std::string> allowed_targets);

  std::optional<PlannedTask> plan(
    const std::string & request,
    const builtin_interfaces::msg::Duration & allowed_duration,
    const std::string & task_id,
    PlanError & error) const;

private:
  std::vector<std::string> allowed_targets_;
};

std::string_view to_string(PlanError error);
std::uint16_t error_code(PlanError error);

}
