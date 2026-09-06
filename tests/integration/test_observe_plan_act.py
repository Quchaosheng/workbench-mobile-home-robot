"""
集成测试:观测 → 规划 → 执行路径
"""

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "tools/scripts"),
    str(ROOT / "libs/contracts"),
    str(ROOT / "services/agent_runtime"),
    str(ROOT / "services/world_model"),
]

import demo_scripted
from workbench_agent_runtime import build_template_plan
from workbench_contracts import VerificationStatus, WorldEvent, WorldEventType
from workbench_world_model import create_world_state_snapshot, reduce_events


def test_observe_to_plan_integration(event_store, sample_observation):
    """
    观测 → 世界状态 → 规划

    步骤:
    1. 收到一个 Observation
    2. WorldState reducer 接收它,更新实体位置
    3. 模板规划器基于目标产出 TaskGraph
    4. TaskGraph 包含合法的 semantic_action
    """
    # 1. 模拟收到观测
    obs_event = WorldEvent(
        event_id="evt-001",
        run_id="integration-test-001",
        sequence_no=1,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-04T10:00:00Z",
        payload={
            "observation_id": sample_observation.observation_id,
            "entity_id": sample_observation.entity_id,
            "entity_type": sample_observation.entity_type,
            "location": "on:table",
            "confidence": sample_observation.confidence,
        },
        evidence_refs=sample_observation.evidence_refs,
    )
    event_store.append(obs_event)

    # 2. Reduce 事件流,构建世界状态
    events = event_store.list_run("integration-test-001")
    state = reduce_events("integration-test-001", events)

    # 断言:世界状态包含实体
    assert sample_observation.entity_id in state.entity_locations

    # 3. 产出规划
    plan = build_template_plan("Place the red block in the tray")

    # 断言:规划合法
    assert plan.task_id.startswith("task-")
    assert len(plan.steps) > 0
    assert all(hasattr(step, "action") for step in plan.steps)

    # 断言:能序列化(不抛异常)
    json.loads(plan.model_dump_json())


def test_plan_to_action_integration(sample_semantic_action):
    """
    规划 → 动作执行(占位)

    步骤:
    1. 模板规划器产出 TaskGraph
    2. 第一个 step 的 action 是合法的 SemanticAction
    3. SemanticAction 能序列化并满足 schema

    注意:
        Motion 层尚未实现,这里只测契约一致性
    """
    plan = build_template_plan("Place the red block in the tray")
    first_step = plan.steps[0]

    # 断言:第一步是 observe(模板规划器的固定顺序)
    from workbench_contracts import ActionType

    assert first_step.action.action_type == ActionType.OBSERVE

    # 断言:能序列化
    action_json = first_step.action.model_dump_json()
    parsed = json.loads(action_json)
    assert "action_id" in parsed
    assert "action_type" in parsed


def test_event_replay_integration(event_store):
    """
    事件回放:写入 → 读取 → reduce

    步骤:
    1. 写入 3 个事件
    2. 从事件库读取
    3. reduce 产出 WorldState
    4. 验证 state_hash 存在(一致性检查的基础)
    """
    events = [
        WorldEvent(
            event_id=f"evt-{i:03d}",
            run_id="replay-test-001",
            sequence_no=i,
            event_type=WorldEventType.OBSERVATION,
            occurred_at=f"2026-08-04T10:00:{i:02d}Z",
            payload={
                "entity_id": f"block-{i}",
                "entity_type": "block",
                "location": "on:table",
                "confidence": 0.9,
            },
            evidence_refs=[f"evidence-{i}"],
        )
        for i in range(1, 4)
    ]

    for e in events:
        event_store.append(e)

    # 回放
    replayed = event_store.list_run("replay-test-001")
    assert len(replayed) == 3

    state = reduce_events("replay-test-001", replayed)
    assert state.applied_event_ids == [item.event_id for item in replayed]
    snapshot = create_world_state_snapshot("replay-test-001", replayed)
    assert len(snapshot.state_hash) == 64
    assert snapshot.state_hash.isascii()
    assert snapshot.state_hash.islower()


def test_scripted_demo_requires_post_action_observation(monkeypatch):
    captured = {}
    original_append = demo_scripted.SQLiteEventStore.append
    original_verify = demo_scripted.verify_object_in_tray

    def append_without_post_action_observation(store, item):
        if item.event_id == "evt-004":
            return None
        return original_append(store, item)

    def capture_verification(state, task_id, object_id, tray_id):
        result = original_verify(state, task_id, object_id, tray_id)
        captured["verification"] = result
        return result

    monkeypatch.setattr(
        demo_scripted.SQLiteEventStore,
        "append",
        append_without_post_action_observation,
    )
    monkeypatch.setattr(demo_scripted, "verify_object_in_tray", capture_verification)

    output = demo_scripted.run_once(
        "scripted-without-post-observation",
        demo_scripted.StructuredLogger("scripted-test", io.StringIO()),
    )
    verification = captured["verification"]

    assert output["verified_complete"] is False
    assert verification.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert "action-result-003" not in verification.evidence_refs


def test_scripted_demo_replay_keeps_execution_and_sensor_evidence_separate(monkeypatch):
    captured = {}
    original_reduce = demo_scripted.reduce_events

    def capture_reduction(run_id, events):
        state = original_reduce(run_id, events)
        captured["events"] = events
        captured["state"] = state
        return state

    monkeypatch.setattr(demo_scripted, "reduce_events", capture_reduction)

    output = demo_scripted.run_once(
        "scripted-with-post-observation",
        demo_scripted.StructuredLogger("scripted-test", io.StringIO()),
    )
    replayed_events = captured["events"]
    state = captured["state"]

    assert output["verified_complete"] is True
    assert output["evidence_refs"] == ["camera-frame-004"]
    assert "state_hash" not in output
    assert [item.event_id for item in replayed_events] == [
        "evt-001",
        "evt-002",
        "evt-003",
        "evt-004",
    ]
    assert any(item.event_type is WorldEventType.ACTION_RESULT for item in replayed_events)
    assert "action-result-003" in state.evidence_refs
    assert "action-result-003" not in state.entity_evidence_refs["red_block"]
    assert state.entity_evidence_refs["red_block"] == ["camera-frame-004"]
    snapshot = create_world_state_snapshot(output["run_id"], replayed_events)
    assert len(snapshot.state_hash) == 64
    assert snapshot.state_hash.isascii()
    assert snapshot.state_hash.islower()
