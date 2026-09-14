# SocketCAN CAN bring-up and HIL evidence

Status: **procedure only; physical execution is `NOT_EXECUTED` until a named
operator records the required evidence**.

This document is the bring-up reference for `hardware/can_adapters`. The
production Linux boundary is one `AF_CAN`/`CAN_RAW` descriptor owned by the
controlled adapter. External dashboards and HTTP clients may read validated
`CanExternalRecord` projections, but they must never receive the descriptor or
write to CAN, debugfs, or a safety control path.

## 1. Virtual prerequisite and evidence

Run the virtual probe after a privileged `wbcan0` or `vcan0` interface is
available:

```bash
python3 kernel/wbcan/test_socketcan_ingress.py wbcan0 \
  --source virtual-wbcan \
  --report /tmp/wbcan-socketcan-ingress-report.json
python3 kernel/wbcan/test_socketcan_ingress.py \
  --validate-report /tmp/wbcan-socketcan-ingress-report.json
```

The report is scoped to `virtual-socketcan-ingress`. It records the Linux
kernel, kernel-config hash, interface, source, exact ACK/telemetry/duplicate/
invalid projections and cleanup state. A missing privilege, CAN netdevice or
kernel capability is `NOT_EXECUTED`; it is never converted into `PASS`.

Virtual `PASS` proves only the Linux software path:

```text
AF_CAN peer -> SocketCAN -> SocketCANTransport -> SafeCANBus
  -> Wire V1 validation -> bounded runtime -> CanExternalRecord
```

It does not prove a transceiver, cable, physical controller, MCU, motor,
emergency stop, PREEMPT_RT scheduling or a hard-real-time deadline.

## 2. Host and interface preflight

Record these fields before opening the physical bus:

| Field | Required value/evidence |
| --- | --- |
| board and Linux image | board serial, revision, distribution, `uname -a` |
| kernel configuration | `/proc/config.gz` or `/boot/config-$(uname -r)` SHA-256; `CONFIG_CAN`, `CONFIG_CAN_RAW`, `CONFIG_CAN_DEV` |
| interface | exact `can0` name, network namespace, `ip -details link show can0` |
| adapter | isolated USB-CAN-FD prototype model, serial, driver and firmware version |
| timing | nominal/data bitrate, sample point, restart policy and host clock source |
| harness | CANH/CANL polarity, isolation reference, two 120-ohm terminations and harness ID |
| calibration | analyser, oscilloscope/probe and current instrument IDs with valid calibration records |
| raw capture | immutable capture file path and SHA-256, not a screenshot-only summary |

The first physical path is the isolated USB-CAN-FD prototype adapter. The
repository does not select a carrier, bitrate, transceiver or vendor driver
on behalf of the Electrical/Hardware owners. If any required value is still
`TBD`, stop and record `NOT_EXECUTED`.

The interface must be placed in the intended network namespace and permissions
must be granted to the named service account or group. Do not broaden device
permissions globally. Verify that only the controlled adapter opens the CAN
receive fd; a dashboard or shell diagnostic may use a separate read-only
capture socket only under the approved test plan.

Example observation commands (they do not claim readiness):

```bash
uname -a
ip -details link show can0
ip netns identify "$(printf '%s' "$$")" || true
sha256sum /boot/config-$(uname -r)
ethtool -i can0
```

## 3. Filter and timestamp contract

Configure the adapter before enabling traffic:

- install only the approved standard Wire V1 arbitration-ID filters and a
  separately reviewed CAN error filter;
- keep standard, extended, RTR and error flag bits explicit;
- require Classic CAN DLC 8 for Wire V1 and reject CAN-FD payload frames,
  malformed ancillary data and contradictory raw IDs;
- enable `SO_TIMESTAMPNS` and preserve the kernel timestamp, host monotonic
  observation, host wall-clock observation, source, interface and ingress
  sequence;
- observe `SO_RXQ_OVFL` when present and retain the counter in the external
  record; a drop counter is evidence of loss, not evidence of successful
  delivery;
- keep command, telemetry, health and external projection capacities fixed and
  record their drop counters.

Only complete ACK, STOP_ACK and telemetry frames cross the Wire V1 boundary.
Malformed, duplicate, late, uncorrelated, error and post-shutdown frames are
rejections and cannot refresh a command or claim completion.

Wire V1 intentionally uses Classic CAN frames with DLC 8 while the selected
STM32G474/STM32G0B1 controllers retain FDCAN hardware for a later reviewed
transport upgrade. This does not make the legacy CH32V307 PCB a CAN-FD node.

## 4. Six-domain discovery and recovery

The BSP contract names six controller domains: `MCU-BASE`, `ARM-L-CTRL`,
`ARM-R-CTRL`, `TOOL-L-CTRL`, `TOOL-R-CTRL` and `MCU-SAFETY`. The existing Wire
V1 IDs do not contain a node address, so do not place independently responding
domains on a shared bus by silently modifying an ID or payload. Use the
owner-approved arbitration/segmentation decision for the physical fixture.

For each domain, capture:

1. fresh boot/session identity and heartbeat;
2. source/interface identity and the exact raw frames;
3. normal telemetry and ACK/action-result records;
4. duplicate, late, malformed, link-down and bus-off/restart behavior;
5. STOP and reset behavior with actuators disabled or replaced by a guarded
   load; and
6. the reviewer and safety owner disposition.

Missing `MCU-SAFETY` or any required domain is a fail-closed stop, not a
partial success. Linux restart, MCU restart and bus-off must clear stale
correlation state and require fresh discovery. Do not infer physical state
from a virtual `wbcan` result.

## 5. Machine-readable HIL record

Store one immutable JSON record beside the raw capture. At minimum it must
contain:

```json
{
  "result": "PASS | FAIL | NOT_EXECUTED",
  "board_revision": "...",
  "kernel_version": "...",
  "kernel_config_sha256": "...",
  "interface": "can0",
  "network_namespace": "...",
  "adapter": {"model": "isolated-usb-can-fd-prototype", "serial": "..."},
  "six_domains": ["MCU-BASE", "ARM-L-CTRL", "ARM-R-CTRL", "TOOL-L-CTRL", "TOOL-R-CTRL", "MCU-SAFETY"],
  "clock_source": "...",
  "nominal_bitrate": "...",
  "data_bitrate": "...",
  "calibration_refs": ["..."],
  "raw_capture": {"path": "...", "sha256": "..."},
  "operator": "...",
  "reviewer": "...",
  "captured_at_utc": "..."
}
```

`PASS` requires every field to be populated, all six domains to be observed,
the raw capture hash to recompute, all required fault/recovery checks to pass,
and owner review to be recorded. Missing board, adapter, capture,
calibration, privilege or physical inputs force `NOT_EXECUTED`. A failed
electrical, safety or protocol check remains `FAIL` and must not be erased by
a later rerun.

## 6. Cleanup checklist

At the end of every run:

- disarm any test-only fault controls and leave the interface in the documented
  safe state;
- stop the runtime, join its one worker and close the adapter fd;
- verify no stale external records remain after shutdown;
- remove temporary virtual interfaces and close peer/capture sockets;
- retain the raw capture, report, command transcript and cleanup result; and
- record any cleanup failure as `FAIL`.

The procedure is an evidence gate, not a physical result. Until a reproducible
record exists, the physical CAN, MCU, actuator, electrical and hard-real-time
claims remain `NOT_EXECUTED`.
