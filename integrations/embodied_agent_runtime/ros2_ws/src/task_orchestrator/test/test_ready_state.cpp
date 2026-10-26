#include "task_orchestrator/ready_state.hpp"

#include <chrono>

#include "gtest/gtest.h"

using namespace std::chrono_literals;

TEST(ReadyState, MissingValueIsNotUsable)
{
  task_orchestrator::ReadyState state;
  EXPECT_FALSE(state.usable(task_orchestrator::SteadyTime{}, 2s));
}

TEST(ReadyState, FalseValueIsNotUsable)
{
  task_orchestrator::ReadyState state;
  state.update(false, task_orchestrator::SteadyTime{});
  EXPECT_FALSE(state.usable(task_orchestrator::SteadyTime{} + 1s, 2s));
}

TEST(ReadyState, FreshTrueValueIsUsable)
{
  task_orchestrator::ReadyState state;
  state.update(true, task_orchestrator::SteadyTime{});
  EXPECT_TRUE(state.usable(task_orchestrator::SteadyTime{} + 1s, 2s));
}

TEST(ReadyState, StaleTrueValueIsNotUsable)
{
  task_orchestrator::ReadyState state;
  state.update(true, task_orchestrator::SteadyTime{});
  EXPECT_FALSE(state.usable(task_orchestrator::SteadyTime{} + 2001ms, 2s));
}

TEST(ReadyState, FreshUpdateMakesNextBoundedRecheckUsable)
{
  task_orchestrator::ReadyState state;
  const auto first_check = task_orchestrator::SteadyTime{} + 2s;
  EXPECT_FALSE(state.usable(first_check, 2s));

  state.update(true, first_check + 100ms);
  EXPECT_TRUE(state.usable(first_check + 100ms, 2s));
}
