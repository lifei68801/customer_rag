import json

import aiosqlite

from app.memory.consolidation import run_memory_consolidation
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


async def test_run_memory_consolidation_no_facts_extracted_writes_nothing():
    conn = await _connect()
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "llm", ScriptedLLMProvider(['{"facts": []}'])
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

    conflict_resolve_request = llm_provider.requests[1]
    prompt_payload = json.loads(conflict_resolve_request.messages[1]["content"])
    existing_texts = {item["text"] for item in prompt_payload["existing_memories"]}
    assert existing_texts == {"很像的历史记忆"}
