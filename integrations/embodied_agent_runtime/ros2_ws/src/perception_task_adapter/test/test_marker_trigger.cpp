#include "perception_task_adapter/marker_trigger.hpp"

#include <gtest/gtest.h>

#include <stdexcept>
#include <string>
#include <vector>

namespace
{

using perception_task_adapter::MarkerMapping;
using perception_task_adapter::MarkerTrigger;

const std::vector<MarkerMapping> kMappings{
  {10, "single_task", "dock_a"},
  {20, "ready_then_task", "home"},
};

TEST(MarkerTriggerValidationTest, RejectsInvalidConfiguration)
{
  EXPECT_FALSE(MarkerTrigger::validate({}, 3, 5).empty());
  EXPECT_FALSE(
    MarkerTrigger::validate(
      {{10, "single_task", "dock_a"}, {10, "other", "home"}}, 3, 5).empty());
  EXPECT_FALSE(MarkerTrigger::validate({{-1, "single_task", "dock_a"}}, 3, 5).empty());
  EXPECT_FALSE(MarkerTrigger::validate({{50, "single_task", "dock_a"}}, 3, 5).empty());
  EXPECT_FALSE(MarkerTrigger::validate({{10, "", "dock_a"}}, 3, 5).empty());
  EXPECT_FALSE(MarkerTrigger::validate({{10, "single_task", ""}}, 3, 5).empty());
  EXPECT_FALSE(MarkerTrigger::validate(kMappings, 0, 5).empty());
  EXPECT_FALSE(MarkerTrigger::validate(kMappings, 3, 0).empty());
  EXPECT_TRUE(MarkerTrigger::validate(kMappings, 3, 5).empty());
  EXPECT_THROW(MarkerTrigger({}, 3, 5), std::invalid_argument);
}

TEST(MarkerTriggerTest, TriggersAfterThreeConsecutiveFrames)
{
  MarkerTrigger trigger(kMappings, 3, 5);

  EXPECT_FALSE(trigger.observe({10}, false).has_value());
  EXPECT_FALSE(trigger.observe({10}, false).has_value());
  const auto event = trigger.observe({10}, false);

  ASSERT_TRUE(event.has_value());
  EXPECT_EQ(event->mapping.marker_id, 10);
  EXPECT_EQ(event->mapping.workflow_id, "single_task");
  EXPECT_EQ(event->mapping.target_id, "dock_a");
}

TEST(MarkerTriggerTest, PersistentVisibilityDoesNotRetrigger)
{
  MarkerTrigger trigger(kMappings, 3, 5);
  trigger.observe({10}, false);
  trigger.observe({10}, false);
  ASSERT_TRUE(trigger.observe({10}, false).has_value());

  for (int frame = 0; frame < 10; ++frame) {
    EXPECT_FALSE(trigger.observe({10}, false).has_value());
  }
}

TEST(MarkerTriggerTest, RearmsAfterFiveFramesWithoutAllowlistedMarkers)
{
  MarkerTrigger trigger(kMappings, 3, 5);
  trigger.observe({10}, false);
  trigger.observe({10}, false);
  ASSERT_TRUE(trigger.observe({10}, false).has_value());

  for (int frame = 0; frame < 4; ++frame) {
    EXPECT_FALSE(trigger.observe({}, false).has_value());
  }
  EXPECT_FALSE(trigger.observe({10}, false).has_value());
  for (int frame = 0; frame < 5; ++frame) {
    EXPECT_FALSE(trigger.observe({}, false).has_value());
  }
  EXPECT_FALSE(trigger.observe({10}, false).has_value());
  EXPECT_FALSE(trigger.observe({10}, false).has_value());
  EXPECT_TRUE(trigger.observe({10}, false).has_value());
}

TEST(MarkerTriggerTest, AmbiguousFramesClearConfirmationAndDoNotRearm)
{
  MarkerTrigger trigger(kMappings, 2, 2);
  EXPECT_FALSE(trigger.observe({10}, false).has_value());
  EXPECT_FALSE(trigger.observe({10, 20}, false).has_value());
  EXPECT_FALSE(trigger.observe({10}, false).has_value());
  ASSERT_TRUE(trigger.observe({10}, false).has_value());

  EXPECT_FALSE(trigger.observe({}, false).has_value());
  EXPECT_FALSE(trigger.observe({10, 20}, false).has_value());
  EXPECT_FALSE(trigger.observe({}, false).has_value());
  EXPECT_FALSE(trigger.observe({20}, false).has_value());
  EXPECT_TRUE(trigger.observe({20}, false).has_value());
}

TEST(MarkerTriggerTest, DirectReplacementCannotBypassRearm)
{
  MarkerTrigger trigger(kMappings, 2, 2);
  trigger.observe({10}, false);
  ASSERT_TRUE(trigger.observe({10}, false).has_value());

  for (int frame = 0; frame < 5; ++frame) {
    EXPECT_FALSE(trigger.observe({20}, false).has_value());
  }
}

TEST(MarkerTriggerTest, ActiveWorkflowSuppressesTrigger)
{
  MarkerTrigger trigger(kMappings, 2, 2);

  EXPECT_FALSE(trigger.observe({10}, true).has_value());
  EXPECT_FALSE(trigger.observe({10}, true).has_value());
  EXPECT_FALSE(trigger.observe({10}, false).has_value());
  EXPECT_TRUE(trigger.observe({10}, false).has_value());
}

TEST(MarkerTriggerTest, TerminalEventDisarmsUntilMarkerIsMissing)
{
  MarkerTrigger trigger(kMappings, 2, 2);
  trigger.observe({10}, false);
  trigger.on_terminal();

  EXPECT_FALSE(trigger.observe({10}, false).has_value());
  EXPECT_FALSE(trigger.observe({}, false).has_value());
  EXPECT_FALSE(trigger.observe({}, false).has_value());
  EXPECT_FALSE(trigger.observe({10}, false).has_value());
  EXPECT_TRUE(trigger.observe({10}, false).has_value());
}

TEST(MarkerTriggerTest, ImmediateModeTriggersOnlyOneAllowlistedMarker)
{
  MarkerTrigger allowed(kMappings, 3, 5);
  const auto event = allowed.observe({10, 99}, false, true);
  ASSERT_TRUE(event.has_value());
  EXPECT_EQ(event->mapping.marker_id, 10);

  MarkerTrigger unknown(kMappings, 3, 5);
  EXPECT_FALSE(unknown.observe({99}, false, true).has_value());

  MarkerTrigger ambiguous(kMappings, 3, 5);
  EXPECT_FALSE(ambiguous.observe({10, 20}, false, true).has_value());

  MarkerTrigger disarmed(kMappings, 3, 5);
  ASSERT_TRUE(disarmed.observe({10}, false, true).has_value());
  EXPECT_FALSE(disarmed.observe({20}, false, true).has_value());
}

}  // namespace
