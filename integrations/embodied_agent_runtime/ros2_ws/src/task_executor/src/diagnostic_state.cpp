#include "task_executor/diagnostic_state.hpp"

#include <diagnostic_msgs/msg/key_value.hpp>

#include <string>

namespace
{

diagnostic_msgs::msg::KeyValue key_value(const std::string & key, const std::string & value)
{
  diagnostic_msgs::msg::KeyValue result;
  result.key = key;
  result.value = value;
  return result;
}

}

namespace task_executor
{

void TaskDiagnosticState::set_bridge_ready(const bool ready)
{
  std::lock_guard<std::mutex> lock(mutex_);
  bridge_ready_ = ready;
}

void TaskDiagnosticState::begin_task(const std::string & task_id)
{
  std::lock_guard<std::mutex> lock(mutex_);
  active_task_id_ = task_id;
  phase_ = TaskPhase::kValidating;
}

void TaskDiagnosticState::mark_validating()
{
  std::lock_guard<std::mutex> lock(mutex_);
  phase_ = TaskPhase::kValidating;
}

void TaskDiagnosticState::mark_running()
{
  std::lock_guard<std::mutex> lock(mutex_);
  phase_ = TaskPhase::kRunning;
}

void TaskDiagnosticState::mark_waiting_ack()
{
  std::lock_guard<std::mutex> lock(mutex_);
  phase_ = TaskPhase::kWaitingAck;
}

void TaskDiagnosticState::mark_recovering()
{
  std::lock_guard<std::mutex> lock(mutex_);
  phase_ = TaskPhase::kRecovering;
}

void TaskDiagnosticState::record_success(const std::string & outcome)
{
  std::lock_guard<std::mutex> lock(mutex_);
  phase_ = TaskPhase::kIdle;
  active_task_id_.clear();
  last_outcome_ = outcome;
  last_error_code_ = 0;
  failure_latched_ = false;
}

void TaskDiagnosticState::record_canceled()
{
  std::lock_guard<std::mutex> lock(mutex_);
  phase_ = TaskPhase::kIdle;
  active_task_id_.clear();
  last_outcome_ = "CANCELED";
}

void TaskDiagnosticState::record_failure(
  const std::string & outcome, const std::uint16_t error_code)
{
  std::lock_guard<std::mutex> lock(mutex_);
  phase_ = TaskPhase::kIdle;
  active_task_id_.clear();
  last_outcome_ = outcome;
  last_error_code_ = error_code;
  failure_latched_ = true;
}

diagnostic_msgs::msg::DiagnosticStatus TaskDiagnosticState::snapshot() const
{
  TaskPhase phase;
  std::string active_task_id;
  std::string last_outcome;
  std::uint16_t last_error_code;
  bool bridge_ready;
  bool failure_latched;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    phase = phase_;
    active_task_id = active_task_id_;
    last_outcome = last_outcome_;
    last_error_code = last_error_code_;
    bridge_ready = bridge_ready_;
    failure_latched = failure_latched_;
  }

  const char * phase_name = "IDLE";
  switch (phase) {
    case TaskPhase::kIdle:
      phase_name = "IDLE";
      break;
    case TaskPhase::kValidating:
      phase_name = "VALIDATING";
      break;
    case TaskPhase::kRunning:
      phase_name = "RUNNING";
      break;
    case TaskPhase::kWaitingAck:
      phase_name = "WAITING_ACK";
      break;
    case TaskPhase::kRecovering:
      phase_name = "RECOVERING";
      break;
  }

  const bool ready = bridge_ready && !failure_latched;
  diagnostic_msgs::msg::DiagnosticStatus status;
  status.name = "runtime/task_executor";
  status.hardware_id = "task_runtime";
  if (!bridge_ready) {
    status.level = diagnostic_msgs::msg::DiagnosticStatus::ERROR;
    status.message = "bridge unavailable";
  } else if (failure_latched) {
    status.level = diagnostic_msgs::msg::DiagnosticStatus::ERROR;
    status.message = "task failure";
  } else if (phase == TaskPhase::kRecovering) {
    status.level = diagnostic_msgs::msg::DiagnosticStatus::WARN;
    status.message = "recovering";
  } else {
    status.level = diagnostic_msgs::msg::DiagnosticStatus::OK;
    status.message = "ready";
  }
  status.values = {
    key_value("ready", ready ? "true" : "false"),
    key_value("state", phase_name),
    key_value("active_task_id", active_task_id),
    key_value("bridge_ready", bridge_ready ? "true" : "false"),
    key_value("last_outcome", last_outcome),
    key_value("last_error_code", std::to_string(last_error_code))};
  return status;
}

}
