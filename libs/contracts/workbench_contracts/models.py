from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from .observed_attributes import (
    ATTRIBUTE_SCHEMA_VERSION,
    LEGACY_ATTRIBUTE_MIGRATION_VERSION,
    AttributeMetadataMap,
    AttributeUpdateMode,
    VersionedObservedAttributes,
    legacy_attribute_keys_allowed,
    validate_attribute_metadata_map,
    validate_observed_attributes,
)


class ActionType(StrEnum):
    OBSERVE = "observe"
    GRASP = "grasp"
    PLACE = "place"
    ASK_CONFIRM = "ask_confirm"
    EXPRESS = "express"
    STOP = "stop"
    NAVIGATE = "navigate"


class ActionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class WorldEventType(StrEnum):
    OBSERVATION = "observation"
    ACTION_REQUEST = "action_request"
    ACTION_RESULT = "action_result"
    VERIFICATION = "verification"
    FAULT = "fault"
    EMOTION = "emotion"
    TASK_ACCEPTED = "task_accepted"
    TASK_TERMINAL = "task_terminal"
    TASK_START = "task_start"
    TOOL_CALL = "tool_call"
    POLICY_VIOLATION = "policy_violation"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETE = "recovery_complete"


class ClockId(StrEnum):
    MONOTONIC = "monotonic"
    WALL = "wall"


class McuFrameKind(StrEnum):
    COMMAND = "command"
    ACK = "ack"
    TELEMETRY = "telemetry"
    STOP = "stop"
    STOP_ACK = "stop_ack"


class McuOpcode(StrEnum):
    MOVE = "move"
    GRIP_OPEN = "grip_open"
    GRIP_CLOSE = "grip_close"
    HOLD = "hold"
    STOP = "stop"
    HEARTBEAT = "heartbeat"


class McuFaultCode(StrEnum):
    NONE = "none"
    ACK_TIMEOUT = "ack_timeout"
    STOP_TIMEOUT = "stop_timeout"
    STOP_REJECTED = "stop_rejected"
    LINK_LOST = "link_lost"
    DUPLICATE_FRAME = "duplicate_frame"
    WATCHDOG_EXPIRED = "watchdog_expired"
    MALFORMED_FRAME = "malformed_frame"


class McuDeviceMode(StrEnum):
    IDLE = "idle"
    MOVING = "moving"
    HOLDING = "holding"
    STOPPED = "stopped"
    FAULTED = "faulted"


McuFrameId = Annotated[
    str,
    StringConstraints(
        min_length=11,
        max_length=74,
        pattern=r"^mcu-frame-[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
    ),
]
McuOrdinaryCommandId = Annotated[int, Field(strict=True, ge=0, le=32767)]
McuStopCommandId = Annotated[int, Field(strict=True, ge=32768, le=65535)]
McuSequenceNo = Annotated[int, Field(strict=True, ge=0, le=4294967295)]
McuRetryCount = Annotated[int, Field(strict=True, ge=0, le=255)]
McuMonotonicUs = Annotated[int, Field(strict=True, ge=0, le=18446744073709551615)]
McuOrdinaryOpcode = Literal["move", "grip_open", "grip_close", "hold", "heartbeat"]
McuResultCode = Literal[0, 1]


class _McuFrameBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"]
    frame_id: McuFrameId
    sent_at_us: McuMonotonicUs
    clock_id: Literal["monotonic"]


class McuCommandFrame(_McuFrameBase):
    frame_kind: Literal["command"]
    command_id: McuOrdinaryCommandId
    opcode: McuOrdinaryOpcode
    retry_count: McuRetryCount


class McuAckFrame(_McuFrameBase):
    frame_kind: Literal["ack"]
    command_id: McuOrdinaryCommandId
    opcode: McuOrdinaryOpcode
    result_code: McuResultCode
    fault_code: McuFaultCode
    device_mode: McuDeviceMode
    retry_count: McuRetryCount

    @model_validator(mode="after")
    def validate_result(self) -> "McuAckFrame":
        if self.result_code == 0:
            if self.fault_code is not McuFaultCode.NONE or self.device_mode is McuDeviceMode.FAULTED:
                raise ValueError("successful ack requires fault_code none and a non-faulted device_mode")
        elif (
            self.fault_code not in {McuFaultCode.DUPLICATE_FRAME, McuFaultCode.MALFORMED_FRAME}
            or self.device_mode is not McuDeviceMode.FAULTED
        ):
            raise ValueError("failed ack requires a command-rejection fault and faulted device_mode")
        return self


class McuTelemetryFrame(_McuFrameBase):
    frame_kind: Literal["telemetry"]
    sequence_no: McuSequenceNo
    fault_code: McuFaultCode
    device_mode: McuDeviceMode

    @model_validator(mode="after")
    def validate_fault_state(self) -> "McuTelemetryFrame":
        if self.fault_code not in {
            McuFaultCode.NONE,
            McuFaultCode.LINK_LOST,
            McuFaultCode.WATCHDOG_EXPIRED,
        }:
            raise ValueError("telemetry fault_code must describe an active device fault")
        if (self.fault_code is McuFaultCode.NONE) == (self.device_mode is McuDeviceMode.FAULTED):
            raise ValueError("telemetry requires faulted mode exactly when fault_code is not none")
        return self


class McuStopFrame(_McuFrameBase):
    frame_kind: Literal["stop"]
    command_id: McuStopCommandId
    opcode: Literal["stop"]
    retry_count: McuRetryCount


class McuStopAckFrame(_McuFrameBase):
    frame_kind: Literal["stop_ack"]
    command_id: McuStopCommandId
    opcode: Literal["stop"]
    result_code: McuResultCode
    fault_code: McuFaultCode
    device_mode: McuDeviceMode
    retry_count: McuRetryCount

    @model_validator(mode="after")
    def validate_stop_result(self) -> "McuStopAckFrame":
        if self.result_code == 0:
            if self.fault_code is not McuFaultCode.NONE or self.device_mode is not McuDeviceMode.STOPPED:
                raise ValueError("successful stop_ack requires fault_code none and stopped device_mode")
        elif self.fault_code is not McuFaultCode.STOP_REJECTED or self.device_mode is not McuDeviceMode.FAULTED:
            raise ValueError("failed stop_ack requires stop_rejected and faulted device_mode")
        return self


McuFrameValue = Annotated[
    McuCommandFrame | McuAckFrame | McuTelemetryFrame | McuStopFrame | McuStopAckFrame,
    Field(discriminator="frame_kind"),
]


class McuFrame(RootModel[McuFrameValue]):
    """Fail-closed MCU logical frame protocol v1.0."""


class _CanonicalRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Position(_CanonicalRuntimeModel):
    x: float
    y: float
    z: float


class Orientation(_CanonicalRuntimeModel):
    """Unit quaternion. A tabletop grasp needs orientation, not just a point."""

    x: float
    y: float
    z: float
    w: float


class Pose(_CanonicalRuntimeModel):
    frame_id: str
    position: Position
    orientation: Orientation


class Detector(StrEnum):
    APRILTAG = "apriltag"
    COLOUR_THRESHOLD = "colour_threshold"
    MOCK = "mock"


class Observation(_CanonicalRuntimeModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "allOf": [
                {"if": {"required": ["attributes_mode"]}, "then": {"required": ["attributes"]}},
                {"if": {"required": ["attributes"]}, "then": {"required": ["attributes_schema_version"]}},
                {
                    "if": {"required": ["attributes_schema_version"]},
                    "then": {"required": ["attributes"]},
                },
                {"if": {"required": ["attribute_metadata"]}, "then": {"required": ["attributes"]}},
                {
                    "if": {
                        "required": ["attributes_schema_version"],
                        "properties": {"attributes_schema_version": {"const": ATTRIBUTE_SCHEMA_VERSION}},
                    },
                    "then": {"required": ["attribute_metadata"]},
                },
            ]
        },
    )

    observation_id: str
    run_id: str
    entity_id: str
    entity_type: str
    pose: Pose
    confidence: float = Field(ge=0.0, le=1.0)
    detector: Detector = Detector.MOCK
    hamming: int | None = Field(default=None, ge=0)
    decision_margin: float | None = None
    observed_at: str
    clock_id: ClockId = ClockId.MONOTONIC
    source: str
    evidence_refs: list[str] = Field(min_length=1)
    attributes: VersionedObservedAttributes | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    attributes_mode: AttributeUpdateMode | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    attributes_schema_version: (
        Literal[ATTRIBUTE_SCHEMA_VERSION, LEGACY_ATTRIBUTE_MIGRATION_VERSION] | SkipJsonSchema[None]
    ) = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    attribute_metadata: AttributeMetadataMap | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("attributes", mode="before")
    @classmethod
    def reject_explicit_null_attributes(cls, value: object) -> object:
        if value is None:
            raise ValueError("attributes may be omitted but cannot be null")
        return value

    @field_validator("attributes_mode", mode="before")
    @classmethod
    def normalize_attributes_mode(cls, value: object) -> object:
        if value is None:
            raise ValueError("attributes_mode may be omitted but cannot be null")
        if isinstance(value, AttributeUpdateMode):
            return value
        if type(value) is not str:
            raise ValueError("attributes_mode must be a string enum value")
        try:
            return AttributeUpdateMode(value)
        except ValueError as error:
            raise ValueError("attributes_mode must be 'complete' or 'partial'") from error

    @field_validator("attributes_schema_version", "attribute_metadata", mode="before")
    @classmethod
    def reject_null_attribute_metadata_fields(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            raise ValueError(f"{info.field_name} may be omitted but cannot be null")
        return value

    @model_validator(mode="after")
    def validate_attribute_semantics(self) -> "Observation":
        if self.attributes is None:
            if self.attributes_mode is not None:
                raise ValueError("attributes_mode requires attributes")
            if self.attributes_schema_version is not None:
                raise ValueError("attributes_schema_version requires attributes")
            if self.attribute_metadata is not None:
                raise ValueError("attribute_metadata requires attributes")
            return self
        if self.attributes_schema_version is None:
            raise ValueError("attributes_schema_version is required when attributes are present")
        allow_legacy = (
            self.attributes_schema_version == LEGACY_ATTRIBUTE_MIGRATION_VERSION
            and legacy_attribute_keys_allowed(self.entity_type)
        )
        validate_observed_attributes(
            self.attributes,
            entity_type=self.entity_type,
            allow_unknown_keys=allow_legacy,
        )
        if self.attributes_schema_version == ATTRIBUTE_SCHEMA_VERSION and self.attribute_metadata is None:
            raise ValueError("modern Observation attributes require attribute_metadata")
        if self.attribute_metadata is not None:
            validate_attribute_metadata_map(
                {key: value.model_dump(mode="json") for key, value in self.attribute_metadata.items()},
                attribute_keys=set(self.attributes),
                entity_type=self.entity_type,
                allow_unknown_keys=allow_legacy,
                require_observed=self.attributes_schema_version == ATTRIBUTE_SCHEMA_VERSION,
                require_complete=True,
                expected_clock_id=self.clock_id,
            )
        return self


class EmotionState(StrEnum):
    IDLE = "idle"
    THINKING = "thinking"
    UNCERTAIN = "uncertain"
    PLEASED = "pleased"


class EmotionTrigger(StrEnum):
    TASK_ACCEPTED = "task_accepted"
    PLANNING_STARTED = "planning_started"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    TASK_VERIFIED = "task_verified"
    TASK_FAILED = "task_failed"
    IDLE_TIMEOUT = "idle_timeout"


class EmotionIntent(_CanonicalRuntimeModel):
    """Display-only expression state; it never gates execution."""

    intent_id: str
    run_id: str
    state: EmotionState
    triggered_by: EmotionTrigger
    verification_ref: str | None = None
    issued_at: str
    clock_id: ClockId = ClockId.MONOTONIC


class SemanticAction(_CanonicalRuntimeModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"action_type": {"const": "navigate"}}},
                    "then": {
                        "required": ["target_id"],
                        "properties": {
                            "target_id": {"type": "string", "minLength": 1, "pattern": r".*\S.*"},
                            "parameters": {"type": "object", "additionalProperties": False, "maxProperties": 0},
                        },
                    },
                }
            ]
        }
    )

    action_id: str
    action_type: ActionType
    target_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_navigate_boundary(self) -> "SemanticAction":
        if self.action_type is not ActionType.NAVIGATE:
            return self
        if not isinstance(self.target_id, str) or not self.target_id.strip():
            raise ValueError("navigate requires a non-empty configured waypoint target_id")
        if self.parameters:
            raise ValueError("navigate parameters must be empty; waypoint policy is executor-owned")
        return self


class ActionOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    SAFE_STOP = "safe_stop"
    TIMEOUT = "timeout"


class DispatchState(StrEnum):
    """Whether the frame left the host. Not whether the device acted on it."""

    NOT_SENT = "not_sent"
    SENT = "sent"
    SEND_FAILED = "send_failed"


class DeviceState(StrEnum):
    """Whether the device confirmed. Separate from DispatchState by design:
    a written frame is not a confirmed action."""

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    STOPPED = "stopped"


class ActionResult(_CanonicalRuntimeModel):
    result_id: str
    action_id: str
    run_id: str
    outcome: ActionOutcome
    dispatch_state: DispatchState
    device_state: DeviceState
    error_code: int | None = None
    error_reason: str | None = None
    started_at: str
    ended_at: str
    clock_id: ClockId = ClockId.MONOTONIC
    retry_count: int = Field(default=0, ge=0)
    entity_id: str | None = None
    resulting_location: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completion_state(self) -> "ActionResult":
        if self.outcome is ActionOutcome.COMPLETED and (
            self.dispatch_state is not DispatchState.SENT or self.device_state is not DeviceState.CONFIRMED
        ):
            raise ValueError("completed action requires sent dispatch and confirmed device state")
        return self


class WorldEvent(_CanonicalRuntimeModel):
    event_id: str
    run_id: str
    sequence_no: int = Field(ge=0)
    event_type: WorldEventType
    occurred_at: str
    payload: dict[str, Any]
    recorded_at: str | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    clock_id: ClockId | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("recorded_at", "clock_id", mode="before")
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("optional WorldEvent fields must be omitted instead of null")
        return value


class WorldBelief(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    STALE = "stale"
    LOST = "lost"


class WorldRelationPredicate(StrEnum):
    INSIDE = "inside"
    ON_TOP_OF = "on_top_of"
    HELD_BY = "held_by"
    ADJACENT_TO = "adjacent_to"


WorldStateHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class WorldEntity(_CanonicalRuntimeModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "allOf": [
                {"if": {"required": ["attributes"]}, "then": {"required": ["attributes_schema_version"]}},
                {
                    "if": {"required": ["attributes_schema_version"]},
                    "then": {"required": ["attributes"]},
                },
                {"if": {"required": ["attribute_metadata"]}, "then": {"required": ["attributes"]}},
                {
                    "if": {
                        "required": ["attributes_schema_version"],
                        "properties": {"attributes_schema_version": {"const": ATTRIBUTE_SCHEMA_VERSION}},
                    },
                    "then": {"required": ["attribute_metadata"]},
                },
            ]
        },
    )

    entity_id: str
    entity_type: str
    pose: Pose | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    belief: WorldBelief
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    last_observed_at: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    attributes: VersionedObservedAttributes | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    attributes_schema_version: (
        Literal[ATTRIBUTE_SCHEMA_VERSION, LEGACY_ATTRIBUTE_MIGRATION_VERSION] | SkipJsonSchema[None]
    ) = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    attribute_metadata: AttributeMetadataMap | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("pose", "confidence", mode="before")
    @classmethod
    def reject_json_null(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "json" and value is None:
            raise ValueError("optional WorldEntity fields must be omitted instead of null")
        return value

    @field_validator("attributes", mode="before")
    @classmethod
    def reject_json_null_attributes(cls, value: object) -> object:
        if value is None:
            raise ValueError("attributes may be omitted but cannot be null")
        return value

    @field_validator("attributes_schema_version", "attribute_metadata", mode="before")
    @classmethod
    def reject_null_attribute_metadata_fields(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            raise ValueError(f"{info.field_name} may be omitted but cannot be null")
        return value

    @model_validator(mode="after")
    def validate_entity_attributes(self) -> "WorldEntity":
        if self.attributes is not None:
            if self.attributes_schema_version is None:
                raise ValueError("attributes_schema_version is required when attributes are present")
            allow_legacy = (
                self.attributes_schema_version == LEGACY_ATTRIBUTE_MIGRATION_VERSION
                and legacy_attribute_keys_allowed(self.entity_type)
            )
            validate_observed_attributes(
                self.attributes,
                entity_type=self.entity_type,
                allow_unknown_keys=allow_legacy,
            )
            if self.attributes_schema_version == ATTRIBUTE_SCHEMA_VERSION and self.attribute_metadata is None:
                raise ValueError("modern WorldEntity attributes require attribute_metadata")
            if self.attribute_metadata is not None:
                validate_attribute_metadata_map(
                    {key: value.model_dump(mode="json") for key, value in self.attribute_metadata.items()},
                    attribute_keys=set(self.attributes),
                    entity_type=self.entity_type,
                    allow_unknown_keys=allow_legacy,
                    require_complete=True,
                )
        elif self.attributes_schema_version is not None or self.attribute_metadata is not None:
            raise ValueError("attribute metadata fields require attributes")
        return self


class WorldRelation(_CanonicalRuntimeModel):
    subject_id: str
    predicate: WorldRelationPredicate
    object_id: str
    belief: WorldBelief
    evidence_refs: list[str] = Field(default_factory=list)


class WorldState(_CanonicalRuntimeModel):
    """Public JSON-shaped WorldState contract.

    The World Model reducer owns a separate internal state representation; this
    model is only the versioned interface projection described by the schema.
    """

    run_id: str
    sequence_no: int = Field(ge=0)
    state_hash: WorldStateHash
    entities: list[WorldEntity]
    relations: list[WorldRelation] = Field(default_factory=list)
    reduced_at: str
    clock_id: ClockId = ClockId.MONOTONIC


class TaskStep(_CanonicalRuntimeModel):
    step_id: str
    action: SemanticAction
    depends_on: list[str] = Field(default_factory=list)


class TaskGraph(_CanonicalRuntimeModel):
    task_id: str
    goal: str
    steps: list[TaskStep] = Field(min_length=1)
    planner: str
    model_route: str


class VerificationStatus(StrEnum):
    """Three-valued on purpose. A boolean would force the system to guess when
    the evidence does not support either answer."""

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ReasonCode(StrEnum):
    GOAL_SATISFIED = "goal_satisfied"
    GOAL_NOT_SATISFIED = "goal_not_satisfied"
    TARGET_NOT_OBSERVED = "target_not_observed"
    CONFIDENCE_BELOW_THRESHOLD = "confidence_below_threshold"
    CONFLICTING_OBSERVATIONS = "conflicting_observations"
    EVIDENCE_MISSING = "evidence_missing"
    STALE_OBSERVATION = "stale_observation"


class RecoveryHint(StrEnum):
    RE_OBSERVE = "re_observe"
    RETRY_ACTION = "retry_action"
    ASK_CONFIRM = "ask_confirm"
    ABORT = "abort"
    NONE = "none"


class VerificationResult(_CanonicalRuntimeModel):
    verification_id: str
    run_id: str
    task_id: str
    claim: str
    status: VerificationStatus
    reason_code: ReasonCode | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    completeness: Annotated[float, Field(ge=0.0, le=1.0)] | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )
    evidence_refs: list[str] = Field(min_length=1)
    recovery_hint: RecoveryHint = RecoveryHint.NONE
    verified_at: str
    clock_id: ClockId = ClockId.MONOTONIC
    rule_version: str = "unversioned"

    @field_validator("reason_code", "completeness", mode="before")
    @classmethod
    def reject_json_null(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "json" and value is None:
            raise ValueError("optional VerificationResult fields must be omitted instead of null")
        return value

    @model_validator(mode="after")
    def validate_status_semantics(self) -> "VerificationResult":
        if self.status is VerificationStatus.CONFIRMED:
            if self.reason_code not in {None, ReasonCode.GOAL_SATISFIED}:
                raise ValueError("confirmed verification cannot use a non-success reason")
            if self.completeness is not None and self.completeness != 1.0:
                raise ValueError("confirmed verification completeness must be 1")
            if self.recovery_hint is not RecoveryHint.NONE:
                raise ValueError("confirmed verification cannot request recovery")
        elif self.reason_code is ReasonCode.GOAL_SATISFIED:
            raise ValueError("non-confirmed verification cannot use goal_satisfied")
        return self

    @property
    def completed(self) -> bool:
        """True only for a confirmed goal. Both `refuted` and
        `insufficient_evidence` are not-completed, but they are not the same
        thing — read `status` when the distinction matters."""
        return self.status is VerificationStatus.CONFIRMED

    @property
    def reason(self) -> str:
        return self.reason_code.value if self.reason_code else self.status.value


class ScenarioManifest(_CanonicalRuntimeModel):
    scenario_id: str
    seed: int
    task_id: str
    world_version: str
    fault_type: str
    timeout_s: int = Field(gt=0, le=600)
    oracle_allowed: bool
