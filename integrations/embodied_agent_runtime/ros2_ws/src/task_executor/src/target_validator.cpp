#include "task_executor/target_validator.hpp"

#include <algorithm>
#include <stdexcept>
#include <cctype>

namespace task_executor
{

TargetValidator::TargetValidator(const std::vector<std::string> & targets)
{
  for (const auto & target : targets) {
    if (has_non_whitespace(target)) {
      targets_.insert(target);
    }
  }
  if (targets_.empty()) {
    throw std::invalid_argument(
      "target allowlist must contain at least one non-empty target id");
  }
}

bool TargetValidator::is_known(std::string_view target_id) const
{
  return has_non_whitespace(target_id) &&
         targets_.find(std::string(target_id)) != targets_.end();
}

bool TargetValidator::has_non_whitespace(const std::string_view value)
{
  return std::any_of(value.begin(), value.end(), [](const unsigned char character) {
             return std::isspace(character) == 0;
  });
}

std::size_t TargetValidator::size() const
{
  return targets_.size();
}

}
