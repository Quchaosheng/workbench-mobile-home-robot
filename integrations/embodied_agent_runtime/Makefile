IMAGE ?= embodied-agent-runtime:jazzy
DOCKER ?= docker

.PHONY: image docker-build demo demo-local check-linux

image: docker-build

docker-build:
	$(DOCKER) build --tag $(IMAGE) .

check-linux:
	@if [ "$$(uname -s)" != "Linux" ]; then \
		echo "make demo requires a Linux Docker host; vcan needs Linux networking" >&2; \
		exit 1; \
	fi

demo: check-linux docker-build
	$(DOCKER) run --rm --name embodied-agent-runtime-demo \
		--network host --privileged \
		$(IMAGE) make demo-local

demo-local:
	WORKSPACE=/workspace/ros2_ws SETUP_VCAN=1 \
		bash /workspace/scripts/run_industrial_e2e.sh
