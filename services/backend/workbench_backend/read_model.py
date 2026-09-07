import json
import math
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from .expression import ALLOWED_TRANSITIONS, ExpressionState, derive_expression
from .remote_http import RemoteHttpClient, RemoteHttpError, RemoteHttpResponseTooLarge

STATUS_LABELS = {
    "confirmed": "已确认",
    "insufficient_evidence": "证据不足",
    "refuted": "未满足",
    "running": "执行中",
}

STEP_LABELS = {
    "action_request": "执行语义动作",
    "action_result": "检查动作结果",
    "observation": "观察工作区",
    "task_accepted": "接收任务",
    "task_graph": "生成任务计划",
    "task_terminal": "任务结束",
    "verification": "验证任务结果",
}

EVENT_TYPES = {
    "action_request",
    "action_result",
    "emotion",
    "fault",
    "observation",
    "policy_violation",
    "task_accepted",
    "task_graph",
    "task_terminal",
    "verification",
}
MAX_EVENT_LOG_BYTES = 10 * 1024 * 1024
MAX_EVENTS_PER_RUN = 10_000
MAX_READ_ATTEMPTS = 2


class ReadModelError(ValueError):
    """Raised when a persisted run cannot be trusted as an ordered event stream."""


class ReadModelResponseTooLarge(ReadModelError):
    """Raised when a local or remote projection exceeds the HTTP contract."""


class _DuplicateJsonKey(ValueError):
    """Raised before JSON decoding can silently discard an object member."""


class _NonFiniteJsonNumber(ValueError):
    """Raised before an event source can introduce NaN or infinity."""


def _reject_json_constant(value: str) -> None:
    # Do not include attacker-controlled input or field names in diagnostics.
    raise _NonFiniteJsonNumber("non-finite JSON number")


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        # Valid JSON syntax such as 1e400 can overflow Python's float.
        raise _NonFiniteJsonNumber("non-finite JSON number")
    return number


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key!r}")
        payload[key] = value
    return payload


def _decode_event_source_json(contents: str | bytes) -> Any:
    return json.loads(
        contents,
        object_pairs_hook=_object_without_duplicates,
        parse_constant=_reject_json_constant,
        parse_float=_finite_json_float,
    )


class DashboardReadModel:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self._event_cache: dict[Path, tuple[int, int, list[dict[str, Any]]]] = {}
        self._cache_lock = threading.RLock()

    def _paths(self) -> list[Path]:
        return sorted(self.data_dir.glob("*.jsonl"))

    def ready(self) -> bool:
        if not self.data_dir.is_dir():
            return False
        try:
            return bool(self._runs_by_id())
        except (OSError, ReadModelError):
            return False

    def _load_path(self, path: Path) -> list[dict[str, Any]]:
        with self._cache_lock:
            contents = None
            stable_stat = None
            for _ in range(MAX_READ_ATTEMPTS):
                try:
                    before = path.stat()
                    if before.st_size > MAX_EVENT_LOG_BYTES:
                        raise ReadModelError(f"event log exceeds {MAX_EVENT_LOG_BYTES} bytes: {path.name}")
                    cached = self._event_cache.get(path)
                    if cached and cached[:2] == (before.st_mtime_ns, before.st_size):
                        return cached[2]
                    candidate = path.read_text(encoding="utf-8")
                    after = path.stat()
                except ReadModelError:
                    raise
                except (OSError, UnicodeError) as exc:
                    raise ReadModelError(f"event source is unavailable or not UTF-8: {path.name}") from exc
                if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size):
                    contents = candidate
                    stable_stat = after
                    break
            if contents is None or stable_stat is None:
                raise ReadModelError(f"event source changed while being read: {path.name}")
            try:
                events = [_decode_event_source_json(line) for line in contents.splitlines() if line.strip()]
            except _NonFiniteJsonNumber as exc:
                raise ReadModelError("event log contains a non-finite JSON number") from exc
            except _DuplicateJsonKey as exc:
                raise ReadModelError(f"event log contains {exc}: {path.name}") from exc
            except json.JSONDecodeError as exc:
                raise ReadModelError(f"event log is not valid JSONL: {path.name}") from exc
            if not events or len(events) > MAX_EVENTS_PER_RUN:
                raise ReadModelError(f"event log has an invalid event count: {path.name}")
            if any(not isinstance(event, dict) for event in events):
                raise ReadModelError(f"event log contains a non-object event: {path.name}")
            run_ids = [event.get("run_id") for event in events]
            if any(not isinstance(run_id, str) or not run_id for run_id in run_ids) or any(
                run_id != run_ids[0] for run_id in run_ids
            ):
                raise ReadModelError(f"event log has inconsistent run_id values: {path.name}")
            sequences = [event.get("sequence_no") for event in events]
            if any(type(sequence) is not int for sequence in sequences) or sequences != list(range(len(events))):
                raise ReadModelError(f"event log sequence_no must be contiguous from zero: {path.name}")
            event_ids = [event.get("event_id") for event in events]
            if any(not isinstance(event_id, str) or not event_id for event_id in event_ids):
                raise ReadModelError(f"event log has an invalid event_id: {path.name}")
            if len(event_ids) != len(set(event_ids)):
                raise ReadModelError(f"event log has duplicate event_id values: {path.name}")
            for event in events:
                event_type = event.get("event_type")
                if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
                    raise ReadModelError(f"event log has an unknown event_type: {path.name}")
                if not isinstance(event.get("payload"), dict):
                    raise ReadModelError(f"event payload must be an object: {path.name}")
                if not isinstance(event.get("occurred_at"), str) or not event["occurred_at"]:
                    raise ReadModelError(f"event occurred_at must be a non-empty string: {path.name}")
                evidence_refs = event.get("evidence_refs", [])
                if not isinstance(evidence_refs, list) or any(not isinstance(ref, str) for ref in evidence_refs):
                    raise ReadModelError(f"event evidence_refs must be a string list: {path.name}")
            self._event_cache[path] = (stable_stat.st_mtime_ns, stable_stat.st_size, events)
            return events

    def _runs_by_id(self) -> dict[str, list[dict[str, Any]]]:
        runs: dict[str, list[dict[str, Any]]] = {}
        for path in self._paths():
            events = self._load_path(path)
            run_id = events[0]["run_id"]
            if run_id in runs:
                raise ReadModelError(f"duplicate run_id across event logs: {run_id}")
            runs[run_id] = events
        return runs

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        try:
            return self._runs_by_id()[run_id]
        except KeyError:
            raise KeyError(run_id) from None

    def list_runs(self) -> list[dict[str, Any]]:
        return [self.summarize(events) for events in self._runs_by_id().values()]

    def summarize(self, events: list[dict[str, Any]], replay_index: int | None = None) -> dict[str, Any]:
        if not events:
            raise ValueError("cannot summarize an empty run")
        visible = events if replay_index is None else events[: replay_index + 1]
        accepted = next((event for event in events if event.get("event_type") == "task_accepted"), events[0])
        verifications = [event for event in visible if event.get("event_type") == "verification"]
        final_verification = verifications[-1] if verifications else None
        raw_status = final_verification.get("payload", {}).get("status") if final_verification else "running"
        status = raw_status if isinstance(raw_status, str) and raw_status else "unknown"
        raw_missing_evidence = (
            final_verification.get("payload", {}).get("missing_evidence", []) if final_verification else []
        )
        missing_evidence = (
            raw_missing_evidence
            if isinstance(raw_missing_evidence, list)
            and all(isinstance(reference, str) for reference in raw_missing_evidence)
            else []
        )
        current_event = visible[-1] if visible else None
        recovery_count = sum(
            event.get("event_type") == "verification" and event.get("payload", {}).get("status") == "refuted"
            for event in visible
        )
        evidence = []
        for event in visible:
            for reference in event.get("evidence_refs", []):
                if reference not in evidence:
                    evidence.append(reference)
        return {
            "run_id": events[0]["run_id"],
            "task_id": accepted.get("payload", {}).get("task_id", "unknown"),
            "goal": accepted.get("payload", {}).get("goal", "Place the red block in the tray"),
            "mode": accepted.get("payload", {}).get("mode", "scripted"),
            "status": status,
            "status_label": STATUS_LABELS.get(status, "未知状态"),
            "expression": derive_expression(visible).value,
            "current_step": STEP_LABELS.get(current_event.get("event_type"), "等待任务")
            if current_event
            else "等待任务",
            "progress": round(len(visible) / len(events) * 100) if events else 0,
            "event_count": len(events),
            "visible_event_count": len(visible),
            "updated_at": visible[-1].get("occurred_at") if visible else None,
            "missing_evidence": missing_evidence,
            "evidence_refs": evidence,
            "recovery_count": recovery_count,
            "safety": {"hardware_estop": "not_connected", "software_control": "read_only"},
        }


class RemoteDashboardReadModel(DashboardReadModel):
    """Read the simulation event source over HTTP for a split-host controller."""

    data_source = "remote-simulation-event-source"

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 1.0,
        event_source_allowlist: str | None = None,
    ) -> None:
        self._http = RemoteHttpClient(
            base_url,
            allowlist=event_source_allowlist,
            timeout_s=timeout_s,
        )
        self.base_url = self._http.base_url
        self.timeout_s = self._http.timeout_s

    def _request(self, path: str) -> dict[str, Any]:
        try:
            response = self._http.get(path)
        except RemoteHttpResponseTooLarge as exc:
            raise ReadModelResponseTooLarge("remote event source response is too large") from exc
        except RemoteHttpError as exc:
            raise ReadModelError(f"remote event source unavailable: {self.base_url}") from exc
        if response.status == 404:
            raise KeyError(path)
        if not 200 <= response.status < 300:
            raise ReadModelError(f"remote event source unavailable: {self.base_url}")
        try:
            payload = _decode_event_source_json(response.body)
        except _NonFiniteJsonNumber as exc:
            raise ReadModelError("remote event source contains a non-finite JSON number") from exc
        except _DuplicateJsonKey as exc:
            raise ReadModelError(f"remote event source returned {exc}: {self.base_url}") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReadModelError(f"remote event source unavailable: {self.base_url}") from exc
        if not isinstance(payload, dict):
            raise ReadModelError("remote event source returned a non-object payload")
        return payload

    def ready(self) -> bool:
        try:
            payload = self._request("/readyz")
        except (KeyError, ReadModelError):
            return False
        return payload.get("status") == "ready"

    def list_runs(self) -> list[dict[str, Any]]:
        payload = self._request("/api/v1/runs")
        runs = payload.get("runs")
        if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
            raise ReadModelError("remote event source returned invalid runs")
        return runs

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(run_id, safe="")
        payload = self._request(f"/api/v1/runs/{encoded}/events")
        events = payload.get("events")
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            raise ReadModelError("remote event source returned invalid events")
        if not events or any(event.get("run_id") != run_id for event in events):
            raise ReadModelError("remote event source returned inconsistent run_id values")
        sequences = [event.get("sequence_no") for event in events]
        if sequences != list(range(len(events))):
            raise ReadModelError("remote event source sequence_no is not contiguous")
        return events

    def expression_contract(self) -> dict[str, Any]:
        return {
            "states": [state.value for state in ExpressionState],
            "transitions": {
                state.value: sorted(next_state.value for next_state in transitions)
                for state, transitions in ALLOWED_TRANSITIONS.items()
            },
        }


class UnavailableRemoteDashboardReadModel(RemoteDashboardReadModel):
    """Keep the Backend live while rejecting an invalid remote configuration."""

    def __init__(self, error: Exception) -> None:
        self._configuration_error = str(error)
        self.base_url = "invalid-remote-event-source"
        self.timeout_s = 0.0

    def ready(self) -> bool:
        return False

    def list_runs(self) -> list[dict[str, Any]]:
        raise ReadModelError("remote event source configuration is invalid")

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        raise ReadModelError("remote event source configuration is invalid")
