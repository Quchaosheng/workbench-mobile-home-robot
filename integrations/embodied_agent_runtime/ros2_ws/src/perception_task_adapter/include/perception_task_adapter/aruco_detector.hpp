#pragma once

#include <array>
#include <vector>

#include <opencv2/aruco.hpp>

namespace perception_task_adapter
{

struct DetectedMarker
{
  int marker_id;
  std::array<cv::Point2f, 4> corners;
};

class ArucoDetector
{
public:
  ArucoDetector();

  std::vector<DetectedMarker> detect(const cv::Mat & image) const;

private:
  cv::Ptr<cv::aruco::Dictionary> dictionary_;
};

}  // namespace perception_task_adapter
