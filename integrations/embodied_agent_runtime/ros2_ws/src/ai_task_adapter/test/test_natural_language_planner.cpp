#include "ai_task_adapter/natural_language_planner.hpp"

#include <gtest/gtest.h>

namespace
{

builtin_interfaces::msg::Duration one_second()
{
  builtin_interfaces::msg::Duration duration;
  duration.sec = 1;
  return duration;
}

}

TEST(NaturalLanguagePlannerTest, ExtractsOneAllowedTarget)
{
  ai_task_adapter::NaturalLanguagePlanner planner({"dock_a", "home"});
  ai_task_adapter::PlanError error;
  const auto result = planner.plan("please move to dock_a", one_second(), "ai_task_1", error);

  ASSERT_TRUE(result.has_value());
  EXPECT_EQ(error, ai_task_adapter::PlanError::kNone);
  EXPECT_EQ(result->target_id, "dock_a");
  EXPECT_EQ(result->task_id, "ai_task_1");
}

TEST(NaturalLanguagePlannerTest, RejectsUnknownAndAmbiguousTargets)
{
  ai_task_adapter::NaturalLanguagePlanner planner({"dock_a", "home"});
  ai_task_adapter::PlanError error;

  EXPECT_FALSE(planner.plan("go to charger", one_second(), "ai_task_2", error).has_value());
  EXPECT_EQ(error, ai_task_adapter::PlanError::kUnknownTarget);

  EXPECT_FALSE(
    planner.plan("go to dock_a and then home", one_second(), "ai_task_3", error).has_value());
  EXPECT_EQ(error, ai_task_adapter::PlanError::kAmbiguousTarget);
}

TEST(NaturalLanguagePlannerTest, RejectsEmptyAndInvalidDuration)
{
  ai_task_adapter::NaturalLanguagePlanner planner({"dock_a"});
  ai_task_adapter::PlanError error;
  builtin_interfaces::msg::Duration invalid_duration;

  EXPECT_FALSE(planner.plan("go to dock_a", invalid_duration, "ai_task_4", error).has_value());
  EXPECT_EQ(error, ai_task_adapter::PlanError::kInvalidDuration);
  EXPECT_FALSE(planner.plan("", one_second(), "ai_task_5", error).has_value());
  EXPECT_EQ(error, ai_task_adapter::PlanError::kEmptyRequest);
}
