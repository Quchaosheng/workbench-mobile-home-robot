#include "perception_task_adapter/aruco_detector.hpp"

#include <gtest/gtest.h>

#include <algorithm>
#include <vector>

#include <opencv2/aruco.hpp>
#include <opencv2/imgproc.hpp>

namespace
{

cv::Mat marker_image(int marker_id)
{
  const auto dictionary = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_4X4_50);
  cv::Mat marker;
  cv::aruco::drawMarker(dictionary, marker_id, 300, marker);
  cv::Mat canvas(400, 400, CV_8UC1, cv::Scalar(255));
  marker.copyTo(canvas(cv::Rect(50, 50, marker.cols, marker.rows)));
  return canvas;
}

TEST(ArucoDetectorTest, DetectsConfiguredDictionaryIds)
{
  perception_task_adapter::ArucoDetector detector;

  for (const int marker_id : {10, 20}) {
    const auto markers = detector.detect(marker_image(marker_id));
    ASSERT_EQ(markers.size(), 1U);
    EXPECT_EQ(markers.front().marker_id, marker_id);
    EXPECT_EQ(markers.front().corners.size(), 4U);
  }
}

TEST(ArucoDetectorTest, ReturnsNoMarkersForEmptyImage)
{
  perception_task_adapter::ArucoDetector detector;
  const cv::Mat empty(400, 400, CV_8UC1, cv::Scalar(255));

  EXPECT_TRUE(detector.detect(empty).empty());
  EXPECT_TRUE(detector.detect(cv::Mat{}).empty());
}

TEST(ArucoDetectorTest, DetectsMultipleMarkers)
{
  perception_task_adapter::ArucoDetector detector;
  cv::Mat image(500, 900, CV_8UC1, cv::Scalar(255));
  const auto ten = marker_image(10)(cv::Rect(50, 50, 300, 300));
  const auto twenty = marker_image(20)(cv::Rect(50, 50, 300, 300));
  ten.copyTo(image(cv::Rect(75, 100, 300, 300)));
  twenty.copyTo(image(cv::Rect(525, 100, 300, 300)));

  const auto markers = detector.detect(image);
  std::vector<int> ids;
  std::transform(
    markers.begin(), markers.end(), std::back_inserter(ids),
    [](const auto & marker) {return marker.marker_id;});
  std::sort(ids.begin(), ids.end());

  EXPECT_EQ(ids, (std::vector<int>{10, 20}));
}

}  // namespace
