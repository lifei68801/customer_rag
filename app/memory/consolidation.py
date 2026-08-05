from __future__ import annotations

import aiosqlite

from app.memory.action_executor import apply_memory_actions
from app.memory.conflict_resolver import resolve_memory_actions
from app.memory.fact_extractor import extract_facts
from app.memory.memory_store import list_active_memory_items
from app.providers.registry import ProviderRegistry


async def run_memory_consolidation(
    conn: aiosqlite.Connection,
    *,
    user_id: str,
    user_input: str,
    assistant_output: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    fact_extract_timeout_sec: float = 2.0,
    conflict_resolve_timeout_sec: float = 2.0,
) -> list[dict[str, str]]:
    """对话后置处理：抽取事实 -> 与已有记忆比对冲突 -> 执行记忆动作。

    简化说明：架构文档设想已有记忆的"相似候选"检索走向量相似度（Milvus
    投影），本实现直接把该用户全部 active 记忆条目传给冲突决策器判断，
    不做向量检索窄化候选集——对单用户记忆条目规模不大的场景是正确的，
    但不能扩展到单用户成千上万条记忆的情况，那时需要引入向量检索先
    收窄候选池，本实现暂不涉及。
    """
    facts = await extract_facts(
        user_input=user_input,
        assistant_output=assistant_output,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        timeout_sec=fact_extract_timeout_sec,
    )
    if not facts:
        return []

    existing_memories = await list_active_memory_items(conn, user_id=user_id)
    actions = await resolve_memory_actions(
        new_facts=facts,
        existing_memories=existing_memories,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        timeout_sec=conflict_resolve_timeout_sec,
    )
    return await apply_memory_actions(conn, user_id=user_id, actions=actions)
