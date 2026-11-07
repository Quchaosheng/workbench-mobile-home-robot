"""Small, fail-closed REST client for a separately hosted OmniLink instance."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

MAX_RESPONSE_BYTES = 1 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0


class OmniLinkError(RuntimeError):
    """OmniLink is unavailable or returned an invalid response."""


class OmniLinkResponseTooLarge(OmniLinkError):
    """The remote response exceeded the integration limit."""


class _DuplicateJsonKey(ValueError):
    """Raised while parsing a response that names the same key twice."""


class _NonFiniteJsonConstant(ValueError):
    """Raised while parsing a response that carries NaN or Infinity."""


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    # NaN and Infinity are Python extensions, not JSON. Accepting them made the
    # parser read a value no other JSON consumer would (issue #452).
    raise _NonFiniteJsonConstant(f"non-finite JSON constant: {name}")


def _decode_object(raw: bytes) -> dict[str, Any]:
    """Parse an OmniLink response with the strict rules the other clients use.

    The response is an external trust boundary: duplicate keys are ambiguous and
    non-finite constants are not JSON, so both fail closed with a bounded
    diagnostic rather than being resolved by parser order (issue #452).
    """
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKey, _NonFiniteJsonConstant) as exc:
        raise OmniLinkError("OmniLink returned invalid strict UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise OmniLinkError("OmniLink response must be a JSON object")
    return decoded


@dataclass(frozen=True)
class OmniLinkClient:
    base_url: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL")
        if self.timeout_seconds <= 0 or self.max_response_bytes <= 0:
            raise ValueError("timeout and response limit must be positive")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                # A malformed Content-Length is a typed refusal, not a ValueError
                # escaping this method (issue #453). int() is called once, inside
                # its own try, so a hostile header cannot reach the caller as a
                # standard-library exception.
                raw_length = response.headers.get("Content-Length")
                if raw_length is not None:
                    try:
                        declared = int(raw_length)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise OmniLinkError("OmniLink returned an invalid Content-Length") from exc
                    if declared < 0:
                        raise OmniLinkError("OmniLink returned an invalid Content-Length")
                    if declared > self.max_response_bytes:
                        raise OmniLinkResponseTooLarge("OmniLink response exceeds configured limit")
                raw = response.read(self.max_response_bytes + 1)
        except OmniLinkResponseTooLarge:
            raise
        except OmniLinkError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OmniLinkError("OmniLink request failed") from exc
        if len(raw) > self.max_response_bytes:
            raise OmniLinkResponseTooLarge("OmniLink response exceeds configured limit")
        return _decode_object(raw)

    def search(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query must be non-empty")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        result = self._post("/api/ai/search/hybrid", {"query": query.strip(), "limit": limit})
        if not isinstance(result.get("results"), list):
            raise OmniLinkError("OmniLink search response is missing results")
        return result

    def ask(self, question: str) -> dict[str, Any]:
        if not question.strip():
            raise ValueError("question must be non-empty")
        result = self._post("/api/ai/ask", {"question": question.strip()})
        if not isinstance(result.get("answer"), str):
            raise OmniLinkError("OmniLink answer response is missing answer")
        return result

    def save_bookmark(self, url: str, *, title: str, notes: str, tags: list[str] | None = None) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("bookmark URL must be an http(s) URL")
        result = self._post(
            "/api/links",
            {
                "url": url,
                "title": title[:500],
                "notes": notes[:4000],
                "tags": (tags or [])[:20],
                "autoAiExtract": False,
            },
        )
        if not isinstance(result.get("link"), dict):
            raise OmniLinkError("OmniLink bookmark response is missing link")
        return result
