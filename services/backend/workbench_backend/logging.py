import json
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any, TextIO

from workbench.application.observability import (
    OBSERVABILITY_SCHEMA_VERSION,
    TRUNCATION_MARKER_KEY,
    CorrelationFields,
    bounded_payload,
)
from workbench.application.redaction import (
    REDACTION_MARKER_KEY,
    REDACTION_RULES_VERSION,
    redact_exception,
    redact_mapping,
)


class StructuredLogger:
    """Emit one stable JSON object per line for simulation and hardware services.

    Every record is scrubbed by ``workbench.application.redaction`` before it is
    written, because a telemetry file, a CI artifact and a support bundle are all
    copies of the same line.  Redaction never changes the record shape: the
    ``run_id``, ``sequence_no`` and ``details`` fields survive, so an analyst can
    still join a line to its run and recompute a percentile.

    Correlation follows ``workbench.application.observability``.  A line carries
    the run, the component and the schema version, and adds the action, attempt
    and monotonic timestamp only when the caller has them; an attempt without an
    action is refused rather than written as an unjoinable retry.
    """

    def __init__(self, service: str, stream: TextIO | None = None) -> None:
        self.service = service
        self.stream = stream or sys.stdout
        self._sequences: dict[str, int] = {}
        self._lock = threading.Lock()

    def emit(
        self,
        event: str,
        message: str,
        *,
        run_id: str = "system",
        level: str = "INFO",
        source: str = "simulation",
        component: str | None = None,
        action_id: str | None = None,
        attempt_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        correlation = CorrelationFields(
            run_id=run_id,
            component=component or self.service,
            action_id=action_id,
            attempt_id=attempt_id,
            monotonic_ns=time.monotonic_ns(),
        )
        bounded_details, report = bounded_payload(details or {})
        with self._lock:
            sequence_no = self._sequences.get(run_id, 0)
            self._sequences[run_id] = sequence_no + 1
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level.upper(),
            "service": self.service,
            "source": source,
            "sequence_no": sequence_no,
            "event": event,
            "message": message,
            "details": bounded_details,
            **correlation.as_dict(),
        }
        if report.omitted:
            record[TRUNCATION_MARKER_KEY] = report.as_dict()
        sanitized, findings = redact_mapping(record)
        if findings:
            sanitized[REDACTION_MARKER_KEY] = {"rules": REDACTION_RULES_VERSION, "redacted_values": findings}
        self.stream.write(json.dumps(sanitized, separators=(",", ":"), sort_keys=True) + "\n")
        self.stream.flush()
        return sanitized

    def emit_failure(self, event: str, exc: BaseException, **options: Any) -> dict[str, Any]:
        """Log an exception without letting its text carry a credential out.

        Producing the message inside the logger is the point: a caller that builds
        ``f"...: {exc}"`` has already copied the secret into a local string, and
        the redactor would only be guessing at it afterwards.
        """
        options.setdefault("level", "ERROR")
        return self.emit(event, redact_exception(exc), **options)


__all__ = ["OBSERVABILITY_SCHEMA_VERSION", "StructuredLogger"]
