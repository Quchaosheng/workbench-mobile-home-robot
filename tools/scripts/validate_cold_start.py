#!/usr/bin/env python3
"""Validate the three-person clean-machine cold-start evidence table.

The protocol fixes the panel at three independent clean-machine participants.
Accepting more records would let a project publish a five-attempt run as if it
were the documented three-participant result, so the count is exact rather than a
minimum.
"""

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

from _jsonio import load_json

REQUIRED_PARTICIPANTS = 3
MINIMUM_PASSES = 2
MAX_PASS_MINUTES = 60

# The checked-in template ships these literals. Any of them surviving into a
# submitted record means the participant never filled the field in.
PLACEHOLDERS = frozenset({"", "-", "fill-in", "fill in", "n/a", "na", "tbd", "todo", "unknown", "none", "null"})

IDENTITY_FIELDS = ("participant_id", "os", "cpu_memory", "docker_version")
TIMESTAMP_FIELDS = ("started_at", "first_health_at", "first_ready_at")


def _required_text(participant: dict, field: str, participant_id: str) -> str:
    value = participant.get(field)
    if not isinstance(value, str) or value.strip().lower() in PLACEHOLDERS:
        raise RuntimeError(f"participant {participant_id} has a missing or placeholder {field}")
    return value


def _parse_utc(value: object, field: str, participant_id: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or value.strip().lower() in PLACEHOLDERS:
        raise RuntimeError(f"participant {participant_id} has a missing or placeholder {field}")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeError(f"participant {participant_id} has a malformed {field}: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"participant {participant_id} has a timezone-naive {field}: {value!r}")
    return parsed


def validate(payload: object) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("participants"), list):
        raise RuntimeError("cold-start evidence must contain a participants list")
    participants = payload["participants"]
    if len(participants) != REQUIRED_PARTICIPANTS:
        raise RuntimeError(
            f"the protocol requires exactly {REQUIRED_PARTICIPANTS} participant records, found {len(participants)}"
        )
    if any(not isinstance(participant, dict) for participant in participants):
        raise RuntimeError("every participant record must be an object")

    identifiers = [participant.get("participant_id") for participant in participants]
    if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
        raise RuntimeError("every participant needs a non-empty participant_id")
    if len(set(identifiers)) != len(identifiers):
        raise RuntimeError("participant_id values must be unique")

    for participant, identifier in zip(participants, identifiers, strict=True):
        for field in IDENTITY_FIELDS:
            _required_text(participant, field, identifier)
        started_at, first_health_at, first_ready_at = (
            _parse_utc(participant.get(field), field, identifier) for field in TIMESTAMP_FIELDS
        )

        if participant.get("result") not in {"pass", "fail"}:
            raise RuntimeError(f"invalid participant result: {identifier}")
        elapsed = participant.get("elapsed_minutes")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed):
            raise RuntimeError(f"invalid elapsed_minutes: {identifier}")
        if elapsed < 0:
            raise RuntimeError(f"invalid elapsed_minutes: {identifier}")
        if not started_at <= first_health_at <= first_ready_at:
            raise RuntimeError(f"participant {identifier} timestamps are not ordered started <= health <= ready")
        if participant["result"] == "pass":
            _required_text(participant, "log_reference", identifier)
            if elapsed > MAX_PASS_MINUTES:
                raise RuntimeError(f"passing participant exceeded {MAX_PASS_MINUTES} minutes: {identifier}")
        else:
            _required_text(participant, "blocking_log_reference", identifier)

    passed = sum(participant["result"] == "pass" for participant in participants)
    return {"participant_count": len(participants), "pass_count": passed, "accepted": passed >= MINIMUM_PASSES}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate external cold-start records")
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    summary = validate(load_json(args.evidence))
    print(json.dumps(summary, indent=2))
    if not summary["accepted"]:
        raise SystemExit(f"cold-start acceptance requires at least {MINIMUM_PASSES} passes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
