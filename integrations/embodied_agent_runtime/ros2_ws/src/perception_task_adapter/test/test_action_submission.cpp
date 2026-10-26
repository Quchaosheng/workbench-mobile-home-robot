#include "perception_task_adapter/perception_task_adapter_node.hpp"

#include "robot_task_interfaces/action/execute_workflow.hpp"

#include <gtest/gtest.h>

#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <opencv2/aruco.hpp>
#include <opencv2/imgcodecs.hpp>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp_action/rclcpp_action.hpp>

namespace
{

using namespace std::chrono_literals;
using ExecuteWorkflow = robot_task_interfaces::action::ExecuteWorkflow;
using ServerGoalHandle = rclcpp_action::ServerGoalHandle<ExecuteWorkflow>;

cv::Mat marker_image(int marker_id)
{
  const auto dictionary = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_4X4_50);
  cv::Mat marker;
  cv::aruco::drawMarker(dictionary, marker_id, 300, marker);
  cv::Mat canvas(400, 400, CV_8UC1, cv::Scalar(255));
  marker.copyTo(canvas(cv::Rect(50, 50, marker.cols, marker.rows)));
  return canvas;
}

cv::Mat ambiguous_image()
{
  cv::Mat image(500, 900, CV_8UC1, cv::Scalar(255));
  const auto ten = marker_image(10)(cv::Rect(50, 50, 300, 300));
  const auto twenty = marker_image(20)(cv::Rect(50, 50, 300, 300));
  ten.copyTo(image(cv::Rect(75, 100, 300, 300)));
  twenty.copyTo(image(cv::Rect(525, 100, 300, 300)));
  return image;
}

class FakeWorkflowServer : public rclcpp::Node
{
public:
  FakeWorkflowServer()
  : Node("perception_fake_workflow_server")
  {
    server_ = rclcpp_action::create_server<ExecuteWorkflow>(
      this, "execute_workflow",
      [this](const rclcpp_action::GoalUUID &, const std::shared_ptr<const ExecuteWorkflow::Goal>) {
        if (accept_) {
          return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
        }
        return rclcpp_action::GoalResponse::REJECT;
      },
      [](const std::shared_ptr<ServerGoalHandle>) {
        return rclcpp_action::CancelResponse::ACCEPT;
      },
      [this](const std::shared_ptr<ServerGoalHandle> handle) {
        {
          const std::lock_guard<std::mutex> lock(mutex_);
          handles_.push_back(handle);
        }
        changed_.notify_all();
      });
  }

  bool wait_for_goals(std::size_t count)
  {
    std::unique_lock<std::mutex> lock(mutex_);
    return changed_.wait_for(lock, 2s, [this, count]() {return handles_.size() >= count;});
  }

  ExecuteWorkflow::Goal goal(std::size_t index) const
  {
    const std::lock_guard<std::mutex> lock(mutex_);
    return *handles_.at(index)->get_goal();
  }

  std::size_t goal_count() const
  {
    const std::lock_guard<std::mutex> lock(mutex_);
    return handles_.size();
  }

  void complete(std::size_t index)
  {
    std::shared_ptr<ServerGoalHandle> handle;
    {
      const std::lock_guard<std::mutex> lock(mutex_);
      handle = handles_.at(index);
    }
    auto result = std::make_shared<ExecuteWorkflow::Result>();
    result->outcome = ExecuteWorkflow::Result::COMPLETED;
    result->message = "done";
    handle->succeed(result);
  }

  void reject_goals(bool reject) {accept_ = !reject;}

private:
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  std::vector<std::shared_ptr<ServerGoalHandle>> handles_;
  bool accept_{true};
  rclcpp_action::Server<ExecuteWorkflow>::SharedPtr server_;
};

class ActionSubmissionTest : public testing::Test
{
protected:
  static void SetUpTestSuite()
  {
    int argc = 1;
    char name[] = "test_action_submission";
    char * argv[] = {name, nullptr};
    rclcpp::init(argc, argv);
  }

  static void TearDownTestSuite() {rclcpp::shutdown();}

  void SetUp() override
  {
    rclcpp::NodeOptions options;
    options.parameter_overrides(
      {
        rclcpp::Parameter("input_mode", "camera"),
        rclcpp::Parameter("confirm_frames", 3),
        rclcpp::Parameter("rearm_missing_frames", 5),
      });
    adapter_ = std::make_shared<perception_task_adapter::PerceptionTaskAdapterNode>(options);
    server_ = std::make_shared<FakeWorkflowServer>();
    executor_.add_node(adapter_);
    executor_.add_node(server_);
    spin_thread_ = std::thread([this]() {executor_.spin();});
  }

  void TearDown() override
  {
    executor_.cancel();
    spin_thread_.join();
    executor_.remove_node(adapter_);
    executor_.remove_node(server_);
    adapter_.reset();
    server_.reset();
  }

  std::shared_ptr<perception_task_adapter::PerceptionTaskAdapterNode> adapter_;
  std::shared_ptr<FakeWorkflowServer> server_;
  rclcpp::executors::MultiThreadedExecutor executor_;
  std::thread spin_thread_;
};

TEST_F(ActionSubmissionTest, SubmitsExactWorkflowAndSuppressesDuplicates)
{
  ASSERT_TRUE(adapter_->process_frame(marker_image(10), true));
  ASSERT_TRUE(server_->wait_for_goals(1));
  const auto first = server_->goal(0);
  EXPECT_EQ(first.workflow_id, "single_task");
  EXPECT_EQ(first.target_id, "dock_a");
  EXPECT_FALSE(first.request_id.empty());
  EXPECT_EQ(first.task_id, first.request_id);
  EXPECT_EQ(first.allowed_duration.sec, 3);
  EXPECT_EQ(first.allowed_duration.nanosec, 0U);
  EXPECT_FALSE(adapter_->process_frame(marker_image(10), true));
  EXPECT_EQ(server_->goal_count(), 1U);

  server_->complete(0);
  for (int frame = 0; frame < 100; ++frame) {
    EXPECT_FALSE(adapter_->process_frame(cv::Mat{}, false));
    std::this_thread::sleep_for(5ms);
  }
  ASSERT_TRUE(adapter_->process_frame(marker_image(10), true));
  ASSERT_TRUE(server_->wait_for_goals(2));
  EXPECT_NE(server_->goal(1).request_id, first.request_id);
}

TEST_F(ActionSubmissionTest, RejectsAmbiguousImage)
{
  EXPECT_FALSE(adapter_->process_frame(ambiguous_image(), true));
  EXPECT_EQ(server_->goal_count(), 0U);
}

TEST_F(ActionSubmissionTest, HandlesRejectedGoalWithoutRetry)
{
  server_->reject_goals(true);
  EXPECT_TRUE(adapter_->process_frame(marker_image(20), true));
  for (int attempt = 0; attempt < 20 && server_->goal_count() == 0; ++attempt) {
    std::this_thread::sleep_for(5ms);
  }
  EXPECT_EQ(server_->goal_count(), 0U);
  EXPECT_FALSE(adapter_->process_frame(marker_image(20), true));
}

TEST_F(ActionSubmissionTest, CameraFramesConfirmSuppressAndRearm)
{
  EXPECT_FALSE(adapter_->process_frame(marker_image(20), false));
  EXPECT_FALSE(adapter_->process_frame(marker_image(20), false));
  ASSERT_TRUE(adapter_->process_frame(marker_image(20), false));
  ASSERT_TRUE(server_->wait_for_goals(1));
  EXPECT_EQ(server_->goal(0).workflow_id, "ready_then_task");
  EXPECT_EQ(server_->goal(0).target_id, "home");

  for (int frame = 0; frame < 10; ++frame) {
    EXPECT_FALSE(adapter_->process_frame(marker_image(20), false));
  }
  EXPECT_EQ(server_->goal_count(), 1U);

  server_->complete(0);
  for (int frame = 0; frame < 100; ++frame) {
    EXPECT_FALSE(adapter_->process_frame(cv::Mat{}, false));
    std::this_thread::sleep_for(5ms);
  }
  EXPECT_FALSE(adapter_->process_frame(marker_image(20), false));
  EXPECT_FALSE(adapter_->process_frame(marker_image(20), false));
  ASSERT_TRUE(adapter_->process_frame(marker_image(20), false));
  ASSERT_TRUE(server_->wait_for_goals(2));
}

TEST_F(ActionSubmissionTest, AmbiguousCameraFrameResetsConfirmation)
{
  EXPECT_FALSE(adapter_->process_frame(marker_image(10), false));
  EXPECT_FALSE(adapter_->process_frame(marker_image(10), false));
  EXPECT_FALSE(adapter_->process_frame(ambiguous_image(), false));
  EXPECT_FALSE(adapter_->process_frame(marker_image(10), false));
  EXPECT_FALSE(adapter_->process_frame(marker_image(10), false));
  ASSERT_TRUE(adapter_->process_frame(marker_image(10), false));
  ASSERT_TRUE(server_->wait_for_goals(1));
}

class ImageModeTest : public testing::Test
{
protected:
  static void SetUpTestSuite()
  {
    int argc = 1;
    char name[] = "test_image_mode";
    char * argv[] = {name, nullptr};
    rclcpp::init(argc, argv);
  }

  static void TearDownTestSuite() {rclcpp::shutdown();}

  void SetUp() override
  {
    image_path_ = std::filesystem::temp_directory_path() /
      ("perception_aruco_" + std::to_string(counter_++) + ".png");
    ASSERT_TRUE(cv::imwrite(image_path_.string(), marker_image(10)));
    rclcpp::NodeOptions options;
    options.parameter_overrides(
      {
        rclcpp::Parameter("input_mode", "image"),
        rclcpp::Parameter("image_path", image_path_.string()),
      });
    adapter_ = std::make_shared<perception_task_adapter::PerceptionTaskAdapterNode>(options);
    server_ = std::make_shared<FakeWorkflowServer>();
    executor_.add_node(adapter_);
    executor_.add_node(server_);
    spin_thread_ = std::thread([this]() {executor_.spin();});
  }

  void TearDown() override
  {
    executor_.cancel();
    spin_thread_.join();
    executor_.remove_node(adapter_);
    executor_.remove_node(server_);
    adapter_.reset();
    server_.reset();
    std::filesystem::remove(image_path_);
  }

  static inline unsigned counter_{};
  std::filesystem::path image_path_;
  std::shared_ptr<perception_task_adapter::PerceptionTaskAdapterNode> adapter_;
  std::shared_ptr<FakeWorkflowServer> server_;
  rclcpp::executors::MultiThreadedExecutor executor_;
  std::thread spin_thread_;
};

TEST_F(ImageModeTest, GeneratedPngSubmitsOneWorkflowAndCompletes)
{
  EXPECT_EQ(adapter_->run_image(), 0);
  ASSERT_TRUE(server_->wait_for_goals(1));
  const auto goal = server_->goal(0);
  EXPECT_EQ(goal.workflow_id, "single_task");
  EXPECT_EQ(goal.target_id, "dock_a");
  server_->complete(0);
  for (int attempt = 0; attempt < 100 && !adapter_->finished(); ++attempt) {
    std::this_thread::sleep_for(5ms);
  }
  EXPECT_TRUE(adapter_->finished());
  EXPECT_EQ(adapter_->exit_code(), 0);
  EXPECT_EQ(server_->goal_count(), 1U);
}

}  // namespace
