"""Validate and calibrate observations before they enter the World Model.

The adapter deliberately owns no detector and writes no ``WorldState`` facts.
It turns one untrusted Observation mapping into a contract-shaped observation
``WorldEvent`` only after every boundary check succeeds.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Literal

from pydantic import ValidationError
from workbench_contracts import (
    ATTRIBUTE_SCHEMA_VERSION,
    AttributeUpdateMode,
    ClockId,
    Observation,
    WorldEvent,
    WorldEventType,
    materialize_attribute_metadata,
)

_REQUIRED_OBSERVATION_KEYS = frozenset(
    {
        "observation_id",
        "run_id",
        "entity_id",
        "entity_type",
        "pose",
        "confidence",
        "observed_at",
        "source",
        "evidence_refs",
    }
)
_QUATERNION_TOLERANCE = 1e-6


class ObservationRejected(ValueError):
    """An observation failed a fail-closed ingestion boundary check."""


@dataclass(frozen=True)
class CalibrationRecord:
    """One approved rigid transform for a camera calibration revision."""

    camera_id: str
    revision: str
    source_frame: str
    target_frame: str
    clock_id: ClockId
    translation_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_xyzw: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    pose_units: Literal["m"] = "m"

    def __post_init__(self) -> None:
        for name in ("camera_id", "revision", "source_frame", "target_frame"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"calibration {name} must be non-empty")
        if not isinstance(self.clock_id, ClockId):
            raise ValueError("calibration clock_id must be a ClockId")
        if self.pose_units != "m":
            raise ValueError("calibration pose_units must be 'm'")
        if (
            not isinstance(self.translation_m, tuple)
            or len(self.translation_m) != 3
            or not all(_is_finite_number(value) for value in self.translation_m)
        ):
            raise ValueError("calibration translation_m must contain three finite numbers")
        if not isinstance(self.rotation_xyzw, tuple):
            raise ValueError("calibration rotation_xyzw must be a tuple")
        _require_unit_quaternion(self.rotation_xyzw, "calibration rotation_xyzw")


class ObservationIngestionAdapter:
    """Emit only validated, fresh and calibrated observation events."""

    def __init__(
        self,
        calibrations: Iterable[CalibrationRecord],
        *,
        now: Callable[[ClockId], datetime],
        sink: Callable[[WorldEvent], None] | None = None,
        minimum_confidence: float = 0.5,
        maximum_age: timedelta = timedelta(seconds=1),
        maximum_future_skew: timedelta = timedelta(milliseconds=50),
        duplicate_window_size: int = 1024,
    ) -> None:
        if not callable(now):
            raise ValueError("now must be callable")
        if sink is not None and not callable(sink):
            raise ValueError("sink must be callable or None")
        if isinstance(minimum_confidence, bool) or not _is_finite_number(minimum_confidence):
            raise ValueError("minimum_confidence must be a finite number")
        if not 0.0 <= float(minimum_confidence) <= 1.0:
            raise ValueError("minimum_confidence must be between 0 and 1")
        if not isinstance(maximum_age, timedelta) or not isinstance(maximum_future_skew, timedelta):
            raise ValueError("freshness durations must be timedeltas")
        if maximum_age < timedelta(0) or maximum_future_skew < timedelta(0):
            raise ValueError("freshness durations must be non-negative")
        if (
            isinstance(duplicate_window_size, bool)
            or not isinstance(duplicate_window_size, int)
            or duplicate_window_size < 1
        ):
            raise ValueError("duplicate_window_size must be a positive integer")

        calibration_by_key: dict[tuple[str, str], CalibrationRecord] = {}
        for calibration in calibrations:
            if not isinstance(calibration, CalibrationRecord):
                raise ValueError("calibrations must contain CalibrationRecord values")
            key = (calibration.camera_id, calibration.revision)
            if key in calibration_by_key:
                raise ValueError(f"duplicate calibration {key!r}")
            calibration_by_key[key] = calibration
        if not calibration_by_key:
            raise ValueError("at least one calibration is required")

        self._calibrations = calibration_by_key
        self._now = now
        self._sink = sink
        self._minimum_confidence = float(minimum_confidence)
        self._maximum_age = maximum_age
        self._maximum_future_skew = maximum_future_skew
        self._duplicate_window_size = duplicate_window_size
        self._seen_ids: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._seen_frames: OrderedDict[tuple[str, str, str, tuple[str, ...]], None] = OrderedDict()
        self._lock = Lock()

    def ingest(
        self,
        record: Mapping[str, object] | None,
        *,
        camera_id: str,
        calibration_revision: str,
        pose_units: str,
        sequence_no: int,
    ) -> WorldEvent:
        """Validate one record, optionally deliver it, and return its WorldEvent.

        The injected sink is called only after the complete record has passed
        structural, calibration, freshness and duplicate checks.
        """
        raw_record, observation = _parse_contract_observation(record)
        _require_non_empty(camera_id, "camera_id")
        _require_non_empty(calibration_revision, "calibration_revision")
        if isinstance(sequence_no, bool) or not isinstance(sequence_no, int) or sequence_no < 0:
            raise ObservationRejected("sequence_no must be a non-negative integer")

        calibration = self._calibrations.get((camera_id, calibration_revision))
        if calibration is None:
            raise ObservationRejected(f"unknown calibration revision {calibration_revision!r} for camera {camera_id!r}")
        if pose_units != "m" or pose_units != calibration.pose_units:
            raise ObservationRejected("pose_units must match the declared calibration unit 'm'")
        if observation.source != camera_id:
            raise ObservationRejected(
                f"Observation source {observation.source!r} does not match camera_id {camera_id!r}"
            )
        if observation.clock_id is not calibration.clock_id:
            raise ObservationRejected(
                f"Observation clock {observation.clock_id.value!r} does not match calibration clock "
                f"{calibration.clock_id.value!r}"
            )

        observed_at = _parse_timestamp(observation.observed_at)
        now = self._now(observation.clock_id)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ObservationRejected("injected clock must return a timezone-aware datetime")
        now = now.astimezone(UTC)
        age = now - observed_at
        if age > self._maximum_age:
            raise ObservationRejected(f"Observation is stale by {age.total_seconds():.6f} seconds")
        if age < -self._maximum_future_skew:
            raise ObservationRejected(f"Observation is future-skewed by {-age.total_seconds():.6f} seconds")

        if observation.confidence < self._minimum_confidence:
            raise ObservationRejected(
                f"Observation confidence {observation.confidence} is below {self._minimum_confidence}"
            )
        _validate_observation_values(observation)
        transformed_pose = _transform_pose(observation, calibration)

        payload: dict[str, object] = {
            "entity_id": observation.entity_id,
            "entity_type": observation.entity_type,
            "pose": transformed_pose,
            "confidence": observation.confidence,
            "observed_at": observation.observed_at,
            "clock_id": observation.clock_id.value,
            "source": observation.source,
            "detector": observation.detector.value,
            "camera_id": camera_id,
            "calibration_revision": calibration_revision,
            "pose_units": pose_units,
            "raw_observation": raw_record,
        }
        if observation.attributes is not None:
            payload["attributes"] = dict(observation.attributes)
            payload["attributes_mode"] = (observation.attributes_mode or AttributeUpdateMode.COMPLETE).value
            payload["attributes_schema_version"] = ATTRIBUTE_SCHEMA_VERSION
            payload["attribute_metadata"] = materialize_attribute_metadata(
                payload["attributes"],
                observation.attribute_metadata,
                observed_at=observation.observed_at,
                confidence=observation.confidence,
                evidence_refs=list(observation.evidence_refs),
                clock_id=observation.clock_id,
                source=observation.source,
                entity_type=observation.entity_type,
            )

        event = WorldEvent(
            event_id=f"observation:{observation.observation_id}",
            run_id=observation.run_id,
            sequence_no=sequence_no,
            event_type=WorldEventType.OBSERVATION,
            occurred_at=observation.observed_at,
            payload=payload,
            evidence_refs=list(observation.evidence_refs),
        )

        observation_key = (observation.run_id, observation.observation_id)
        frame_key = (observation.run_id, camera_id, observation.entity_id, tuple(observation.evidence_refs))
        with self._lock:
            if observation_key in self._seen_ids:
                raise ObservationRejected(f"duplicate observation_id {observation.observation_id!r}")
            if frame_key in self._seen_frames:
                raise ObservationRejected("duplicate camera-frame observation for entity")
            if self._sink is not None:
                self._sink(event)
            _remember(self._seen_ids, observation_key, self._duplicate_window_size)
            _remember(self._seen_frames, frame_key, self._duplicate_window_size)
        return event


def _parse_contract_observation(record: Mapping[str, object] | None) -> tuple[dict[str, Any], Observation]:
    if not isinstance(record, Mapping):
        raise ObservationRejected("Observation record must be a mapping")
    missing = _REQUIRED_OBSERVATION_KEYS - set(record)
    if missing:
        raise ObservationRejected(f"Observation is missing contract fields: {sorted(missing)}")
    try:
        raw_serialized = json.dumps(dict(record), allow_nan=False, ensure_ascii=False)
        raw_record = json.loads(raw_serialized)
        candidate = dict(raw_record)
        if (
            "attributes" in candidate
            and candidate.get("attributes_schema_version") == ATTRIBUTE_SCHEMA_VERSION
            and "attribute_metadata" not in candidate
        ):
            candidate["attribute_metadata"] = materialize_attribute_metadata(
                candidate["attributes"],
                None,
                observed_at=candidate["observed_at"],
                confidence=candidate["confidence"],
                evidence_refs=candidate["evidence_refs"],
                clock_id=candidate.get("clock_id", ClockId.MONOTONIC),
                source=candidate["source"],
                entity_type=candidate["entity_type"],
            )
        serialized = json.dumps(candidate, allow_nan=False, ensure_ascii=False)
        observation = Observation.model_validate_json(serialized, strict=True)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ObservationRejected(f"Observation is not contract-valid: {exc}") from exc
    return raw_record, observation


def _validate_observation_values(observation: Observation) -> None:
    for name in ("observation_id", "run_id", "entity_id", "entity_type", "source"):
        _require_non_empty(getattr(observation, name), name)
    _require_non_empty(observation.pose.frame_id, "pose.frame_id")
    if observation.hamming is not None and (isinstance(observation.hamming, bool) or observation.hamming < 0):
        raise ObservationRejected("hamming must be a non-negative integer or null")
    if observation.decision_margin is not None and not _is_finite_number(observation.decision_margin):
        raise ObservationRejected("decision_margin must be finite or null")

    position = observation.pose.position
    if not all(_is_finite_number(value) for value in (position.x, position.y, position.z)):
        raise ObservationRejected("pose position must contain finite numbers")
    orientation = observation.pose.orientation
    _require_unit_quaternion(
        (orientation.x, orientation.y, orientation.z, orientation.w),
        "pose orientation",
        rejection=True,
    )
    if len(set(observation.evidence_refs)) != len(observation.evidence_refs):
        raise ObservationRejected("evidence_refs must not contain duplicates")
    for reference in observation.evidence_refs:
        _require_non_empty(reference, "evidence_refs item")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationRejected("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ObservationRejected("observed_at must include a timezone")
    return parsed.astimezone(UTC)


def _transform_pose(observation: Observation, calibration: CalibrationRecord) -> dict[str, object]:
    pose = observation.pose
    if pose.frame_id == calibration.target_frame:
        return pose.model_dump(mode="json")
    if pose.frame_id != calibration.source_frame:
        raise ObservationRejected(
            f"pose frame {pose.frame_id!r} matches neither calibration source "
            f"{calibration.source_frame!r} nor target {calibration.target_frame!r}"
        )

    source_position = (pose.position.x, pose.position.y, pose.position.z)
    rotated = _rotate_vector(calibration.rotation_xyzw, source_position)
    translated = tuple(rotated[index] + calibration.translation_m[index] for index in range(3))
    source_orientation = (pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w)
    target_orientation = _multiply_quaternions(calibration.rotation_xyzw, source_orientation)
    return {
        "frame_id": calibration.target_frame,
        "position": {"x": translated[0], "y": translated[1], "z": translated[2]},
        "orientation": {
            "x": target_orientation[0],
            "y": target_orientation[1],
            "z": target_orientation[2],
            "w": target_orientation[3],
        },
    }


def _multiply_quaternions(
    left: tuple[float, float, float, float], right: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    lx, ly, lz, lw = left
    rx, ry, rz, rw = right
    product = (
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
        lw * rw - lx * rx - ly * ry - lz * rz,
    )
    norm = math.sqrt(sum(component * component for component in product))
    return tuple(component / norm for component in product)  # type: ignore[return-value]


def _rotate_vector(
    rotation: tuple[float, float, float, float], vector: tuple[float, float, float]
) -> tuple[float, float, float]:
    vector_quaternion = (vector[0], vector[1], vector[2], 0.0)
    conjugate = (-rotation[0], -rotation[1], -rotation[2], rotation[3])
    rotated = _multiply_raw(_multiply_raw(rotation, vector_quaternion), conjugate)
    return rotated[0], rotated[1], rotated[2]


def _multiply_raw(
    left: tuple[float, float, float, float], right: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    lx, ly, lz, lw = left
    rx, ry, rz, rw = right
    return (
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
        lw * rw - lx * rx - ly * ry - lz * rz,
    )


def _require_unit_quaternion(value: tuple[float, float, float, float], name: str, *, rejection: bool = False) -> None:
    error_type = ObservationRejected if rejection else ValueError
    if len(value) != 4 or not all(_is_finite_number(component) for component in value):
        raise error_type(f"{name} must contain four finite numbers")
    norm = math.sqrt(sum(float(component) * float(component) for component in value))
    if not math.isclose(norm, 1.0, rel_tol=_QUATERNION_TOLERANCE, abs_tol=_QUATERNION_TOLERANCE):
        raise error_type(f"{name} must be a unit quaternion")


def _is_finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(float(value))


def _require_non_empty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ObservationRejected(f"{name} must be a non-empty string")


def _remember(cache: OrderedDict[Any, None], key: Any, maximum_size: int) -> None:
    cache[key] = None
    while len(cache) > maximum_size:
        cache.popitem(last=False)
