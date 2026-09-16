"""Strict JSON readers shared by the evaluation toolchain.

Every evaluation input is untrusted evidence. Python's ``json`` keeps the last
value when an object repeats a key, so a manifest holding both ``"timeout_s": 1``
and ``"timeout_s": 120`` decodes as ``120`` and a hostile first ``run_id`` can be
hidden behind the expected one. Decoding therefore rejects duplicate object keys
at every nesting level before any validator sees the payload.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MAX_DIAGNOSTIC_KEYS = 1


class JsonInputError(ValueError):
    """A JSON/JSONL evidence input is malformed, ambiguous or unreadable."""


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise JsonInputError(f"duplicate JSON object key: {key!r}")
        payload[key] = value
    return payload


def _format_source(source: Path | str, line_number: int | None) -> str:
    return f"{source}:{line_number}" if line_number is not None else str(source)


def loads(text: str, source: Path | str = "<memory>", *, line_number: int | None = None) -> Any:
    """Decode one JSON document, rejecting duplicate object keys."""
    location = _format_source(source, line_number)
    try:
        return json.loads(text, object_pairs_hook=_object_without_duplicates)
    except JsonInputError as exc:
        # The duplicate key name is safe to echo; the untrusted body is not.
        raise JsonInputError(f"{location}: {exc}") from exc
    except ValueError as exc:
        raise JsonInputError(f"{location}: not valid JSON: {exc}") from exc


def load_json(path: Path | str) -> Any:
    """Read and decode one JSON file, rejecting duplicate object keys."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise JsonInputError(f"{source}: cannot read JSON input: {exc}") from exc
    return loads(text, source)


def iter_jsonl(path: Path | str) -> Iterator[tuple[int, Any]]:
    """Yield ``(line_number, value)`` for each non-blank JSON Lines record."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise JsonInputError(f"{source}: cannot read JSONL input: {exc}") from exc
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        yield line_number, loads(line, source, line_number=line_number)


def load_jsonl(path: Path | str) -> list[Any]:
    """Read a JSON Lines file, rejecting duplicate keys and blank-line drift."""
    return [value for _, value in iter_jsonl(path)]
