# ROS 2 DeviceRuntime bridge V1

Status: **implemented first slice** for Issue #230.  The bridge is a
bounded, read-only ROS 2 boundary around the existing host hardware runtime;
it is not a physical CAN, MCU, actuator, or hard-real-time validation.

## Scope and ownership

The first slice uses the already merged `SafeCANBus` and
`SocketCANTransport` implementation from Issues #55 and #229:

```text
SocketCANTransport (one synchronous AF_CAN descriptor)
        -> SafeCANBus (CAN Wire V1 validation and correlation)
        -> one DeviceRuntime (worker, cancellation and bounded planes)
        -> RuntimeBridgeCore (bounded read-only drain and allowlist)
        -> ROS 2 LifecycleNode / String topics
        -> selected RMW (Fast DDS in the deployment profile)
```

`SafeCANBus` exposes its existing `DeviceRuntime`, so the bridge reuses that
object and never wraps it in a second runtime.  A plain injected
`DeviceAdapter` without a runtime is supported for tests; in that case the
bridge creates exactly one `DeviceRuntime` with the configured capacities.
The adapter remains responsible for blocking device I/O, while the runtime
remains the sole owner of its worker, cancellation, lifecycle calls and data
planes.

The bridge has no command publisher, service, action server, socket handle,
WorldState writer or HTTP path.  It can only publish validated telemetry,
accepted ACK/action-result records and bounded health records.  E-stop,
watchdog, safe-enable and direct motion authority remain with the hardware,
MCU, Safety and Motion owners outside DDS.

## Lifecycle behavior

`create_lifecycle_node()` imports `rclpy` only when the factory is called;
importing `workbench.hardware` remains valid in a regular Python environment.
The node uses one `LifecycleNode`, three lifecycle publishers and one canceled
timer:

| Transition | Bridge action | Resource rule |
| --- | --- | --- |
| configure | construct adapter/runtime, create publishers and timer | no SocketCAN open and no worker |
| activate | call the existing runtime `start(background=True)` and reset timer | one runtime worker per adapter instance |
| deactivate | cancel timer, call bounded runtime shutdown, discard the instance | a timeout returns transition failure |
| cleanup | release an inactive runtime and destroy ROS entities | returns to unconfigured |
| shutdown | perform terminal bounded cleanup and destroy ROS entities | terminal success is idempotent |

Because the current `DeviceRuntime` deliberately has terminal cleanup
semantics, a later `inactive -> active` transition creates a fresh adapter and
runtime through the factory.  This prevents stale correlation windows,
queues, file descriptors and workers from being reused.  A failed start or
shutdown is retained as a failed transition; it is never silently reported as
success.

`ingress_sequence` is scoped to one adapter/runtime activation.  The fresh
activation intentionally resets the underlying adapter's source sequence; this
slice does not claim cross-restart sequence continuity or globally unique
evidence references.  Consumers must treat a lifecycle reconfiguration as a
new source epoch and must not infer continuity from a repeated sequence number.

The node does not construct an auto-sized executor.  Callers use
`create_bounded_executor(config)`, which creates a
`MultiThreadedExecutor` with the configured fixed thread count (default `2`,
maximum `8`).  The timer is in a mutually exclusive callback group, and its
callback only drains already-produced records; it never performs hardware
I/O or waits for an ACK.

## Bounded data and publication planes

The source `DeviceRuntime` already owns the bounded command/ACK, telemetry,
health and external projection planes.  The bridge adds no adapter-local
worker or unbounded queue.  Its timer handles at most
`max_records_per_tick` records (default `32`, maximum `1024`) and gives health
a small quota before routing the external projection.

Accepted external records are routed to independent ROS publication planes:

| Plane | Topic | QoS | Meaning |
| --- | --- | --- | --- |
| telemetry | `/workbench/device/telemetry` | best-effort, keep-last, depth 16, volatile | freshness-oriented device telemetry; source drops remain counted |
| ACK/action result | `/workbench/device/ack` | reliable, keep-last, depth 16, volatile, 100 ms deadline | accepted correlated ACK/STOP_ACK only; missing ACK is never success |
| health/provenance | `/workbench/device/health` | reliable, keep-last, depth 32, volatile | bounded runtime diagnostics and bridge lifecycle/projection failures |

The source adapter performs validation and correlation before an item enters
the external projection.  The bridge defensively rejects anything that is
not an immutable, exposed `CanExternalRecord`, and it accepts only known
inbound frame kinds (`ACK`, `STOP_ACK` and `TELEMETRY`).  Runtime health is
read from a snapshot with an identity cursor so a diagnostic is not published
again on every timer invocation.  Bridge-local failures use their own bounded
health record capacity and are never allowed to recursively consume the same
timer budget that created them.

The metrics snapshot reports source queue depth, oldest age where timestamps
are available, telemetry/health/external drop counters, bridge-health drops,
per-plane publication counts, unsupported records, serialization failures,
publisher failures, timer budget hits and worker liveness.  A publisher
exception consumes only the current record and becomes a bounded health
event; it cannot terminate the runtime worker.

## Projection and provenance policy

The temporary ROS carrier is `std_msgs/msg/String` containing deterministic
JSON.  It is deliberately not a replacement for a frozen public interface.
No file under `interfaces/` changes in this slice.

The external JSON has `schema_version` and `record_type` metadata plus the
explicit `EXTERNAL_PROJECTION_ALLOWLIST`.  It contains validated source,
interface, ingress sequence, clocks, frame identity, protocol fields,
health, exposure status and evidence references.  Payload bytes are rendered
as `data_hex`; sockets, file descriptors, callbacks, mutable device objects,
and arbitrary `device_state` fields cannot be serialized.  Health JSON has a
separate explicit allowlist for diagnostic code, observation time, detail,
command ID, source and bounded sequence.

Serialization uses sorted keys, compact separators and `allow_nan=False`.
Invalid, duplicate, late, uncorrelated, malformed and non-exposed records are
not published as external events.  They remain observable through the
source runtime's bounded diagnostics and receive results.

## SocketCAN and deployment settings

`create_socketcan_adapter_factory(interface, ...)` constructs a new
`SocketCANTransport` and `SafeCANBus` per activation.  Construction is side
effect free; the descriptor is opened only when the runtime activates.  The
factory maps bridge capacities and shutdown timing into the existing
`CanTransportConfig`, preserving one source of truth for bounds.

The deployment snapshot records:

- ROS domain ID: `42` by default; deployment sets `ROS_DOMAIN_ID`;
- RMW selection: `rmw_fastrtps_cpp` by default;
- topic names and each reliability/history/depth/deadline;
- fixed executor thread count and its maximum;
- command, telemetry, health and external capacities; and
- a deployment-managed security profile.

Fast DDS is selected by deployment (`RMW_IMPLEMENTATION` and the existing
container profile), not by importing vendor APIs or changing the Python
contract.  The bridge does not mutate either environment variable and does
not claim that a security profile, DDS discovery session or network isolation
has been physically verified.  A supported alternate RMW can be used for
tests as long as the same bounded ROS boundary is preserved.

## Verification boundary

The focused unit tests cover configuration bounds, delayed ROS imports,
allowlist serialization, queue saturation, health de-duplication, publisher
failure isolation, SafeCANBus runtime reuse, lifecycle failure, fresh
activation and terminal cleanup.  The optional ROS test exercises the Jazzy
LifecycleNode transition path and timer drain when `/opt/ros/jazzy` is
available.

The focused tests include a local ROS topic publish/receive loopback and a
virtual SocketCAN construction path.  They establish software ordering,
bounded lifecycle behavior, source metadata and cleanup for those paths.  They
do not establish:

- physical CAN arbitration or USB-CAN electrical integrity;
- MCU acceptance, watchdog or safe-enable behavior;
- actuator or drive motion;
- camera/touch, arm or safety-plugin integration;
- PREEMPT_RT scheduling or hard-real-time deadlines; or
- CPU/RSS/load results for a production deployment.

Those items remain `NOT_EXECUTED` or owner-gated until their respective raw
captures, kernel/DDS configuration, calibration references and independent
owner reviews are recorded.  Camera/touch and drive/arm adapters remain
separate follow-up slices; this bridge does not merge their hardware code.
