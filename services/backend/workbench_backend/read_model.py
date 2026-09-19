import json
import threading
import urllib.parse
from functools import lru_cache
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

# The label of the *run's evidence chain*, which is a different question from the
# verifier's verdict on the last claim. ``refuted`` is what one verification said;
# ``failed`` is a run that ended on that refutation without recovering. A run that
# did recover keeps ``refuted`` because its evidence chain still contains the
# refutation, while its ``outcome`` reports the final ``confirmed`` verdict.
EVIDENCE_LABELS = {
    "confirmed": "已确认",
    "insufficient_evidence": "证据不足",
    "refuted": "未满足",
    "failed": "未达成",
    "not_executed": "未执行",
    "running": "执行中",
}
EVIDENCE_STATES = frozenset(EVIDENCE_LABELS)
OUTCOME_LABELS = {
    "confirmed": "已确认",
    "refuted": "未满足",
    "insufficient_evidence": "证据不足",
    "none": "未验证",
    "running": "执行中",
}
# ``none`` is not a verifier verdict: no verifier ran, so only the evidence axis
# names that gap. ``unknown`` is not listed because an unrecognised status is
# never a filter the caller may ask for.
OUTCOME_FILTERS = frozenset({"confirmed", "refuted", "insufficient_evidence", "none", "running"})
SCENARIO_SOURCES = frozenset({"registry", "unresolved"})

STEP_LABELS = {
    "action_request": "执行语义动作",
    "action_result": "检查动作结果",
    "observation": "观察工作区",
    "task_accepted": "接收任务",
    "task_graph": "生成任务计划",
    "task_terminal": "任务结束",
    "verification": "验证任务结果",
}

# The full committed world-event vocabulary, so a stream that carries recovery or
# tool-call events is read rather than refused as an unknown event_type.
EVENT_TYPES = {
    "action_request",
    "action_result",
    "emotion",
    "fault",
    "observation",
    "policy_violation",
    "recovery_complete",
    "recovery_started",
    "task_accepted",
    "task_graph",
    "task_start",
    "task_terminal",
    "tool_call",
    "verification",
}

# The four evidence kinds the run timeline separates. Everything else is task
# context: it explains the run but is not evidence for or against a claim.
EXECUTION_EVENT_TYPES = frozenset({"action_request", "action_result"})
OBSERVATION_EVENT_TYPES = frozenset({"observation"})
VERIFICATION_EVENT_TYPES = frozenset({"verification"})
RECOVERY_EVENT_TYPES = frozenset({"recovery_started", "recovery_complete"})
TIMELINE_PHASES = ("execution", "observation", "verification", "recovery", "context")

MAX_EVENT_LOG_BYTES = 10 * 1024 * 1024
MAX_EVENTS_PER_RUN = 10_000
MAX_READ_ATTEMPTS = 2
MAX_EVIDENCE_REFS = 256
VERIFICATION_STATUSES = {"confirmed", "insufficient_evidence", "refuted"}
# Filters are bounded so one request cannot ask for an unbounded projection, and
# the facet lists are bounded so the response cannot grow with the run count.
MAX_RUN_PAGE_SIZE = 200
DEFAULT_RUN_PAGE_SIZE = MAX_RUN_PAGE_SIZE
MAX_RUN_FACET_VALUES = 64
RUN_FILTER_KEYS = ("scenario_id", "scenario_version", "outcome", "evidence")


class RunFilterError(ValueError):
    """A run-list filter names a value this run set does not contain."""

    def __init__(self, key: str) -> None:
        super().__init__(f"the {key} filter has no such value in this run set")
        self.key = key


class ReadModelError(ValueError):
    """Raised when a persisted run cannot be trusted as an ordered event stream."""


class ReadModelResponseTooLarge(ReadModelError):
    """Raised when a local or remote projection exceeds the HTTP contract."""


class _DuplicateJsonKey(ValueError):
    """Raised before JSON decoding can silently discard an object member."""


class _NonFiniteJson(ValueError):
    """Raised when a document uses a JSON constant that RFC 8259 forbids."""


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key!r}")
        payload[key] = value
    return payload


def _reject_json_constant(name: str) -> None:
    """Reject the bare NaN/Infinity tokens that RFC 8259 does not allow.

    Python's decoder accepts them by default and the encoder emits them again,
    so a non-finite value otherwise survives a full round trip and only fails in
    the browser's strict `response.json()` - after `/readyz` already claimed the
    source was usable.
    """
    raise _NonFiniteJson(f"the non-standard JSON constant {name} is not allowed")


def load_strict_json(text: str) -> Any:
    """Decode JSON with duplicate-key and non-standard-constant rejection."""
    return json.loads(
        text,
        object_pairs_hook=_object_without_duplicates,
        parse_constant=_reject_json_constant,
    )


def _validate_event_stream(events: list[Any], source: str) -> list[dict[str, Any]]:
    """Apply the semantic event-stream contract to one candidate run.

    The remote simulation host is a separate trust domain, so a payload it
    returns must clear exactly the same checks as a local log before the
    controller renders it as trusted state. Only the field names are echoed;
    response bodies and payload contents are never included in the error.
    """
    if not isinstance(events, list) or not events:
        raise ReadModelError(f"event stream has an invalid event count: {source}")
    if len(events) > MAX_EVENTS_PER_RUN:
        raise ReadModelError(f"event stream exceeds {MAX_EVENTS_PER_RUN} events: {source}")
    if any(not isinstance(event, dict) for event in events):
        raise ReadModelError(f"event stream contains a non-object event: {source}")
    run_ids = [event.get("run_id") for event in events]
    if any(not isinstance(run_id, str) or not run_id for run_id in run_ids) or any(
        run_id != run_ids[0] for run_id in run_ids
    ):
        raise ReadModelError(f"event stream has inconsistent run_id values: {source}")
    sequences = [event.get("sequence_no") for event in events]
    if any(type(sequence) is not int for sequence in sequences) or sequences != list(range(len(events))):
        raise ReadModelError(f"event stream sequence_no must be contiguous from zero: {source}")
    event_ids = [event.get("event_id") for event in events]
    if any(not isinstance(event_id, str) or not event_id for event_id in event_ids):
        raise ReadModelError(f"event stream has an invalid event_id: {source}")
    if len(event_ids) != len(set(event_ids)):
        raise ReadModelError(f"event stream has duplicate event_id values: {source}")
    for event in events:
        event_type = event.get("event_type")
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            raise ReadModelError(f"event stream has an unknown event_type: {source}")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ReadModelError(f"event payload must be an object: {source}")
        # Whole-response size stays owned by MAX_RESPONSE_BYTES (413); this layer
        # only rejects payloads that are not representable as finite JSON.
        try:
            json.dumps(payload, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ReadModelError(f"event payload is not finite JSON: {source}") from exc
        if not isinstance(event.get("occurred_at"), str) or not event["occurred_at"].strip():
            raise ReadModelError(f"event occurred_at must be a non-empty string: {source}")
        evidence_refs = event.get("evidence_refs", [])
        if (
            not isinstance(evidence_refs, list)
            or len(evidence_refs) > MAX_EVIDENCE_REFS
            or any(not isinstance(reference, str) or not reference for reference in evidence_refs)
        ):
            raise ReadModelError(f"event evidence_refs must be a bounded string list: {source}")
        if event_type == "verification":
            status = payload.get("status")
            if not isinstance(status, str) or status not in VERIFICATION_STATUSES:
                raise ReadModelError(f"verification event has an unknown status: {source}")
    return events


@lru_cache(maxsize=1)
def _task_id_index() -> dict[str, tuple[str, str, str]]:
    """Resolve a legacy ``task_id`` to ``(scenario_id, scenario_version, identity)``.

    The migration table is the committed join between the old task-family entry
    points and the registry identities, so this reads it instead of inventing an
    identity from a free-text field. The import is optional and cached: a
    deployment without the kernel package reports every run as unresolved rather
    than failing to serve the run list at all.
    """
    try:
        from workbench.kernel.scenario_migration import BY_TASK_ID
    except ImportError:
        return {}
    return {
        task_id: (family.scenario_id, family.scenario_version, family.identity)
        for task_id, family in BY_TASK_ID.items()
    }


def scenario_identity_for(task_id: object) -> dict[str, Any]:
    """Project one ``task_id`` into the scenario identity the registry owns.

    An unmapped, missing or non-string ``task_id`` resolves to a null
    ``scenario_id`` with ``scenario_source`` ``unresolved``. The projection never
    guesses an identity, because a guessed scenario would attribute a run to a
    manifest whose verifier never ran against it.
    """
    resolved = _task_id_index().get(task_id) if isinstance(task_id, str) else None
    if resolved is None:
        return {
            "scenario_id": None,
            "scenario_version": None,
            "scenario_label": None,
            "scenario_source": "unresolved",
        }
    scenario_id, scenario_version, identity = resolved
    return {
        "scenario_id": scenario_id,
        "scenario_version": scenario_version,
        "scenario_label": identity,
        "scenario_source": "registry",
    }


def run_outcome(last_verification: dict[str, Any] | None) -> str:
    """The verifier's verdict on the last claim, or ``none`` when none was made."""
    if last_verification is None:
        return "none"
    status = last_verification.get("payload", {}).get("status")
    return status if isinstance(status, str) and status in VERIFICATION_STATUSES else "running"


def run_evidence_state(events: list[dict[str, Any]], last_verification: dict[str, Any] | None) -> str:
    """Name the run's *evidence chain*, which is not the same as its outcome.

    ``refuted`` is what one verification said. A run that recovered from that
    refutation keeps ``refuted`` here, because its evidence chain still contains
    the refutation, while its outcome reports the final ``confirmed`` verdict.
    ``failed`` is therefore reserved for a refutation the run ended on without
    recovering, and ``not_executed`` for a run that ended with no verifier having
    run at all - the two gaps an operator must be able to tell apart.
    """
    terminal = any(event.get("event_type") == "task_terminal" for event in events)
    if last_verification is None:
        return "not_executed" if terminal else "running"
    status = last_verification.get("payload", {}).get("status")
    if status == "confirmed":
        recovered = any(
            event.get("event_type") == "verification" and event.get("payload", {}).get("status") == "refuted"
            for event in events
        )
        return "refuted" if recovered else "confirmed"
    if status == "insufficient_evidence":
        return "insufficient_evidence"
    if status == "refuted":
        return "failed" if terminal else "refuted"
    return "running"


def timeline_phase(event_type: object) -> str:
    """Map one event type to exactly one timeline phase.

    Anything that is not evidence is task context: it explains the run but is
    neither for nor against a claim, so it is never rendered as verification.
    """
    if event_type in EXECUTION_EVENT_TYPES:
        return "execution"
    if event_type in OBSERVATION_EVENT_TYPES:
        return "observation"
    if event_type in VERIFICATION_EVENT_TYPES:
        return "verification"
    if event_type in RECOVERY_EVENT_TYPES:
        return "recovery"
    return "context"


def run_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project a run into a sequence-ordered, phase-tagged timeline.

    The order is the committed ``sequence_no`` order the stream was validated in,
    so ties and recovery events are never reordered into a narrative the run did
    not have. The result is bounded by the same event ceiling as the stream.
    """
    return [
        {
            "event_id": event["event_id"],
            "sequence_no": event["sequence_no"],
            "event_type": event["event_type"],
            "phase": timeline_phase(event["event_type"]),
            "occurred_at": event["occurred_at"],
            "payload": event["payload"],
            "evidence_refs": list(event.get("evidence_refs", [])),
        }
        for event in events[:MAX_EVENTS_PER_RUN]
    ]


def project_runs(
    runs: list[dict[str, Any]],
    filters: dict[str, str] | None = None,
    *,
    page: int = 1,
    page_size: int = DEFAULT_RUN_PAGE_SIZE,
) -> dict[str, Any]:
    """Filter and page a run list, reporting the totals that bound the page.

    ``unfiltered_count`` is reported next to the matched ``total`` so a page of a
    filtered view can never be mistaken for the whole run set, and the committed
    sorted order is preserved because the slice is taken last.

    This is a module-level projection rather than a method so a caller that only
    implements ``list_runs`` - the concurrency, drain and response-size
    substitutes in the tests - is served without having to grow a new method.
    """
    if page < 1:
        raise ValueError("page must be at least 1")
    if not 1 <= page_size <= MAX_RUN_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_RUN_PAGE_SIZE}")
    projected = [normalize_run(run) for run in runs]
    matched = filter_runs(projected, filters or {})
    offset = (page - 1) * page_size
    return {
        "runs": matched[offset : offset + page_size],
        "total": len(matched),
        "unfiltered_count": len(projected),
        "page": page,
        "page_size": page_size,
        "max_page_size": MAX_RUN_PAGE_SIZE,
        "facets": run_facets(projected),
        "filters": {key: value for key, value in (filters or {}).items()},
        "read_only": True,
    }


def project_run_list(
    read_model: Any,
    filters: dict[str, str] | None = None,
    *,
    page: int = 1,
    page_size: int = DEFAULT_RUN_PAGE_SIZE,
) -> dict[str, Any]:
    """Project any read model's run list, preferring its own ``query_runs``."""
    query = getattr(read_model, "query_runs", None)
    if callable(query):
        return query(filters, page=page, page_size=page_size)
    return project_runs(read_model.list_runs(), filters, page=page, page_size=page_size)


def project_run_timeline(read_model: Any, run_id: str) -> list[dict[str, Any]]:
    """Project one run's timeline, preferring the read model's own method."""
    timeline = getattr(read_model, "run_timeline", None)
    if callable(timeline):
        return timeline(run_id)
    return run_timeline(read_model.list_events(run_id))


def normalize_run(run: dict[str, Any]) -> dict[str, Any]:
    """Fill the scenario/outcome/evidence fields a projection did not carry.

    A remote peer is a separate trust domain and may predate these fields, so a
    missing block is reported as unresolved rather than omitted. That keeps the
    response shape stable for the Dashboard without inventing an identity for a
    run whose events this host never read.
    """
    projected = dict(run)
    projected.setdefault("scenario_id", None)
    projected.setdefault("scenario_version", None)
    projected.setdefault("scenario_label", None)
    projected.setdefault("scenario_source", "unresolved")
    projected.setdefault("outcome", "none")
    projected.setdefault("evidence", "not_executed")
    return projected


def run_facets(runs: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The bounded, sorted values the run set actually contains.

    The Dashboard builds its filter options from these lists, so it cannot offer
    a value that would return nothing, and the response cannot grow with the run
    count.
    """
    facets: dict[str, set[str]] = {key: set() for key in RUN_FILTER_KEYS}
    for run in runs:
        scenario_id = run.get("scenario_id")
        scenario_version = run.get("scenario_version")
        outcome = run.get("outcome")
        evidence = run.get("evidence")
        if isinstance(scenario_id, str) and scenario_id:
            facets["scenario_id"].add(scenario_id)
        if isinstance(scenario_version, str) and scenario_version:
            facets["scenario_version"].add(scenario_version)
        if isinstance(outcome, str) and outcome in OUTCOME_FILTERS:
            facets["outcome"].add(outcome)
        if isinstance(evidence, str) and evidence in EVIDENCE_STATES:
            facets["evidence"].add(evidence)
    return {key: sorted(values)[:MAX_RUN_FACET_VALUES] for key, values in facets.items()}


def filter_runs(runs: list[dict[str, Any]], filters: dict[str, str]) -> list[dict[str, Any]]:
    """Apply validated filters, preserving the committed sorted run order.

    A value outside the bounded vocabulary is refused rather than ignored: a
    filter that silently returns the whole set looks identical to one that
    matched everything, which is how a typo becomes a wrong conclusion.
    """
    for key, value in filters.items():
        if value not in run_facets(runs)[key]:
            raise RunFilterError(key)
    return [run for run in runs if all(run.get(key) == value for key, value in filters.items())]


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
                events = [load_strict_json(line) for line in contents.splitlines() if line.strip()]
            except (_DuplicateJsonKey, _NonFiniteJson) as exc:
                raise ReadModelError(f"event log contains {exc}: {path.name}") from exc
            except json.JSONDecodeError as exc:
                raise ReadModelError(f"event log is not valid JSONL: {path.name}") from exc
            events = _validate_event_stream(events, path.name)
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

    def query_runs(
        self,
        filters: dict[str, str] | None = None,
        page: int = 1,
        page_size: int = DEFAULT_RUN_PAGE_SIZE,
    ) -> dict[str, Any]:
        """The bounded, filtered, paged run list with the facets that describe it."""
        runs = [normalize_run(run) for run in self.list_runs()]
        return project_runs(runs, filters, page=page, page_size=page_size)

    def run_timeline(self, run_id: str) -> list[dict[str, Any]]:
        """The sequence-ordered, phase-tagged evidence timeline for one run."""
        try:
            events = self.list_events(run_id)
        except KeyError:
            raise KeyError(run_id) from None
        return run_timeline(events)

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
        task_id = accepted.get("payload", {}).get("task_id", "unknown")
        # The outcome and the evidence state answer different questions: the
        # outcome is the verifier's verdict, the evidence state describes the
        # chain that led to it. Both travel so neither can stand in for the other.
        outcome = run_outcome(final_verification)
        evidence_state = run_evidence_state(visible, final_verification)
        return {
            "run_id": events[0]["run_id"],
            "task_id": task_id,
            "goal": accepted.get("payload", {}).get("goal", "Place the red block in the tray"),
            "mode": accepted.get("payload", {}).get("mode", "scripted"),
            "status": status,
            "status_label": STATUS_LABELS.get(status, "未知状态"),
            "outcome": outcome,
            "outcome_label": OUTCOME_LABELS.get(outcome, "未知状态"),
            "evidence": evidence_state,
            "evidence_label": EVIDENCE_LABELS.get(evidence_state, "未知状态"),
            **scenario_identity_for(task_id),
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
            payload = load_strict_json(response.body)
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
        if not isinstance(runs, list) or len(runs) > MAX_EVENTS_PER_RUN:
            raise ReadModelError("remote event source returned invalid runs")
        if any(not isinstance(run, dict) for run in runs):
            raise ReadModelError("remote event source returned invalid runs")
        run_ids = [run.get("run_id") for run in runs]
        if any(not isinstance(run_id, str) or not run_id for run_id in run_ids):
            raise ReadModelError("remote event source returned a run without a run_id")
        if len(run_ids) != len(set(run_ids)):
            raise ReadModelError("remote event source returned duplicate run_id values")
        for run in runs:
            if not isinstance(run.get("event_count"), int) or isinstance(run.get("event_count"), bool):
                raise ReadModelError("remote event source returned a run without an integer event_count")
        return runs

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(run_id, safe="")
        payload = self._request(f"/api/v1/runs/{encoded}/events")
        events = payload.get("events")
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise ReadModelError("remote event source returned invalid events")
        # The peer is a separate trust domain, so its events clear the same
        # semantic contract as a local log before the controller displays them.
        events = _validate_event_stream(events, "remote event source")
        if any(event["run_id"] != run_id for event in events):
            raise ReadModelError("remote event source returned unexpected run_id values")
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
