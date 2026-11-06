#pragma once

#include <memory>

#include <opencv2/core.hpp>
#include <rclcpp/rclcpp.hpp>

namespace perception_task_adapter
{

class PerceptionTaskAdapterNode : public rclcpp::Node
{
public:
  explicit PerceptionTaskAdapterNode(const rclcpp::NodeOptions & options = rclcpp::NodeOptions{});
  ~PerceptionTaskAdapterNode() override;

  bool process_frame(const cv::Mat & frame, bool immediate);
  int run_image();
  int run_camera();
  void cancel_active();
  bool finished() const;
  int exit_code() const;

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace perception_task_adapter
