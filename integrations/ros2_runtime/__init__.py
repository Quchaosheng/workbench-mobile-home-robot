"""Optional adapter from the ROS 2 task runtime's TaskEvent into Workbench evidence."""

from .task_events import (
    TaskEventIngestResult,
    TaskEventRejected,
    bind_task_event,
    ingest_capture,
    ingest_task_event,
)

__all__ = [
    "TaskEventIngestResult",
    "TaskEventRejected",
    "bind_task_event",
    "ingest_capture",
    "ingest_task_event",
]
