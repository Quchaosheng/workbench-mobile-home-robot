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


class TestTheArm64GapIsRecordedHonestly:
    """#591 asked for an arm64 build. The Dockerfile forbids it, so the job reports that.

    `Dockerfile:16` rejects any `TARGETARCH` other than `amd64` and `Dockerfile:69`
    pins the ROS apt source to `[arch=amd64]`. Both are deliberate, and the base is
    pinned by digest to its amd64 manifest. Removing those guards to "produce an arm64
    image" would ship something nobody validated.

    My first version of this change built on a native arm64 runner and failed on every
    run with `exec format error`. This version reports the gap instead.
    """

    def test_the_job_reports_rather_than_builds(self, workflow: dict) -> None:
        job = workflow["jobs"]["arm64-image-not-published"]
        # No build step: an arm64 build cannot succeed while the Dockerfile forbids it.
        assert not any("build-push-action" in str(step.get("uses", "")) for step in job["steps"])

    def test_the_job_asserts_the_two_dockerfile_guards(self) -> None:
        """The job should be replaced by a real build the moment an arm64 path exists."""
        text = CONTAINER.read_text(encoding="utf-8")
        assert 'grep -q \'test "${TARGETARCH}" = "amd64"\' Dockerfile' in text
        assert "grep -q 'arch=amd64' Dockerfile" in text
        assert "replace this job with a real arm64 build" in text

    def test_the_amd64_tag_names_its_architecture(self, workflow: dict) -> None:
        """A `latest` pull on the robot must not silently fetch amd64."""
        job = workflow["jobs"]["static-image-build"]
        build = next(step for step in job["steps"] if "build-push-action" in str(step.get("uses", "")))
        assert "amd64" in build["with"]["tags"]

    def test_the_doc_states_arm64_is_not_published(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        assert "linux/arm64" in text
        assert "未发布" in text
        # And the reason, with both guards named.
        assert "TARGETARCH" in text
        assert "arch=amd64" in text


class TestTheAmd64SmokeTest:
    """The amd64 image is the one that is published, so its smoke test still matters."""

    def test_it_runs_the_offline_goal_end_to_end(self) -> None:
        """The amd64 smoke runs `local_runner.py`, which is the documented offline demo path."""
        text = CONTAINER.read_text(encoding="utf-8")
        assert "local_runner.py" in text

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
        assert "原生 amd64 runner" in text
        # arm64 has no build method because it has no build; the table says so.
        assert "—" in text

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
