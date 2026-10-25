#include "perception_task_adapter/perception_task_adapter_node.hpp"

#include "perception_task_adapter/aruco_detector.hpp"
#include "perception_task_adapter/marker_trigger.hpp"
#include "robot_task_interfaces/action/execute_workflow.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <opencv2/imgcodecs.hpp>
#include <opencv2/videoio.hpp>
#include <rclcpp_action/rclcpp_action.hpp>

namespace perception_task_adapter
{

using ExecuteWorkflow = robot_task_interfaces::action::ExecuteWorkflow;
using GoalHandle = rclcpp_action::ClientGoalHandle<ExecuteWorkflow>;

struct PerceptionTaskAdapterNode::Impl
{
  explicit Impl(PerceptionTaskAdapterNode & owner)
  : owner(owner)
  {
    input_mode = owner.declare_parameter<std::string>("input_mode", "image");
    image_path = owner.declare_parameter<std::string>("image_path", "");
    const auto camera_index_value = owner.declare_parameter<std::int64_t>("camera_index", 0);
    const auto marker_ids = owner.declare_parameter<std::vector<std::int64_t>>(
      "marker_ids", {10, 20});
    const auto workflow_ids = owner.declare_parameter<std::vector<std::string>>(
      "workflow_ids", {"single_task", "ready_then_task"});
    const auto target_ids = owner.declare_parameter<std::vector<std::string>>(
      "target_ids", {"dock_a", "home"});
    allowed_duration_ms = owner.declare_parameter<std::int64_t>("allowed_duration_ms", 3000);
    const auto confirm_frames_value = owner.declare_parameter<std::int64_t>("confirm_frames", 3);
    const auto rearm_missing_frames_value = owner.declare_parameter<std::int64_t>(
      "rearm_missing_frames", 5);

    if (input_mode != "image" && input_mode != "camera") {
      throw std::invalid_argument("input_mode must be image or camera");
    }
    if (camera_index_value < 0 ||
      camera_index_value > std::numeric_limits<int>::max())
    {
      throw std::invalid_argument("camera_index must be a nonnegative int");
    }
    camera_index = static_cast<int>(camera_index_value);
    if (input_mode == "image" && image_path.empty()) {
      throw std::invalid_argument("image_path must not be empty in image mode");
    }
    if (allowed_duration_ms <= 0 ||
      allowed_duration_ms > static_cast<std::int64_t>(std::numeric_limits<std::int32_t>::max()) *
      1000 + 999)
    {
      throw std::invalid_argument("allowed_duration_ms is outside ROS Duration range");
    }
    if (confirm_frames_value <= 0 || rearm_missing_frames_value <= 0) {
      throw std::invalid_argument("confirmation and rearm frame counts must be positive");
    }

    if (marker_ids.empty() || marker_ids.size() != workflow_ids.size() ||
      marker_ids.size() != target_ids.size())
    {
      throw std::invalid_argument(
              "marker_ids, workflow_ids, and target_ids must have equal nonzero length");
    }

    std::vector<MarkerMapping> mappings;
    mappings.reserve(marker_ids.size());
    for (std::size_t index = 0; index < marker_ids.size(); ++index) {
      if (marker_ids[index] < std::numeric_limits<int>::min() ||
        marker_ids[index] > std::numeric_limits<int>::max())
      {
        throw std::invalid_argument("marker_id is outside int range");
      }
      mappings.push_back(
        {
          static_cast<int>(marker_ids[index]), workflow_ids[index], target_ids[index]});
    }
    const auto validation = MarkerTrigger::validate(
      mappings, static_cast<std::size_t>(confirm_frames_value),
      static_cast<std::size_t>(rearm_missing_frames_value));
    if (!validation.empty()) {
      throw std::invalid_argument(validation);
    }

    trigger = std::make_unique<MarkerTrigger>(
      std::move(mappings), static_cast<std::size_t>(confirm_frames_value),
      static_cast<std::size_t>(rearm_missing_frames_value));
    action_client = rclcpp_action::create_client<ExecuteWorkflow>(&owner, "execute_workflow");
  }

  void finish_locked(int code)
  {
    active = false;
    goal_handle.reset();
    trigger->on_terminal();
    if (input_mode == "image") {
      finished = true;
      exit_code = code;
    }
  }

  void stop_locked(int code)
  {
    active = false;
    goal_handle.reset();
    trigger->on_terminal();
    finished = true;
    exit_code = code;
  }

  void goal_response(const GoalHandle::SharedPtr & handle)
  {
    std::lock_guard<std::mutex> lock(mutex);
    if (!handle) {
      RCLCPP_ERROR(owner.get_logger(), "ExecuteWorkflow goal rejected");
      finish_locked(2);
      return;
    }
    goal_handle = handle;
    RCLCPP_INFO(owner.get_logger(), "ExecuteWorkflow goal accepted");
  }

  void feedback(
    GoalHandle::SharedPtr,
    const std::shared_ptr<const ExecuteWorkflow::Feedback> feedback_message)
  {
    RCLCPP_DEBUG(
      owner.get_logger(), "ExecuteWorkflow feedback active_node=%s progress=%.3f",
      feedback_message->active_node.c_str(), feedback_message->progress);
  }

  void result(const GoalHandle::WrappedResult & wrapped_result)
  {
    std::lock_guard<std::mutex> lock(mutex);
    const bool completed = wrapped_result.code == rclcpp_action::ResultCode::SUCCEEDED &&
      wrapped_result.result &&
      wrapped_result.result->outcome == ExecuteWorkflow::Result::COMPLETED;
    if (completed) {
      RCLCPP_INFO(
        owner.get_logger(), "ExecuteWorkflow result code=%d outcome=%u error_code=%u message=%s",
        static_cast<int>(wrapped_result.code), wrapped_result.result->outcome,
        wrapped_result.result->error_code, wrapped_result.result->message.c_str());
    } else if (wrapped_result.result) {
      RCLCPP_ERROR(
        owner.get_logger(), "ExecuteWorkflow result code=%d outcome=%u error_code=%u message=%s",
        static_cast<int>(wrapped_result.code), wrapped_result.result->outcome,
        wrapped_result.result->error_code, wrapped_result.result->message.c_str());
    } else {
      RCLCPP_ERROR(owner.get_logger(), "ExecuteWorkflow returned no result");
    }
    finish_locked(completed ? 0 : 3);
  }

  PerceptionTaskAdapterNode & owner;
  std::string input_mode;
  std::string image_path;
  int camera_index{0};
  std::int64_t allowed_duration_ms{0};
  std::uint64_t sequence{0};
  bool active{false};
  bool finished{false};
  int exit_code{5};
  std::mutex mutex;
  ArucoDetector detector;
  std::unique_ptr<MarkerTrigger> trigger;
  rclcpp_action::Client<ExecuteWorkflow>::SharedPtr action_client;
  GoalHandle::SharedPtr goal_handle;
};

PerceptionTaskAdapterNode::PerceptionTaskAdapterNode(const rclcpp::NodeOptions & options)
: Node("perception_task_adapter", options), impl_(std::make_unique<Impl>(*this))
{
}

PerceptionTaskAdapterNode::~PerceptionTaskAdapterNode() = default;

bool PerceptionTaskAdapterNode::process_frame(const cv::Mat & frame, bool immediate)
{
  std::vector<DetectedMarker> detections;
  try {
    detections = impl_->detector.detect(frame);
  } catch (const cv::Exception & error) {
    RCLCPP_ERROR(get_logger(), "ArUco detection failed: %s", error.what());
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->stop_locked(2);
    return false;
  }

  std::vector<int> visible_ids;
  visible_ids.reserve(detections.size());
  for (const auto & detection : detections) {
    visible_ids.push_back(detection.marker_id);
  }

  std::optional<TriggerEvent> event;
  {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    if (impl_->finished) {
      return false;
    }
    event = impl_->trigger->observe(visible_ids, impl_->active, immediate);
    if (!event) {
      return false;
    }
    impl_->active = true;
    ++impl_->sequence;
  }

  using namespace std::chrono_literals;
  if (!impl_->action_client->wait_for_action_server(5s)) {
    RCLCPP_ERROR(get_logger(), "ExecuteWorkflow server unavailable after 5 seconds");
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->finish_locked(1);
    return false;
  }

  const auto timestamp_ms = static_cast<std::uint64_t>(
    std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count());
  ExecuteWorkflow::Goal goal;
  goal.request_id = "aruco-" + std::to_string(event->mapping.marker_id) + "-" +
    std::to_string(timestamp_ms) + "-" + std::to_string(impl_->sequence);
  goal.workflow_id = event->mapping.workflow_id;
  goal.task_id = goal.request_id;
  goal.target_id = event->mapping.target_id;
  goal.allowed_duration.sec = static_cast<std::int32_t>(impl_->allowed_duration_ms / 1000);
  goal.allowed_duration.nanosec = static_cast<std::uint32_t>(
    (impl_->allowed_duration_ms % 1000) * 1000000);

  rclcpp_action::Client<ExecuteWorkflow>::SendGoalOptions options;
  options.goal_response_callback = [this](const GoalHandle::SharedPtr & handle) {
      impl_->goal_response(handle);
    };
  options.feedback_callback = [this](
    GoalHandle::SharedPtr handle,
    const std::shared_ptr<const ExecuteWorkflow::Feedback> feedback_message) {
      impl_->feedback(handle, feedback_message);
    };
  options.result_callback = [this](const GoalHandle::WrappedResult & wrapped_result) {
      impl_->result(wrapped_result);
    };

  try {
    impl_->action_client->async_send_goal(goal, options);
  } catch (const std::exception & error) {
    RCLCPP_ERROR(get_logger(), "ExecuteWorkflow submission failed: %s", error.what());
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->finish_locked(2);
    return false;
  }
  return true;
}

int PerceptionTaskAdapterNode::run_image()
{
  cv::Mat image;
  try {
    image = cv::imread(impl_->image_path, cv::IMREAD_COLOR);
  } catch (const cv::Exception & error) {
    RCLCPP_ERROR(
      get_logger(), "Unable to read image %s: %s", impl_->image_path.c_str(),
      error.what());
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->stop_locked(2);
    return 2;
  }
  if (image.empty()) {
    RCLCPP_ERROR(get_logger(), "Unable to read image: %s", impl_->image_path.c_str());
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->stop_locked(2);
    return 2;
  }
  if (!process_frame(image, true)) {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    if (!impl_->finished) {
      impl_->stop_locked(2);
    }
    return impl_->exit_code;
  }
  return 0;
}

int PerceptionTaskAdapterNode::run_camera()
{
  cv::VideoCapture camera;
  try {
    camera.open(impl_->camera_index);
  } catch (const cv::Exception & error) {
    RCLCPP_ERROR(
      get_logger(), "Unable to open camera index %d: %s", impl_->camera_index, error.what());
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->stop_locked(2);
    return 2;
  }
  if (!camera.isOpened()) {
    RCLCPP_ERROR(get_logger(), "Unable to open camera index %d", impl_->camera_index);
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->stop_locked(2);
    return 2;
  }

  int consecutive_failures = 0;
  while (rclcpp::ok() && !finished()) {
    rclcpp::spin_some(shared_from_this());
    cv::Mat frame;
    bool read_ok = false;
    try {
      read_ok = camera.read(frame);
    } catch (const cv::Exception & error) {
      RCLCPP_ERROR(get_logger(), "Camera read failed: %s", error.what());
      std::lock_guard<std::mutex> lock(impl_->mutex);
      impl_->stop_locked(2);
      return 2;
    }
    if (!read_ok) {
      ++consecutive_failures;
      if (consecutive_failures >= 5) {
        RCLCPP_ERROR(get_logger(), "Camera read failed five consecutive times");
        std::lock_guard<std::mutex> lock(impl_->mutex);
        impl_->stop_locked(2);
        return 2;
      }
      continue;
    }
    consecutive_failures = 0;
    try {
      process_frame(frame, false);
    } catch (const cv::Exception & error) {
      RCLCPP_ERROR(get_logger(), "Camera frame processing failed: %s", error.what());
    }
  }
  return exit_code();
}

void PerceptionTaskAdapterNode::cancel_active()
{
  using namespace std::chrono_literals;
  const auto deadline = std::chrono::steady_clock::now() + 2s;
  GoalHandle::SharedPtr handle;
  while (std::chrono::steady_clock::now() < deadline && rclcpp::ok()) {
    {
      std::lock_guard<std::mutex> lock(impl_->mutex);
      if (!impl_->active) {
        return;
      }
      handle = impl_->goal_handle;
    }
    if (handle) {
      break;
    }
    rclcpp::spin_some(shared_from_this());
    std::this_thread::sleep_for(10ms);
  }
  if (!handle) {
    RCLCPP_WARN(get_logger(), "No GoalHandle available before shutdown deadline");
    return;
  }
  auto future = impl_->action_client->async_cancel_goal(handle);
  const auto remaining = deadline - std::chrono::steady_clock::now();
  if (remaining > std::chrono::steady_clock::duration::zero()) {
    rclcpp::spin_until_future_complete(shared_from_this(), future, remaining);
  }
}

bool PerceptionTaskAdapterNode::finished() const
{
  std::lock_guard<std::mutex> lock(impl_->mutex);
  return impl_->finished;
}

int PerceptionTaskAdapterNode::exit_code() const
{
  std::lock_guard<std::mutex> lock(impl_->mutex);
  return impl_->exit_code;
}

}  // namespace perception_task_adapter
