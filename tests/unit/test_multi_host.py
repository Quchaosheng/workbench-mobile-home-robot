import contextlib
import json
import math
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from workbench_backend.inbound_http import InboundHttpConfigurationError, InboundHttpPolicy
from workbench_backend.read_model import ReadModelError, RemoteDashboardReadModel
from workbench_backend.remote_http import (
    MAX_REMOTE_RESPONSE_BYTES,
    RemoteHttpClient,
    RemoteHttpConfigurationError,
    RemoteHttpError,
    RemoteHttpResponseTooLarge,
)
from workbench_backend.server import create_server, main


@contextlib.contextmanager
def response_server(responder):
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass

        def do_GET(self) -> None:
            hits.append(self.path)
            status, headers, body = responder(self.path)
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def json_headers(body: bytes, *, content_type: str = "application/json", include_length: bool = True):
    headers = [("Content-Type", content_type)]
    if include_length:
        headers.append(("Content-Length", str(len(body))))
    return headers


def json_body_with_size(size: int) -> bytes:
    prefix = b'{"value":"'
    suffix = b'"}'
    body = prefix + (b"x" * (size - len(prefix) - len(suffix))) + suffix
    if len(body) != size:
        raise AssertionError("incorrect test body size")
    return body


class ControllerInboundPolicyTests(unittest.TestCase):
    def test_loopback_and_private_reverse_proxy_configurations_are_accepted(self) -> None:
        local = InboundHttpPolicy(published_host="127.0.0.1")
        self.assertEqual(str(local.published_address), "127.0.0.1")
        self.assertEqual(local.trust_mode, "local")

        for published_host, allowlist, allowed_peer in (
            ("10.20.30.40", "10.20.31.10/32", "10.20.31.10"),
            ("fd12:3456::40", "fd12:3456::10/128", "fd12:3456::10"),
            ("127.0.0.1", "127.0.0.1/32", "127.0.0.1"),
        ):
            with self.subTest(published_host=published_host):
                policy = InboundHttpPolicy(
                    published_host=published_host,
                    trust_mode="reverse_proxy",
                    trusted_proxy_allowlist=allowlist,
                )
                self.assertTrue(policy.allows_peer(allowed_peer))

    def test_public_and_ambiguous_bind_addresses_fail_closed(self) -> None:
        rejected = (
            "",
            "   ",
            "0.0.0.0",
            "::",
            "8.8.8.8",
            "2001:4860:4860::8888",
            "169.254.1.10",
            "fe80::1",
            "224.0.0.1",
            "ff02::1",
            "240.0.0.1",
            "controller.internal",
        )
        for published_host in rejected:
            with self.subTest(published_host=published_host), self.assertRaises(InboundHttpConfigurationError):
                InboundHttpPolicy(published_host=published_host)

    def test_private_bind_requires_reverse_proxy_trust(self) -> None:
        for published_host in ("10.20.30.40", "fd12:3456::40"):
            with self.subTest(published_host=published_host, reason="local_mode"):
                with self.assertRaisesRegex(InboundHttpConfigurationError, "reverse_proxy"):
                    InboundHttpPolicy(published_host=published_host)
            with self.subTest(published_host=published_host, reason="missing_allowlist"):
                with self.assertRaisesRegex(InboundHttpConfigurationError, "allow-list"):
                    InboundHttpPolicy(published_host=published_host, trust_mode="reverse_proxy")

        for trust_mode in ("", "LOCAL", "proxy", "unknown"):
            with self.subTest(trust_mode=trust_mode), self.assertRaises(InboundHttpConfigurationError):
                InboundHttpPolicy(published_host="127.0.0.1", trust_mode=trust_mode)

    def test_trusted_proxy_allowlist_is_bounded_and_fail_closed(self) -> None:
        rejected = (
            "",
            "   ",
            "0.0.0.0/0",
            "::/0",
            "8.8.8.8/32",
            "2001:4860:4860::8888/128",
            "169.254.0.0/16",
            "fe80::/10",
            "10.0.0.1,,10.0.0.2",
            "proxy.internal",
            "10.0.0.1/999",
        )
        for allowlist in rejected:
            with self.subTest(allowlist=allowlist), self.assertRaises(InboundHttpConfigurationError):
                InboundHttpPolicy(
                    published_host="127.0.0.1",
                    trust_mode="reverse_proxy",
                    trusted_proxy_allowlist=allowlist,
                )

        policy = InboundHttpPolicy(
            published_host="127.0.0.1",
            trust_mode="reverse_proxy",
            trusted_proxy_allowlist="10.20.30.0/24,fd12:3456::/64",
        )
        self.assertTrue(policy.allows_peer("10.20.30.40"))
        self.assertTrue(policy.allows_peer("fd12:3456::40"))
        self.assertFalse(policy.allows_peer("10.20.31.40"))
        self.assertFalse(policy.allows_peer("not-an-address"))
        self.assertFalse(policy.allows_peer("127.0.0.1"))


class MultiHostReadModelTests(unittest.TestCase):
    def test_remote_json_rejects_top_level_and_nested_duplicate_keys(self) -> None:
        cases = {
            "runs": b'{"runs":[],"runs":[]}',
            "task_id": b'{"runs":[{"task_id":"secret-marker","task_id":"trusted"}]}',
        }
        for duplicate_key, body in cases.items():
            with (
                self.subTest(duplicate_key=duplicate_key),
                response_server(lambda path, body=body: (200, json_headers(body), body)) as (base_url, _),
            ):
                model = RemoteDashboardReadModel(base_url)
                with self.assertRaisesRegex(ReadModelError, rf"duplicate JSON key: '{duplicate_key}'") as caught:
                    model.list_runs()
            self.assertNotIn("secret-marker", str(caught.exception))

        body = b'{"status":"not_ready","status":"ready"}'
        with response_server(lambda path: (200, json_headers(body), body)) as (base_url, _):
            self.assertFalse(RemoteDashboardReadModel(base_url).ready())

    def test_remote_events_clear_the_same_semantic_contract_as_local_logs(self) -> None:
        def event(**overrides: object) -> dict:
            base = {
                "event_id": "evt-0",
                "run_id": "run-remote",
                "sequence_no": 0,
                "event_type": "verification",
                "occurred_at": "2026-08-08T00:00:00Z",
                "payload": {"status": "confirmed"},
                "evidence_refs": ["frame-1"],
            }
            base.update(overrides)
            return base

        cases = {
            "unknown event_type": [event(event_type="not_a_real_event")],
            "unknown verification status": [event(payload={"status": "maybe"})],
            "non-object payload": [event(payload=[])],
            "blank occurred_at": [event(occurred_at="  ")],
            "non-string evidence refs": [event(evidence_refs=[1])],
            "duplicate event_id": [event(), event(**{"sequence_no": 1})],
            "sequence gap": [event(), event(**{"event_id": "evt-2", "sequence_no": 2})],
            "run_id drift": [event(), event(**{"event_id": "evt-1", "sequence_no": 1, "run_id": "other"})],
        }
        for label, events in cases.items():
            with self.subTest(case=label):
                body = json.dumps({"events": events}).encode()
                with response_server(lambda path, body=body: (200, json_headers(body), body)) as (base_url, _):
                    with self.assertRaisesRegex(ReadModelError, "remote event source") as caught:
                        RemoteDashboardReadModel(base_url).list_events("run-remote")
                self.assertNotIn(json.dumps(events), str(caught.exception))

        # A canonical remote stream still projects.
        body = json.dumps({"events": [event(event_type="task_accepted", payload={"task_id": "task-1"})]}).encode()
        with response_server(lambda path, body=body: (200, json_headers(body), body)) as (base_url, _):
            events = RemoteDashboardReadModel(base_url).list_events("run-remote")
        self.assertEqual([item["event_id"] for item in events], ["evt-0"])

    def test_remote_run_summaries_are_validated_before_display(self) -> None:
        summaries = {
            "missing run_id": [{"event_count": 1}],
            "duplicate run_id": [{"run_id": "a", "event_count": 1}, {"run_id": "a", "event_count": 2}],
            "non-integer event_count": [{"run_id": "a", "event_count": "1"}],
            "non-object run": ["not-an-object"],
        }
        for label, runs in summaries.items():
            with self.subTest(case=label):
                body = json.dumps({"runs": runs}).encode()
                with response_server(lambda path, body=body: (200, json_headers(body), body)) as (base_url, _):
                    with self.assertRaisesRegex(ReadModelError, "remote event source"):
                        RemoteDashboardReadModel(base_url).list_runs()

        body = json.dumps({"runs": [{"run_id": "a", "event_count": 3}]}).encode()
        with response_server(lambda path, body=body: (200, json_headers(body), body)) as (base_url, _):
            runs = RemoteDashboardReadModel(base_url).list_runs()
        self.assertEqual(runs, [{"run_id": "a", "event_count": 3}])

    def test_controller_reads_remote_source_and_fails_ready_when_peer_is_down(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            data_dir.joinpath("run-remote.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event_id": "evt-1",
                                "run_id": "run-remote",
                                "sequence_no": 0,
                                "event_type": "task_accepted",
                                "occurred_at": "2026-08-08T00:00:00Z",
                                "payload": {"task_id": "task-1", "goal": "test"},
                                "evidence_refs": [],
                            }
                        ),
                        json.dumps(
                            {
                                "event_id": "evt-2",
                                "run_id": "run-remote",
                                "sequence_no": 1,
                                "event_type": "task_terminal",
                                "occurred_at": "2026-08-08T00:00:01Z",
                                "payload": {},
                                "evidence_refs": [],
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            sim = create_server("127.0.0.1", 0, data_dir=data_dir)
            sim_thread = threading.Thread(target=sim.serve_forever, daemon=True)
            sim_thread.start()
            sim_url = f"http://127.0.0.1:{sim.server_address[1]}"
            controller = create_server("127.0.0.1", 0, event_source_url=sim_url)
            controller_thread = threading.Thread(target=controller.serve_forever, daemon=True)
            controller_thread.start()
            controller_url = f"http://127.0.0.1:{controller.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{controller_url}/readyz", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())["data_source"], "remote-simulation-event-source")
                with urllib.request.urlopen(f"{controller_url}/api/v1/runs/run-remote/events", timeout=2) as response:
                    payload = json.loads(response.read())
                    self.assertEqual(payload["events"][0]["run_id"], "run-remote")
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(f"{controller_url}/api/v1/runs/missing/events", timeout=2)
                self.assertEqual(missing.exception.code, 404)
                sim.shutdown()
                sim.server_close()
                sim_thread.join(timeout=2)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"{controller_url}/readyz", timeout=3)
                self.assertEqual(caught.exception.code, 503)
            finally:
                controller.shutdown()
                controller.server_close()
                controller_thread.join(timeout=2)
                if sim_thread.is_alive():
                    sim.shutdown()
                    sim.server_close()
                    sim_thread.join(timeout=2)


class RemoteHttpSecurityTests(unittest.TestCase):
    def test_remote_source_accepts_ipv4_and_ipv6_loopback_without_allowlist(self) -> None:
        ipv4 = RemoteHttpClient("http://127.0.0.1:8090")
        ipv6 = RemoteHttpClient("http://[::1]:8090/")
        self.assertEqual(ipv4._resolve_endpoint().address, "127.0.0.1")
        self.assertEqual(ipv6._resolve_endpoint().address, "::1")

        localhost = RemoteHttpClient("http://localhost:8090")
        resolved = [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 8090, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 8090)),
        ]
        with mock.patch("workbench_backend.remote_http.socket.getaddrinfo", return_value=resolved):
            self.assertIn(localhost._resolve_endpoint().address, {"127.0.0.1", "::1"})

    def test_remote_source_requires_allowlist_for_non_loopback_addresses(self) -> None:
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.20.30.40", 8090)),
        ]
        with mock.patch("workbench_backend.remote_http.socket.getaddrinfo", return_value=resolved):
            with self.assertRaisesRegex(RemoteHttpError, "allow-list"):
                RemoteHttpClient("http://simulation.internal:8090")._resolve_endpoint()
            endpoint = RemoteHttpClient(
                "http://simulation.internal:8090", allowlist="10.20.30.40/32"
            )._resolve_endpoint()
        self.assertEqual(endpoint.address, "10.20.30.40")

        for malformed in ("not-an-address", "10.0.0.1,,10.0.0.2", "10.0.0.1/999"):
            with self.subTest(malformed=malformed), self.assertRaises(RemoteHttpConfigurationError):
                RemoteHttpClient("http://127.0.0.1", allowlist=malformed)
        with self.assertRaises(RemoteHttpConfigurationError):
            RemoteHttpClient("http://127.0.0.1", allowlist=123)  # type: ignore[arg-type]

    def test_remote_source_rejects_unsafe_url_forms(self) -> None:
        unsafe_urls = (
            "https://127.0.0.1",
            "http://user@127.0.0.1",
            "http://user:password@127.0.0.1",
            "http://127.0.0.1/?query=value",
            "http://127.0.0.1/#fragment",
            "http://127.0.0.1/api/v1/runs",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://127.0.0.1:not-a-port",
            "http://127.0.0.1:",
            "http://☃.example:8090",
        )
        for url in unsafe_urls:
            with self.subTest(url=url), self.assertRaises(RemoteHttpConfigurationError):
                RemoteHttpClient(url)

    def test_remote_source_rejects_disallowed_address_ranges(self) -> None:
        cases = (
            (socket.AF_INET, "0.0.0.0", ("0.0.0.0", 8090)),
            (socket.AF_INET, "169.254.169.254", ("169.254.169.254", 8090)),
            (socket.AF_INET, "224.0.0.1", ("224.0.0.1", 8090)),
            (socket.AF_INET, "240.0.0.1", ("240.0.0.1", 8090)),
            (socket.AF_INET6, "::", ("::", 8090, 0, 0)),
            (socket.AF_INET6, "fe80::1", ("fe80::1", 8090, 0, 0)),
            (socket.AF_INET6, "ff02::1", ("ff02::1", 8090, 0, 0)),
            (socket.AF_INET6, "fec0::1", ("fec0::1", 8090, 0, 0)),
        )
        for family, address, sockaddr in cases:
            resolved = [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]
            with (
                self.subTest(address=address),
                mock.patch("workbench_backend.remote_http.socket.getaddrinfo", return_value=resolved),
                self.assertRaisesRegex(RemoteHttpError, "prohibited"),
            ):
                RemoteHttpClient("http://simulation.internal:8090", allowlist="0.0.0.0/0,::/0")._resolve_endpoint()

    def test_remote_source_validates_ipv4_mapped_and_every_dns_address(self) -> None:
        mapped_loopback = [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("::ffff:127.0.0.1", 8090, 0, 0),
            )
        ]
        with mock.patch("workbench_backend.remote_http.socket.getaddrinfo", return_value=mapped_loopback):
            endpoint = RemoteHttpClient("http://simulation.internal:8090")._resolve_endpoint()
        self.assertEqual(endpoint.effective_address, "127.0.0.1")

        mapped_private = [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("::ffff:10.20.30.40", 8090, 0, 0),
            )
        ]
        with mock.patch("workbench_backend.remote_http.socket.getaddrinfo", return_value=mapped_private):
            endpoint = RemoteHttpClient(
                "http://simulation.internal:8090", allowlist="10.20.30.0/24"
            )._resolve_endpoint()
        self.assertEqual(endpoint.effective_address, "10.20.30.40")

        mixed = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 8090)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", 8090)),
        ]
        with (
            mock.patch("workbench_backend.remote_http.socket.getaddrinfo", return_value=mixed),
            self.assertRaisesRegex(RemoteHttpError, "prohibited"),
        ):
            RemoteHttpClient("http://simulation.internal:8090")._resolve_endpoint()

        mapped_mixed = [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("::ffff:127.0.0.1", 8090, 0, 0),
            ),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("::ffff:169.254.169.254", 8090, 0, 0),
            ),
        ]
        with (
            mock.patch("workbench_backend.remote_http.socket.getaddrinfo", return_value=mapped_mixed),
            self.assertRaisesRegex(RemoteHttpError, "prohibited"),
        ):
            RemoteHttpClient("http://simulation.internal:8090", allowlist="0.0.0.0/0")._resolve_endpoint()

    def test_remote_source_pins_the_validated_dns_address(self) -> None:
        body = b'{"status":"ready"}'
        with response_server(lambda path: (200, json_headers(body), body)) as (base_url, hits):
            port = urllib.parse.urlsplit(base_url).port
            resolved = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
            ]
            with mock.patch("workbench_backend.remote_http.socket.getaddrinfo", return_value=resolved) as resolver:
                response = RemoteHttpClient(f"http://safe.example:{port}").get("/readyz")
        self.assertEqual(response.body, body)
        self.assertEqual(hits, ["/readyz"])
        self.assertEqual(resolver.call_count, 1)

    def test_remote_redirect_does_not_reach_second_local_server(self) -> None:
        target_body = b'{"runs":[]}'
        with response_server(lambda path: (200, json_headers(target_body), target_body)) as (
            target_url,
            target_hits,
        ):

            def redirect(path):
                return 302, [("Location", f"{target_url}/forbidden")], b""

            with response_server(redirect) as (redirect_url, redirect_hits):
                with self.assertRaisesRegex(RemoteHttpError, "redirect"):
                    RemoteHttpClient(redirect_url).get("/api/v1/runs")
        self.assertEqual(redirect_hits, ["/api/v1/runs"])
        self.assertEqual(target_hits, [])

    def test_remote_response_enforces_json_content_type(self) -> None:
        body = b'{"runs":[]}'
        with response_server(
            lambda path: (200, json_headers(body, content_type="application/json; charset=utf-8"), body)
        ) as (base_url, _):
            self.assertEqual(RemoteHttpClient(base_url).get("/api/v1/runs").body, body)

        for content_type in ("text/html", "application/problem+json", "application/json; boundary=unsafe", ""):
            headers = (
                json_headers(body, content_type=content_type) if content_type else [("Content-Length", str(len(body)))]
            )
            with (
                self.subTest(content_type=content_type),
                response_server(lambda path, headers=headers: (200, headers, body)) as (base_url, _),
                self.assertRaisesRegex(RemoteHttpError, "Content-Type"),
            ):
                RemoteHttpClient(base_url).get("/api/v1/runs")

    def test_remote_response_enforces_exact_size_boundary(self) -> None:
        exact = json_body_with_size(MAX_REMOTE_RESPONSE_BYTES)
        oversized = json_body_with_size(MAX_REMOTE_RESPONSE_BYTES + 1)
        for include_length in (True, False):
            with (
                self.subTest(exact=True, include_length=include_length),
                response_server(
                    lambda path, include_length=include_length: (
                        200,
                        json_headers(exact, include_length=include_length),
                        exact,
                    )
                ) as (base_url, _),
            ):
                response = RemoteHttpClient(base_url, timeout_s=2.0).get("/api/v1/runs")
                self.assertEqual(len(response.body), MAX_REMOTE_RESPONSE_BYTES)
                self.assertIsInstance(json.loads(response.body), dict)

        for include_length in (True, False):
            with (
                self.subTest(include_length=include_length),
                response_server(
                    lambda path, include_length=include_length: (
                        200,
                        json_headers(oversized, include_length=include_length),
                        oversized,
                    )
                ) as (base_url, _),
                self.assertRaises(RemoteHttpResponseTooLarge),
            ):
                RemoteHttpClient(base_url, timeout_s=2.0).get("/api/v1/runs")

    def test_remote_source_enforces_timeout_configuration_and_errors(self) -> None:
        self.assertEqual(RemoteHttpClient("http://127.0.0.1", timeout_s=2.0).timeout_s, 2.0)
        for timeout in (0.0, -1.0, math.nan, math.inf, -math.inf, 2.000001):
            with self.subTest(timeout=timeout), self.assertRaises(RemoteHttpConfigurationError):
                RemoteHttpClient("http://127.0.0.1", timeout_s=timeout)

        client = RemoteHttpClient("http://127.0.0.1:1")
        with (
            mock.patch("workbench_backend.remote_http._PinnedHTTPConnection.request", side_effect=TimeoutError),
            self.assertRaisesRegex(RemoteHttpError, "timed out"),
        ):
            client.get("/readyz")

        model = RemoteDashboardReadModel("http://127.0.0.1:1")
        with (
            mock.patch("workbench_backend.remote_http._PinnedHTTPConnection.request", side_effect=TimeoutError),
            self.assertRaisesRegex(ReadModelError, "unavailable"),
        ):
            model.list_runs()

        read_timeout_response = mock.Mock(status=200, fp=None)
        read_timeout_response.getheader.side_effect = lambda name, default=None: (
            "application/json" if name == "Content-Type" else default
        )
        read_timeout_response.read.side_effect = TimeoutError
        with (
            mock.patch("workbench_backend.remote_http._PinnedHTTPConnection.request"),
            mock.patch(
                "workbench_backend.remote_http._PinnedHTTPConnection.getresponse",
                return_value=read_timeout_response,
            ),
            self.assertRaisesRegex(ReadModelError, "unavailable"),
        ):
            model.list_runs()
        read_timeout_response.read.assert_called_once()

    def test_invalid_remote_configuration_is_live_but_not_ready(self) -> None:
        cases = (
            {"event_source_url": "https://simulation.internal:8090"},
            {
                "event_source_url": "http://10.20.30.40:8090",
                "event_source_allowlist": None,
            },
            {
                "event_source_url": "http://10.20.30.40:8090",
                "event_source_allowlist": "",
            },
            {
                "event_source_url": "http://10.20.30.40:8090",
                "event_source_allowlist": "   ",
            },
            {
                "event_source_url": "http://10.20.30.40:8090",
                "event_source_allowlist": "not-a-network",
            },
            {
                "event_source_url": "http://10.20.30.40:8090",
                "event_source_allowlist": 123,
            },
            {"event_source_url": "http://☃.example:8090"},
        )
        for options in cases:
            with self.subTest(options=options):
                server = create_server("127.0.0.1", 0, **options)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                try:
                    with urllib.request.urlopen(f"{base_url}/healthz", timeout=2) as response:
                        self.assertEqual(response.status, 200)
                    for path in ("/readyz", "/api/v1/runs"):
                        with self.assertRaises(urllib.error.HTTPError) as caught:
                            urllib.request.urlopen(f"{base_url}{path}", timeout=2)
                        self.assertEqual(caught.exception.code, 503)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_cli_allowlist_defaults_to_environment(self) -> None:
        event_source_url = "http://10.20.30.40:8090"
        environment_allowlist = "10.20.30.40/32,2001:db8:1::/48"
        fake_server = mock.Mock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        argv = ["workbench-backend", "--event-source-url", event_source_url]
        with (
            mock.patch.dict(
                os.environ,
                {"WORKBENCH_EVENT_SOURCE_ALLOWLIST": environment_allowlist},
                clear=False,
            ),
            mock.patch.object(sys, "argv", argv),
            mock.patch("workbench_backend.server.create_server", return_value=fake_server) as factory,
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(factory.call_args.kwargs["event_source_url"], event_source_url)
        self.assertEqual(factory.call_args.kwargs["event_source_allowlist"], environment_allowlist)
        fake_server.server_close.assert_called_once()

    def test_cli_allowlist_argument_overrides_environment(self) -> None:
        event_source_url = "http://10.20.30.40:8090"
        environment_allowlist = "10.20.30.0/24"
        cli_allowlist = "10.20.30.40/32"
        fake_server = mock.Mock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        argv = [
            "workbench-backend",
            "--event-source-url",
            event_source_url,
            "--event-source-allowlist",
            cli_allowlist,
        ]
        with (
            mock.patch.dict(
                os.environ,
                {"WORKBENCH_EVENT_SOURCE_ALLOWLIST": environment_allowlist},
                clear=False,
            ),
            mock.patch.object(sys, "argv", argv),
            mock.patch("workbench_backend.server.create_server", return_value=fake_server) as factory,
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(factory.call_args.kwargs["event_source_url"], event_source_url)
        self.assertEqual(factory.call_args.kwargs["event_source_allowlist"], cli_allowlist)
        fake_server.server_close.assert_called_once()

    def test_cli_allowlist_is_none_when_environment_is_unset(self) -> None:
        event_source_url = "http://10.20.30.40:8090"
        fake_server = mock.Mock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        argv = ["workbench-backend", "--event-source-url", event_source_url]
        environment = os.environ.copy()
        environment.pop("WORKBENCH_EVENT_SOURCE_ALLOWLIST", None)
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(sys, "argv", argv),
            mock.patch("workbench_backend.server.create_server", return_value=fake_server) as factory,
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(factory.call_args.kwargs["event_source_url"], event_source_url)
        self.assertIsNone(factory.call_args.kwargs["event_source_allowlist"])
        fake_server.server_close.assert_called_once()

    def test_cli_allowlist_preserves_blank_environment_values(self) -> None:
        event_source_url = "http://10.20.30.40:8090"
        for environment_allowlist in ("", "   "):
            with self.subTest(environment_allowlist=environment_allowlist):
                fake_server = mock.Mock()
                fake_server.serve_forever.side_effect = KeyboardInterrupt
                argv = ["workbench-backend", "--event-source-url", event_source_url]
                with (
                    mock.patch.dict(
                        os.environ,
                        {"WORKBENCH_EVENT_SOURCE_ALLOWLIST": environment_allowlist},
                        clear=False,
                    ),
                    mock.patch.object(sys, "argv", argv),
                    mock.patch("workbench_backend.server.create_server", return_value=fake_server) as factory,
                ):
                    self.assertEqual(main(), 0)
                self.assertEqual(factory.call_args.kwargs["event_source_url"], event_source_url)
                self.assertEqual(factory.call_args.kwargs["event_source_allowlist"], environment_allowlist)
                fake_server.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
