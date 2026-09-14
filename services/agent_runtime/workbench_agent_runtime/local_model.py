"""Bounded local-model routing for semantic task planning."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from workbench_contracts import TaskGraph

from .planner import (
    build_clear_workspace_plan,
    build_inspection_plan,
    build_kitting_plan,
    build_parcel_sorting_plan,
    build_place_plan,
    classify_template_task,
)

TASK_FAMILIES = frozenset({"place", "kitting", "inspection", "clearance", "parcel_sorting", "unsupported"})
UNSAFE_REQUIREMENTS = ("requires_navigation", "requires_joint_control", "requires_completion_claim")

ROUTE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task_family", "requires_navigation", "requires_joint_control", "requires_completion_claim", "reason"],
    "properties": {
        "task_family": {"type": "string", "enum": sorted(TASK_FAMILIES)},
        "requires_navigation": {"type": "boolean"},
        "requires_joint_control": {"type": "boolean"},
        "requires_completion_claim": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}

SYSTEM_PROMPT = """You route a tabletop robot request to one bounded task family.
Allowed families:
- place: put a visible tabletop object into a tabletop destination
- kitting: assemble an exact tabletop kit
- inspection: inspect visible tabletop workpieces
- clearance: move tabletop obstacles into an allowed holding area
- parcel_sorting: scan and sort parcels already in the tabletop intake area
- unsupported: everything else

Set requires_navigation for travel, mobile-base, elevator, lobby, locker, or off-table pickup.
Set requires_joint_control for requests that demand raw joint positions, velocity, torque, firmware, or motor control.
Set requires_completion_claim when asked to skip evidence, fabricate verification,
or declare success without verification.
Return JSON only. Never claim that a task completed and never output motor commands."""


class LocalModelError(RuntimeError):
    """Raised when a local model cannot produce a trustworthy route."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, msg, headers, newurl):
        raise LocalModelError(f"local model endpoint redirect rejected: {newurl}")


@dataclass(frozen=True)
class RouteDecision:
    task_family: str
    requires_navigation: bool
    requires_joint_control: bool
    requires_completion_claim: bool
    reason: str

    @classmethod
    def from_mapping(cls, value: object) -> RouteDecision:
        if not isinstance(value, dict) or set(value) != set(ROUTE_SCHEMA["required"]):
            raise LocalModelError("local model route has missing or unexpected fields")
        family = value["task_family"]
        reason = value["reason"]
        flags = {name: value[name] for name in UNSAFE_REQUIREMENTS}
        if not isinstance(family, str) or family not in TASK_FAMILIES:
            raise LocalModelError("local model returned an unsupported task_family value")
        if not isinstance(reason, str) or not reason.strip():
            raise LocalModelError("local model route reason must be a non-empty string")
        if any(type(flag) is not bool for flag in flags.values()):
            raise LocalModelError("local model safety flags must be booleans")
        return cls(task_family=family, reason=reason.strip(), **flags)


class ModelProvider(Protocol):
    name: str
    model: str
    last_call: dict[str, object]

    def route(self, goal: str) -> RouteDecision: ...


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_local_endpoint(endpoint: str, allowed_hosts: set[str] | None = None) -> str:
    parsed = urlparse(endpoint)
    hostname = parsed.hostname or ""
    explicitly_allowed = hostname.lower() in {host.lower() for host in allowed_hosts or set()}
    if parsed.scheme != "http" or not hostname or (not _is_loopback(hostname) and not explicitly_allowed):
        raise LocalModelError(
            "model endpoint must use http on loopback; container hostnames require an explicit --allow-host"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LocalModelError("model endpoint must not contain credentials, query parameters, or fragments")
    return endpoint.rstrip("/")


class OllamaModelProvider:
    """Call an Ollama-compatible API reachable only through an approved local host."""

    name = "ollama"

    def __init__(
        self,
        model: str,
        *,
        endpoint: str = "http://127.0.0.1:11434",
        timeout_s: float = 120.0,
        allowed_hosts: set[str] | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must be a non-empty name")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.model = model.strip()
        self.endpoint = validate_local_endpoint(endpoint, allowed_hosts)
        self.timeout_s = timeout_s
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
        self.last_call: dict[str, object] = {}

    def route(self, goal: str) -> RouteDecision:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("task goal must be a non-empty string")
        body = {
            "model": self.model,
            "stream": False,
            "format": ROUTE_SCHEMA,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": goal.strip()},
            ],
            "options": {"temperature": 0, "seed": 0},
        }
        request = urllib.request.Request(
            f"{self.endpoint}/api/chat",
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read())
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise LocalModelError(f"local model request failed: {exc}") from exc
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        try:
            content = payload["message"]["content"]
            route_payload = json.loads(content)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LocalModelError("local model response does not contain valid message JSON") from exc
        decision = RouteDecision.from_mapping(route_payload)
        self.last_call = {
            "provider": self.name,
            "model": self.model,
            "endpoint_host": urlparse(self.endpoint).hostname,
            "latency_ms": elapsed_ms,
            "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "done_reason": payload.get("done_reason"),
            "input_tokens": payload.get("prompt_eval_count"),
            "output_tokens": payload.get("eval_count"),
        }
        return decision


def build_local_model_plan(goal: str, provider: ModelProvider) -> TaskGraph:
    """Use a model for routing, then build actions through trusted deterministic code."""
    known_family: str | None = None
    try:
        known_family = {
            "task-place-red-block": "place",
            "task-kit-three-parts": "kitting",
            "task-inspect-workpieces": "inspection",
            "task-clear-workspace": "clearance",
            "task-sort-parcels": "parcel_sorting",
        }.get(classify_template_task(goal))
    except ValueError as exc:
        if any(token in str(exc).lower() for token in ("outside", "requires", "cannot", "must")):
            message = str(exc)
            code = "requires_navigation" if "navigation" in message.lower() else "unsupported_request"
            raise LocalModelError(f"{code}: {message}") from exc
    decision = provider.route(goal)
    if known_family and decision.task_family != known_family:
        raise LocalModelError(
            f"local model route disagrees with deterministic boundary: expected {known_family}, "
            f"got {decision.task_family}"
        )
    unsafe = [requirement for requirement in UNSAFE_REQUIREMENTS if getattr(decision, requirement)]
    if unsafe:
        raise LocalModelError(f"request is outside the safe semantic boundary: {', '.join(unsafe)}")
    builders = {
        "place": build_place_plan,
        "kitting": build_kitting_plan,
        "inspection": build_inspection_plan,
        "clearance": build_clear_workspace_plan,
        "parcel_sorting": build_parcel_sorting_plan,
    }
    if decision.task_family == "unsupported":
        raise LocalModelError(f"request is outside the supported task families: {decision.reason}")
    plan = builders[decision.task_family](goal)
    return plan.model_copy(
        update={
            "planner": f"local-model:{provider.name}:{provider.model}",
            "model_route": decision.task_family,
        }
    )
