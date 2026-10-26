#include "task_executor/target_validator.hpp"

#include <gtest/gtest.h>

#include <stdexcept>
#include <string>
#include <vector>

TEST(TargetValidatorTest, AcceptsConfiguredTargets)
{
  const task_executor::TargetValidator validator({"dock_a", "home"});

  EXPECT_TRUE(validator.is_known("dock_a"));
  EXPECT_TRUE(validator.is_known("home"));
}

TEST(TargetValidatorTest, RejectsUnknownAndEmptyTargets)
{
  const task_executor::TargetValidator validator({"dock_a", "home"});

  EXPECT_FALSE(validator.is_known("unknown"));
  EXPECT_FALSE(validator.is_known(""));
}

TEST(TargetValidatorTest, RemovesEmptyAndDuplicateConfigurationEntries)
{
  const task_executor::TargetValidator validator({"dock_a", "", "dock_a"});

  EXPECT_EQ(validator.size(), 1U);
}

TEST(TargetValidatorTest, RejectsConfigurationWithNoUsableTargets)
{
  const std::vector<std::string> no_entries{};
  const std::vector<std::string> one_empty{""};
  const std::vector<std::string> only_empty{"", ""};
  const std::vector<std::string> only_whitespace{"  ", "\t"};

  EXPECT_THROW((task_executor::TargetValidator{no_entries}), std::invalid_argument);
  EXPECT_THROW((task_executor::TargetValidator{one_empty}), std::invalid_argument);
  EXPECT_THROW((task_executor::TargetValidator{only_empty}), std::invalid_argument);
  EXPECT_THROW((task_executor::TargetValidator{only_whitespace}), std::invalid_argument);
}

TEST(TargetValidatorTest, AcceptsSingleTargetAfterErasingEmptyEntries)
{
  const task_executor::TargetValidator validator({"", "dock_a"});

  EXPECT_EQ(validator.size(), 1U);
  EXPECT_TRUE(validator.is_known("dock_a"));
}

TEST(TargetValidatorTest, RejectsWhitespaceOnlyLookupValues)
{
  const task_executor::TargetValidator validator({"dock_a"});

  EXPECT_FALSE(validator.is_known("  "));
  EXPECT_FALSE(validator.is_known("\t"));
}
