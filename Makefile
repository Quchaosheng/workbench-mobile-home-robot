PYTHON ?= python3

.PHONY: bootstrap lint fmt test contract scenario-check golden-check evaluation-check evaluation-scripted context-check \
	dashboard-test dashboard demo demo-scripted demo-offline demo-model model-provision performance-test benchmark-startup \
	benchmark-resources performance-regression-test offline-integration uart-spi-test irq-test docs task-check check container-smoke \
	sim sim-doctor sim-list sim-run container-build container-check container-colcon-build container-colcon-test \
	container-image-verify container-python-test container-sim-check container-mujoco-check container-hardware-doctor \
	container-gpu-matrix-check container-host-doctor container-dashboard-check container-project-check

bootstrap:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"

# Both rules and formatting are gated in CI. Run `make fmt` to fix locally.
lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

fmt:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

test:
	$(PYTHON) -m pytest -v

# Everything CI runs, in one command. Run this before opening a PR.
check: lint test contract scenario-check golden-check context-check demo-scripted demo-offline

contract:
	$(PYTHON) tools/scripts/validate_contracts.py

scenario-check:
	$(PYTHON) tools/scripts/validate_scenarios.py

golden-check:
	$(PYTHON) tools/scripts/validate_golden_set.py

evaluation-check: scenario-check golden-check
	$(PYTHON) -m unittest tests.unit.test_evaluation_tools -v

evaluation-scripted: scenario-check golden-check
	$(PYTHON) tools/scripts/run_evaluation.py --versions v0.2-A,v0.2-B,v0.2-C \
		--scenarios sim/scenarios/frozen/*.json sim/scenarios/expanded/*.json \
		--output-dir runs/nightly-scripted

context-check:
	$(PYTHON) tools/scripts/check_context.py

demo-scripted:
	$(PYTHON) tools/scripts/demo_scripted.py

demo-offline:
	$(PYTHON) tools/scripts/local_runner.py --goal "Place the red block in the tray"

demo: demo-offline

# Simulation control is deliberately truthful: without a configured Gazebo
# adapter, sim-run exits NOT_EXECUTED instead of manufacturing a pass.
sim: sim-run

sim-doctor:
	$(PYTHON) tools/scripts/sim_cli.py doctor

sim-list:
	$(PYTHON) tools/scripts/sim_cli.py list

sim-run:
	$(PYTHON) tools/scripts/sim_cli.py run --all --runner gazebo

demo-model:
	$(PYTHON) tools/scripts/local_runner.py --provider ollama --goal "Sort the parcels already in the intake area"

model-provision:
	docker compose --profile model-bootstrap run --rm model-bootstrap

performance-test:
	$(PYTHON) tools/scripts/demo_scripted.py --iterations 30 --telemetry runs/performance/simulation.jsonl
	$(PYTHON) tools/scripts/analyze_telemetry.py runs/performance/simulation.jsonl --output runs/performance/stages.json

benchmark-startup:
	$(PYTHON) tools/scripts/benchmark_startup.py --output runs/performance/startup.json

benchmark-resources:
	$(PYTHON) tools/scripts/benchmark_resources.py --project workbench-startup-benchmark --output runs/performance/resources.json

performance-regression-test:
	$(PYTHON) -m pytest tests/unit/test_performance_regression.py -v

dashboard-test:
	$(PYTHON) -m unittest tests.unit.test_dashboard_backend -v

offline-integration:
	$(PYTHON) -m pytest tests/integration/test_offline_system.py -v

uart-spi-test:
	$(PYTHON) -m pytest tests/unit/test_uart_spi_contract.py -v

irq-test:
	$(PYTHON) -m pytest tests/unit/test_irq_contract.py -v

dashboard:
	$(PYTHON) -m workbench_backend.server --host 127.0.0.1 --port 8080

docs:
	$(PYTHON) -m mkdocs build --strict

container-smoke:
	$(MAKE) container-check

container-build:
	docker compose build dashboard

container-image-verify:
	@tag_id=$$(docker image inspect workbench-1:local --format '{{.Id}}'); \
		test -n "$$tag_id"; \
		running_id=$$(docker inspect workbench-desk-robot-dashboard-1 --format '{{.Image}}' 2>/dev/null || true); \
		if test -n "$$running_id" && test "$$tag_id" != "$$running_id"; then \
			echo "dashboard image $$running_id differs from workbench-1:local $$tag_id" >&2; exit 2; \
		fi; \
		echo "workbench-1:local=$$tag_id"; \
		if test -n "$$running_id"; then echo "dashboard=$$running_id"; fi

container-check:
	docker compose config >/dev/null
	docker compose run --rm dashboard python3 tools/scripts/local_runner.py --goal "Place the red block in the tray"
	$(MAKE) container-dashboard-check
	$(MAKE) container-project-check

container-dashboard-check:
	docker compose up -d --no-build dashboard
	trap 'docker compose down' EXIT; \
	for attempt in $$(seq 1 30); do \
		if curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null \
			&& curl --fail --silent http://127.0.0.1:8080/readyz >/dev/null; then \
			echo "dashboard healthz=200 readyz=200"; exit 0; \
		fi; \
		sleep 1; \
	done; \
	docker compose logs dashboard; exit 2

container-project-check:
	docker compose run --rm dashboard bash -lc 'set -euo pipefail; test_root=$$(mktemp -d /tmp/workbench-project-check.XXXXXX); cp -a /workspace/src/. "$$test_root/"; cd "$$test_root"; PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS="--ignore=tests/unit/test_multi_host_deployment.py" make test && make contract && make scenario-check && make context-check && make demo-scripted'

container-colcon-build:
	docker compose run --rm dashboard colcon --log-base /workspace/log build --base-paths /workspace/src/robot/control --build-base /workspace/build --install-base /workspace/install --merge-install --packages-select workbench_motion

container-colcon-test:
	docker compose run --rm dashboard bash -lc 'set +u; source /opt/ros/jazzy/setup.bash; source /workspace/install/setup.bash; set -u; colcon --log-base /workspace/log test --base-paths /workspace/src/robot/control --build-base /workspace/build --install-base /workspace/install --merge-install --packages-select workbench_motion && colcon --log-base /workspace/log test-result --test-result-base /workspace/build --verbose'

container-python-test:
	docker compose run --rm dashboard bash -lc 'set -euo pipefail; test_root=$$(mktemp -d /tmp/workbench-python-test.XXXXXX); cp -a /workspace/src/. "$$test_root/"; cd "$$test_root"; PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -v --ignore=tests/unit/test_multi_host_deployment.py'

container-sim-check:
	@status=0; \
		docker compose run --rm ros-sim /usr/local/bin/workbench-gazebo-render-smoke || status=$$?; \
		docker compose run --rm ros-sim /usr/local/bin/workbench-sim-smoke || status=$$?; \
		exit $$status

container-mujoco-check:
	docker compose run --rm mujoco-gpu /opt/workbench-mujoco-venv/bin/python /usr/local/bin/workbench-mujoco-smoke

container-hardware-doctor:
	docker compose run --rm hardware-shell /usr/local/bin/workbench-container-doctor --profile hardware-shell

container-gpu-matrix-check:
	@test "$(WORKBENCH_GPU_TIER)" = rtx30 -o "$(WORKBENCH_GPU_TIER)" = rtx40 -o "$(WORKBENCH_GPU_TIER)" = rtx50 || \
		{ echo "Set WORKBENCH_GPU_TIER to rtx30, rtx40, or rtx50 for the physical card under test" >&2; exit 2; }
	@test "$(WORKBENCH_IMAGE_DIGEST)" != "" -a "$(WORKBENCH_IMAGE_DIGEST)" != "local-unpinned" || \
		{ echo "Set WORKBENCH_IMAGE_DIGEST to the immutable image digest shared by all RTX generations" >&2; exit 2; }
	docker compose run --rm mujoco-gpu /usr/local/bin/workbench-container-doctor --profile gpu-validation --output /workspace/log/gpu-$(WORKBENCH_GPU_TIER).json
	docker compose run --rm mujoco-gpu /opt/workbench-mujoco-venv/bin/python /usr/local/bin/workbench-mujoco-smoke

container-host-doctor:
	python3 docker/host_doctor.py

task-check:
	$(PYTHON) tools/scripts/check_task_packet.py $(PACKET)
