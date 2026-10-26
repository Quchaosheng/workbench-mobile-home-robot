#pragma once

#include <cstddef>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

namespace task_executor
{

class TargetValidator
{
public:
  explicit TargetValidator(const std::vector<std::string> & targets);

  bool is_known(std::string_view target_id) const;
  std::size_t size() const;

private:
  static bool has_non_whitespace(std::string_view value);

  std::unordered_set<std::string> targets_;
};

}
