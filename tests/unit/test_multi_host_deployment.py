import ipaddress
import json
import os
import re
import socket
import subprocess
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yaml
from workbench_backend.inbound_http import InboundHttpConfigurationError, InboundHttpPolicy

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER_COMPOSE = ROOT / "deploy" / "multi-host" / "compose.controller.yaml"
SIM_COMPOSE = ROOT / "deploy" / "multi-host" / "compose.sim.yaml"
CONTROLLER_ENV_FIXTURE = ROOT / "tests" / "fixtures" / "issue-71-controller.env"
ISSUE_66_CONTROLLER_ENV_FIXTURE = ROOT / "tests" / "fixtures" / "issue-66-controller.env"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment" / "multi-host.md"
SMOKE_IMAGE = "workbench-1:issue-71-smoke"
ISSUE_66_SMOKE_IMAGE = "workbench-1:issue-66-smoke"
EVENT_SOURCE_ENVIRONMENT = {
    "WORKBENCH_EVENT_SOURCE_URL": "http://10.20.30.40:8090",
    "WORKBENCH_EVENT_SOURCE_ALLOWLIST": "10.20.30.0/24",
}
INBOUND_ENVIRONMENT_VARIABLES = {
    "CONTROLLER_BIND_ADDRESS",
    "WORKBENCH_CONTROLLER_TRUST_MODE",
    "WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST",
}


class SimComposePublishedBoundaryTests(unittest.TestCase):
    """Issue #182: the simulation event source must not default to a wildcard bind.

    These assertions parse the Compose file directly instead of shelling out to
    ``docker compose config``. Rendering needs a Docker daemon-free CLI, but the
    exposure regression this guards is a property of the checked-in file, so the
    guard has to hold in the no-daemon environment the portable test suite runs
    in as well.
    """

    def sim_service(self) -> dict[str, object]:
        document = yaml.safe_load(SIM_COMPOSE.read_text(encoding="utf-8"))
        return document["services"]["sim"]

    def test_published_port_defaults_to_loopback(self) -> None:
        service = self.sim_service()
        published = service["ports"]
        self.assertEqual(len(published), 1)
        port = published[0]
        self.assertEqual(port["target"], 8090)
        self.assertEqual(port["host_ip"], "${SIM_BIND_ADDRESS:-127.0.0.1}")
        self.assertIn("${SIM_PORT:-8090}", port["published"])

    def test_no_wildcard_host_publication_is_configured(self) -> None:
        # Assert on the parsed publication rather than the raw file text: the
        # service command legitimately passes --host 0.0.0.0 because the
        # process listens on every interface of its *own* network namespace.
        # What must never be a wildcard is the host-side publication.
        for port in self.sim_service()["ports"]:
            with self.subTest(port=port):
                self.assertNotIn("0.0.0.0", port["host_ip"])
                self.assertNotIn("::", port["host_ip"])
                self.assertTrue(port["host_ip"].startswith("${SIM_BIND_ADDRESS:-"))

    def test_command_receives_the_published_address_and_trust_settings(self) -> None:
        command = self.sim_service()["command"]
        for argument in ("--published-host", "--trust-mode", "--trusted-proxy-allowlist"):
            with self.subTest(argument=argument):
                self.assertIn(argument, command)
        self.assertEqual(
            command[command.index("--published-host") + 1],
            "${SIM_BIND_ADDRESS:-127.0.0.1}",
        )
        self.assertEqual(command[command.index("--trust-mode") + 1], "${WORKBENCH_SIM_TRUST_MODE:-local}")

    def test_container_listen_address_stays_inside_the_namespace(self) -> None:
        # The container still listens on every interface of its own network
        # namespace; only the host publication is bounded. Asserting the pair
        # keeps a future edit from "fixing" one half and silently reverting the
        # other.
        command = self.sim_service()["command"]
        self.assertEqual(command[command.index("--host") + 1], "0.0.0.0")

    def test_read_only_hardening_is_preserved(self) -> None:
        service = self.sim_service()
        self.assertTrue(service["read_only"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["security_opt"], ["no-new-privileges:true"])
        for prohibited in ("cap_add", "devices", "privileged", "volumes"):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, service)

    def test_documented_cross_host_example_uses_a_private_address(self) -> None:
        documentation = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        self.assertNotIn("curl --fail http://0.0.0.0:8090", documentation)
        self.assertIn("SIM_BIND_ADDRESS", documentation)


class ControllerComposeDeploymentTests(unittest.TestCase):
    def compose_environment(self, **overrides: str) -> dict[str, str]:
        environment = os.environ.copy()
        for variable in set(EVENT_SOURCE_ENVIRONMENT) | INBOUND_ENVIRONMENT_VARIABLES:
            environment.pop(variable, None)
        environment.update(EVENT_SOURCE_ENVIRONMENT)
        environment.update(overrides)
        return environment

    def render_controller(
        self,
        *,
        environment: dict[str, str] | None = None,
        env_file: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose"]
        if env_file is not None:
            command.extend(["--env-file", str(env_file.relative_to(ROOT))])
        command.extend(
            [
                "-f",
                str(CONTROLLER_COMPOSE.relative_to(ROOT)),
                "config",
                "--format",
                "json",
            ]
        )
        if environment is None:
            environment = os.environ.copy()
            for variable in EVENT_SOURCE_ENVIRONMENT:
                environment.pop(variable, None)
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_controller_compose_requires_non_empty_event_source_settings(self) -> None:
        for variable in EVENT_SOURCE_ENVIRONMENT:
            missing_environment = self.compose_environment()
            missing_environment.pop(variable)
            with self.subTest(variable=variable, value="missing"):
                result = self.render_controller(environment=missing_environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(variable, result.stdout + result.stderr)

            with self.subTest(variable=variable, value="blank"):
                result = self.render_controller(environment=self.compose_environment(**{variable: ""}))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(variable, result.stdout + result.stderr)

    def test_controller_compose_passes_both_settings_without_permissive_default(self) -> None:
        source = CONTROLLER_COMPOSE.read_text(encoding="utf-8")
        for variable in EVENT_SOURCE_ENVIRONMENT:
            self.assertIn(f"${{{variable}:?", source)
        self.assertIn('"--event-source-url"', source)
        self.assertIn('"--event-source-allowlist"', source)
        self.assertNotIn("0.0.0.0/0", source)
        self.assertNotIn("::/0", source)

    def test_controller_compose_fixture_renders_expected_read_only_service(self) -> None:
        result = self.render_controller(env_file=CONTROLLER_ENV_FIXTURE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rendered = json.loads(result.stdout)
        controller = rendered["services"]["controller"]
        command = controller["command"]
        for argument, expected_value in (
            ("--event-source-url", EVENT_SOURCE_ENVIRONMENT["WORKBENCH_EVENT_SOURCE_URL"]),
            ("--event-source-allowlist", EVENT_SOURCE_ENVIRONMENT["WORKBENCH_EVENT_SOURCE_ALLOWLIST"]),
        ):
            argument_index = command.index(argument)
            self.assertEqual(command[argument_index + 1], expected_value)

        self.assertTrue(controller["read_only"])
        self.assertEqual(controller["cap_drop"], ["ALL"])
        self.assertEqual(controller["security_opt"], ["no-new-privileges:true"])
        self.assertEqual(controller["tmpfs"], ["/tmp:size=32m"])
        for prohibited_capability in ("cap_add", "devices", "privileged", "volumes"):
            self.assertNotIn(prohibited_capability, controller)

    def test_controller_defaults_to_loopback_host_binding(self) -> None:
        result = self.render_controller(environment=self.compose_environment())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        controller = json.loads(result.stdout)["services"]["controller"]
        self.assertEqual(controller["ports"][0]["host_ip"], "127.0.0.1")
        command = controller["command"]
        self.assertEqual(command[command.index("--published-host") + 1], "127.0.0.1")
        self.assertEqual(command[command.index("--trust-mode") + 1], "local")
        self.assertEqual(command[command.index("--trusted-proxy-allowlist") + 1], "")

    def test_private_bind_requires_reverse_proxy_trust(self) -> None:
        private_environment = self.compose_environment(
            CONTROLLER_BIND_ADDRESS="10.20.30.40",
            WORKBENCH_CONTROLLER_TRUST_MODE="reverse_proxy",
            WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST="10.20.31.10/32,127.0.0.1/32",
        )
        result = self.render_controller(environment=private_environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        controller = json.loads(result.stdout)["services"]["controller"]
        self.assertEqual(controller["ports"][0]["host_ip"], "10.20.30.40")
        command = controller["command"]
        options = {
            argument: command[command.index(argument) + 1]
            for argument in ("--published-host", "--trust-mode", "--trusted-proxy-allowlist")
        }
        policy = InboundHttpPolicy(
            published_host=options["--published-host"],
            trust_mode=options["--trust-mode"],
            trusted_proxy_allowlist=options["--trusted-proxy-allowlist"],
        )
        self.assertTrue(policy.allows_peer("10.20.31.10"))
        self.assertTrue(policy.allows_peer("127.0.0.1"))

        missing_trust = self.render_controller(
            environment=self.compose_environment(CONTROLLER_BIND_ADDRESS="10.20.30.40")
        )
        self.assertEqual(missing_trust.returncode, 0, missing_trust.stdout + missing_trust.stderr)
        command = json.loads(missing_trust.stdout)["services"]["controller"]["command"]
        with self.assertRaises(InboundHttpConfigurationError):
            InboundHttpPolicy(
                published_host=command[command.index("--published-host") + 1],
                trust_mode=command[command.index("--trust-mode") + 1],
                trusted_proxy_allowlist=command[command.index("--trusted-proxy-allowlist") + 1],
            )

    def test_public_and_ambiguous_bind_addresses_fail_closed(self) -> None:
        for published_host in ("", "0.0.0.0", "::", "8.8.8.8", "controller.internal"):
            with self.subTest(published_host=published_host):
                result = self.render_controller(
                    environment=self.compose_environment(CONTROLLER_BIND_ADDRESS=published_host)
                )
                if result.returncode != 0:
                    self.assertIn("invalid ip address", (result.stdout + result.stderr).lower())
                    continue
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                command = json.loads(result.stdout)["services"]["controller"]["command"]
                with self.assertRaises(InboundHttpConfigurationError):
                    InboundHttpPolicy(
                        published_host=command[command.index("--published-host") + 1],
                        trust_mode=command[command.index("--trust-mode") + 1],
                        trusted_proxy_allowlist=command[command.index("--trusted-proxy-allowlist") + 1],
                    )


class ControllerComposeRuntimeSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            image = subprocess.run(
                ["docker", "image", "inspect", SMOKE_IMAGE],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise unittest.SkipTest(f"Docker is unavailable: {exc}") from exc
        if image.returncode != 0:
            raise unittest.SkipTest(f"build {SMOKE_IMAGE!r} before running the split-host runtime smoke")

    def setUp(self) -> None:
        token = uuid.uuid4().hex[:12]
        self.sim_project = f"issue71-sim-{token}"
        self.controller_project = f"issue71-controller-{token}"
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sim_socket,
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as controller_socket,
        ):
            sim_socket.bind(("127.0.0.1", 0))
            controller_socket.bind(("127.0.0.1", 0))
            self.sim_port = sim_socket.getsockname()[1]
            self.controller_port = controller_socket.getsockname()[1]
        self.sim_environment = os.environ | {
            "WORKBENCH_IMAGE": SMOKE_IMAGE,
            "SIM_PORT": str(self.sim_port),
        }
        self.controller_environment = os.environ | {
            "WORKBENCH_IMAGE": SMOKE_IMAGE,
            "CONTROLLER_PORT": str(self.controller_port),
            "WORKBENCH_EVENT_SOURCE_URL": "http://127.0.0.1:1",
            "WORKBENCH_EVENT_SOURCE_ALLOWLIST": "127.0.0.1/32",
        }
        self.addCleanup(self._cleanup)

    def _run(
        self,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
        check: bool = True,
        timeout: float = 90,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail(
                f"command failed ({result.returncode}): {' '.join(command)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def _compose(
        self,
        project: str,
        compose_file: Path,
        environment: dict[str, str],
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(compose_file.relative_to(ROOT)),
                *arguments,
            ],
            environment=environment,
            check=check,
        )

    def _cleanup(self) -> None:
        self._compose(
            self.controller_project,
            CONTROLLER_COMPOSE,
            self.controller_environment,
            "down",
            "--remove-orphans",
            check=False,
        )
        self._compose(
            self.sim_project,
            SIM_COMPOSE,
            self.sim_environment,
            "down",
            "--remove-orphans",
            check=False,
        )

    def _wait_for_status(self, url: str, expected_status: int) -> dict[str, object]:
        deadline = time.monotonic() + 30
        last_status: int | None = None
        last_body = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    last_status = response.status
                    last_body = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                last_body = exc.read().decode("utf-8")
            except (ConnectionError, TimeoutError, urllib.error.URLError):
                time.sleep(0.25)
                continue
            if last_status == expected_status:
                payload = json.loads(last_body)
                self.assertIsInstance(payload, dict)
                return payload
            time.sleep(0.25)
        self.fail(f"{url} returned status={last_status}, body={last_body!r}; expected {expected_status}")

    def test_controller_compose_runtime_enforces_event_source_allowlist(self) -> None:
        self._compose(self.sim_project, SIM_COMPOSE, self.sim_environment, "up", "-d")
        sim_payload = self._wait_for_status(f"http://127.0.0.1:{self.sim_port}/readyz", 200)
        self.assertEqual(sim_payload["status"], "ready")

        self._compose(
            self.controller_project,
            CONTROLLER_COMPOSE,
            self.controller_environment,
            "up",
            "-d",
        )
        network = self._run(
            [
                "docker",
                "network",
                "inspect",
                f"{self.controller_project}_default",
                "--format",
                "{{(index .IPAM.Config 0).Gateway}}",
            ]
        )
        gateway = network.stdout.strip()
        gateway_address = ipaddress.ip_address(gateway)
        self.assertFalse(gateway_address.is_loopback)

        event_source_url = f"http://{gateway}:{self.sim_port}"
        self.controller_environment |= {
            "WORKBENCH_EVENT_SOURCE_URL": event_source_url,
            "WORKBENCH_EVENT_SOURCE_ALLOWLIST": f"{gateway}/32",
        }
        self._compose(
            self.controller_project,
            CONTROLLER_COMPOSE,
            self.controller_environment,
            "up",
            "-d",
            "--force-recreate",
        )
        allowed_payload = self._wait_for_status(f"http://127.0.0.1:{self.controller_port}/readyz", 200)
        self.assertEqual(allowed_payload["status"], "ready")
        self.assertEqual(allowed_payload["data_source"], "remote-simulation-event-source")

        with urllib.request.urlopen(f"http://127.0.0.1:{self.controller_port}/api/v1/runs", timeout=2) as response:
            remote_runs = json.loads(response.read())
        self.assertTrue(remote_runs["runs"])

        self.controller_environment["WORKBENCH_EVENT_SOURCE_ALLOWLIST"] = "203.0.113.1/32"
        self._compose(
            self.controller_project,
            CONTROLLER_COMPOSE,
            self.controller_environment,
            "up",
            "-d",
            "--force-recreate",
        )
        denied_payload = self._wait_for_status(f"http://127.0.0.1:{self.controller_port}/readyz", 503)
        self.assertEqual(denied_payload["status"], "not_ready")
        self.assertEqual(denied_payload["data_source"], "remote-simulation-event-source")
        sim_after_denial = self._wait_for_status(f"http://127.0.0.1:{self.sim_port}/readyz", 200)
        self.assertEqual(sim_after_denial["status"], "ready")

        print(
            json.dumps(
                {
                    "allowed_status": 200,
                    "controller_gateway": gateway,
                    "denied_status": 503,
                    "event_source_url": event_source_url,
                    "sim_status_after_denial": 200,
                },
                sort_keys=True,
            )
        )


class Issue66ControllerComposeRuntimeSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            image = subprocess.run(
                ["docker", "image", "inspect", ISSUE_66_SMOKE_IMAGE],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise unittest.SkipTest(f"Docker is unavailable: {exc}") from exc
        if image.returncode != 0:
            raise unittest.SkipTest(f"build {ISSUE_66_SMOKE_IMAGE!r} before running the Issue #66 runtime smoke")

    def setUp(self) -> None:
        token = uuid.uuid4().hex[:12]
        self.project = f"issue66-controller-{token}"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as controller_socket:
            controller_socket.bind(("127.0.0.1", 0))
            self.controller_port = controller_socket.getsockname()[1]
        self.environment = os.environ | {
            "WORKBENCH_IMAGE": ISSUE_66_SMOKE_IMAGE,
            "CONTROLLER_PORT": str(self.controller_port),
            "WORKBENCH_EVENT_SOURCE_URL": "http://127.0.0.1:1",
            "WORKBENCH_EVENT_SOURCE_ALLOWLIST": "127.0.0.1/32",
        }
        for variable in INBOUND_ENVIRONMENT_VARIABLES:
            self.environment.pop(variable, None)
        self.addCleanup(self._cleanup)

    def _run(self, command: list[str], *, check: bool = True, timeout: float = 90):
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail(
                f"command failed ({result.returncode}): {' '.join(command)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def _compose(self, *arguments: str, check: bool = True):
        return self._run(
            [
                "docker",
                "compose",
                "-p",
                self.project,
                "-f",
                str(CONTROLLER_COMPOSE.relative_to(ROOT)),
                *arguments,
            ],
            check=check,
        )

    def _cleanup(self) -> None:
        self._compose("down", "--remove-orphans", check=False)

    def _wait_for_status(
        self,
        path: str,
        expected_status: int,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        method: str | None = None,
    ) -> dict[str, object]:
        deadline = time.monotonic() + 30
        last_status: int | None = None
        last_body = ""
        url = f"http://127.0.0.1:{self.controller_port}{path}"
        while time.monotonic() < deadline:
            request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    last_status = response.status
                    last_body = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                last_body = exc.read().decode("utf-8")
            except (ConnectionError, TimeoutError, urllib.error.URLError):
                time.sleep(0.25)
                continue
            if last_status == expected_status:
                payload = json.loads(last_body)
                self.assertIsInstance(payload, dict)
                return payload
            time.sleep(0.25)
        self.fail(f"{url} returned status={last_status}, body={last_body!r}; expected {expected_status}")

    def _logged_peer(self) -> str:
        logs = self._compose("logs", "--no-color", "--no-log-prefix", "controller").stdout
        for line in reversed(logs.splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") == "http_access" and "/api/v1/runs" in str(record.get("message")):
                return str(record["details"]["client"])
        self.fail(f"controller logs contain no API access record:\n{logs}")

    def test_issue_66_controller_runtime_is_loopback_only_and_read_only(self) -> None:
        self._compose("up", "-d")
        health = self._wait_for_status("/healthz", 200)
        readiness = self._wait_for_status("/readyz", 503)
        data = self._wait_for_status("/api/v1/runs", 503)
        rejected_write = self._wait_for_status("/api/v1/control", 405, data=b"{}", method="POST")
        self.assertEqual(health["status"], "ok")
        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(data["error"], "invalid_event_source")
        self.assertEqual(rejected_write["error"], "read_only")

        container_id = self._compose("ps", "-q", "controller").stdout.strip()
        bindings = json.loads(
            self._run(["docker", "inspect", container_id, "--format", "{{json .NetworkSettings.Ports}}"]).stdout
        )
        published = bindings["8080/tcp"]
        self.assertEqual({binding["HostIp"] for binding in published}, {"127.0.0.1"})

        self.environment |= {
            "CONTROLLER_BIND_ADDRESS": "127.0.0.1",
            "WORKBENCH_CONTROLLER_TRUST_MODE": "reverse_proxy",
            "WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST": "10.123.0.1/32",
        }
        self._compose("up", "-d", "--force-recreate")
        for path in ("/", "/healthz", "/readyz", "/api/v1/openapi.json", "/api/v1/runs"):
            denied = self._wait_for_status(
                path,
                403,
                headers={"X-Forwarded-For": "10.123.0.1", "Forwarded": "for=10.123.0.1"},
            )
            self.assertEqual(denied["error"], "untrusted_client")
        peer = ipaddress.ip_address(self._logged_peer())
        self.assertTrue(peer.is_loopback or peer.is_private)

        prefix_length = 32 if peer.version == 4 else 128
        self.environment["WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST"] = f"{peer}/{prefix_length},127.0.0.1/32"
        self._compose("up", "-d", "--force-recreate")

        health = self._wait_for_status("/healthz", 200)
        readiness = self._wait_for_status("/readyz", 503)
        data = self._wait_for_status("/api/v1/runs", 503)
        rejected_write = self._wait_for_status("/api/v1/control", 405, data=b"{}", method="POST")
        self.assertEqual(health["status"], "ok")
        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(data["error"], "invalid_event_source")
        self.assertEqual(rejected_write["error"], "read_only")
        print(
            json.dumps(
                {
                    "authorized_peer": str(peer),
                    "data_status": 503,
                    "health_status": 200,
                    "host_ips": sorted(binding["HostIp"] for binding in published),
                    "readiness_status": 503,
                    "spoofed_untrusted_status": 403,
                    "write_status": 405,
                },
                sort_keys=True,
            )
        )


class MultiHostDeploymentDocumentationTests(unittest.TestCase):
    def test_allowlist_contract_is_ip_or_cidr_only_with_both_examples(self) -> None:
        documentation = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        self.assertIn("WORKBENCH_EVENT_SOURCE_ALLOWLIST", documentation)
        self.assertIn("逗号分隔", documentation)
        self.assertIn("字面 IP 地址", documentation)
        self.assertIn("CIDR", documentation)
        self.assertIn("不能使用主机名", documentation)

        assignments = re.findall(
            r'(?:export WORKBENCH_EVENT_SOURCE_ALLOWLIST=|\$env:WORKBENCH_EVENT_SOURCE_ALLOWLIST = ")([^"\s]+)',
            documentation,
        )
        self.assertGreaterEqual(len(assignments), 2)
        for assignment in assignments:
            for entry in assignment.split(","):
                with self.subTest(entry=entry):
                    ipaddress.ip_network(entry, strict=False)

    def test_controller_reverse_proxy_boundary_is_documented(self) -> None:
        documentation = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        for requirement in (
            "CONTROLLER_BIND_ADDRESS",
            "WORKBENCH_CONTROLLER_TRUST_MODE",
            "WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST",
            "reverse_proxy",
            "TLS",
            "认证",
            "反向代理",
            "/healthz",
            "/readyz",
            "X-Forwarded-For",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, documentation)
        self.assertIn("不得包含密码、令牌或其他凭据", documentation)
        self.assertIn("#65", documentation)
        self.assertIn("#65 完成前", documentation)
        self.assertIn(
            "WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST=10.20.30.10/32,127.0.0.1/32",
            documentation,
        )
        self.assertIn("容器自身的健康检查", documentation)

    def test_controller_loopback_proxy_example_uses_local_backend_trust(self) -> None:
        documentation = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        self.assertIn(
            "export CONTROLLER_BIND_ADDRESS=127.0.0.1\n"
            "export WORKBENCH_CONTROLLER_TRUST_MODE=local\n"
            "unset WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST",
            documentation,
        )
        self.assertNotIn("WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST=127.0.0.1/32", documentation)


if __name__ == "__main__":
    unittest.main()
