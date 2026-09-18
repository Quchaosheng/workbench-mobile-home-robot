import argparse
import json
import math
import mimetypes
import os
import re
import signal
import socket
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .health import HealthHistoryError, HealthReadModel
from .inbound_http import InboundHttpConfigurationError, InboundHttpPolicy
from .logging import StructuredLogger
from .read_model import (
    DashboardReadModel,
    ReadModelError,
    ReadModelResponseTooLarge,
    RemoteDashboardReadModel,
    UnavailableRemoteDashboardReadModel,
)
from .remote_http import RemoteHttpError

SOURCE_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path.cwd() if (Path.cwd() / "apps" / "dashboard").is_dir() else SOURCE_ROOT
DEFAULT_STATIC_DIR = ROOT / "apps" / "dashboard"
DEFAULT_DATA_DIR = DEFAULT_STATIC_DIR / "data"
OPENAPI_RESOURCE = resources.files("workbench_backend").joinpath("api-openapi-v1.json")
API_VERSION = "1"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REJECTED_REQUEST_BODY_BYTES = 1024 * 1024
MAX_CONCURRENT_REQUESTS = 16
DEFAULT_DRAIN_TIMEOUT_SECONDS = 5.0
MAX_DRAIN_TIMEOUT_SECONDS = 30.0
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server with a hard bound and an explicit drain contract.

    Shutdown is fail-closed in three ordered steps: readiness starts reporting
    NOT_READY, new requests are refused with a retryable 503, and only then is
    the accept loop stopped. In-flight handlers are given `drain_timeout` to
    finish, after which the server is closed instead of blocking forever.
    """

    daemon_threads = True

    def __init__(
        self,
        server_address,
        request_handler_class,
        *,
        max_concurrent_requests: int,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        if not 1 <= max_concurrent_requests <= MAX_CONCURRENT_REQUESTS:
            raise ValueError(f"max_concurrent_requests must be between 1 and {MAX_CONCURRENT_REQUESTS}")
        if not 0 <= drain_timeout <= MAX_DRAIN_TIMEOUT_SECONDS:
            raise ValueError(f"drain_timeout must be between 0 and {MAX_DRAIN_TIMEOUT_SECONDS} seconds")
        self.max_concurrent_requests = max_concurrent_requests
        self.drain_timeout = float(drain_timeout)
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self._condition = threading.Condition()
        self._active_requests = 0
        self._draining = False
        self.request_queue_size = max_concurrent_requests
        super().__init__(server_address, request_handler_class)

    @property
    def draining(self) -> bool:
        """True once shutdown began: readiness is NOT_READY and new work is refused."""
        with self._condition:
            return self._draining

    @property
    def active_request_count(self) -> int:
        with self._condition:
            return self._active_requests

    def begin_drain(self) -> None:
        """Publish NOT_READY and start refusing new requests."""
        with self._condition:
            self._draining = True
            self._condition.notify_all()

    def wait_for_idle(self, timeout: float) -> bool:
        """Wait until no request handler is running. Returns True when idle."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _refuse(self, request, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode()
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Cache-Control: no-store\r\n"
            + b"Retry-After: 1\r\n"
            + b"X-Content-Type-Options: nosniff\r\n"
            + b"Connection: close\r\n\r\n"
            + body
        )
        try:
            request.sendall(response)
            # Half-close after the complete response to avoid a TCP reset on
            # Windows when the client has no unread request body.
            request.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self.shutdown_request(request)

    def process_request(self, request, client_address) -> None:
        # The concurrency bound is enforced here, before a worker thread exists,
        # so an overloaded server cannot be made to allocate unbounded threads.
        # Draining is deliberately NOT decided here: this layer cannot see the
        # request path, and the life-cycle probes must still answer so that an
        # orchestrator can observe NOT_READY. The handler refuses drained work
        # for every other route.
        if not self._reserve_slot():
            self._refuse(
                request,
                {"error": "server_busy", "message": "The server has reached its request concurrency limit."},
            )
            return
        with self._condition:
            self._active_requests += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._finish_request()
            raise

    def _reserve_slot(self) -> bool:
        return self._request_slots.acquire(blocking=False)

    def _finish_request(self) -> None:
        self._request_slots.release()
        with self._condition:
            self._active_requests -= 1
            self._condition.notify_all()

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._finish_request()

    def shutdown_with_drain(self, drain_timeout: float | None = None) -> bool:
        """Drain in flight work, then stop the accept loop.

        The order matters: readiness must flip and new work must be refused while
        the listening socket is still open, otherwise an orchestrator has no
        chance to observe NOT_READY before the port disappears. Returns True when
        every in-flight request finished before the deadline. The accept loop is
        stopped either way, so a wedged handler cannot keep the process alive.
        """
        timeout = self.drain_timeout if drain_timeout is None else float(drain_timeout)
        self.begin_drain()
        drained = self.wait_for_idle(timeout)
        self.shutdown()
        return drained


class DashboardHandler(BaseHTTPRequestHandler):
    read_model = DashboardReadModel(DEFAULT_DATA_DIR)
    # The health document lives in its own subdirectory. Run logs are discovered
    # with a top-level glob("*.jsonl"), so a sibling health.jsonl would be parsed
    # as a run and fail the whole read model; a subdirectory keeps the two
    # artifact families from colliding.
    health_model = HealthReadModel(Path(DEFAULT_DATA_DIR) / "health" / "health.jsonl")
    static_dir = DEFAULT_STATIC_DIR
    logger = StructuredLogger("workbench-backend")
    data_source = "dashboard-fixtures"
    inbound_policy = InboundHttpPolicy()
    server_version = "WorkbenchBackend/0.1"

    def log_message(self, format: str, *args) -> None:
        self.logger.emit("http_access", format % args, details={"client": self.client_address[0]})

    def _send_json(
        self,
        payload: object,
        status: HTTPStatus = HTTPStatus.OK,
        *,
        api_version: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        try:
            # allow_nan=False is what keeps the response parseable by a strict
            # client. The default encoder emits bare NaN/Infinity, which is not
            # JSON, so a browser's response.json() rejects the whole body while
            # this server reports success.
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError):
            # Handled before any header is written, so the client receives a
            # complete error response instead of a truncated 200.
            self._send_unencodable_response()
            return
        if len(body) > MAX_RESPONSE_BYTES:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            body = json.dumps(
                {"error": "response_too_large", "message": "The requested read model exceeds the response limit."}
            ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if api_version:
            self.send_header("X-API-Version", api_version)
        if retry_after is not None:
            # Tell a client that the refusal is transient, so it backs off and
            # retries instead of treating the instance as permanently gone.
            self.send_header("Retry-After", str(retry_after))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_unencodable_response(self) -> None:
        """Report a read model that cannot be encoded as strict JSON.

        The payload is never echoed: it is the value that failed to encode, and
        its contents are exactly what must not reach the client.
        """
        body = json.dumps(
            {
                "error": "response_not_encodable",
                "message": "The read model contains a value that is not valid JSON.",
            }
        ).encode()
        self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _authorize_request(self) -> bool:
        if self.inbound_policy.allows_peer(self.client_address[0]):
            return True
        self.close_connection = True
        self._send_json(
            {"error": "untrusted_client", "message": "The client is outside the trusted proxy boundary."},
            HTTPStatus.FORBIDDEN,
        )
        return False

    def _validated_content_length(self, *, body_allowed: bool) -> int | None:
        if self.headers.get_all("Transfer-Encoding", []):
            self.close_connection = True
            self._send_json({"error": "invalid_request_body"}, HTTPStatus.BAD_REQUEST)
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            self.close_connection = True
            self._send_json({"error": "invalid_request_body"}, HTTPStatus.BAD_REQUEST)
            return None
        raw_length = lengths[0].strip() if lengths else "0"
        if not re.fullmatch(r"[0-9]+", raw_length):
            self.close_connection = True
            self._send_json({"error": "invalid_request_body"}, HTTPStatus.BAD_REQUEST)
            return None
        normalized_length = raw_length.lstrip("0") or "0"
        maximum_length = str(MAX_REJECTED_REQUEST_BODY_BYTES)
        if len(normalized_length) > len(maximum_length) or (
            len(normalized_length) == len(maximum_length) and normalized_length > maximum_length
        ):
            self.close_connection = True
            self._send_json({"error": "request_too_large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        content_length = int(normalized_length)
        if not body_allowed and content_length:
            self.close_connection = True
            self._send_json({"error": "invalid_request_body"}, HTTPStatus.BAD_REQUEST)
            return None
        return content_length

    def _send_file(self, relative_path: str) -> None:
        try:
            requested = (self.static_dir / relative_path).resolve()
            static_root = self.static_dir.resolve()
        except (OSError, RuntimeError):
            self._send_json({"error": "invalid_path"}, HTTPStatus.BAD_REQUEST)
            return
        if requested != static_root and static_root not in requested.parents:
            self._send_json({"error": "invalid_path"}, HTTPStatus.BAD_REQUEST)
            return
        if not requested.is_file():
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            stat = requested.stat()
        except OSError:
            self._send_json({"error": "asset_unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        cache_control = (
            "public, max-age=31536000, immutable"
            if "vendor" in requested.relative_to(static_root).parts
            else "no-cache"
        )
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self._send_security_headers()
            self.end_headers()
            return
        try:
            body = requested.read_bytes()
        except OSError:
            self._send_json({"error": "asset_unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        content_type, _ = mimetypes.guess_type(requested.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache_control)
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _drain_request_body(self, content_length: int) -> bool:
        """Consume a bounded rejected body so the 405 response is not reset."""
        if content_length == 0:
            return True
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(1.0)
            remaining = content_length
            while remaining:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    return False
                remaining -= len(chunk)
            return True
        except (TimeoutError, OSError):
            return False
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                pass

    def _server_is_draining(self) -> bool:
        return bool(getattr(self.server, "draining", False))

    def _reject_if_draining(self) -> bool:
        """Refuse new work once shutdown began, before any read model access."""
        if not self._server_is_draining():
            return False
        self.close_connection = True
        self._send_json(
            {
                "error": "server_draining",
                "message": "The server is shutting down and is no longer accepting requests.",
            },
            HTTPStatus.SERVICE_UNAVAILABLE,
            retry_after=1,
        )
        return True

    def do_GET(self) -> None:
        if not self._authorize_request() or self._validated_content_length(body_allowed=False) is None:
            return
        # Liveness stays up during a drain - the process is still running and
        # finishing in-flight work. Only readiness and new work change, so an
        # orchestrator removes the instance from rotation without killing it
        # mid-request.
        if self.path.split("?", 1)[0] not in {"/healthz", "/readyz"} and self._reject_if_draining():
            return
        try:
            self._do_get()
        except ReadModelResponseTooLarge:
            self._send_json(
                {"error": "response_too_large", "message": "The requested read model exceeds the response limit."},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        except ReadModelError as exc:
            # The exception text is the one place a bad event source can echo a
            # credential back at us, so the message is produced by the logger
            # instead of being formatted here and then sanitized.
            self.logger.emit_failure("read_model_error", exc)
            self._send_json(
                {"error": "invalid_event_source", "message": "The event source is unavailable or malformed."},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

    def _do_get(self) -> None:
        route = urlparse(self.path).path
        api_version = API_VERSION if route.startswith("/api/v1/") else None
        if route == "/healthz":
            self._send_json({"status": "ok", "service": "workbench-backend", "version": "0.2.0"})
            return
        if route == "/readyz":
            # Readiness flips to NOT_READY as soon as shutdown begins, so a load
            # balancer stops sending work before the process stops accepting it.
            ready = self.read_model.ready() and not self._server_is_draining()
            self._send_json(
                {"status": "ready" if ready else "not_ready", "data_source": self.data_source},
                HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if route in {"/api/openapi.json", "/api/v1/openapi.json"}:
            try:
                contract = json.loads(OPENAPI_RESOURCE.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                self._send_json(
                    {"error": "contract_unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE, api_version=api_version
                )
                return
            self._send_json(contract, api_version=api_version)
            return
        if route in {"/api/runs", "/api/v1/runs"}:
            self._send_json({"runs": self.read_model.list_runs(), "read_only": True}, api_version=api_version)
            return
        if route in {"/api/expression-states", "/api/v1/expression-states"}:
            self._send_json(self.read_model.expression_contract(), api_version=api_version)
            return
        if route in {"/api/health", "/api/v1/health"}:
            # Read-only current health, active alerts and bounded history. It
            # never writes, clears an alarm or acknowledges anything.
            self._send_health(lambda: self.health_model.current_payload(), api_version=api_version)
            return
        if route in {"/api/health/history", "/api/v1/health/history"}:
            try:
                since = self._health_since()
            except ValueError:
                self._send_json({"error": "invalid_since"}, HTTPStatus.BAD_REQUEST, api_version=api_version)
                return
            self._send_health(lambda: self.health_model.history_payload(since=since), api_version=api_version)
            return
        run_prefix = "/api/v1/runs/" if route.startswith("/api/v1/runs/") else "/api/runs/"
        if route.startswith(run_prefix):
            suffix = unquote(route.removeprefix(run_prefix))
            include_events = suffix.endswith("/events")
            run_id = suffix.removesuffix("/events") if include_events else suffix
            if not RUN_ID_PATTERN.fullmatch(run_id):
                self._send_json({"error": "invalid_run_id"}, HTTPStatus.BAD_REQUEST, api_version=api_version)
                return
            try:
                events = self.read_model.list_events(run_id)
            except KeyError:
                self._send_json(
                    {"error": "run_not_found", "run_id": run_id}, HTTPStatus.NOT_FOUND, api_version=api_version
                )
                return
            payload = {"run": self.read_model.summarize(events)}
            if include_events:
                payload["events"] = events
            self._send_json(payload, api_version=api_version)
            return
        static_path = "index.html" if route in {"", "/"} else route.lstrip("/")
        self._send_file(static_path)

    def _health_since(self) -> float | None:
        """Parse the optional ``since`` history filter.

        Raises ``ValueError`` when the filter is present but unusable, so the
        caller can answer 400 instead of silently ignoring a malformed bound.
        """
        values = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("since")
        if not values:
            return None
        if len(values) != 1:
            raise ValueError("since must appear once")
        try:
            value = float(values[0])
        except (TypeError, ValueError):
            raise ValueError("since must be a number") from None
        if not math.isfinite(value) or value < 0:
            raise ValueError("since must be a finite non-negative number")
        return value

    def _send_health(self, project: "Callable[[], dict[str, Any]]", *, api_version: str | None = None) -> None:
        """Serve one health projection, failing closed on a bad document."""
        try:
            payload = project()
        except HealthHistoryError:
            # The document is present but not trustworthy. Report a transient,
            # retryable failure instead of a partial or inferred projection.
            self._send_json(
                {"error": "invalid_health_source", "message": "The health document is unavailable or malformed."},
                HTTPStatus.SERVICE_UNAVAILABLE,
                api_version=api_version,
            )
            return
        self._send_json(payload, api_version=api_version)

    def _reject_write(self) -> None:
        if not self._authorize_request():
            return
        if self._reject_if_draining():
            return
        content_length = self._validated_content_length(body_allowed=True)
        if content_length is None:
            return
        if not self._drain_request_body(content_length):
            self.close_connection = True
            self._send_json({"error": "incomplete_request_body"}, HTTPStatus.BAD_REQUEST)
            return
        self.close_connection = True
        self._send_json(
            {"error": "read_only", "message": "This service exposes no robot or ROS control operations."},
            HTTPStatus.METHOD_NOT_ALLOWED,
        )

    do_POST = _reject_write
    do_PUT = _reject_write
    do_PATCH = _reject_write
    do_DELETE = _reject_write


def create_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    static_dir: str | Path = DEFAULT_STATIC_DIR,
    event_source_url: str | None = None,
    event_source_allowlist: str | None = None,
    health_path: str | Path | None = None,
    published_host: str = "127.0.0.1",
    trust_mode: str = "local",
    trusted_proxy_allowlist: str | None = None,
    max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
    drain_timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
) -> BoundedThreadingHTTPServer:
    inbound_policy = InboundHttpPolicy(
        published_host=published_host,
        trust_mode=trust_mode,
        trusted_proxy_allowlist=trusted_proxy_allowlist,
    )
    configured_inbound_policy = inbound_policy
    if event_source_url:
        try:
            configured_read_model = RemoteDashboardReadModel(
                event_source_url,
                event_source_allowlist=event_source_allowlist,
            )
        except RemoteHttpError as exc:
            configured_read_model = UnavailableRemoteDashboardReadModel(exc)
    else:
        configured_read_model = DashboardReadModel(data_dir)
    configured_static_dir = Path(static_dir)
    configured_health_model = HealthReadModel(
        health_path if health_path is not None else Path(data_dir) / "health" / "health.jsonl"
    )

    class ConfiguredHandler(DashboardHandler):
        read_model = configured_read_model
        health_model = configured_health_model
        static_dir = configured_static_dir
        data_source = configured_read_model.data_source if event_source_url else "dashboard-fixtures"
        inbound_policy = configured_inbound_policy

    return BoundedThreadingHTTPServer(
        (host, port),
        ConfiguredHandler,
        max_concurrent_requests=max_concurrent_requests,
        drain_timeout=drain_timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the read-only Workbench-1 dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--event-source-url", default=os.environ.get("WORKBENCH_EVENT_SOURCE_URL"))
    parser.add_argument(
        "--event-source-allowlist",
        default=os.environ.get("WORKBENCH_EVENT_SOURCE_ALLOWLIST"),
    )
    parser.add_argument("--published-host", default=os.environ.get("CONTROLLER_BIND_ADDRESS", "127.0.0.1"))
    parser.add_argument("--trust-mode", default=os.environ.get("WORKBENCH_CONTROLLER_TRUST_MODE", "local"))
    parser.add_argument(
        "--trusted-proxy-allowlist",
        default=os.environ.get("WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST"),
    )
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=float(os.environ.get("WORKBENCH_DRAIN_TIMEOUT_SECONDS", DEFAULT_DRAIN_TIMEOUT_SECONDS)),
        help="Seconds to let in-flight requests finish after SIGTERM or Ctrl-C before exiting.",
    )
    args = parser.parse_args()
    try:
        server = create_server(
            args.host,
            args.port,
            data_dir=args.data_dir,
            event_source_url=args.event_source_url,
            event_source_allowlist=args.event_source_allowlist,
            published_host=args.published_host,
            trust_mode=args.trust_mode,
            trusted_proxy_allowlist=args.trusted_proxy_allowlist,
            drain_timeout=args.drain_timeout,
        )
    except (InboundHttpConfigurationError, ValueError) as exc:
        parser.error(str(exc))
    DashboardHandler.logger.emit(
        "service_started",
        f"dashboard listening on http://{args.host}:{args.port}",
        details={
            "offline": True,
            "read_only": True,
            "data_source": "remote" if args.event_source_url else "local",
            "published_host": args.published_host,
            "trust_mode": args.trust_mode,
        },
    )
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    stop = threading.Event()
    stop_lock = threading.Lock()

    def request_stop(_signum=None, _frame=None) -> None:
        """Fail closed on SIGTERM without doing blocking work in the handler.

        `serve_forever` runs on this same thread, so `shutdown()` cannot be called
        directly here - it waits for the very loop it would be blocking. The flag
        is published immediately so readiness already reports NOT_READY, and the
        loop is unblocked from a helper thread after the drain deadline.
        """
        with stop_lock:
            if stop.is_set():
                return
            stop.set()
        server.begin_drain()
        threading.Thread(target=server.shutdown_with_drain, name="workbench-backend-drain", daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        request_stop()
    finally:
        # The configured timeout is reported, not whatever the server object
        # happens to expose, so the record always matches the operator's intent.
        drained = bool(server.shutdown_with_drain(args.drain_timeout))
        DashboardHandler.logger.emit(
            "service_draining",
            "dashboard stopped accepting requests",
            level="INFO" if drained else "WARNING",
            details={"drained": drained, "drain_timeout_s": args.drain_timeout},
        )
        server.server_close()
        signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
