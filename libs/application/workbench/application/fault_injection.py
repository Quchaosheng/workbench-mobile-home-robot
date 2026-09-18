"""One executable contract for the declared simulation fault taxonomy.

`tools/scripts/scenario_tools.py` declares six fault types and `sim/scenarios/`
materialises them, but the declaration was only ever consumed by the scripted
log generator. A fault name that appears in a manifest and nowhere else is a
promise, not coverage.

This module turns each declared fault into four executable facts:

* a *trigger* - the exact input that reproduces the fault;
* an *expected safe behaviour* - what the runtime must do when it happens;
* an *evidence requirement* - what a run must retain to show it happened;
* an *injector* - a wrapper around a real port, so a test drives the shipped
  ``PostActionLoop``, ``ExecutionController`` and ``VirtualMcu`` rather than a
  private copy of their rules.

Three rules are deliberate:

* A fault never becomes a completion. The injectors can only remove evidence,
  delay an action, fail a result, or move a target; none of them can make a
  verification succeed that the truth does not support.
* Injection is seeded; the same seed replays the same injected sequence.
* Coverage is reported as ``SIMULATED``, ``HARDWARE_TESTED`` or
  ``NOT_EXECUTED``. A fixture passing on a development host is not physical
  evidence, and the report says so rather than implying otherwise.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

FAULT_INJECTION_VERSION = "fault-injection-v1"
COVERAGE_STATUSES: tuple[str, ...] = ("SIMULATED", "HARDWARE_TESTED", "NOT_EXECUTED")

CoverageStatus = Literal["SIMULATED", "HARDWARE_TESTED", "NOT_EXECUTED"]


class FaultInjectionError(ValueError):
    """A fault specification, injector or coverage report violates this contract."""


class FaultType(StrEnum):
    """The six faults a scenario manifest is allowed to declare.

    These mirror ``tools/scripts/scenario_tools.P2_FAULT_TYPES``. A test asserts
    the two sets are equal, so this copy cannot drift from the manifests.
    """

    ACTUATOR_TIMEOUT = "actuator_timeout"
    CAMERA_DROPOUT = "camera_dropout"
    GRASP_FAILURE = "grasp_failure"
    MOVING_TARGET = "moving_target"
    OCCLUSION = "occlusion"
    STALE_OBSERVATION = "stale_observation"


class RuntimeFault(StrEnum):
    """Faults on the runtime boundary rather than in a scenario manifest.

    Neither of these belongs in a scenario manifest: a lost link and a process
    restart are conditions of the runtime, not properties of one scenario. They
    are declared here so the coverage report can account for them too.
    """

    LINK_LOSS = "link_loss"
    PROCESS_RESTART = "process_restart"


class SafeBehavior(StrEnum):
    """What the runtime must do when a fault occurs.

    Each member names an observable outcome, never an intention: a test asserts
    the outcome, so "we would stop" cannot be reported as "we stopped".
    """

    BOUNDED_RETRY_THEN_STOP = "bounded_retry_then_stop"
    REFUSE_CONFIRMATION_WITHOUT_FRESH_EVIDENCE = "refuse_confirmation_without_fresh_evidence"
    RETAIN_REFUTED_VERIFICATION = "retain_refuted_verification"
    RE_OBSERVE_BEFORE_DEPENDENTS = "re_observe_before_dependents"
    MCU_SAFE_STATE = "mcu_safe_state"
    INVALIDATE_RUN_CORRELATION = "invalidate_run_correlation"


FaultName = FaultType | RuntimeFault


@dataclass(frozen=True)
class FaultSpec:
    """One declared fault, its trigger, its required behaviour and its evidence."""

    fault: FaultName
    trigger: str
    expected_safe_behaviour: SafeBehavior
    evidence_requirement: str
    declared_by: str
    hardware_tested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.fault, FaultType | RuntimeFault):
            raise FaultInjectionError(f"fault must be a FaultType or RuntimeFault: {self.fault!r}")
        for name in ("trigger", "evidence_requirement", "declared_by"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise FaultInjectionError(f"fault {self.fault.value} {name} must be a non-empty string")
        if type(self.hardware_tested) is not bool:
            raise FaultInjectionError(f"fault {self.fault.value} hardware_tested must be boolean")

    def as_dict(self) -> dict[str, Any]:
        return {
            "fault": self.fault.value,
            "trigger": self.trigger,
            "expected_safe_behaviour": self.expected_safe_behaviour.value,
            "evidence_requirement": self.evidence_requirement,
            "declared_by": self.declared_by,
            "hardware_tested": self.hardware_tested,
            "fault_injection_version": FAULT_INJECTION_VERSION,
        }


_SCENARIOS = "sim/scenarios/frozen,sim/scenarios/expanded"

FAULT_SPECS: tuple[FaultSpec, ...] = (
    FaultSpec(
        FaultType.ACTUATOR_TIMEOUT,
        trigger="the action adapter raises TimeoutError on dispatch",
        expected_safe_behaviour=SafeBehavior.BOUNDED_RETRY_THEN_STOP,
        evidence_requirement="the controller's ADAPTER_TIMEOUT transition and the terminal execution state",
        declared_by=_SCENARIOS,
    ),
    FaultSpec(
        FaultType.CAMERA_DROPOUT,
        trigger="every observation attempt after the action completes reports PERCEPTION_FAILURE",
        expected_safe_behaviour=SafeBehavior.REFUSE_CONFIRMATION_WITHOUT_FRESH_EVIDENCE,
        evidence_requirement="the gate's PERCEPTION_FAILURE reason and the unconfirmed step",
        declared_by=_SCENARIOS,
    ),
    FaultSpec(
        FaultType.GRASP_FAILURE,
        trigger="the action adapter returns an ActionResult whose outcome is failed",
        expected_safe_behaviour=SafeBehavior.REFUSE_CONFIRMATION_WITHOUT_FRESH_EVIDENCE,
        evidence_requirement="the failed ActionResult and the gate outcome that refused to advance",
        declared_by=_SCENARIOS,
    ),
    FaultSpec(
        FaultType.MOVING_TARGET,
        trigger="a fresh observation reports the entity at a location other than the claimed one",
        expected_safe_behaviour=SafeBehavior.RETAIN_REFUTED_VERIFICATION,
        evidence_requirement="a REFUTED verification carrying the observed location and its evidence reference",
        declared_by=_SCENARIOS,
    ),
    FaultSpec(
        FaultType.OCCLUSION,
        trigger="the named entity's observation is withheld while other entities still report",
        expected_safe_behaviour=SafeBehavior.REFUSE_CONFIRMATION_WITHOUT_FRESH_EVIDENCE,
        evidence_requirement="a MISSING attempt for that entity and no confirmation for it",
        declared_by=_SCENARIOS,
    ),
    FaultSpec(
        FaultType.STALE_OBSERVATION,
        trigger="the observation answering an action is timestamped at or before the action completed",
        expected_safe_behaviour=SafeBehavior.RE_OBSERVE_BEFORE_DEPENDENTS,
        evidence_requirement="the gate's OBSERVATION_STALE reason and the blocked dependent step",
        declared_by=_SCENARIOS,
    ),
    FaultSpec(
        RuntimeFault.LINK_LOSS,
        trigger="the safety MCU stops heartbeating and its watchdog expires",
        expected_safe_behaviour=SafeBehavior.MCU_SAFE_STATE,
        evidence_requirement="the MCU fault state and its fault code, with the host-side stop record",
        declared_by="runtime boundary (#76, #151)",
    ),
    FaultSpec(
        RuntimeFault.PROCESS_RESTART,
        trigger="the process restarts while a run is in flight",
        expected_safe_behaviour=SafeBehavior.INVALIDATE_RUN_CORRELATION,
        evidence_requirement="the gate's RESTART_INVALIDATED_RUN reason for every later step of that run",
        declared_by="runtime boundary (#151)",
    ),
)

_SPECS_BY_FAULT = {spec.fault: spec for spec in FAULT_SPECS}


def fault_spec(fault: FaultName | str) -> FaultSpec:
    """Return the declared specification for one fault, refusing an unknown name."""
    if isinstance(fault, FaultType | RuntimeFault):
        return _SPECS_BY_FAULT[fault]
    for spec in FAULT_SPECS:
        if spec.fault.value == fault:
            return spec
    raise FaultInjectionError(f"undeclared fault: {fault!r}")


def declared_faults() -> frozenset[str]:
    """Every fault name this contract can execute, for a drift check."""
    return frozenset(spec.fault.value for spec in FAULT_SPECS)


def _checked_seed(seed: int) -> random.Random:
    if type(seed) is not int or isinstance(seed, bool):
        raise FaultInjectionError("seed must be an integer")
    if not 0 <= seed < 2**32:
        raise FaultInjectionError("seed must be between 0 and 2**32 - 1")
    return random.Random(seed)


def _action_id(action: object) -> str:
    value = getattr(action, "action_id", None)
    return value if isinstance(value, str) and value else "<unknown-action>"


class AdapterFaultInjector:
    """Wrap an action adapter and inject one declared actuator fault.

    A timeout raises instead of dispatching, because a real timeout is a
    transport event rather than a returned result; the controller, not this
    class, decides that it is a safe-stop condition. A grasp failure wraps the
    adapter's own returned result, so the injected value stays inside the
    caller's contract instead of this module inventing a shape.
    """

    _FAULTS = (FaultType.ACTUATOR_TIMEOUT, FaultType.GRASP_FAILURE)

    def __init__(self, adapter: object, *, fault: FaultName | str, seed: int = 0, skip_first: int = 0) -> None:
        if not hasattr(adapter, "dispatch"):
            raise FaultInjectionError("adapter must implement dispatch")
        spec = fault_spec(fault)
        if spec.fault not in self._FAULTS:
            raise FaultInjectionError(f"{spec.fault.value} is not an adapter fault")
        if type(skip_first) is not int or isinstance(skip_first, bool) or skip_first < 0:
            raise FaultInjectionError("skip_first must be a non-negative integer")
        self._adapter = adapter
        self.spec = spec
        self._rng = _checked_seed(seed)
        self._skip_first = skip_first
        self._dispatched = 0
        self.injected: list[str] = []

    @property
    def dispatch_count(self) -> int:
        return self._dispatched

    def dispatch(self, action: object) -> object:
        from workbench_contracts import ActionOutcome, ActionResult

        self._dispatched += 1
        if self._dispatched <= self._skip_first:
            return self._adapter.dispatch(action)
        self.injected.append(_action_id(action))
        if self.spec.fault is FaultType.ACTUATOR_TIMEOUT:
            raise TimeoutError(f"{FAULT_INJECTION_VERSION}: injected {self.spec.fault.value}")
        produced = self._adapter.dispatch(action)
        if not isinstance(produced, ActionResult):
            raise FaultInjectionError("adapter did not return an ActionResult to convert into a failure")
        if produced.outcome is ActionOutcome.FAILED:
            # Already a failure: injecting again would double-count one fault.
            return produced
        return produced.model_copy(update={"outcome": ActionOutcome.FAILED})


class ObservationFaultInjector:
    """Wrap an observation port and inject one declared perception fault.

    ``CAMERA_DROPOUT`` and ``OCCLUSION`` differ only in scope: a dropout fails
    every entity, an occlusion fails the named entity and lets the others
    report. That difference is the point - an operator must be able to tell a
    dead camera from one blocked object.
    """

    _FAULTS = (FaultType.CAMERA_DROPOUT, FaultType.OCCLUSION, FaultType.STALE_OBSERVATION)

    def __init__(
        self,
        port: object,
        *,
        fault: FaultName | str,
        seed: int = 0,
        entity_id: str | None = None,
    ) -> None:
        if not hasattr(port, "observe"):
            raise FaultInjectionError("observation port must implement observe")
        spec = fault_spec(fault)
        if spec.fault not in self._FAULTS:
            raise FaultInjectionError(f"{spec.fault.value} is not a perception fault")
        # Only an occlusion has an entity scope; a dropout is camera-wide and a
        # stale sample is aged rather than targeted.
        if spec.fault is FaultType.OCCLUSION and (not isinstance(entity_id, str) or not entity_id.strip()):
            raise FaultInjectionError("an occlusion must name the entity it blocks")
        self._port = port
        self.spec = spec
        self._rng = _checked_seed(seed)
        self._entity_id = entity_id
        self.injected: list[str] = []

    def observe(self, *, run_id: str, action_id: str, entity_id: str | None, not_before: object) -> object:
        from workbench_agent_runtime.post_action import ObservationAttempt

        if self.spec.fault is FaultType.OCCLUSION and entity_id != self._entity_id:
            return self._port.observe(run_id=run_id, action_id=action_id, entity_id=entity_id, not_before=not_before)
        self.injected.append(_action_id_from(action_id))
        if self.spec.fault is FaultType.CAMERA_DROPOUT:
            return ObservationAttempt.perception_failure(f"injected {self.spec.fault.value}")
        if self.spec.fault is FaultType.OCCLUSION:
            return ObservationAttempt.missing(f"injected {self.spec.fault.value} for {self._entity_id}")
        return self._stale(run_id=run_id, action_id=action_id, entity_id=entity_id, not_before=not_before)

    def _stale(self, *, run_id: str, action_id: str, entity_id: str | None, not_before: object) -> object:
        """Age a real observation to the action's completion instant.

        The sample still comes from the caller's port, so only its freshness
        changes; the injected value keeps the producer's own pose, confidence
        and evidence references rather than a fabricated replacement.
        """
        from workbench_agent_runtime.post_action import ObservationAttempt

        attempt = self._port.observe(
            run_id=run_id,
            action_id=action_id,
            entity_id=entity_id,
            not_before=not_before,
        )
        sample = getattr(attempt, "sample", None)
        if sample is None:
            # There was nothing to age. Reporting the miss is the honest answer;
            # manufacturing a sample here would fake the very evidence under test.
            return ObservationAttempt.missing("no observation was available to age")
        aged = sample.observation.model_copy(update={"observed_at": _isoformat(not_before)})
        return ObservationAttempt.observed(type(sample)(action_id=sample.action_id, observation=aged))


def _action_id_from(action_id: object) -> str:
    return action_id if isinstance(action_id, str) and action_id else "<unknown-action>"


def _isoformat(value: object) -> str:
    render = getattr(value, "isoformat", None)
    if not callable(render):
        raise FaultInjectionError("not_before must be a datetime-like value")
    rendered = render()
    if not isinstance(rendered, str):
        raise FaultInjectionError("not_before did not render to a timestamp string")
    return rendered


def inject_link_loss(mcu: object, *, fault_code: str = "WATCHDOG_TIMEOUT") -> str:
    """Drive the safety MCU to its fault state and return the fault code.

    A lost CAN link is observable on the host as an expired watchdog, so this
    uses the MCU's own watchdog path instead of writing a fault field directly.
    """
    expire = getattr(mcu, "watchdog_timeout", None)
    if not callable(expire):
        raise FaultInjectionError("mcu must implement watchdog_timeout")
    state = expire()
    observed = getattr(mcu, "fault_code", None)
    if observed != fault_code:
        raise FaultInjectionError(f"mcu reported fault code {observed!r}, expected {fault_code!r}")
    return str(state.value if isinstance(state, StrEnum) else state)


def inject_process_restart(loop: object, *, run_id: str) -> None:
    """Record a process restart so the run's correlation is explicitly invalidated."""
    note = getattr(loop, "note_restart", None)
    if not callable(note):
        raise FaultInjectionError("loop must implement note_restart")
    note(run_id=run_id)


def coverage_report(
    *,
    simulated: Iterable[str] = (),
    hardware_tested: Iterable[str] = (),
    physical_evidence: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Report every declared fault as simulated, hardware-tested or not executed.

    ``HARDWARE_TESTED`` requires a named physical evidence reference. A caller
    that claims hardware coverage without one is refused, because the cheapest
    way to overstate this report is to assert a status instead of attaching the
    artifact that supports it.
    """
    evidence = dict(physical_evidence or {})
    statuses: dict[str, CoverageStatus] = {}
    for name in simulated:
        _require_declared(name)
        statuses[name] = "SIMULATED"
    for name in hardware_tested:
        _require_declared(name)
        reference = evidence.get(name)
        if not isinstance(reference, str) or not reference.strip():
            raise FaultInjectionError(f"{name} claims HARDWARE_TESTED without a physical evidence reference")
        statuses[name] = "HARDWARE_TESTED"
    entries = [
        {
            **spec.as_dict(),
            "status": statuses.get(spec.fault.value, "NOT_EXECUTED"),
            "physical_evidence_ref": evidence.get(spec.fault.value),
        }
        for spec in FAULT_SPECS
    ]
    return {
        "fault_injection_version": FAULT_INJECTION_VERSION,
        "coverage_statuses": list(COVERAGE_STATUSES),
        "declared_count": len(FAULT_SPECS),
        "simulated_count": sum(1 for entry in entries if entry["status"] == "SIMULATED"),
        "hardware_tested_count": sum(1 for entry in entries if entry["status"] == "HARDWARE_TESTED"),
        "not_executed_count": sum(1 for entry in entries if entry["status"] == "NOT_EXECUTED"),
        "faults": entries,
    }


def _require_declared(name: str) -> None:
    if name not in declared_faults():
        raise FaultInjectionError(f"coverage report names an undeclared fault: {name!r}")


__all__ = [
    "COVERAGE_STATUSES",
    "FAULT_INJECTION_VERSION",
    "FAULT_SPECS",
    "AdapterFaultInjector",
    "FaultInjectionError",
    "FaultSpec",
    "FaultType",
    "ObservationFaultInjector",
    "RuntimeFault",
    "SafeBehavior",
    "coverage_report",
    "declared_faults",
    "fault_spec",
    "inject_link_loss",
    "inject_process_restart",
]
