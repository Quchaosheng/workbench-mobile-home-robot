import argparse
import json
import mimetypes
import os
import re
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import unquote, urlparse

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
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server with a hard bound before worker creation."""

    daemon_threads = True

    def __init__(self, server_address, request_handler_class, *, max_concurrent_requests: int) -> None:
        if not 1 <= max_concurrent_requests <= MAX_CONCURRENT_REQUESTS:
            raise ValueError(f"max_concurrent_requests must be between 1 and {MAX_CONCURRENT_REQUESTS}")
        self.max_concurrent_requests = max_concurrent_requests
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self.request_queue_size = max_concurrent_requests
        super().__init__(server_address, request_handler_class)

    def process_request(self, request, client_address) -> None:
        if not self._request_slots.acquire(blocking=False):
            body = json.dumps(
                {"error": "server_busy", "message": "The server has reached its request concurrency limit."},
                allow_nan=False,
            ).encode()
            response = (
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Cache-Control: no-store\r\n"
                + b"X-Content-Type-Options: nosniff\r\n"
                + b"Connection: close\r\n\r\n"
                + body
            )
            try:
                request.sendall(response)
                # Half-close after the complete response to avoid a TCP reset
                # on Windows when the client has no unread request body.
                request.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class DashboardHandler(BaseHTTPRequestHandler):
    read_model = DashboardReadModel(DEFAULT_DATA_DIR)
    static_dir = DEFAULT_STATIC_DIR
    logger = StructuredLogger("workbench-backend")
    data_source = "dashboard-fixtures"
    inbound_policy = InboundHttpPolicy()
    server_version = "WorkbenchBackend/0.1"

    def log_message(self, format: str, *args) -> None:
        self.logger.emit("http_access", format % args, details={"client": self.client_address[0]})

    def _send_json(
        self, payload: object, status: HTTPStatus = HTTPStatus.OK, *, api_version: str | None = None
    ) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError):
            # Includes UnicodeError from UTF-8 encoding. No response headers have
            # been sent, and the fixed fallback never re-enters a failing encoder.
            status = HTTPStatus.SERVICE_UNAVAILABLE
            body = b'{"error":"response_serialization_failed","message":"The response could not be encoded as JSON."}'
        if len(body) > MAX_RESPONSE_BYTES:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            body = json.dumps(
                {"error": "response_too_large", "message": "The requested read model exceeds the response limit."},
                allow_nan=False,
            ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if api_version:
            self.send_header("X-API-Version", api_version)
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

    def do_GET(self) -> None:
        if not self._authorize_request() or self._validated_content_length(body_allowed=False) is None:
            return
        try:
            self._do_get()
        except ReadModelResponseTooLarge:
            self._send_json(
                {"error": "response_too_large", "message": "The requested read model exceeds the response limit."},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        except ReadModelError as exc:
            self.logger.emit("read_model_error", str(exc), level="ERROR")
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
            ready = self.read_model.ready()
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

    def _reject_write(self) -> None:
        if not self._authorize_request():
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
    published_host: str = "127.0.0.1",
    trust_mode: str = "local",
    trusted_proxy_allowlist: str | None = None,
    max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
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

    class ConfiguredHandler(DashboardHandler):
        read_model = configured_read_model
        static_dir = configured_static_dir
        data_source = configured_read_model.data_source if event_source_url else "dashboard-fixtures"
        inbound_policy = configured_inbound_policy

    return BoundedThreadingHTTPServer(
        (host, port),
        ConfiguredHandler,
        max_concurrent_requests=max_concurrent_requests,
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
        )
    except InboundHttpConfigurationError as exc:
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
