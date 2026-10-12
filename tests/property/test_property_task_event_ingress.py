"""Property suite: ROS 2 TaskEvent ingress is total and fail-closed.

The boundary under test is ``integrations.ros2_runtime.task_events.bind_task_event``.
It promises two things at once:

* every record a real ``task_executor`` can publish binds to the same run and
  action identity the runtime itself recorded, and the terminal outcome maps to
  the device state the runtime actually reported; and
* every record the runtime cannot publish raises ``TaskEventRejected`` rather
  than being coerced into execution evidence, including a pair of terminal
  fields the three publish sites never emit together.

The generator builds one legal record and then perturbs exactly one field, or the
one terminal pair, at a time, so each rejection is attributable to what changed.
A capture that can be edited into a confirmed completion is the failure this
suite exists to prevent: ``ABORTED`` with ``COMPLETED`` is not a task result.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workbench_contracts import ActionOutcome, DeviceState

from integrations.ros2_runtime import TaskEventRejected, bind_task_event

from ._generator import SEEDS, Rng, assert_corpus_clean, report_for, run_corpus, write_archive_if_requested

SUITE = "task_event_ingress"
MIN_CASES = 3 * 48

# ``TaskEvent.action_status`` and the ``ExecuteTask`` result it must pair with.
SUCCEEDED, ABORTED, CANCELED = 1, 2, 3
LEGAL_PAIRS = ((SUCCEEDED, 0), (ABORTED, 2), (ABORTED, 3), (CANCELED, 1))

# Pairs at least one field of which the runtime never publishes together.
ILLEGAL_PAIRS = (
    (SUCCEEDED, 1),
    (SUCCEEDED, 2),
    (SUCCEEDED, 3),
    (ABORTED, 0),
    (ABORTED, 1),
    (CANCELED, 0),
    (CANCELED, 2),
    (CANCELED, 3),
)

OUTCOME_TO_STATE = {
    0: (ActionOutcome.COMPLETED, DeviceState.CONFIRMED),
    1: (ActionOutcome.CANCELED, DeviceState.STOPPED),
    2: (ActionOutcome.SAFE_STOP, DeviceState.STOPPED),
    3: (ActionOutcome.FAILED, DeviceState.REJECTED),
}

BLANK = ("", " ", "\t", "\n")
UNREADABLE_STAMPS = (
    None,
    [],
    {},
    {"sec": 1},
    {"sec": 1, "nanosec": 0, "extra": 1},
    {"sec": 0, "nanosec": 0},
    {"sec": -1, "nanosec": 0},
    {"sec": 1, "nanosec": -1},
    {"sec": 1, "nanosec": 10**9},
    {"sec": 1, "nanosec": 10**20},
    {"sec": True, "nanosec": 0},
    {"sec": 1.5, "nanosec": 0},
    {"sec": "1", "nanosec": "2"},
    {"sec": 2**62, "nanosec": 0},
)


def identifier_source(rng: Rng) -> list[str]:
    return [
        "task-" + str(rng.integer(0, 10**6)),
        "dock_" + rng.choice(("a", "b", "home")),
        "request-" + str(rng.integer(0, 10**6)),
        rng.choice(("single_task", "ready_then_task")),
        "goal-" + str(rng.integer(0, 10**9)),
    ]


def legal_record(rng: Rng) -> dict:
    task_id, target_id, request_id, workflow_id, goal_id = identifier_source(rng)
    status, outcome = rng.choice(LEGAL_PAIRS)
    return {
        "stamp": {"sec": rng.integer(1, 2**31), "nanosec": rng.integer(0, 10**9 - 1)},
        "task_id": task_id,
        "target_id": target_id,
        "request_id": request_id,
        "workflow_id": workflow_id,
        "goal_id": goal_id,
        "action_status": status,
        "outcome": outcome,
        "error_code": rng.integer(0, 0xFFFF),
        "duration_ms": rng.integer(0, 600_000),
        "message": "task completed through Device Bridge",
    }


def perturb(rng: Rng) -> dict:
    """Return one case whose record differs from a legal one in exactly one way."""

    record = legal_record(rng)
    field = rng.choice(
        (
            "action_status",
            "outcome",
            "pair",
            "request_id",
            "workflow_id",
            "goal_id",
            "task_id",
            "target_id",
            "message",
            "error_code",
            "duration_ms",
            "stamp",
            "missing",
        )
    )
    if field == "pair":
        record["action_status"], record["outcome"] = rng.choice(ILLEGAL_PAIRS)
    elif field == "action_status":
        record["action_status"] = rng.choice((0, 4, 5, 255, -1, True, "1", None))
    elif field == "outcome":
        record["outcome"] = rng.choice((4, 5, 255, -1, True, "0", None))
    elif field in ("request_id", "workflow_id", "goal_id", "task_id", "target_id", "message"):
        record[field] = rng.choice((*BLANK, None, 7, [], {}))
    elif field == "error_code":
        record["error_code"] = rng.choice((-1, 0x10000, 2**40, True, "0", None, 1.5))
    elif field == "duration_ms":
        record["duration_ms"] = rng.choice((-1, -(10**6), True, "37", None, 1.5))
    elif field == "stamp":
        record["stamp"] = rng.choice(UNREADABLE_STAMPS)
    else:
        del record[rng.choice(tuple(record))]
    return {"record": record, "expect": "reject", "field": field}


def cases() -> list[dict]:
    generated: list[dict] = []
    for seed in SEEDS:
        rng = Rng(seed)
        for _ in range(16):
            generated.append({"record": legal_record(rng), "expect": "accept", "field": "none"})
        for _ in range(32):
            generated.append(perturb(rng))
    return generated


def check(case: dict) -> None:
    record = case["record"]
    if case["expect"] == "reject":
        with pytest.raises(TaskEventRejected):
            bind_task_event(record)
        return

    run_id, result = bind_task_event(record)
    expected_outcome, expected_state = OUTCOME_TO_STATE[record["outcome"]]
    assert result.outcome is expected_outcome, case
    assert result.device_state is expected_state, case
    assert result.action_id == record["goal_id"], case
    assert result.run_id == run_id, case
    assert result.error_code == record["error_code"], case
    # The runtime reports the device it commanded, never where the entity ended
    # up, so no legal capture may carry a spatial claim.
    assert result.entity_id is None and result.resulting_location is None, case
    assert result.dispatch_state.value == "sent", case


def test_generated_task_events_bind_or_fail_closed() -> None:
    report = run_corpus(SUITE, cases(), check)
    assert_corpus_clean(report)
    assert report.case_count >= MIN_CASES, f"corpus shrank to {report.case_count} cases"


def test_every_legal_terminal_pair_binds() -> None:
    for status, outcome in LEGAL_PAIRS:
        record = legal_record(Rng(SEEDS[0]))
        record["action_status"], record["outcome"] = status, outcome
        run_id, result = bind_task_event(record)
        assert run_id.startswith("run:"), (status, outcome)
        assert result.outcome is OUTCOME_TO_STATE[outcome][0]


@pytest.mark.parametrize(("status", "outcome"), ILLEGAL_PAIRS)
def test_a_terminal_pair_the_runtime_cannot_emit_is_rejected(status: int, outcome: int) -> None:
    # ABORTED+COMPLETED must never be stored as a confirmed completion, and
    # CANCELED+DEVICE_FAULT must never be stored as a device rejection: the
    # runtime's three publish sites can produce neither pair.
    record = legal_record(Rng(SEEDS[1]))
    record["action_status"], record["outcome"] = status, outcome
    with pytest.raises(TaskEventRejected, match="cannot carry runtime outcome"):
        bind_task_event(record)


def test_a_missing_correlation_field_is_named() -> None:
    for field in ("request_id", "workflow_id", "goal_id"):
        record = legal_record(Rng(SEEDS[2]))
        del record[field]
        with pytest.raises(TaskEventRejected, match=f"missing {field}"):
            bind_task_event(record)


def test_identity_separators_cannot_be_forged() -> None:
    # Two different field tuples must not render to one run identity, which is
    # why the encoding is length-prefixed rather than joined with a separator.
    first = legal_record(Rng(SEEDS[0]))
    first["request_id"], first["workflow_id"], first["task_id"] = "a", "b|c", "d"
    second = legal_record(Rng(SEEDS[0]))
    second["request_id"], second["workflow_id"], second["task_id"] = "a|b", "c", "d"
    assert bind_task_event(first)[0] != bind_task_event(second)[0]


def test_rejection_precedes_any_identity() -> None:
    # A refused line must not yield a run id a caller could then write under.
    record = legal_record(Rng(SEEDS[0]))
    record["outcome"] = 99
    with pytest.raises(TaskEventRejected):
        bind_task_event(record)


def test_the_corpus_is_reproducible_for_a_fixed_seed() -> None:
    write_archive_if_requested()
    first = cases()
    second = cases()
    assert [case["record"] for case in first] == [case["record"] for case in second]
    assert report_for(SUITE).seeds == SEEDS
