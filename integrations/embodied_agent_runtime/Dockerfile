FROM ros:jazzy-ros-base

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    build-essential \
    can-utils \
    iproute2 \
    kmod \
    libgrpc++-dev \
    libopencv-dev \
    libprotobuf-dev \
    libsqlite3-dev \
    pkg-config \
    protobuf-compiler \
    protobuf-compiler-grpc \
    python3-colcon-common-extensions \
    python3-rosdep \
    ros-jazzy-behaviortree-cpp \
    sudo \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY ros2_ws/src ./ros2_ws/src
COPY scripts ./scripts
COPY Makefile ./Makefile

RUN chmod +x scripts/*.sh \
 && (rosdep init 2>/dev/null || true) \
 && rosdep update \
 && source /opt/ros/jazzy/setup.bash \
 && rosdep install \
      --from-paths ros2_ws/src \
      --ignore-src \
      --rosdistro jazzy \
      --skip-keys "libgrpc protobuf-dev protobuf-compiler-grpc libopencv-dev libsqlite3-dev" \
      -r -y \
 && cd ros2_ws \
 && colcon build --cmake-args -DBUILD_TESTING=OFF

CMD ["make", "demo-local"]
