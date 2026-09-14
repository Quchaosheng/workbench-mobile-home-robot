# syntax=docker/dockerfile:1.7
# linux/amd64 manifest verified from Docker Hub on 2026-08-28.
FROM nvidia/cuda:13.3.1-runtime-ubuntu24.04@sha256:63da350831208559df18c7b8f3e0d5d1c984eaa815cdde690aacd6606cd0cb11

ARG ROS_KEY_SHA256=4a91c49af0d6f0016108b93698782b596c27ccd836937e18e0e36c3347dc602f
ARG WORKBENCH_VERSION=development
ARG VCS_REF=unknown
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG TARGETARCH
ARG ERB_GEM_SHA256=bcaaef8cbaa9c46674487c95636050820262ba61293cf33f10242a90dc80654f

RUN test "${TARGETARCH}" = "amd64" || { echo "linux/amd64 is the only supported image platform (got ${TARGETARCH})" >&2; exit 2; }

LABEL org.opencontainers.image.title="workbench-1 full development runtime" \
      org.opencontainers.image.version="${WORKBENCH_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.base.name="nvidia/cuda:12.8.1-runtime-ubuntu24.04" \
      org.opencontainers.image.base.digest="sha256:828c4d878adcaa4265d80c95d8ec877149b49bb2419a4cf3bb6aa889bbb7ca2e"

# GPU dependency layers in this single image:
#   gpu-runtime: CUDA 12.8 user-space runtime; the host owns the driver/toolkit.
#   gpu-simulation: ROS 2 Jazzy, Gazebo Harmonic, EGL/OGRE and MuJoCo.
#   gpu-validation: architecture, driver, EGL renderer and physical-card checks.
# NVIDIA drivers, NVIDIA Container Toolkit, PyTorch/JAX and an RL training stack
# are deliberately not installed here.
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKBENCH_OFFLINE=1 \
    RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
    ROS_DOMAIN_ID=42 \
    ROS_LOCALHOST_ONLY=1 \
    PATH=/opt/workbench-venv/bin:/opt/workbench-mujoco-venv/bin:$PATH

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /opt/workbench_source

COPY docker/apt-packages.txt /tmp/apt-packages.txt
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    set -eux; \
    [[ "${ROS_KEY_SHA256}" =~ ^[0-9a-f]{64}$ ]]; \
    [[ "${ERB_GEM_SHA256}" =~ ^[0-9a-f]{64}$ ]]; \
    printf '%s\n' \
      'Acquire::Retries "10";' \
      'Acquire::http::Timeout "120";' \
      'Acquire::https::Timeout "120";' \
      'Acquire::http::Proxy::archive.ubuntu.com "DIRECT";' \
      'Acquire::http::Proxy::security.ubuntu.com "DIRECT";' \
      'Acquire::http::Proxy::packages.ros.org "DIRECT";' \
      'Acquire::https::Proxy::archive.ubuntu.com "DIRECT";' \
      'Acquire::https::Proxy::security.ubuntu.com "DIRECT";' \
      'Acquire::https::Proxy::packages.ros.org "DIRECT";' \
      > /etc/apt/apt.conf.d/80-workbench-retries; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl --fail --silent --show-error --location \
      https://raw.githubusercontent.com/ros/rosdistro/master/ros.key --output /tmp/ros.key; \
    echo "${ROS_KEY_SHA256}  /tmp/ros.key" | sha256sum --check --strict; \
    gpg --dearmor < /tmp/ros.key > /etc/apt/keyrings/ros-archive-keyring.gpg; \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu noble main" > /etc/apt/sources.list.d/ros2.list; \
    apt-get update; \
    xargs -r apt-get install -y --no-install-recommends < /tmp/apt-packages.txt; \
    curl --fail --silent --show-error --location https://rubygems.org/downloads/erb-4.0.3.1.gem --output /tmp/erb-4.0.3.1.gem; \
    echo "${ERB_GEM_SHA256}  /tmp/erb-4.0.3.1.gem" | sha256sum --check --strict; \
    gem install --no-document --ignore-dependencies /tmp/erb-4.0.3.1.gem; \
    rm -f /tmp/erb-4.0.3.1.gem; \
    erb_lib_path="$(gem contents erb -v 4.0.3.1 | awk '/\/lib\/erb\.rb$/ {print; exit}')"; \
    test -n "${erb_lib_path}"; \
    erb_lib_dir="$(dirname "${erb_lib_path}")"; \
    ruby_lib_dir="$(ruby -rrbconfig -e 'print RbConfig::CONFIG["rubylibdir"]')"; \
    cp -a "${erb_lib_dir}"/. "${ruby_lib_dir}"/; \
    for gem_root in /usr/lib/ruby/gems /var/lib/gems /usr/local/lib/ruby/gems /usr/share/rubygems-integration; do \
      if test -d "${gem_root}"; then \
        find "${gem_root}" -type f -name 'erb-4.0.2.gemspec' -delete; \
        find "${gem_root}" -type d -name 'erb-4.0.2' -prune -exec rm -rf '{}' +; \
      fi; \
    done; \
    ! find /usr/lib/ruby/gems /var/lib/gems /usr/local/lib/ruby/gems /usr/share/rubygems-integration \
      \( -type f -name 'erb-4.0.2.gemspec' -o -type d -name 'erb-4.0.2' \) -print -quit 2>/dev/null | grep -q .; \
    rm -f /tmp/ros.key

RUN python3 -m venv --system-site-packages /opt/workbench-venv \
    && python3 -m venv --system-site-packages /opt/workbench-mujoco-venv

COPY requirements-mujoco.txt /tmp/requirements-mujoco.txt
COPY docker/python-constraints.txt /tmp/python-constraints.txt
COPY pyproject.toml README.md README.zh-CN.md LICENSE NOTICE ./
COPY libs/application ./libs/application
COPY libs/contracts ./libs/contracts
COPY libs/hardware ./libs/hardware
COPY libs/kernel ./libs/kernel
COPY libs/task_utils ./libs/task_utils
COPY services/agent_runtime ./services/agent_runtime
COPY services/backend ./services/backend
COPY services/world_model ./services/world_model
COPY firmware/virtual_mcu ./firmware/virtual_mcu
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/workbench-venv/bin/python -m pip install --no-compile ".[dev]" \
      --constraint /tmp/python-constraints.txt \
    && /opt/workbench-mujoco-venv/bin/python -m pip install --no-compile \
      --constraint /tmp/python-constraints.txt -r /tmp/requirements-mujoco.txt

COPY . /opt/workbench_source
RUN mkdir -p \
      /opt/workbench_ws/src/robot/control \
      /opt/workbench_ws/src/robot/description \
      /usr/share/workbench/container \
      /workspace/src /workspace/build /workspace/install /workspace/log \
    && cp -a robot/control/workbench_motion /opt/workbench_ws/src/robot/control/ \
    && cp -a robot/description/. /opt/workbench_ws/src/robot/description/ \
    && source /opt/ros/jazzy/setup.bash \
    && colcon --log-base /opt/workbench_ws/log build \
      --base-paths /opt/workbench_ws/src/robot/control \
      --build-base /opt/workbench_ws/build \
      --install-base /opt/workbench_ws/install \
      --merge-install --packages-select workbench_motion \
    && dpkg-query -W -f='${binary:Package}\t${Version}\n' | sort > /usr/share/workbench/container/apt-packages.tsv \
    && /opt/workbench-venv/bin/python -m pip freeze --all > /usr/share/workbench/container/python-packages.txt \
    && /opt/workbench-mujoco-venv/bin/python -m pip freeze --all > /usr/share/workbench/container/mujoco-python-packages.txt \
    && printf '%s\n' \
      'base=nvidia/cuda:12.8.1-runtime-ubuntu24.04' \
      'base_digest=sha256:828c4d878adcaa4265d80c95d8ec877149b49bb2419a4cf3bb6aa889bbb7ca2e' \
      'platform=linux/amd64' 'ubuntu=24.04' 'ros=jazzy' 'cuda=12.8.1' 'mujoco=3.3.7' \
      > /usr/share/workbench/container/build-versions.txt \
    && useradd --create-home --uid 10001 --shell /bin/bash workbench \
    && chown -R workbench:workbench /opt/workbench_source /opt/workbench_ws /workspace /home/workbench

COPY docker/entrypoint.sh /usr/local/bin/workbench-entrypoint
COPY docker/container-doctor.py /usr/local/bin/workbench-container-doctor
COPY docker/dds_config.py /usr/local/bin/workbench-dds-config
COPY docker/mujoco_smoke.py /usr/local/bin/workbench-mujoco-smoke
COPY docker/sim_smoke.sh /usr/local/bin/workbench-sim-smoke
COPY docker/gazebo_render_smoke.sh /usr/local/bin/workbench-gazebo-render-smoke
COPY docker/camera-rendering-smoke.sdf /usr/share/workbench/container/camera-rendering-smoke.sdf
COPY docker/gpu-arch-matrix.json /usr/share/workbench/container/gpu-arch-matrix.json
COPY docker/erb-regression-test.rb /usr/share/workbench/container/erb-regression-test.rb
COPY docker/vex.json /usr/share/workbench/container/vex.json
RUN chmod 0755 /usr/local/bin/workbench-*
RUN ruby /usr/share/workbench/container/erb-regression-test.rb

USER workbench
WORKDIR /workspace/src
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8080/healthz > /dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/workbench-entrypoint"]
CMD ["python", "-m", "workbench_backend.server", "--host", "0.0.0.0", "--port", "8080"]
