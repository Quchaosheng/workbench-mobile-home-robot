"""The arm64 image the robot needs is built and smoke-tested (#591).

The product compute is a Jetson Orin Nano Super, which is arm64. The shipped image
was built for `linux/amd64` only, so `deploy/multi-host/` pulled
`${WORKBENCH_IMAGE}` and found no image for the robot's architecture. The repository
tested what an arm64 environment *should* look like — `check_arm64_environment.sh`,
`run_arm64_smoke.sh`, `test_arm64_configuration.sh` all exist — and never built one.

These tests assert the workflow structure, the architecture-named tag and the
documented build path. They do **not** run an arm64 build: this container is amd64
with no Docker, so the job itself is unexecuted here and the pull request says so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = ROOT / ".github" / "workflows" / "container-full-stack.yml"
DOC = ROOT / "docs" / "deployment" / "container.md"
SMOKE_SCRIPT = ROOT / "integrations" / "embodied_agent_runtime" / "scripts" / "check_arm64_environment.sh"
INTEGRATIONS_README = ROOT / "integrations" / "embodied_agent_runtime" / "README.md"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(CONTAINER.read_text(encoding="utf-8"))


class TestTheArm64JobExists:
    def test_the_job_is_declared(self, workflow: dict) -> None:
        assert "arm64-image-build" in workflow["jobs"]

    def test_it_runs_on_a_native_arm64_runner(self, workflow: dict) -> None:
        """The choice is recorded with its cost: qemu over a CUDA base times out."""
        job = workflow["jobs"]["arm64-image-build"]
        assert job["runs-on"] == "ubuntu-24.04-arm"

    def test_it_does_not_cross_build(self, workflow: dict) -> None:
        """A `platforms:` on a native arm64 runner would mean somebody re-added qemu."""
        job = workflow["jobs"]["arm64-image-build"]
        build = [step for step in job["steps"] if "build-push-action" in str(step.get("uses", ""))]
        assert build, "no build-push step"
        assert "platforms" not in (build[0].get("with") or {}), "the runner already is arm64"

    def test_the_choice_and_its_cost_are_written_down(self) -> None:
        text = CONTAINER.read_text(encoding="utf-8")
        assert "native arm64 runner" in text
        assert "qemu" in text
        assert "CUDA" in text

    def test_the_tag_names_the_architecture(self, workflow: dict) -> None:
        """A `latest` pull on the robot must not silently fetch amd64."""
        job = workflow["jobs"]["arm64-image-build"]
        build = next(step for step in job["steps"] if "build-push-action" in str(step.get("uses", "")))
        assert "arm64" in build["with"]["tags"]

    def test_the_amd64_tag_names_its_architecture_too(self, workflow: dict) -> None:
        job = workflow["jobs"]["static-image-build"]
        build = next(step for step in job["steps"] if "build-push-action" in str(step.get("uses", "")))
        assert "amd64" in build["with"]["tags"]

    def test_the_architecture_is_asserted_not_assumed(self) -> None:
        """A mislabelled image would pass every other step.

        The check runs `uname -m` *inside* the built image via `--entrypoint`, so
        the string in the file is `--entrypoint uname ... -m` rather than a shell
        `uname -m`.
        """
        text = CONTAINER.read_text(encoding="utf-8")
        assert "--entrypoint uname" in text
        assert 'test "$arch" = "aarch64"' in text


class TestTheSmokeTest:
    def test_it_reuses_the_existing_arm64_script(self) -> None:
        """A second recipe would let CI and the bench check different things."""
        text = CONTAINER.read_text(encoding="utf-8")
        assert "check_arm64_environment.sh" in text
        assert SMOKE_SCRIPT.is_file()

    def test_it_runs_the_offline_demo_and_asserts_the_documented_result(self) -> None:
        text = CONTAINER.read_text(encoding="utf-8")
        assert "demo_scripted.py" in text
        # The output is asserted, not eyeballed.
        assert "grep -q" in text
        assert "Traceback" in text

    def test_it_checks_the_dashboard_starts(self) -> None:
        text = CONTAINER.read_text(encoding="utf-8")
        assert "healthz" in text


class TestTheDocumentStatesTheArchitectures:
    def test_it_lists_which_are_published_and_which_are_not(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        assert "linux/amd64" in text
        assert "linux/arm64" in text
        assert "未发布" in text

    def test_it_records_the_build_method_per_architecture(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        assert "ubuntu-24.04-arm" in text
        assert "原生" in text

    def test_it_explains_why_the_tag_carries_the_architecture(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        assert "exec-format" in text


class TestTheDanglingReferenceIsGone:
    def test_the_integrations_readme_points_at_a_real_document(self) -> None:
        """`docs/PORTABILITY.md` never existed; this is the doc that answers the question."""
        text = INTEGRATIONS_README.read_text(encoding="utf-8")
        assert "docs/PORTABILITY.md" not in text
        assert "docs/deployment/container.md" in text
        assert (ROOT / "docs" / "deployment" / "container.md").is_file()


class TestNoWorkflowBecamePathFiltered:
    def test_the_trigger_still_has_no_paths_filter(self, workflow: dict) -> None:
        """The required check must always report; a filter makes GitHub create no run."""
        triggers = workflow.get("on") or workflow.get(True)

        def walk(node: object) -> list[str]:
            found: list[str] = []
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"paths", "paths-ignore"}:
                        found.append(key)
                    found.extend(walk(value))
            elif isinstance(node, list):
                for item in node:
                    found.extend(walk(item))
            return found

        assert walk(triggers) == []

    def test_every_action_stays_pinned(self) -> None:
        text = CONTAINER.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "uses:" in line:
                assert re.search(r"@[0-9a-f]{40}\s+# v[0-9]", line.strip()), line
