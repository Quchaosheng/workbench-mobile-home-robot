import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from integrations.omnilink import OmniLinkClient, OmniLinkError, OmniLinkResponseTooLarge, RunSummaryExporter


class Handler(BaseHTTPRequestHandler):
    response: ClassVar[dict[str, Any]] = {"results": [{"link": {"title": "Guide"}}]}
    requests: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.__class__.requests.append((self.path, json.loads(self.rfile.read(length))))
        body = json.dumps(self.response).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def server():
    Handler.requests = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_search_and_export_send_only_safe_projection(server):
    client = OmniLinkClient(server)
    assert client.search("robot safety")["results"]
    Handler.response = {"link": {"id": "x"}}
    RunSummaryExporter(client, server).export(
        {"run_id": "run-1", "status": "confirmed", "goal": "secret?", "event_count": 3, "evidence_refs": ["sha256:abc"]}
    )
    assert Handler.requests[0][0] == "/api/ai/search/hybrid"
    path, payload = Handler.requests[1]
    assert path == "/api/links"
    assert payload["autoAiExtract"] is False
    assert "secret?" in payload["notes"]
    assert "events" not in payload["notes"]


def test_invalid_response_and_size_fail_closed(server):
    Handler.response = {"unexpected": True}
    with pytest.raises(OmniLinkError):
        OmniLinkClient(server).search("x")
    Handler.response = {"results": ["x" * 1000]}
    with pytest.raises(OmniLinkResponseTooLarge):
        OmniLinkClient(server, max_response_bytes=50).search("x")


class RawHandler(BaseHTTPRequestHandler):
    """Serves exact bytes so a malformed response can be exercised."""

    body: ClassVar[bytes] = b"{}"
    content_length: ClassVar[str | None] = None
    status: ClassVar[int] = 200

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        if self.content_length is not None:
            self.send_header("Content-Length", self.content_length)
        else:
            self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def raw_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RawHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_a_duplicate_key_is_refused(raw_server):
    """#452: the response is a trust boundary, so an ambiguous object fails closed."""

    RawHandler.body = b'{"results": [], "results": []}'
    RawHandler.content_length = None
    with pytest.raises(OmniLinkError, match="strict"):
        OmniLinkClient(raw_server).search("x")


def test_a_non_finite_constant_is_refused(raw_server):
    """#452: NaN is not JSON, and accepting it reads a value nothing else would."""

    RawHandler.body = b'{"results": [{"score": NaN}]}'
    RawHandler.content_length = None
    with pytest.raises(OmniLinkError, match="strict"):
        OmniLinkClient(raw_server).search("x")


def test_a_malformed_content_length_is_a_typed_refusal(raw_server):
    """#453: a hostile header must not escape as ValueError."""

    RawHandler.body = b"{}"
    RawHandler.content_length = "not-a-number"
    with pytest.raises(OmniLinkError, match="Content-Length"):
        OmniLinkClient(raw_server).search("x")
    RawHandler.content_length = None


def test_a_negative_content_length_is_a_typed_refusal(raw_server):
    RawHandler.body = b"{}"
    RawHandler.content_length = "-1"
    with pytest.raises(OmniLinkError, match="Content-Length"):
        OmniLinkClient(raw_server).search("x")
    RawHandler.content_length = None
