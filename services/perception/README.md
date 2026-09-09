# Perception (Owner: Perception)

P0 emits `Observation` from a controlled Gazebo camera using OpenCV plus AprilTag or color recognition. Required fields are documented in `interfaces/json_schema/observation.schema.json`.

Do not use simulator Oracle values in sensor-mode metrics.

`workbench_perception.ObservationIngestionAdapter` is the fail-closed boundary
between an Observation producer and the World Model event stream. Callers must
name an approved camera calibration revision and pose unit, and inject a clock
from the same domain as the Observation. The adapter rejects malformed, stale,
future-skewed, low-confidence, duplicate, uncalibrated, and frame-mismatched
records before invoking its World Model sink. It preserves the unmodified JSON
Observation under `payload.raw_observation` and emits the calibrated pose at
`payload.pose`; it never writes `WorldState` facts directly.

## Bounded observed attributes

`Observation.attributes` is an optional, versioned string-to-string map. It is
deliberately a finite contract rather than an arbitrary JSON property bag:

| Scope | Supported keys |
| --- | --- |
| Common entities | `colour`, `presence`, `identity`, `orientation` |
| Parcel entities | `label_status`, `condition`, `tracking_id`, `barcode`, `parcel_uid` |
| Appliance entities | `door_state`, `rack_state` (a washer or washer door supports `door_state`; a dishwasher supports both) |
| Managed slots | `slot_state`, `slot_occupancy` |

Unknown entity types receive only the common keys. The producer must reject
unknown keys, keys that do not apply to the entity type, invalid enum values,
empty or surrounding-whitespace text, control characters, invalid UTF-8, and
oversized values before an event is emitted. The limits are 32 attributes, a
64-character key, a 256-character value, and 4096 bytes for canonical UTF-8
JSON. The bounded enums are:

| Key | Allowed values |
| --- | --- |
| `label_status` | `verified`, `unreadable`, `missing`, `unknown` |
| `condition` | `intact`, `damaged`, `unknown` |
| `door_state`, `rack_state` | `open`, `closed`, `unknown` |
| `slot_state`, `slot_occupancy` | `empty`, `occupied`, `blocked`, `unknown` |
| `presence` | `present`, `absent`, `unknown` |

Every modern attribute value carries matching `attribute_metadata`. Metadata
contains `observed_at`, finite confidence in `[0, 1]`, one to 32 unique
evidence references, a belief (`observed`, `inferred`, `stale`, or `lost`), a
`clock_id` (`monotonic` or `wall`), and an optional source. Metadata text and
evidence references are at most 256 characters; canonical metadata JSON is at
most 16 KiB. The modern version marker is
`observed-attributes-v1`. The explicit
`legacy-observed-attributes-v0` marker exists only for migration of older
parcel payloads; it does not permit arbitrary keys or values.

At the ingestion boundary, a modern record that has attributes but omits
per-attribute metadata can have metadata materialized from the enclosing
observation timestamp, confidence, evidence, clock, and source. The canonical
JSON Schema and direct Pydantic model still require metadata for a modern
payload, so records entering through another producer cannot silently bypass
the evidence contract. Legacy payloads may remain sparse until the reducer's
explicit migration path handles them.

`attributes_mode` is either `complete` or `partial`. A complete observation is
a replacement snapshot; a partial observation is a per-key patch and is
accepted only after a complete baseline for that entity exists. Neither mode
allows an action result to create or refresh observed attributes.
