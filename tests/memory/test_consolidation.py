import aiosqlite

from app.memory.consolidation import run_memory_consolidation
from app.memory.memory_store import list_active_memory_items
from app.memory.schema import ensure_schema
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
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
    items = await list_active_memory_items(conn, user_id="u1")
    assert [i["text"] for i in items] == ["客户使用企业版套餐"]


async def test_run_memory_consolidation_no_facts_extracted_writes_nothing():
    conn = await _connect()
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "llm", ScriptedLLMProvider(['{"facts": []}'])
    )

    applied = await run_memory_consolidation(
        conn,
        user_id="u1",
        user_input="你好",
        assistant_output="您好，有什么可以帮您",
        llm_registry=llm_registry,
        llm_provider_name="llm",
    )

    assert applied == []
    items = await list_active_memory_items(conn, user_id="u1")
    assert items == []
