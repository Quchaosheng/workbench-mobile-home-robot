#include "perception_task_adapter/aruco_detector.hpp"

#include <algorithm>
#include <array>
#include <utility>
#include <vector>

namespace perception_task_adapter
{

ArucoDetector::ArucoDetector()
: dictionary_(cv::aruco::getPredefinedDictionary(cv::aruco::DICT_4X4_50))
{
}

std::vector<DetectedMarker> ArucoDetector::detect(const cv::Mat & image) const
{
  if (image.empty()) {
    return {};
  }

  std::vector<int> ids;
  std::vector<std::vector<cv::Point2f>> corners;
  cv::aruco::detectMarkers(image, dictionary_, corners, ids);

  std::vector<DetectedMarker> markers;
  markers.reserve(ids.size());
  for (std::size_t index = 0; index < ids.size(); ++index) {
    if (corners[index].size() != 4U) {
      continue;
    }
    std::array<cv::Point2f, 4> marker_corners;
    std::copy_n(corners[index].begin(), marker_corners.size(), marker_corners.begin());
    markers.push_back({ids[index], std::move(marker_corners)});
  }
  return markers;
}

}  // namespace perception_task_adapter
