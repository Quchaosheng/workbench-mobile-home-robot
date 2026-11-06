import threading
from collections import deque
from dataclasses import FrozenInstanceError

import pytest
from workbench.hardware import (
    CAN_ERR_BUSOFF,
    CAN_ERR_FLAG,
    MCU_CAN_ID_ACK,
    MCU_CAN_ID_COMMAND,
    MCU_CAN_ID_STOP,
    MCU_CAN_ID_STOP_ACK,
    MCU_CAN_ID_TELEMETRY,
    CanBusOffError,
    CanDiagnosticCode,
    CanExternalRecord,
    CanFrame,
    CanFrameKind,
    CanFrameValidationError,
    CanLinkLostError,
    CanLinkState,
    CanReceiveStatus,
    CanSendStatus,
    CanTransportBackpressureError,
    CanTransportConfig,
    CanTransportEnvelope,
    CanTransportFrameError,
    DeviceRuntime,
    DeviceRuntimeState,
    SafeCANBus,
    decode_can_frame,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTransport:
    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.sent: list[CanFrame] = []
        self.incoming: deque[CanFrame] = deque()
        self.send_error: Exception | None = None
        self.receive_error: Exception | None = None
        self.recover_result = True

    def open(self) -> None:
        self.opened = True

    def send(self, frame: CanFrame) -> None:
        if self.send_error is not None:
            error = self.send_error
            self.send_error = None
            raise error
        self.sent.append(frame)

    def receive(self, timeout_s: float) -> CanFrame | None:
        del timeout_s
        if self.receive_error is not None:
            error = self.receive_error
            self.receive_error = None
            raise error
        return self.incoming.popleft() if self.incoming else None

    def recover(self) -> bool:
        return self.recover_result

    def close(self) -> None:
        self.closed = True


class RecordingAdapter:
    def __init__(self) -> None:
        self.events: list[str] = []

    def configure(self) -> bool:
        self.events.append("configure")
        return True

    def activate(self) -> bool:
        self.events.append("activate")
        return True

    def poll(self, receive_timeout_s: float) -> None:
        del receive_timeout_s
        self.events.append("poll")

    def deactivate(self) -> bool:
        self.events.append("deactivate")
        return True

    def cleanup(self) -> bool:
        self.events.append("cleanup")
        return True


def can_frame(arbitration_id: int, payload: list[int], **flags: bool) -> CanFrame:
    return CanFrame(arbitration_id, bytes(payload), **flags)


def command(command_id: int = 1, opcode: int = 1, retry_count: int = 0) -> CanFrame:
    return can_frame(MCU_CAN_ID_COMMAND, [0x10, command_id >> 8, command_id & 0xFF, opcode, retry_count, 0, 0, 0])


def stop(command_id: int = 0x8001, retry_count: int = 0) -> CanFrame:
    return can_frame(MCU_CAN_ID_STOP, [0x10, command_id >> 8, command_id & 0xFF, 5, retry_count, 0, 0, 0])


def ack(command_id: int = 1, opcode: int = 1, retry_count: int = 0) -> CanFrame:
    return can_frame(MCU_CAN_ID_ACK, [0x10, command_id >> 8, command_id & 0xFF, opcode, retry_count, 0, 0, 0])


def rejected_ack(command_id: int = 1, opcode: int = 1, retry_count: int = 0) -> CanFrame:
    return can_frame(MCU_CAN_ID_ACK, [0x10, command_id >> 8, command_id & 0xFF, opcode, retry_count, 1, 5, 4])


def stop_ack(command_id: int = 0x8001, retry_count: int = 0, accepted: bool = True) -> CanFrame:
    return can_frame(
        MCU_CAN_ID_STOP_ACK,
        [
            0x10,
            command_id >> 8,
            command_id & 0xFF,
            5,
            retry_count,
            0 if accepted else 1,
            0 if accepted else 3,
            3 if accepted else 4,
        ],
    )


def telemetry(sequence_no: int = 1, *, fault_code: int = 0, device_mode: int = 0) -> CanFrame:
    return can_frame(
        MCU_CAN_ID_TELEMETRY,
        [0x10, *sequence_no.to_bytes(4, "big"), fault_code, device_mode, 0],
    )


def running_bus(
    transport: FakeTransport,
    clock: FakeClock,
    config: CanTransportConfig | None = None,
) -> SafeCANBus:
    bus = SafeCANBus(transport, clock=clock, config=config)
    assert bus.start(background=False)
    return bus


def test_decode_rejects_non_wire_frames_and_bad_cross_fields() -> None:
    valid = command()
    assert decode_can_frame(valid).kind is CanFrameKind.COMMAND
    cases = (
        CanFrame(MCU_CAN_ID_COMMAND, bytearray(valid.data)),
        CanFrame(MCU_CAN_ID_COMMAND, valid.data[:-1]),
        CanFrame(0x123, valid.data),
        CanFrame(MCU_CAN_ID_COMMAND, valid.data, is_extended_id=True),
        CanFrame(MCU_CAN_ID_COMMAND, bytes([0x11, *valid.data[1:]])),
        CanFrame(MCU_CAN_ID_COMMAND, bytes([0x10, 0, 1, 1, 0, 1, 0, 0])),
        stop(1),
        ack(0x8001),
        can_frame(MCU_CAN_ID_ACK, [0x10, 0, 1, 1, 0, 0, 0, 4]),
    )
    for frame in cases:
        with pytest.raises(ValueError):
            decode_can_frame(frame)


def test_send_requires_lifecycle_and_queue_backpressure_is_typed() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = SafeCANBus(transport, clock=clock, config=CanTransportConfig(queue_capacity=1))

    assert bus.send(command()).status is CanSendStatus.NOT_RUNNING
    assert bus.start(background=False)
    assert bus.send(command(1)).status is CanSendStatus.QUEUED
    assert bus.send(command(2)).status is CanSendStatus.BACKPRESSURE
    assert bus.queued_count == 1
    assert bus.send(telemetry()).status is CanSendStatus.INVALID_FRAME


def test_can_adapter_uses_one_runtime_for_lifecycle_worker_and_command_plane() -> None:
    transport = FakeTransport()
    bus = SafeCANBus(transport, clock=FakeClock())

    assert bus.runtime.state is DeviceRuntimeState.NEW
    assert not hasattr(bus, "_worker")
    assert bus.start(background=True)
    assert bus.runtime.state is DeviceRuntimeState.ACTIVE
    assert bus.runtime.worker_alive
    assert bus.runtime.command_depth == bus.queued_count

    assert bus.shutdown(timeout_s=1.0)
    assert bus.runtime.state is DeviceRuntimeState.CLEANED
    assert not bus.runtime.worker_alive
    assert bus.shutdown(timeout_s=0.0)


def test_device_runtime_owns_the_adapter_lifecycle_sequence() -> None:
    adapter = RecordingAdapter()
    runtime = DeviceRuntime(
        adapter,
        command_capacity=1,
        telemetry_capacity=1,
        health_capacity=1,
        max_subscribers_per_id=1,
        poll_interval_s=0.001,
    )

    assert runtime.start(background=False)
    assert runtime.state is DeviceRuntimeState.ACTIVE
    assert runtime.shutdown(timeout_s=1.0)
    assert runtime.state is DeviceRuntimeState.CLEANED
    assert adapter.events == ["configure", "activate", "deactivate", "cleanup"]


def test_worker_drains_one_command_and_correlates_ack_once() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)

    assert bus.send(command(7)).accepted
    assert bus.service_once() is None
    assert transport.sent == [command(7)]

    transport.incoming.append(ack(7))
    result = bus.service_once()
    assert result is not None
    assert result.status is CanReceiveStatus.ACCEPTED
    assert result.confirmed
    assert bus.pending_command_id is None

    transport.incoming.append(ack(7))
    duplicate = bus.service_once()
    assert duplicate is not None
    assert duplicate.status is CanReceiveStatus.DUPLICATE
    assert duplicate.external_record is not None
    assert not duplicate.external_record.exposure_allowed
    assert len(bus.external_records()) == 1


def test_accepted_ingress_exposes_a_bounded_immutable_projection() -> None:
    transport = FakeTransport()
    bus = running_bus(transport, FakeClock(), CanTransportConfig(external_capacity=2))
    frame = telemetry(27)
    frame = CanFrame(
        frame.arbitration_id,
        frame.data,
        dlc=8,
        kernel_timestamp_ns=1_700_000_000_123_000_000,
        kernel_drop_count=4,
        observed_monotonic_ts=12.5,
        observed_wall_ts=1_700_000_000.5,
        raw_can_id=frame.arbitration_id,
    )
    transport.incoming.append(frame)

    result = bus.service_once()

    assert result is not None
    assert isinstance(result.external_record, CanExternalRecord)
    record = result.external_record
    assert record.source == "mcu-can"
    assert record.interface == "injected-can"
    assert record.ingress_sequence == 0
    assert record.event_type == "telemetry"
    assert record.frame_kind is CanFrameKind.TELEMETRY
    assert record.dlc == 8
    assert record.kernel_timestamp_ns == 1_700_000_000_123_000_000
    assert record.kernel_drop_count == 4
    assert record.timestamp_source == "kernel+host"
    assert record.exposure_allowed
    assert record.sequence_no == 27
    assert record.data_hex == frame.data.hex()
    assert record.to_dict()["evidence_refs"] == ["can-ingress://mcu-can/injected-can/0"]
    assert bus.external_records() == (record,)
    with pytest.raises(FrozenInstanceError):
        record.ingress_sequence = 4  # type: ignore[misc]


def test_external_projection_drops_oldest_record_and_records_backpressure() -> None:
    transport = FakeTransport()
    bus = running_bus(transport, FakeClock(), CanTransportConfig(external_capacity=1))
    transport.incoming.extend((telemetry(1), telemetry(2)))

    assert bus.service_once() is not None
    assert bus.service_once() is not None

    records = bus.external_records()
    assert len(records) == 1
    assert records[0].sequence_no == 2
    assert bus.external_drop_count == 1
    assert any(item.code is CanDiagnosticCode.EXTERNAL_BACKPRESSURE for item in bus.diagnostics())


def test_malformed_raw_id_is_rejected_without_corrupting_the_external_projection() -> None:
    transport = FakeTransport()
    bus = running_bus(transport, FakeClock())
    valid = telemetry(28)
    transport.incoming.append(CanFrame(valid.arbitration_id, valid.data, raw_can_id=0x123))

    result = bus.service_once()

    assert result is not None
    assert result.status is CanReceiveStatus.INVALID_FRAME
    assert result.external_record is not None
    assert not result.external_record.exposure_allowed
    assert not result.external_record.frame_valid
    assert result.external_record.raw_can_id is None
    assert result.external_record.arbitration_id == MCU_CAN_ID_TELEMETRY


def test_transport_frame_error_is_an_observable_bounded_rejection() -> None:
    transport = FakeTransport()
    bus = running_bus(transport, FakeClock())
    transport.receive_error = CanTransportFrameError("truncated SocketCAN record")

    result = bus.service_once()

    assert result is not None
    assert result.status is CanReceiveStatus.INVALID_FRAME
    assert result.external_record is not None
    assert result.external_record.reason == "truncated SocketCAN record"
    assert not result.external_record.exposure_allowed


def test_transport_backpressure_retries_within_the_fixed_budget() -> None:
    class TwiceBackpressuredTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.send_attempts = 0

        def send(self, frame: CanFrame) -> None:
            self.send_attempts += 1
            if self.send_attempts <= 2:
                raise CanTransportBackpressureError("kernel transmit queue is full")
            super().send(frame)

    transport = TwiceBackpressuredTransport()
    bus = running_bus(
        transport,
        FakeClock(),
        CanTransportConfig(transport_backpressure_budget=2),
    )
    assert bus.send(command(30)).accepted

    assert bus.service_once() is None
    assert bus.service_once() is None
    assert bus.state is CanLinkState.ACTIVE
    assert bus.queued_count == 1

    assert bus.service_once() is None
    assert transport.sent == [command(30)]
    assert bus.pending_command_id == 30
    assert [item.code for item in bus.diagnostics()].count(CanDiagnosticCode.TRANSPORT_BACKPRESSURE) == 2


def test_transport_backpressure_exhaustion_fails_closed_without_replay() -> None:
    class AlwaysBackpressuredTransport(FakeTransport):
        def send(self, frame: CanFrame) -> None:
            del frame
            raise CanTransportBackpressureError("kernel transmit queue is full")

    transport = AlwaysBackpressuredTransport()
    bus = running_bus(
        transport,
        FakeClock(),
        CanTransportConfig(transport_backpressure_budget=1),
    )
    assert bus.send(command(31)).accepted

    assert bus.service_once() is None
    assert bus.state is CanLinkState.ACTIVE
    assert bus.queued_count == 1

    assert bus.service_once() is None
    assert bus.state is CanLinkState.LINK_LOST
    assert bus.queued_count == 0
    assert bus.pending_command_id is None
    assert bus.send(command(32)).status is CanSendStatus.LINK_UNAVAILABLE
    codes = [item.code for item in bus.diagnostics()]
    assert codes.count(CanDiagnosticCode.TRANSPORT_BACKPRESSURE) == 2
    assert codes.count(CanDiagnosticCode.LINK_LOST) == 1


def test_bus_off_error_frame_is_observable_but_never_externally_exposed() -> None:
    transport = FakeTransport()
    bus = running_bus(transport, FakeClock())
    transport.incoming.append(
        CanFrame(
            CAN_ERR_BUSOFF,
            b"\0" * 8,
            is_error_frame=True,
            dlc=8,
            raw_can_id=CAN_ERR_FLAG | CAN_ERR_BUSOFF,
        )
    )

    result = bus.service_once()

    assert result is not None
    assert result.status is CanReceiveStatus.INVALID_FRAME
    assert result.external_record is not None
    assert result.external_record.health is CanLinkState.BUS_OFF
    assert result.external_record.is_error_frame is True
    assert not result.external_record.frame_valid
    assert not result.external_record.exposure_allowed
    assert bus.state is CanLinkState.BUS_OFF
    assert bus.external_records() == ()
    assert any(item.code is CanDiagnosticCode.BUS_OFF for item in bus.diagnostics())


def test_clock_failure_emits_one_rejection_without_recursive_record_generation() -> None:
    class BrokenClock:
        def __init__(self) -> None:
            self.calls = 0

        def monotonic(self) -> float:
            self.calls += 1
            if self.calls == 1:
                return 0.0
            raise RuntimeError("clock unavailable")

    transport = FakeTransport()
    bus = running_bus(transport, BrokenClock())
    transport.incoming.append(telemetry(29))

    result = bus.service_once()

    assert result is not None
    assert result.status is CanReceiveStatus.INVALID_FRAME
    assert result.external_record is not None
    assert result.external_record.reason == "monotonic clock failed: clock unavailable"
    assert bus.external_records() == ()
    assert bus.take_external_record() is None
    codes = [item.code for item in bus.diagnostics()]
    assert codes.count(CanDiagnosticCode.CLOCK_ROLLBACK) == 1
    assert bus.state is CanLinkState.LINK_LOST


def test_malformed_inbound_frame_is_rejected_without_clearing_pending_command() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)

    assert bus.send(command(8)).accepted
    assert bus.service_once() is None
    transport.incoming.append(can_frame(MCU_CAN_ID_ACK, [0x10, 0, 8, 1, 0, 0, 0, 4]))
    malformed = bus.service_once()
    assert malformed is not None
    assert malformed.status is CanReceiveStatus.INVALID_FRAME
    assert not malformed.confirmed
    assert bus.pending_command_id == 8


def test_retry_keeps_correlation_and_timeout_escalates_to_stop() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    config = CanTransportConfig(ack_timeout_s=1.0, ack_retry_budget=1, stop_timeout_s=1.0)
    bus = running_bus(transport, clock, config)

    assert bus.send(command(3)).accepted
    assert bus.service_once() is None
    clock.advance(1.0)
    assert bus.service_once() is None
    assert transport.sent[-1] == command(3, retry_count=1)

    clock.advance(1.0)
    assert bus.service_once() is None
    assert transport.sent[-1].arbitration_id == MCU_CAN_ID_STOP
    assert bus.state is CanLinkState.STOPPING
    generated_stop = transport.sent[-1]
    assert bus.pending_command_id == int.from_bytes(generated_stop.data[1:3], "big")

    transport.incoming.append(stop_ack(int.from_bytes(generated_stop.data[1:3], "big")))
    result = bus.service_once()
    assert result is not None and result.confirmed
    assert bus.state is CanLinkState.SAFE_STOPPED


def test_explicit_stop_preempts_queued_ordinary_commands() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)

    assert bus.send(command(1)).accepted
    assert bus.send(command(2)).accepted
    assert bus.send(stop(0x8002)).accepted
    assert bus.queued_count == 1
    assert bus.service_once() is None
    assert [frame.arbitration_id for frame in transport.sent] == [MCU_CAN_ID_STOP]
    assert any(item.code.value == "stop_preempted" for item in bus.diagnostics())


def test_stop_rejection_and_timeout_fail_closed() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    config = CanTransportConfig(stop_timeout_s=1.0, stop_retry_budget=0)
    bus = running_bus(transport, clock, config)

    assert bus.send(stop(0x8003)).accepted
    assert bus.service_once() is None
    transport.incoming.append(stop_ack(0x8003, accepted=False))
    rejected = bus.service_once()
    assert rejected is not None and not rejected.confirmed
    assert bus.state is CanLinkState.LINK_LOST

    second_transport = FakeTransport()
    second_clock = FakeClock()
    second_bus = running_bus(second_transport, second_clock, config)
    assert second_bus.send(stop(0x8004)).accepted
    assert second_bus.service_once() is None
    second_clock.advance(1.0)
    assert second_bus.service_once() is None
    assert second_bus.state is CanLinkState.LINK_LOST
    assert second_bus.send(command(4)).status is CanSendStatus.LINK_UNAVAILABLE


def test_stop_retry_retains_stop_correlation() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    config = CanTransportConfig(stop_timeout_s=1.0, stop_retry_budget=1)
    bus = running_bus(transport, clock, config)

    assert bus.send(stop(0x8005)).accepted
    assert bus.service_once() is None
    clock.advance(1.0)
    assert bus.service_once() is None
    assert transport.sent[-1] == stop(0x8005, retry_count=1)

    transport.incoming.append(stop_ack(0x8005, retry_count=1))
    result = bus.service_once()
    assert result is not None and result.confirmed
    assert bus.state is CanLinkState.SAFE_STOPPED


def test_second_stop_is_rejected_while_first_stop_is_in_flight() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)

    assert bus.send(stop(0x8006)).accepted
    assert bus.send(stop(0x8007)).status is CanSendStatus.CORRELATION_CONFLICT
    assert bus.service_once() is None
    assert [frame.arbitration_id for frame in transport.sent] == [MCU_CAN_ID_STOP]


def test_late_ack_does_not_confirm_after_command_timeout() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    config = CanTransportConfig(ack_timeout_s=1.0, ack_retry_budget=0, stop_timeout_s=1.0)
    bus = running_bus(transport, clock, config)

    assert bus.send(command(9)).accepted
    assert bus.service_once() is None
    clock.advance(1.0)
    assert bus.service_once() is None
    generated_stop = transport.sent[-1]
    transport.incoming.append(ack(9))
    late = bus.service_once()
    assert late is not None and late.status is CanReceiveStatus.LATE
    assert not late.confirmed
    assert bus.pending_command_id == int.from_bytes(generated_stop.data[1:3], "big")
    assert late.external_record is not None
    assert not late.external_record.exposure_allowed
    assert bus.external_records() == ()


def test_uncorrelated_ack_is_observable_but_not_externally_exposed() -> None:
    transport = FakeTransport()
    bus = running_bus(transport, FakeClock())
    transport.incoming.append(ack(0x1234))

    result = bus.service_once()

    assert result is not None and result.status is CanReceiveStatus.UNCORRELATED
    assert not result.confirmed
    assert result.external_record is not None
    assert not result.external_record.exposure_allowed
    assert bus.external_records() == ()


def test_rejects_a_foreign_classic_can_encoding() -> None:
    # ``embodied-agent-runtime`` speaks a different classic-CAN layout on the same
    # arbitration IDs (version byte 0x01, argument in bytes 4..7, STOP as opcode 0xFF
    # on 0x100). The codecs refuse each other; this pins the refusal so the
    # incompatibility stays deliberate and documented rather than becoming a bug.
    from workbench.hardware.can_driver_safe import decode_can_frame

    foreign_command = CanFrame(MCU_CAN_ID_COMMAND, bytes([0x01, 0x00, 0x01, 0x01, 0x00, 0x00, 0x01, 0xF4]))
    foreign_stop = CanFrame(MCU_CAN_ID_COMMAND, bytes([0x01, 0x80, 0x00, 0xFF, 0x00, 0x00, 0x00, 0x01]))

    with pytest.raises(CanFrameValidationError, match="unsupported CAN Wire version"):
        decode_can_frame(foreign_command)
    with pytest.raises(CanFrameValidationError, match="unsupported CAN Wire version"):
        decode_can_frame(foreign_stop)


def test_bus_off_clears_pending_work_and_recovery_does_not_replay_it() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)
    assert bus.send(command(11)).accepted
    transport.send_error = CanBusOffError("bus-off")

    assert bus.service_once() is None
    assert bus.state is CanLinkState.BUS_OFF
    assert bus.pending_command_id is None
    assert bus.recover()
    assert bus.state is CanLinkState.ACTIVE
    assert bus.queued_count == 0
    assert bus.send(command(12)).accepted


def test_receive_link_loss_is_fail_closed_until_recovery() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)
    transport.receive_error = CanLinkLostError("link lost")

    assert bus.service_once() is None
    assert bus.state is CanLinkState.LINK_LOST
    assert bus.send(command(13)).status is CanSendStatus.LINK_UNAVAILABLE
    assert bus.recover()
    assert bus.send(command(14)).accepted


def test_subscriber_snapshot_is_mutable_and_callback_failures_are_isolated() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)
    calls: list[str] = []

    def second(_frame: object) -> None:
        calls.append("second")

    def first(_frame: object) -> None:
        calls.append("first")
        bus.unsubscribe(MCU_CAN_ID_ACK, first)
        bus.subscribe(MCU_CAN_ID_ACK, second)
        raise RuntimeError("subscriber failure")

    def third(_frame: object) -> None:
        calls.append("third")

    assert bus.subscribe(MCU_CAN_ID_ACK, first)
    assert bus.subscribe(MCU_CAN_ID_ACK, third)
    assert bus.send(command(15)).accepted
    assert bus.service_once() is None
    transport.incoming.append(ack(15))
    result = bus.service_once()
    assert result is not None
    assert result.callback_errors == 1
    assert calls == ["first", "third"]

    assert bus.send(command(16)).accepted
    assert bus.service_once() is None
    transport.incoming.append(ack(16))
    bus.service_once()
    assert calls[-3:] == ["third", "third", "second"]


def test_telemetry_is_delivered_without_confirming_command() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)
    received: list[CanFrameKind] = []
    bus.subscribe(MCU_CAN_ID_TELEMETRY, lambda frame: received.append(frame.kind))

    transport.incoming.append(telemetry(17))
    result = bus.service_once()
    assert result is not None
    assert result.status is CanReceiveStatus.ACCEPTED
    assert not result.confirmed
    assert isinstance(result.envelope, CanTransportEnvelope)
    assert result.envelope.source == "mcu-can"
    assert result.envelope.interface == "injected-can"
    assert result.envelope.sequence == 0
    assert received == [CanFrameKind.TELEMETRY]


def test_telemetry_ordering_handles_wrap_and_rejects_duplicate_or_stale_frames() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)

    for sequence_no, status in (
        (0xFFFFFFFF, CanReceiveStatus.ACCEPTED),
        (0, CanReceiveStatus.ACCEPTED),
        (0, CanReceiveStatus.DUPLICATE),
        (0xFFFFFFFF, CanReceiveStatus.LATE),
    ):
        transport.incoming.append(telemetry(sequence_no))
        result = bus.service_once()
        assert result is not None and result.status is status


def test_fault_telemetry_fails_closed_until_explicit_recovery() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)
    assert bus.send(command(19)).accepted
    assert bus.service_once() is None

    transport.incoming.append(telemetry(1, fault_code=4, device_mode=4))
    result = bus.service_once()
    assert result is not None and result.status is CanReceiveStatus.ACCEPTED
    assert not result.confirmed
    assert bus.state is CanLinkState.LINK_LOST
    assert bus.pending_command_id is None
    assert bus.send(command(20)).status is CanSendStatus.LINK_UNAVAILABLE

    assert bus.recover()
    transport.incoming.append(telemetry(0))
    recovered = bus.service_once()
    assert recovered is not None and recovered.status is CanReceiveStatus.ACCEPTED
    assert recovered.envelope is not None
    assert recovered.envelope.sequence == 1


def test_health_plane_capacity_is_bounded_independently_from_command_plane() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    config = CanTransportConfig(queue_capacity=2, diagnostic_capacity=1, health_capacity=1)
    bus = running_bus(transport, clock, config)

    assert bus.send(telemetry()).status is CanSendStatus.INVALID_FRAME
    assert bus.send(telemetry(2)).status is CanSendStatus.INVALID_FRAME
    assert len(bus.diagnostics()) == 1
    assert bus.runtime.health_drop_count == 1
    assert bus.runtime.command_depth == 0


def test_runtime_telemetry_plane_drops_oldest_when_not_drained() -> None:
    transport = FakeTransport()
    bus = SafeCANBus(
        transport,
        clock=FakeClock(),
        config=CanTransportConfig(telemetry_capacity=1),
    )
    first = decode_can_frame(telemetry(1))
    second = decode_can_frame(telemetry(2))

    bus.runtime.publish_telemetry(first)
    bus.runtime.publish_telemetry(second)

    assert bus.runtime.telemetry_drop_count == 1
    assert bus.runtime.take_telemetry() == second


def test_wall_clock_rollback_is_an_observable_fail_closed_ingress_error() -> None:
    transport = FakeTransport()
    wall_times = iter((10.0, 9.0))
    bus = SafeCANBus(transport, clock=FakeClock(), wall_clock=lambda: next(wall_times))
    assert bus.start(background=False)

    transport.incoming.append(telemetry(1))
    first = bus.service_once()
    assert first is not None and first.status is CanReceiveStatus.ACCEPTED

    transport.incoming.append(telemetry(2))
    second = bus.service_once()
    assert second is not None and second.status is CanReceiveStatus.INVALID_FRAME
    assert bus.state is CanLinkState.LINK_LOST
    assert any(item.code.value == "clock_rollback" for item in bus.diagnostics())
    assert bus.send(command(24)).status is CanSendStatus.LINK_UNAVAILABLE


def test_monotonic_rollback_is_rejected_without_external_exposure() -> None:
    transport = FakeTransport()
    bus = running_bus(transport, FakeClock())
    first_frame = telemetry(1)
    second_frame = telemetry(2)
    transport.incoming.extend(
        (
            CanFrame(
                first_frame.arbitration_id,
                first_frame.data,
                observed_monotonic_ts=10.0,
                observed_wall_ts=20.0,
            ),
            CanFrame(
                second_frame.arbitration_id,
                second_frame.data,
                observed_monotonic_ts=9.0,
                observed_wall_ts=21.0,
            ),
        )
    )

    first = bus.service_once()
    second = bus.service_once()

    assert first is not None and first.status is CanReceiveStatus.ACCEPTED
    assert second is not None and second.status is CanReceiveStatus.INVALID_FRAME
    assert second.external_record is not None
    assert second.external_record.reason == "monotonic observation moved backwards"
    assert not second.external_record.exposure_allowed
    assert tuple(record.sequence_no for record in bus.external_records()) == (1,)
    assert bus.state is CanLinkState.LINK_LOST
    assert any(item.code is CanDiagnosticCode.CLOCK_ROLLBACK for item in bus.diagnostics())


def test_rejected_ordinary_ack_is_not_confirmation_and_fails_closed() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)
    assert bus.send(command(21)).accepted
    assert bus.service_once() is None

    transport.incoming.append(rejected_ack(21))
    result = bus.service_once()
    assert result is not None and result.status is CanReceiveStatus.ACCEPTED
    assert not result.confirmed
    assert bus.state is CanLinkState.LINK_LOST
    assert bus.send(command(22)).status is CanSendStatus.LINK_UNAVAILABLE


def test_stop_preempts_a_command_blocked_inside_transport_send() -> None:
    class BlockingSendTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.send_entered = threading.Event()
            self.release_send = threading.Event()

        def send(self, frame: CanFrame) -> None:
            if frame.arbitration_id == MCU_CAN_ID_COMMAND:
                self.send_entered.set()
                assert self.release_send.wait(1.0)
            super().send(frame)

    transport = BlockingSendTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)
    assert bus.send(command(23)).accepted
    service_thread = threading.Thread(target=bus.service_once)
    service_thread.start()
    assert transport.send_entered.wait(1.0)

    assert bus.send(stop(0x8017)).accepted
    transport.release_send.set()
    service_thread.join(1.0)
    assert not service_thread.is_alive()
    assert bus.queued_count == 1

    assert bus.service_once() is None
    assert bus.pending_command_id == 0x8017
    assert [frame.arbitration_id for frame in transport.sent] == [MCU_CAN_ID_COMMAND, MCU_CAN_ID_STOP]


def test_shutdown_during_lifecycle_operation_stays_false_until_cleanup_finishes() -> None:
    class BlockingConfigureAdapter(RecordingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.configure_entered = threading.Event()
            self.release_configure = threading.Event()
            self.cleanup_entered = threading.Event()
            self.release_cleanup = threading.Event()

        def configure(self) -> bool:
            self.events.append("configure")
            self.configure_entered.set()
            assert self.release_configure.wait(1.0)
            return True

        def cleanup(self) -> bool:
            self.events.append("cleanup")
            self.cleanup_entered.set()
            assert self.release_cleanup.wait(1.0)
            return True

    adapter = BlockingConfigureAdapter()
    runtime = DeviceRuntime(
        adapter,
        command_capacity=1,
        telemetry_capacity=1,
        health_capacity=1,
        max_subscribers_per_id=1,
        poll_interval_s=0.001,
    )
    start_result: list[bool] = []
    start_thread = threading.Thread(target=lambda: start_result.append(runtime.start(background=False)))
    start_thread.start()
    assert adapter.configure_entered.wait(1.0)

    assert not runtime.shutdown(timeout_s=0.0)
    assert runtime.state is DeviceRuntimeState.DEACTIVATING
    assert not adapter.cleanup_entered.is_set()

    adapter.release_configure.set()
    assert adapter.cleanup_entered.wait(1.0)
    assert not runtime.shutdown(timeout_s=0.0)
    assert runtime.state is DeviceRuntimeState.DEACTIVATING

    adapter.release_cleanup.set()
    start_thread.join(1.0)
    assert not start_thread.is_alive()
    assert start_result == [False]
    assert adapter.events == ["configure", "deactivate", "cleanup"]
    assert runtime.state is DeviceRuntimeState.CLEANED
    assert runtime.shutdown(timeout_s=0.0)


def test_start_cannot_reactivate_after_concurrent_shutdown() -> None:
    class BlockingOpenTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.open_entered = threading.Event()
            self.release_open = threading.Event()

        def open(self) -> None:
            self.open_entered.set()
            assert self.release_open.wait(1.0)
            super().open()

    transport = BlockingOpenTransport()
    bus = SafeCANBus(transport, clock=FakeClock())
    start_result: list[bool] = []
    start_thread = threading.Thread(target=lambda: start_result.append(bus.start(background=False)))
    start_thread.start()
    assert transport.open_entered.wait(1.0)

    assert not bus.shutdown(timeout_s=0.0)
    assert not transport.closed
    assert bus.runtime.state is DeviceRuntimeState.DEACTIVATING
    transport.release_open.set()
    start_thread.join(1.0)
    assert start_result == [False]
    assert transport.closed
    assert bus.state is CanLinkState.SHUTDOWN
    assert bus.shutdown(timeout_s=0.0)


def test_shutdown_retries_join_after_an_initial_timeout() -> None:
    class BlockingReceiveTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.receive_entered = threading.Event()
            self.release_receive = threading.Event()

        def receive(self, timeout_s: float) -> CanFrame | None:
            del timeout_s
            self.receive_entered.set()
            assert self.release_receive.wait(1.0)
            return None

    transport = BlockingReceiveTransport()
    bus = SafeCANBus(transport, clock=FakeClock())
    assert bus.start(background=True)
    assert transport.receive_entered.wait(1.0)

    assert not bus.shutdown(timeout_s=0.0)
    assert not bus.shutdown(timeout_s=0.0)
    transport.release_receive.set()
    assert bus.shutdown(timeout_s=1.0)
    assert transport.closed


def test_shutdown_does_not_publish_a_frame_returned_by_an_inflight_receive() -> None:
    class BlockingFrameReceiveTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.receive_entered = threading.Event()
            self.release_receive = threading.Event()

        def receive(self, timeout_s: float) -> CanFrame | None:
            del timeout_s
            self.receive_entered.set()
            assert self.release_receive.wait(1.0)
            return telemetry(91)

    transport = BlockingFrameReceiveTransport()
    bus = SafeCANBus(transport, clock=FakeClock())
    assert bus.start(background=True)
    assert transport.receive_entered.wait(1.0)

    assert not bus.shutdown(timeout_s=0.0)
    transport.release_receive.set()
    assert bus.shutdown(timeout_s=1.0)
    assert bus.external_records() == ()
    assert bus.external_depth == 0


def test_recovery_cannot_reactivate_after_concurrent_shutdown() -> None:
    class BlockingRecoverTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.recover_entered = threading.Event()
            self.release_recover = threading.Event()

        def recover(self) -> bool:
            self.recover_entered.set()
            assert self.release_recover.wait(1.0)
            return True

    transport = BlockingRecoverTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock)
    transport.receive_error = CanLinkLostError("link lost")
    assert bus.service_once() is None

    recovery_result: list[bool] = []
    recovery_thread = threading.Thread(target=lambda: recovery_result.append(bus.recover()))
    recovery_thread.start()
    assert transport.recover_entered.wait(1.0)
    assert not bus.shutdown(timeout_s=0.0)
    assert not transport.closed
    assert bus.runtime.state is DeviceRuntimeState.DEACTIVATING
    transport.release_recover.set()
    recovery_thread.join(1.0)

    assert recovery_result == [False]
    assert transport.closed
    assert bus.state is CanLinkState.SHUTDOWN
    assert bus.shutdown(timeout_s=0.0)


def test_shutdown_stops_worker_closes_transport_and_rejects_future_send() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = SafeCANBus(transport, clock=clock)
    assert bus.start(background=True)
    assert bus.shutdown(timeout_s=1.0)
    assert transport.closed
    assert bus.state is CanLinkState.SHUTDOWN
    assert bus.send(command(18)).status is CanSendStatus.NOT_RUNNING


def test_error_count_is_bounded_by_diagnostic_storage_but_counts_all_failures() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    bus = running_bus(transport, clock, CanTransportConfig(diagnostic_capacity=2))
    assert bus.send(command(1)).status is CanSendStatus.QUEUED
    assert bus.send(command(1)).status is CanSendStatus.CORRELATION_CONFLICT
    assert bus.send(telemetry()).status is CanSendStatus.INVALID_FRAME
    assert len(bus.diagnostics()) == 2
    assert bus.get_error_count() >= 1
