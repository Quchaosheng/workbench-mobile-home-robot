"""Bounded loopback client for the OmniSim World Harness protocol."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 130.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class OmniSimError(RuntimeError):
    """Base error for the isolated OmniSim integration."""


class OmniSimUnavailable(OmniSimError):
    """The configured loopback harness could not be reached."""


class OmniSimRequestError(OmniSimError):
    """The harness rejected a valid HTTP request."""


class OmniSimProtocolError(OmniSimError):
    """The harness response did not satisfy the expected protocol."""


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True)
class OmniSimClient:
    """Small HTTP/JSON client restricted to a local OmniSim harness."""

    base_url: str = "http://127.0.0.1:6789"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOOPBACK_HOSTS:
            raise ValueError("base_url must be an http(s) loopback URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, a query, or a fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("base_url must not contain a path")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if self.max_response_bytes <= 0 or self.max_response_bytes > 64 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 1 and 67108864")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def load_world(self, path: str, *, wait_seconds: float = 30.0, light: bool = True) -> dict[str, Any]:
        """Load one world and return the harness's load report.

        The report is trusted to name the world it actually loaded: the pilot
        reads the world digest from ``world.digest`` (or the top-level
        ``digest``) and refuses the run when neither is present, so a harness
        that cannot say which revision it loaded cannot produce a pilot identity.
        """
        if not isinstance(path, str) or not path.strip():
            raise ValueError("world path must be non-empty")
        if wait_seconds <= 0 or wait_seconds > 120:
            raise ValueError("wait_seconds must be between 0 and 120")
        return self._request(
            "POST",
            "/world/load",
            {"path": path, "wait_s": wait_seconds, "with_supervisor": True, "light": light},
        )

    def capabilities(self) -> dict[str, Any]:
        """Return the harness capability report, which may also carry the digest."""
        return self._request("GET", "/capabilities")

    def reset(self) -> dict[str, Any]:
        return self._request("POST", "/sim/reset", {"restore": "__init__", "verify": True, "settle_steps": 1})

    def step(self, steps: int) -> dict[str, Any]:
        if type(steps) is not int or not 1 <= steps <= 100:
            raise ValueError("steps must be an integer between 1 and 100")
        return self._request("POST", "/sim/step", {"steps": steps})

    def events(self, *, limit: int = 1024) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise ValueError("limit must be an integer between 1 and 1024")
        query = urllib.parse.urlencode({"since": 0, "log_since": 0, "limit": limit})
        return self._request("GET", f"/sim/events?{query}")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Accept": "application/json",
                "Accept-Protocol-Version": "1.1",
                "Content-Type": "application/json; charset=utf-8",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = _read_bounded(response, self.max_response_bytes)
        except urllib.error.HTTPError as error:
            raw = error.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise OmniSimProtocolError("OmniSim error response exceeded the configured limit") from error
            code = _error_code(raw)
            raise OmniSimRequestError(f"OmniSim {method} request failed with HTTP {error.code}: {code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise OmniSimUnavailable(f"OmniSim harness is unavailable at {self.base_url}") from error
        return _decode_object(raw)


def _read_bounded(response: Any, limit: int) -> bytes:
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            if int(length) > limit:
                raise OmniSimProtocolError("OmniSim response exceeded the configured limit")
        except ValueError as error:
            raise OmniSimProtocolError("OmniSim returned an invalid Content-Length") from error
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise OmniSimProtocolError("OmniSim response exceeded the configured limit")
    return raw


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _decode_object(raw: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates)
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKey) as error:
        raise OmniSimProtocolError("OmniSim returned invalid strict UTF-8 JSON") from error
    if not isinstance(decoded, dict):
        raise OmniSimProtocolError("OmniSim response must be a JSON object")
    return decoded


def _error_code(raw: bytes) -> str:
    try:
        decoded = _decode_object(raw)
    except OmniSimProtocolError:
        return "invalid_error_response"
    code = decoded.get("error")
    return code if isinstance(code, str) and code else "unknown_error"
