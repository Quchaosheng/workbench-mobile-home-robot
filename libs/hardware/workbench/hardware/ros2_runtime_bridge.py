"""Bounded ROS 2 boundary for the existing hardware ``DeviceRuntime``.

The module deliberately keeps its core free of ROS imports.  A
``SafeCANBus`` already owns a ``DeviceRuntime``; the bridge reuses that
runtime instead of wrapping it in another worker or lifecycle state machine.
For a plain injected adapter, the core creates exactly one runtime and owns
it for the adapter's lifetime.

Only validated, immutable ``CanExternalRecord`` and ``CanDiagnostic`` values
can cross the read-only projection.  The ROS integration uses ``String`` as a
temporary JSON carrier until a separately approved public message schema is
available.  Fast DDS remains a deployment-selected RMW, never a dependency
of this module's core.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .can_driver_safe import (
    CanDiagnostic,
    CanDiagnosticCode,
    CanExternalRecord,
    CanFrameKind,
    CanReceiveStatus,
    CanTransportConfig,
    DeviceAdapter,
    DeviceRuntime,
    SafeCANBus,
)
from .socketcan_transport import SocketCANTransport

BRIDGE_SCHEMA_VERSION = "device-runtime-bridge-v1"
MAX_CAPACITY = 4096
MAX_EXECUTOR_THREADS = 8
MAX_RECORDS_PER_TICK = 1024
MAX_PERIOD_S = 60.0

DEFAULT_NODE_NAME = "workbench_device_runtime"
DEFAULT_TELEMETRY_TOPIC = "/workbench/device/telemetry"
DEFAULT_ACK_TOPIC = "/workbench/device/ack"
DEFAULT_HEALTH_TOPIC = "/workbench/device/health"

# These are intentionally explicit.  Adding a field to CanExternalRecord
# does not automatically make it externally visible.
EXTERNAL_PROJECTION_ALLOWLIST = (
    "status",
    "source",
    "interface",
    "ingress_sequence",
    "health",
    "frame_valid",
    "exposure_allowed",
    "event_type",
    "frame_kind",
    "arbitration_id",
    "raw_can_id",
    "dlc",
    "data_hex",
    "is_extended_id",
    "is_remote_frame",
    "is_error_frame",
    "monotonic_ts",
    "wall_ts",
    "kernel_timestamp_ns",
    "kernel_drop_count",
    "timestamp_source",
    "reason",
    "callback_errors",
    "confirmed",
    "command_id",
    "sequence_no",
    "opcode",
    "retry_count",
    "result_code",
    "fault_code",
    "device_mode",
    "evidence_refs",
)

HEALTH_PROJECTION_ALLOWLIST = (
    "code",
    "observed_at",
    "detail",
    "command_id",
    "source",
    "sequence",
)


class RuntimeBridgeState(StrEnum):
    """Lifecycle owned by the bridge around one adapter runtime instance."""

    UNCONFIGURED = "unconfigured"
    INACTIVE = "inactive"
    ACTIVE = "active"
    FAILED = "failed"
    SHUTDOWN = "shutdown"


class PublisherPort(Protocol):
    """Minimal publisher contract used by the ROS-free core."""

    def publish(self, payload: str) -> None: ...


@dataclass(frozen=True, slots=True)
class BridgeQoS:
    """A ROS-independent description of one bounded DDS QoS profile."""

    reliability: str
    depth: int
    history: str = "keep_last"
    durability: str = "volatile"
    deadline_s: float | None = None

    def __post_init__(self) -> None:
        if self.reliability not in {"best_effort", "reliable"}:
            raise ValueError("reliability must be best_effort or reliable")
        if self.history != "keep_last":
            raise ValueError("only keep_last history is supported")
        if self.durability not in {"volatile", "transient_local"}:
            raise ValueError("durability must be volatile or transient_local")
        _validate_bounded_positive_int(self.depth, "depth")
        if self.deadline_s is not None:
            _validate_period(self.deadline_s, "deadline_s")

    def to_dict(self) -> dict[str, object | None]:
        return {
            "reliability": self.reliability,
            "history": self.history,
            "depth": self.depth,
            "durability": self.durability,
            "deadline_s": self.deadline_s,
        }


@dataclass(frozen=True, slots=True)
class DeviceRuntimeBridgeConfig:
    """Fixed, bounded bridge and deployment settings.

    ``executor_threads`` describes the executor the caller must construct
    with :func:`create_bounded_executor`; the node never silently creates an
    auto-sized executor.
    """

    node_name: str = DEFAULT_NODE_NAME
    telemetry_topic: str = DEFAULT_TELEMETRY_TOPIC
    ack_topic: str = DEFAULT_ACK_TOPIC
    health_topic: str = DEFAULT_HEALTH_TOPIC
    poll_period_s: float = 0.02
    shutdown_timeout_s: float = 1.0
    max_records_per_tick: int = 32
    executor_threads: int = 2
    domain_id: int = 42
    rmw_implementation: str = "rmw_fastrtps_cpp"
    security_profile: str = "deployment-managed"
    command_capacity: int = 64
    telemetry_capacity: int = 64
    health_capacity: int = 128
    external_capacity: int = 64
    max_subscribers_per_id: int = 16
    telemetry_qos: BridgeQoS = field(default_factory=lambda: BridgeQoS("best_effort", depth=16))
    ack_qos: BridgeQoS = field(default_factory=lambda: BridgeQoS("reliable", depth=16, deadline_s=0.1))
    health_qos: BridgeQoS = field(default_factory=lambda: BridgeQoS("reliable", depth=32))

    def __post_init__(self) -> None:
        _validate_node_name(self.node_name)
        for name in ("telemetry_topic", "ack_topic", "health_topic"):
            _validate_topic_name(getattr(self, name), name)
        _validate_period(self.poll_period_s, "poll_period_s")
        _validate_period(self.shutdown_timeout_s, "shutdown_timeout_s")
        _validate_bounded_positive_int(
            self.max_records_per_tick,
            "max_records_per_tick",
            maximum=MAX_RECORDS_PER_TICK,
        )
        _validate_bounded_positive_int(
            self.executor_threads,
            "executor_threads",
            maximum=MAX_EXECUTOR_THREADS,
        )
        if type(self.domain_id) is not int or not 0 <= self.domain_id <= 232:
            raise ValueError("domain_id must be an integer from 0 through 232")
        _validate_identifier(self.rmw_implementation, "rmw_implementation")
        _validate_identifier(self.security_profile, "security_profile")
        for name in (
            "command_capacity",
            "telemetry_capacity",
            "health_capacity",
            "external_capacity",
            "max_subscribers_per_id",
        ):
            _validate_bounded_positive_int(getattr(self, name), name)
        for name in ("telemetry_qos", "ack_qos", "health_qos"):
            if not isinstance(getattr(self, name), BridgeQoS):
                raise TypeError(f"{name} must be BridgeQoS")
        if self.ack_qos.reliability != "reliable":
            raise ValueError("ack_qos must be reliable")
        if self.health_qos.reliability != "reliable":
            raise ValueError("health_qos must be reliable")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "node_name": self.node_name,
            "domain_id": self.domain_id,
            "rmw_implementation": self.rmw_implementation,
            "security_profile": self.security_profile,
            "executor_threads": self.executor_threads,
            "executor_threads_max": MAX_EXECUTOR_THREADS,
            "topics": {
                "telemetry": self.telemetry_topic,
                "ack": self.ack_topic,
                "health": self.health_topic,
            },
            "poll_period_s": self.poll_period_s,
            "shutdown_timeout_s": self.shutdown_timeout_s,
            "max_records_per_tick": self.max_records_per_tick,
            "queues": {
                "command": self.command_capacity,
                "telemetry": self.telemetry_capacity,
                "health": self.health_capacity,
                "external": self.external_capacity,
                "max_subscribers_per_id": self.max_subscribers_per_id,
            },
            "qos": {
                "telemetry": self.telemetry_qos.to_dict(),
                "ack": self.ack_qos.to_dict(),
                "health": self.health_qos.to_dict(),
            },
        }


@dataclass(frozen=True, slots=True)
class BridgePublishers:
    """Three independently routed read-only publication planes."""

    telemetry: PublisherPort
    ack: PublisherPort
    health: PublisherPort

    def __post_init__(self) -> None:
        for name in ("telemetry", "ack", "health"):
            publisher = getattr(self, name)
            if not callable(getattr(publisher, "publish", None)):
                raise TypeError(f"{name} publisher must provide publish(payload)")


@dataclass(frozen=True, slots=True)
class BridgeHealthRecord:
    """Bounded bridge-local health record for lifecycle/projection failures."""

    sequence: int
    code: str
    observed_at: float
    detail: str
    source: str = "ros2-runtime-bridge"
    command_id: int | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("health sequence must be a non-negative integer")
        _validate_identifier(self.code, "health code")
        if isinstance(self.observed_at, bool) or not isinstance(self.observed_at, int | float):
            raise ValueError("health observed_at must be numeric")
        if not math.isfinite(float(self.observed_at)):
            raise ValueError("health observed_at must be finite")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("health detail must be non-empty")
        _validate_identifier(self.source, "health source")
        if self.command_id is not None and (type(self.command_id) is not int or self.command_id < 0):
            raise ValueError("health command_id must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class BridgeMetrics:
    """Immutable snapshot of bounded bridge work and source-plane counters."""

    state: RuntimeBridgeState
    drain_cycles: int
    records_processed: int
    telemetry_published: int
    ack_published: int
    health_published: int
    unsupported_records: int
    serialization_errors: int
    publisher_errors: int
    drain_limit_hits: int
    external_depth: int
    health_depth: int
    telemetry_drop_count: int
    health_drop_count: int
    external_drop_count: int
    bridge_health_drop_count: int
    worker_alive: bool
    external_oldest_age_s: float | None
    health_oldest_age_s: float | None

    def to_dict(self) -> dict[str, object | None]:
        return {
            "state": self.state.value,
            "drain_cycles": self.drain_cycles,
            "records_processed": self.records_processed,
            "telemetry_published": self.telemetry_published,
            "ack_published": self.ack_published,
            "health_published": self.health_published,
            "unsupported_records": self.unsupported_records,
            "serialization_errors": self.serialization_errors,
            "publisher_errors": self.publisher_errors,
            "drain_limit_hits": self.drain_limit_hits,
            "external_depth": self.external_depth,
            "health_depth": self.health_depth,
            "telemetry_drop_count": self.telemetry_drop_count,
            "health_drop_count": self.health_drop_count,
            "external_drop_count": self.external_drop_count,
            "bridge_health_drop_count": self.bridge_health_drop_count,
            "worker_alive": self.worker_alive,
            "external_oldest_age_s": self.external_oldest_age_s,
            "health_oldest_age_s": self.health_oldest_age_s,
        }


@dataclass(slots=True)
class _MutableCounters:
    drain_cycles: int = 0
    records_processed: int = 0
    telemetry_published: int = 0
    ack_published: int = 0
    health_published: int = 0
    unsupported_records: int = 0
    serialization_errors: int = 0
    publisher_errors: int = 0
    drain_limit_hits: int = 0
    bridge_health_drop_count: int = 0


RuntimeFactory = Callable[[DeviceAdapter, DeviceRuntimeBridgeConfig], DeviceRuntime]
AdapterFactory = Callable[[], DeviceAdapter]


def serialize_external_projection(record: CanExternalRecord) -> str:
    """Serialize one exposed CAN record using only the fixed allowlist."""

    if not isinstance(record, CanExternalRecord):
        raise TypeError("external projection requires CanExternalRecord")
    if record.status is not CanReceiveStatus.ACCEPTED or not record.frame_valid or not record.exposure_allowed:
        raise ValueError("only accepted, valid and exposure-allowed records may be projected")
    record_type = _external_record_type(record)
    projection: dict[str, object | None] = {
        "status": record.status.value,
        "source": record.source,
        "interface": record.interface,
        "ingress_sequence": record.ingress_sequence,
        "health": record.health.value,
        "frame_valid": record.frame_valid,
        "exposure_allowed": record.exposure_allowed,
        "event_type": record.event_type,
        "frame_kind": None if record.frame_kind is None else record.frame_kind.value,
        "arbitration_id": record.arbitration_id,
        "raw_can_id": record.raw_can_id,
        "dlc": record.dlc,
        "data_hex": record.data_hex,
        "is_extended_id": record.is_extended_id,
        "is_remote_frame": record.is_remote_frame,
        "is_error_frame": record.is_error_frame,
        "monotonic_ts": record.monotonic_ts,
        "wall_ts": record.wall_ts,
        "kernel_timestamp_ns": record.kernel_timestamp_ns,
        "kernel_drop_count": record.kernel_drop_count,
        "timestamp_source": record.timestamp_source,
        "reason": record.reason,
        "callback_errors": record.callback_errors,
        "confirmed": record.confirmed,
        "command_id": record.command_id,
        "sequence_no": record.sequence_no,
        "opcode": record.opcode,
        "retry_count": record.retry_count,
        "result_code": record.result_code,
        "fault_code": record.fault_code,
        "device_mode": record.device_mode,
        "evidence_refs": list(record.evidence_refs),
    }
    _assert_allowlist(projection, EXTERNAL_PROJECTION_ALLOWLIST)
    return _dump_json(
        {
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "record_type": record_type,
            **projection,
        }
    )


def serialize_health_projection(record: CanDiagnostic | BridgeHealthRecord) -> str:
    """Serialize one runtime or bridge health record using a fixed allowlist."""

    if isinstance(record, CanDiagnostic):
        if not isinstance(record.code, CanDiagnosticCode):
            raise ValueError("diagnostic code must be CanDiagnosticCode")
        if isinstance(record.observed_at, bool) or not isinstance(record.observed_at, int | float):
            raise ValueError("diagnostic observed_at must be numeric")
        if not math.isfinite(float(record.observed_at)):
            raise ValueError("diagnostic observed_at must be finite")
        if not isinstance(record.detail, str) or not record.detail.strip():
            raise ValueError("diagnostic detail must be non-empty")
        if record.command_id is not None and (type(record.command_id) is not int or record.command_id < 0):
            raise ValueError("diagnostic command_id must be a non-negative integer")
        projection: dict[str, object | None] = {
            "code": record.code.value,
            "observed_at": record.observed_at,
            "detail": record.detail,
            "command_id": record.command_id,
            "source": "device-runtime",
            "sequence": None,
        }
    elif isinstance(record, BridgeHealthRecord):
        projection = {
            "code": record.code,
            "observed_at": record.observed_at,
            "detail": record.detail,
            "command_id": record.command_id,
            "source": record.source,
            "sequence": record.sequence,
        }
    else:
        raise TypeError("health projection requires CanDiagnostic or BridgeHealthRecord")
    _assert_allowlist(projection, HEALTH_PROJECTION_ALLOWLIST)
    return _dump_json(
        {
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "record_type": "health",
            **projection,
        }
    )


class RuntimeBridgeCore:
    """ROS-free, bounded controller for one adapter/runtime boundary."""

    def __init__(
        self,
        adapter_factory: AdapterFactory,
        *,
        config: DeviceRuntimeBridgeConfig | None = None,
        publishers: BridgePublishers | None = None,
        runtime_factory: RuntimeFactory | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(adapter_factory):
            raise TypeError("adapter_factory must be callable")
        if runtime_factory is not None and not callable(runtime_factory):
            raise TypeError("runtime_factory must be callable when present")
        if not callable(monotonic_clock) or not callable(wall_clock):
            raise TypeError("clock arguments must be callable")
        if publishers is not None and not isinstance(publishers, BridgePublishers):
            raise TypeError("publishers must be BridgePublishers when present")
        self._adapter_factory = adapter_factory
        self._config = _resolve_config(config)
        self._publishers = publishers
        self._runtime_factory = runtime_factory or _default_runtime_factory
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._state = RuntimeBridgeState.UNCONFIGURED
        self._adapter: DeviceAdapter | None = None
        self._runtime: DeviceRuntime | None = None
        self._health_seen: deque[object] = deque(maxlen=max(self._config.health_capacity * 2, 16))
        self._bridge_health: deque[BridgeHealthRecord] = deque()
        self._next_health_sequence = 0
        self._counters = _MutableCounters()

    @property
    def state(self) -> RuntimeBridgeState:
        with self._lock:
            return self._state

    @property
    def config(self) -> DeviceRuntimeBridgeConfig:
        return self._config

    @property
    def adapter(self) -> DeviceAdapter | None:
        with self._lock:
            return self._adapter

    @property
    def runtime(self) -> DeviceRuntime | None:
        with self._lock:
            return self._runtime

    def attach_publishers(self, publishers: BridgePublishers) -> None:
        if not isinstance(publishers, BridgePublishers):
            raise TypeError("publishers must be BridgePublishers")
        with self._lock:
            if self._state is not RuntimeBridgeState.UNCONFIGURED:
                raise RuntimeError("publishers can only be attached before configure")
            self._publishers = publishers

    def configure(self) -> bool:
        """Construct the adapter and its single runtime without opening I/O."""

        with self._lock:
            if self._state is not RuntimeBridgeState.UNCONFIGURED:
                return False
            try:
                self._create_runtime_locked()
            except Exception as exc:  # noqa: BLE001 - boundary fails closed.
                self._record_bridge_health_locked("configure_failed", str(exc))
                self._state = RuntimeBridgeState.FAILED
                return False
            self._state = RuntimeBridgeState.INACTIVE
            return True

    def activate(self) -> bool:
        """Start the adapter through its existing single DeviceRuntime worker."""

        with self._lock:
            if self._state is not RuntimeBridgeState.INACTIVE:
                return False
            try:
                if self._runtime is None:
                    self._create_runtime_locked()
                runtime = self._require_runtime_locked()
                started = runtime.start(background=True)
            except Exception as exc:  # noqa: BLE001 - boundary fails closed.
                self._record_bridge_health_locked("activate_failed", str(exc))
                self._state = RuntimeBridgeState.FAILED
                return False
            if not started:
                self._record_bridge_health_locked(
                    "activate_failed",
                    f"DeviceRuntime.start returned false (state={runtime.state.value})",
                )
                self._state = RuntimeBridgeState.FAILED
                return False
            self._state = RuntimeBridgeState.ACTIVE
            return True

    def deactivate(self) -> bool:
        """Stop and discard the current runtime so a later activation is fresh."""

        with self._lock:
            if self._state is RuntimeBridgeState.INACTIVE:
                return True
            if self._state not in {RuntimeBridgeState.ACTIVE, RuntimeBridgeState.FAILED}:
                return False
            runtime = self._runtime
            if runtime is None:
                self._state = RuntimeBridgeState.INACTIVE
                return True
            try:
                stopped = runtime.shutdown(timeout_s=self._config.shutdown_timeout_s)
            except Exception as exc:  # noqa: BLE001 - boundary fails closed.
                self._record_bridge_health_locked("deactivate_failed", str(exc))
                self._state = RuntimeBridgeState.FAILED
                return False
            if not stopped:
                self._record_bridge_health_locked(
                    "deactivate_failed",
                    "DeviceRuntime.shutdown did not complete before the deadline",
                )
                self._state = RuntimeBridgeState.FAILED
                return False
            self._discard_runtime_locked()
            self._state = RuntimeBridgeState.INACTIVE
            return True

    def cleanup(self) -> bool:
        """Release the configured adapter and return to the unconfigured state."""

        with self._lock:
            if self._state is RuntimeBridgeState.UNCONFIGURED:
                return True
            if self._state is RuntimeBridgeState.SHUTDOWN:
                return False
            if self._runtime is not None:
                try:
                    stopped = self._runtime.shutdown(timeout_s=self._config.shutdown_timeout_s)
                except Exception as exc:  # noqa: BLE001 - boundary fails closed.
                    self._record_bridge_health_locked("cleanup_failed", str(exc))
                    self._state = RuntimeBridgeState.FAILED
                    return False
                if not stopped:
                    self._record_bridge_health_locked(
                        "cleanup_failed",
                        "DeviceRuntime.shutdown did not complete before the deadline",
                    )
                    self._state = RuntimeBridgeState.FAILED
                    return False
            self._discard_runtime_locked()
            self._state = RuntimeBridgeState.UNCONFIGURED
            self._bridge_health.clear()
            return True

    def shutdown(self) -> bool:
        """Perform terminal cleanup; a failed stop remains observable."""

        with self._lock:
            if self._state is RuntimeBridgeState.SHUTDOWN:
                return True
            if self._state is RuntimeBridgeState.UNCONFIGURED:
                self._state = RuntimeBridgeState.SHUTDOWN
                return True
            if self._runtime is not None:
                try:
                    stopped = self._runtime.shutdown(timeout_s=self._config.shutdown_timeout_s)
                except Exception as exc:  # noqa: BLE001 - boundary fails closed.
                    self._record_bridge_health_locked("shutdown_failed", str(exc))
                    self._state = RuntimeBridgeState.FAILED
                    return False
                if not stopped:
                    self._record_bridge_health_locked(
                        "shutdown_failed",
                        "DeviceRuntime.shutdown did not complete before the deadline",
                    )
                    self._state = RuntimeBridgeState.FAILED
                    return False
            self._discard_runtime_locked()
            self._state = RuntimeBridgeState.SHUTDOWN
            return True

    def record_failure(self, code: str, detail: str) -> None:
        """Record a bounded bridge-local failure for the health topic."""

        with self._lock:
            self._record_bridge_health_locked(code, detail)

    def drain_once(self, *, limit: int | None = None) -> int:
        """Drain at most ``limit`` records without blocking on hardware I/O.

        Health receives a small quota first, then external records are routed
        to ACK or telemetry.  A publisher exception consumes that record and
        increments a metric; it never escapes into the runtime worker.
        """

        if limit is None:
            limit = self._config.max_records_per_tick
        if type(limit) is not int or not 0 < limit <= self._config.max_records_per_tick:
            raise ValueError("limit must be a positive integer within max_records_per_tick")
        with self._lock:
            if self._state is not RuntimeBridgeState.ACTIVE:
                return 0
            runtime = self._require_runtime_locked()
            publishers = self._publishers
            if publishers is None:
                self._record_bridge_health_locked("publisher_missing", "bridge publishers are not attached")
                return 0
            self._counters.drain_cycles += 1
            processed = 0
            health_quota = min(limit, max(1, limit // 4))
            while processed < health_quota:
                record = self._take_health_locked(runtime)
                if record is None:
                    break
                processed += 1
                self._counters.records_processed += 1
                self._publish_health_locked(record, publishers.health)

            external_processed = 0
            while processed < limit:
                record = runtime.take_external()
                if record is None:
                    break
                processed += 1
                external_processed += 1
                self._counters.records_processed += 1
                self._publish_external_locked(record, publishers)

            # If no external record was available, use the remaining budget
            # for health so a quiet device still flushes diagnostics promptly.
            # Records created while handling this tick are intentionally left
            # for the next tick; this prevents publisher failures from
            # recursively consuming the whole budget.
            if external_processed == 0:
                while processed < limit:
                    record = self._take_health_locked(runtime)
                    if record is None:
                        break
                    processed += 1
                    self._counters.records_processed += 1
                    self._publish_health_locked(record, publishers.health)

            if processed >= limit and (runtime.external_depth or self._health_pending_locked(runtime)):
                self._counters.drain_limit_hits += 1
            return processed

    def metrics(self) -> BridgeMetrics:
        with self._lock:
            runtime = self._runtime
            external_depth = 0 if runtime is None else runtime.external_depth
            pending_health = () if runtime is None else self._unseen_health_records_locked(runtime)
            health_depth = len(pending_health) + len(self._bridge_health)
            return BridgeMetrics(
                state=self._state,
                drain_cycles=self._counters.drain_cycles,
                records_processed=self._counters.records_processed,
                telemetry_published=self._counters.telemetry_published,
                ack_published=self._counters.ack_published,
                health_published=self._counters.health_published,
                unsupported_records=self._counters.unsupported_records,
                serialization_errors=self._counters.serialization_errors,
                publisher_errors=self._counters.publisher_errors,
                drain_limit_hits=self._counters.drain_limit_hits,
                external_depth=external_depth,
                health_depth=health_depth,
                telemetry_drop_count=0 if runtime is None else runtime.telemetry_drop_count,
                health_drop_count=0 if runtime is None else runtime.health_drop_count,
                external_drop_count=0 if runtime is None else runtime.external_drop_count,
                bridge_health_drop_count=self._counters.bridge_health_drop_count,
                worker_alive=False if runtime is None else runtime.worker_alive,
                external_oldest_age_s=self._oldest_external_age_locked(runtime),
                health_oldest_age_s=self._oldest_health_age_locked(pending_health),
            )

    def configuration_snapshot(self) -> dict[str, object]:
        return self._config.to_dict()

    def bridge_health_records(self) -> tuple[BridgeHealthRecord, ...]:
        with self._lock:
            return tuple(self._bridge_health)

    def _create_runtime_locked(self) -> None:
        adapter = self._adapter_factory()
        _validate_adapter_port(adapter)
        candidate = getattr(adapter, "runtime", None)
        if isinstance(candidate, DeviceRuntime):
            runtime = candidate
        else:
            runtime = self._runtime_factory(adapter, self._config)
            if not isinstance(runtime, DeviceRuntime):
                raise TypeError("runtime_factory must return DeviceRuntime")
        self._adapter = adapter
        self._runtime = runtime
        self._health_seen.clear()

    def _discard_runtime_locked(self) -> None:
        self._adapter = None
        self._runtime = None
        self._health_seen.clear()
        self._bridge_health.clear()

    def _require_runtime_locked(self) -> DeviceRuntime:
        if self._runtime is None:
            raise RuntimeError("bridge has no configured DeviceRuntime")
        return self._runtime

    def _take_health_locked(self, runtime: DeviceRuntime) -> CanDiagnostic | BridgeHealthRecord | object | None:
        if self._bridge_health:
            return self._bridge_health.popleft()
        for item in self._unseen_health_records_locked(runtime):
            self._health_seen.append(item)
            return item
        return None

    def _health_pending_locked(self, runtime: DeviceRuntime) -> bool:
        return bool(self._bridge_health or self._unseen_health_records_locked(runtime))

    def _unseen_health_records_locked(self, runtime: DeviceRuntime) -> tuple[object, ...]:
        snapshot = runtime.health_records()
        seen_ids = {id(item) for item in self._health_seen}
        return tuple(item for item in snapshot if id(item) not in seen_ids)

    def _publish_health_locked(self, record: object, publisher: PublisherPort) -> None:
        try:
            payload = serialize_health_projection(record)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError) as exc:
            self._counters.unsupported_records += 1
            self._counters.serialization_errors += 1
            self._record_bridge_health_locked("health_projection_rejected", str(exc))
            return
        self._publish_locked(publisher, payload, plane="health")

    def _publish_external_locked(self, record: object, publishers: BridgePublishers) -> None:
        if not isinstance(record, CanExternalRecord):
            self._counters.unsupported_records += 1
            self._record_bridge_health_locked(
                "external_projection_rejected",
                "external queue contained an unsupported object",
            )
            return
        try:
            record_type = _external_record_type(record)
            payload = serialize_external_projection(record)
        except (TypeError, ValueError, OverflowError) as exc:
            self._counters.unsupported_records += 1
            self._counters.serialization_errors += 1
            self._record_bridge_health_locked("external_projection_rejected", str(exc))
            return
        publisher = publishers.ack if record_type == "ack" else publishers.telemetry
        self._publish_locked(publisher, payload, plane=record_type)

    def _publish_locked(self, publisher: PublisherPort, payload: str, *, plane: str) -> None:
        try:
            publisher.publish(payload)
        except Exception as exc:  # noqa: BLE001 - publisher faults are isolated.
            self._counters.publisher_errors += 1
            self._record_bridge_health_locked("publisher_failed", f"{plane}: {exc}")
            return
        if plane == "telemetry":
            self._counters.telemetry_published += 1
        elif plane == "ack":
            self._counters.ack_published += 1
        elif plane == "health":
            self._counters.health_published += 1

    def _record_bridge_health_locked(self, code: str, detail: str) -> None:
        try:
            observed_at = self._wall_clock()
        except Exception:  # noqa: BLE001 - health recording must not raise.
            observed_at = time.time()
        try:
            record = BridgeHealthRecord(
                sequence=self._next_health_sequence,
                code=code,
                observed_at=float(observed_at),
                detail=detail,
            )
        except (TypeError, ValueError, OverflowError):
            record = BridgeHealthRecord(
                sequence=self._next_health_sequence,
                code="bridge_internal_error",
                observed_at=0.0,
                detail="bridge could not construct a health record",
            )
        self._next_health_sequence += 1
        if len(self._bridge_health) >= self._config.health_capacity:
            self._bridge_health.popleft()
            self._counters.bridge_health_drop_count += 1
        self._bridge_health.append(record)

    def _oldest_external_age_locked(self, runtime: DeviceRuntime | None) -> float | None:
        if runtime is None:
            return None
        now = _safe_clock(self._monotonic_clock)
        if now is None:
            return None
        ages: list[float] = []
        for item in runtime.external_records():
            if not isinstance(item, CanExternalRecord):
                continue
            timestamp = item.monotonic_ts
            if isinstance(timestamp, int | float) and not isinstance(timestamp, bool) and math.isfinite(timestamp):
                ages.append(max(0.0, now - float(timestamp)))
        return max(ages) if ages else None

    def _oldest_health_age_locked(self, snapshot: tuple[object, ...]) -> float | None:
        now = _safe_clock(self._wall_clock)
        if now is None:
            return None
        ages: list[float] = []
        for item in (*snapshot, *self._bridge_health):
            if not isinstance(item, (CanDiagnostic, BridgeHealthRecord)):
                continue
            timestamp = item.observed_at
            if isinstance(timestamp, int | float) and not isinstance(timestamp, bool) and math.isfinite(timestamp):
                ages.append(max(0.0, now - float(timestamp)))
        return max(ages) if ages else None


DeviceRuntimeBridge = RuntimeBridgeCore


def create_socketcan_adapter_factory(
    interface: str,
    *,
    config: DeviceRuntimeBridgeConfig | None = None,
    source: str = "socketcan",
    transport_kwargs: Mapping[str, object] | None = None,
) -> AdapterFactory:
    """Create a fresh ``SafeCANBus(SocketCANTransport(...))`` per activation."""

    bridge_config = _resolve_config(config)
    if not isinstance(interface, str) or not interface.strip() or interface != interface.strip():
        raise ValueError("interface must be a non-empty trimmed string")
    if not isinstance(source, str) or not source.strip() or source != source.strip():
        raise ValueError("source must be a non-empty trimmed string")
    options = dict(transport_kwargs or {})
    for reserved in ("interface", "source"):
        if reserved in options:
            raise ValueError(f"transport_kwargs must not override {reserved}")
    can_config = CanTransportConfig(
        queue_capacity=bridge_config.command_capacity,
        telemetry_capacity=bridge_config.telemetry_capacity,
        health_capacity=bridge_config.health_capacity,
        diagnostic_capacity=bridge_config.health_capacity,
        external_capacity=bridge_config.external_capacity,
        max_subscribers_per_id=bridge_config.max_subscribers_per_id,
        poll_interval_s=bridge_config.poll_period_s,
        shutdown_timeout_s=bridge_config.shutdown_timeout_s,
        source=source,
        interface=interface,
    )

    def factory() -> SafeCANBus:
        transport = SocketCANTransport(interface, source=source, **options)
        return SafeCANBus(transport, config=can_config)

    return factory


def create_bounded_executor(config: DeviceRuntimeBridgeConfig | None = None, *, context: Any = None) -> Any:
    """Construct the explicitly sized ROS 2 executor selected by the config."""

    bridge_config = _resolve_config(config)
    try:
        from rclpy.executors import MultiThreadedExecutor
    except ImportError as exc:  # pragma: no cover - depends on host ROS install.
        raise RuntimeError("ROS 2 rclpy is required to create the executor") from exc
    kwargs: dict[str, object] = {"num_threads": bridge_config.executor_threads}
    if context is not None:
        kwargs["context"] = context
    return MultiThreadedExecutor(**kwargs)


def create_lifecycle_node(
    adapter_factory: AdapterFactory,
    *,
    config: DeviceRuntimeBridgeConfig | None = None,
    context: Any = None,
) -> Any:
    """Lazily create a ROS 2 ``LifecycleNode`` around ``RuntimeBridgeCore``.

    Importing this module does not import ``rclpy``.  Callers must initialize
    the desired ROS context before invoking this factory.
    """

    bridge_config = _resolve_config(config)
    try:
        from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
        from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        from std_msgs.msg import String
    except ImportError as exc:  # pragma: no cover - depends on host ROS install.
        raise RuntimeError("ROS 2 Jazzy rclpy and std_msgs are required for the lifecycle bridge") from exc

    def ros_qos(profile: BridgeQoS) -> QoSProfile:
        reliability = ReliabilityPolicy.RELIABLE if profile.reliability == "reliable" else ReliabilityPolicy.BEST_EFFORT
        durability = (
            DurabilityPolicy.TRANSIENT_LOCAL if profile.durability == "transient_local" else DurabilityPolicy.VOLATILE
        )
        kwargs: dict[str, object] = {
            "history": HistoryPolicy.KEEP_LAST,
            "depth": profile.depth,
            "reliability": reliability,
            "durability": durability,
        }
        if profile.deadline_s is not None:
            from rclpy.duration import Duration

            kwargs["deadline"] = Duration(nanoseconds=int(profile.deadline_s * 1_000_000_000))
        return QoSProfile(**kwargs)

    class RosStringPublisher:
        def __init__(self, publisher: Any) -> None:
            self.publisher = publisher

        def publish(self, payload: str) -> None:
            message = String()
            message.data = payload
            self.publisher.publish(message)

    class DeviceRuntimeLifecycleNode(LifecycleNode):
        def __init__(self) -> None:
            node_kwargs: dict[str, object] = {}
            if context is not None:
                node_kwargs["context"] = context
            super().__init__(bridge_config.node_name, **node_kwargs)
            self._bridge = RuntimeBridgeCore(adapter_factory, config=bridge_config)
            self._telemetry_publisher: Any | None = None
            self._ack_publisher: Any | None = None
            self._health_publisher: Any | None = None
            self._timer: Any | None = None
            self._timer_group = MutuallyExclusiveCallbackGroup()

        @property
        def bridge(self) -> RuntimeBridgeCore:
            return self._bridge

        @property
        def bridge_config(self) -> DeviceRuntimeBridgeConfig:
            return bridge_config

        @property
        def timer(self) -> Any | None:
            return self._timer

        def on_configure(self, state: Any) -> Any:
            try:
                self._telemetry_publisher = self.create_lifecycle_publisher(
                    String,
                    bridge_config.telemetry_topic,
                    ros_qos(bridge_config.telemetry_qos),
                )
                self._ack_publisher = self.create_lifecycle_publisher(
                    String,
                    bridge_config.ack_topic,
                    ros_qos(bridge_config.ack_qos),
                )
                self._health_publisher = self.create_lifecycle_publisher(
                    String,
                    bridge_config.health_topic,
                    ros_qos(bridge_config.health_qos),
                )
                self._bridge.attach_publishers(
                    BridgePublishers(
                        telemetry=RosStringPublisher(self._telemetry_publisher),
                        ack=RosStringPublisher(self._ack_publisher),
                        health=RosStringPublisher(self._health_publisher),
                    )
                )
                if not self._bridge.configure():
                    self._bridge.cleanup()
                    self._destroy_entities()
                    return TransitionCallbackReturn.FAILURE
                self._timer = self.create_timer(
                    bridge_config.poll_period_s,
                    self._on_timer,
                    callback_group=self._timer_group,
                    autostart=False,
                )
                result = super().on_configure(state)
            except Exception as exc:  # noqa: BLE001 - failed configure is terminal for this transition.
                self._bridge.record_failure("ros_configure_failed", str(exc))
                self._bridge.cleanup()
                self._destroy_entities()
                return TransitionCallbackReturn.ERROR
            if result is not TransitionCallbackReturn.SUCCESS:
                self._bridge.cleanup()
                self._destroy_entities()
            return result

        def on_activate(self, state: Any) -> Any:
            if not self._bridge.activate():
                return TransitionCallbackReturn.FAILURE
            result = super().on_activate(state)
            if result is not TransitionCallbackReturn.SUCCESS:
                self._bridge.deactivate()
                return result
            if self._timer is not None:
                self._timer.reset()
            return result

        def on_deactivate(self, state: Any) -> Any:
            if self._timer is not None:
                self._timer.cancel()
            runtime_ok = self._bridge.deactivate()
            managed_result = super().on_deactivate(state)
            if not runtime_ok:
                return TransitionCallbackReturn.FAILURE
            return managed_result

        def on_cleanup(self, state: Any) -> Any:
            if self._timer is not None:
                self._timer.cancel()
            if not self._bridge.cleanup():
                return TransitionCallbackReturn.FAILURE
            managed_result = super().on_cleanup(state)
            if managed_result is not TransitionCallbackReturn.SUCCESS:
                return managed_result
            return TransitionCallbackReturn.SUCCESS if self._destroy_entities() else TransitionCallbackReturn.FAILURE

        def on_shutdown(self, state: Any) -> Any:
            if self._timer is not None:
                self._timer.cancel()
            runtime_ok = self._bridge.shutdown()
            managed_result = super().on_shutdown(state)
            entities_ok = self._destroy_entities() if runtime_ok else False
            if not runtime_ok:
                return TransitionCallbackReturn.FAILURE
            if managed_result is not TransitionCallbackReturn.SUCCESS:
                return managed_result
            return TransitionCallbackReturn.SUCCESS if entities_ok else TransitionCallbackReturn.FAILURE

        def on_error(self, state: Any) -> Any:
            if self._timer is not None:
                self._timer.cancel()
            self._bridge.shutdown()
            return super().on_error(state)

        def _on_timer(self) -> None:
            try:
                self._bridge.drain_once()
            except Exception as exc:  # noqa: BLE001 - executor callback must fail closed.
                self._bridge.record_failure("timer_failed", str(exc))

        def _destroy_entities(self) -> bool:
            success = True
            if self._timer is not None:
                success = self.destroy_timer(self._timer) and success
                self._timer = None
            for name in ("_telemetry_publisher", "_ack_publisher", "_health_publisher"):
                publisher = getattr(self, name)
                if publisher is not None:
                    success = self.destroy_lifecycle_publisher(publisher) and success
                    setattr(self, name, None)
            return success

    DeviceRuntimeLifecycleNode.__name__ = "DeviceRuntimeLifecycleNode"
    DeviceRuntimeLifecycleNode.__qualname__ = "DeviceRuntimeLifecycleNode"
    return DeviceRuntimeLifecycleNode()


def create_socketcan_lifecycle_node(
    interface: str,
    *,
    config: DeviceRuntimeBridgeConfig | None = None,
    source: str = "socketcan",
    transport_kwargs: Mapping[str, object] | None = None,
    context: Any = None,
) -> Any:
    """Convenience factory for the concrete SocketCAN read-only bridge path."""

    bridge_config = _resolve_config(config)
    factory = create_socketcan_adapter_factory(
        interface,
        config=bridge_config,
        source=source,
        transport_kwargs=transport_kwargs,
    )
    return create_lifecycle_node(factory, config=bridge_config, context=context)


def _default_runtime_factory(adapter: DeviceAdapter, config: DeviceRuntimeBridgeConfig) -> DeviceRuntime:
    return DeviceRuntime(
        adapter,
        command_capacity=config.command_capacity,
        telemetry_capacity=config.telemetry_capacity,
        health_capacity=config.health_capacity,
        max_subscribers_per_id=config.max_subscribers_per_id,
        poll_interval_s=config.poll_period_s,
        external_capacity=config.external_capacity,
    )


def _validate_adapter_port(adapter: object) -> None:
    for name in ("configure", "activate", "poll", "deactivate", "cleanup"):
        if not callable(getattr(adapter, name, None)):
            raise TypeError(f"adapter is missing DeviceAdapter method {name}")


def _validate_bounded_positive_int(value: int, name: str, *, maximum: int = MAX_CAPACITY) -> None:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 through {maximum}")


def _validate_period(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a finite positive number")
    if not math.isfinite(float(value)) or not 0 < float(value) <= MAX_PERIOD_S:
        raise ValueError(f"{name} must be a finite positive number no greater than {MAX_PERIOD_S}")


def _validate_identifier(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{name} must be a non-empty identifier without whitespace")


def _validate_node_name(value: str) -> None:
    _validate_identifier(value, "node_name")
    if (
        "/" in value
        or not (value[0].isalpha() or value[0] == "_")
        or any(not (character.isalnum() or character == "_") for character in value)
    ):
        raise ValueError("node_name must contain only letters, digits and underscores")


def _validate_topic_name(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed topic name")
    if (
        not value.startswith("/")
        or "//" in value
        or any(not segment for segment in value.split("/")[1:])
        or any(character.isspace() or ord(character) < 32 for character in value)
        or any(segment in {".", ".."} for segment in value.split("/"))
        or any(not (character.isalnum() or character in {"/", "_", "~", ".", "-"}) for character in value)
    ):
        raise ValueError(f"{name} must be an absolute ROS topic without empty segments")


def _external_record_type(record: CanExternalRecord) -> str:
    if record.frame_kind in {CanFrameKind.ACK, CanFrameKind.STOP_ACK}:
        record_type = "ack"
    elif record.frame_kind is CanFrameKind.TELEMETRY:
        record_type = "telemetry"
    else:
        raise ValueError("external record has no routable inbound frame kind")
    expected_event_type = "action_result" if record_type == "ack" else "telemetry"
    if record.event_type != expected_event_type:
        raise ValueError("external record event_type does not match its frame kind")
    return record_type


def _assert_allowlist(payload: Mapping[str, object | None], allowlist: tuple[str, ...]) -> None:
    if set(payload) != set(allowlist):
        raise ValueError("projection field set does not match its explicit allowlist")


def _dump_json(payload: Mapping[str, object | None]) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _safe_clock(clock: Callable[[], float]) -> float | None:
    try:
        value = clock()
    except Exception:  # noqa: BLE001 - metrics are best-effort and never control lifecycle.
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        return None
    return float(value)


def _resolve_config(config: DeviceRuntimeBridgeConfig | None) -> DeviceRuntimeBridgeConfig:
    if config is None:
        return DeviceRuntimeBridgeConfig()
    if not isinstance(config, DeviceRuntimeBridgeConfig):
        raise TypeError("config must be DeviceRuntimeBridgeConfig when present")
    return config
