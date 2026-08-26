import json
from datetime import datetime, timedelta

import aiosqlite

from app.memory.consolidation import run_memory_consolidation
from app.memory.delayed_confirmation import ensure_delayed_confirmation_schema, list_due_confirmations
from app.memory.memory_store import list_active_memory_items, upsert_memory_item
from app.memory.schema import ensure_schema
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


class ScriptedLLMProvider:
    """按调用顺序返回不同响应：先事实抽取，再冲突决策。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


async def test_run_memory_consolidation_adds_new_fact_end_to_end():
    conn = await _connect()
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        ScriptedLLMProvider(
            [
                '{"is_delay": false}',  # detect_delay_intent
                '{"facts": ["客户使用企业版套餐"]}',
                '{"actions": [{"event": "ADD", "target_memory_id": "", '
                '"text": "客户使用企业版套餐", "reason": "首次提及"}]}',
            ]
        ),
    )

    applied = await run_memory_consolidation(
        conn,
        tenant_id="t1",
        user_id="u1",
        user_input="我们公司用的是企业版套餐",
        assistant_output="好的，已记录",
        llm_registry=llm_registry,
        llm_provider_name="llm",
    )

    assert applied == [
        {
            "event": "ADD",
            "memory_id": applied[0]["memory_id"],
            "text": "客户使用企业版套餐",
        }
    ]
    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert [i["text"] for i in items] == ["客户使用企业版套餐"]


async def test_run_memory_consolidation_writes_conflict_type_into_memory_history_end_to_end():
    """resolver 输出的 conflict_type 键名要真正流到 executor 写入 memory_
    history 的那一列——之前只有 test_conflict_resolver.py 证明 resolver
    "产出"这个键，test_action_executor.py 证明 executor 能"消费"一个手写的
    action 字典，但从没有一个测试让真实 resolver 的输出流进真实的
    apply_memory_actions 再去查 memory_history 表。这里预置一条记忆，让
    LLM 返回一个 UPDATE + conflict_type=temporal 的决策，走完整的
    extract_facts -> resolve_memory_actions -> apply_memory_actions 链路，
    再直接查 memory_history 验证 old_text/new_text/conflict_type 三列。"""
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1", text="客户使用企业版套餐",
    )

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        ScriptedLLMProvider(
            [
                '{"is_delay": false}',  # detect_delay_intent
                '{"facts": ["客户已升级为旗舰版套餐"]}',  # extract_facts
                '{"actions": [{"event": "UPDATE", "target_memory_id": "m1", '
                '"text": "客户已升级为旗舰版套餐", "reason": "客户主动更正套餐信息", '
                '"conflict_type": "temporal"}]}',  # resolve_memory_actions
            ]
        ),
    )

    applied = await run_memory_consolidation(
        conn,
        tenant_id="t1",
        user_id="u1",
        user_input="其实我们现在已经升级到旗舰版套餐了",
        assistant_output="好的，已为您更新记录",
        llm_registry=llm_registry,
        llm_provider_name="llm",
    )

    assert applied == [
        {"event": "UPDATE", "memory_id": "m1", "text": "客户已升级为旗舰版套餐"}
    ]

    cursor = await conn.execute(
        "SELECT old_text, new_text, conflict_type FROM memory_history WHERE memory_id = ?",
        ("m1",),
    )
    row = await cursor.fetchone()
    # list_active_memory_items（run_memory_consolidation 内部调用）会把
    # conn.row_factory 设成 aiosqlite.Row，这里显式转成 tuple 再比较，
    # 不依赖调用方是否碰过 row_factory。
    assert tuple(row) == ("客户使用企业版套餐", "客户已升级为旗舰版套餐", "temporal")


async def test_run_memory_consolidation_no_facts_extracted_writes_nothing():
    conn = await _connect()
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        ScriptedLLMProvider(['{"is_delay": false}', '{"facts": []}']),
    )

    applied = await run_memory_consolidation(
        conn,
        tenant_id="t1",
        user_id="u1",
        user_input="你好",
        assistant_output="您好，有什么可以帮您",
        llm_registry=llm_registry,
        llm_provider_name="llm",
    )

    assert applied == []
    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert items == []


class RecordingLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        return ProviderResult(text=self._responses.pop(0))


class FixedEmbeddingProvider:
    """任何文本都 embed 成同一个向量，模拟"新事实和 m1 高度相似"。"""

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


async def test_run_memory_consolidation_narrows_candidates_by_similarity_when_embedding_registry_provided():
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1", text="很像的历史记忆",
        embedding=[1.0, 0.0],
    )
    await upsert_memory_item(
        conn, memory_id="m2", tenant_id="t1", user_id="u1", text="不像的历史记忆",
        embedding=[0.0, 1.0],
    )

    llm_registry = ProviderRegistry()
    llm_provider = RecordingLLMProvider(
        [
            '{"is_delay": false}',  # detect_delay_intent
            '{"facts": ["客户使用企业版套餐"]}',
            '{"actions": []}',
        ]
    )
    llm_registry.register(ProviderCapability.LLM, "llm", llm_provider)

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FixedEmbeddingProvider())

    await run_memory_consolidation(
        conn,
        tenant_id="t1",
        user_id="u1",
        user_input="我们公司用的是企业版套餐",
        assistant_output="好的，已记录",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        similarity_top_k=1,
    )

    conflict_resolve_request = llm_provider.requests[2]
    prompt_payload = json.loads(conflict_resolve_request.messages[1]["content"])
    existing_texts = {item["text"] for item in prompt_payload["existing_memories"]}
    assert existing_texts == {"很像的历史记忆"}


async def test_run_memory_consolidation_schedules_delayed_confirmation_on_delay_intent():
    conn = await _connect()
    await ensure_delayed_confirmation_schema(conn)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        ScriptedLLMProvider(
            [
                '{"is_delay": true}',  # detect_delay_intent
                '{"start": null, "end": null, "confidence": 0}',  # resolve_time_window
                # 低置信度，规则引擎也无法解析"先试试"这种非时间表达，回退默认2小时
                '{"facts": []}',  # fact_extractor（这句话本身没有值得记忆的事实）
            ]
        ),
    )
    now = datetime(2026, 8, 6, 10, 0, 0)

    applied = await run_memory_consolidation(
        conn,
        tenant_id="t1",
        user_id="u1",
        user_input="我先按您说的重启路由器试试，不行再联系",
        assistant_output="好的，麻烦您先试试，有问题随时联系我们。",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        now=now,
    )

    assert applied == []  # 延迟确认调度不影响 consolidation 的返回值
    due = await list_due_confirmations(conn, tenant_id="t1", now=now + timedelta(hours=3))
    assert len(due) == 1
    assert due[0]["user_id"] == "u1"
    assert "重启路由器试试" in due[0]["context"]


async def test_run_memory_consolidation_does_not_schedule_for_normal_turn():
    conn = await _connect()
    await ensure_delayed_confirmation_schema(conn)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        ScriptedLLMProvider(
            [
                '{"is_delay": false}',  # detect_delay_intent
                '{"facts": []}',  # fact_extractor
            ]
        ),
    )
    now = datetime(2026, 8, 6, 10, 0, 0)

    await run_memory_consolidation(
        conn,
        tenant_id="t1",
        user_id="u1",
        user_input="网络连不上怎么办",
        assistant_output="请先重启路由器。",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        now=now,
    )

    due = await list_due_confirmations(conn, tenant_id="t1", now=now + timedelta(hours=3))
    assert due == []


async def test_run_memory_consolidation_delay_intent_uses_future_time_window_when_resolved():
    """延迟意图 + 能解析出具体未来时间点时，用解析出的时间而不是默认 +2h。"""
    conn = await _connect()
    await ensure_delayed_confirmation_schema(conn)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        ScriptedLLMProvider(
            [
                '{"is_delay": true}',  # detect_delay_intent
                '{"start": "2026-08-07T09:00:00", "end": "2026-08-07T10:00:00", "confidence": 0.9}',
                '{"facts": []}',  # fact_extractor
            ]
        ),
    )
    now = datetime(2026, 8, 6, 10, 0, 0)

    await run_memory_consolidation(
        conn,
        tenant_id="t1",
        user_id="u1",
        user_input="明天上午再联系我确认一下",
        assistant_output="好的，明天上午我们再跟进。",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        now=now,
    )

    # 尚未到 confirm_after（2026-08-07T09:00），此时查询不应该命中
    not_yet_due = await list_due_confirmations(conn, tenant_id="t1", now=now + timedelta(hours=3))
    assert not_yet_due == []

    due = await list_due_confirmations(
        conn, tenant_id="t1", now=datetime(2026, 8, 7, 9, 0, 1)
    )
    assert len(due) == 1
