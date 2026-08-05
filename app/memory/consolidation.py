from __future__ import annotations

import aiosqlite

from app.memory.action_executor import apply_memory_actions
from app.memory.conflict_resolver import resolve_memory_actions
from app.memory.fact_extractor import extract_facts
from app.memory.memory_store import list_active_memory_items
from app.memory.similarity import find_similar_memory_items
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
from app.providers.registry import ProviderRegistry


async def _narrow_existing_memories(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    facts: list[str],
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    similarity_top_k: int,
) -> list[dict]:
    """对每条新事实各自做一次相似度检索，取并集作为候选（按 memory_id 去重）。

    每条事实独立检索而不是把所有事实拼一起 embed 一次，是因为一轮对话可能
    抽出好几条主题完全不同的事实，合并成一个查询向量会互相稀释，导致某条
    事实真正相似的历史记忆排不进 Top-K。
    """
    embed_result = await embedding_registry.run(
        EmbeddingRequest(texts=facts), provider_name=embedding_provider_name
    )
    candidates: dict[str, dict] = {}
    for vector in embed_result.vectors:
        similar = await find_similar_memory_items(
            conn,
            tenant_id=tenant_id,
            user_id=user_id,
            query_vector=vector,
            top_k=similarity_top_k,
        )
        for item in similar:
            candidates[item["memory_id"]] = item
    return list(candidates.values())


async def run_memory_consolidation(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    user_input: str,
    assistant_output: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    fact_extract_timeout_sec: float = 2.0,
    conflict_resolve_timeout_sec: float = 2.0,
    embedding_registry: EmbeddingRegistry | None = None,
    embedding_provider_name: str | None = None,
    similarity_top_k: int = 20,
) -> list[dict[str, str]]:
    """对话后置处理：抽取事实 -> 与已有记忆比对冲突 -> 执行记忆动作。

    embedding_registry/embedding_provider_name 均为可选：
    - 不提供（默认）：沿用旧行为，把该用户全部 active 记忆条目传给冲突
      决策器，不做向量检索窄化——对单用户记忆条目规模不大的场景是正确的，
      也是没有配 embedding 时的安全回退；
    - 提供：对每条新抽取的事实做一次向量相似度检索，取 Top-K 候选的并集
      传给冲突决策器，而不是全量，可以扩展到单用户成千上万条记忆的场景；
      同时新增/更新的记忆条目也会被同步 embedding，供下一轮检索使用。
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

    if embedding_registry is not None and embedding_provider_name is not None:
        existing_memories = await _narrow_existing_memories(
            conn,
            tenant_id=tenant_id,
            user_id=user_id,
            facts=facts,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            similarity_top_k=similarity_top_k,
        )
    else:
        existing_memories = await list_active_memory_items(
            conn, tenant_id=tenant_id, user_id=user_id
        )

    actions = await resolve_memory_actions(
        new_facts=facts,
        existing_memories=existing_memories,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        timeout_sec=conflict_resolve_timeout_sec,
    )
    return await apply_memory_actions(
        conn,
        tenant_id=tenant_id,
        user_id=user_id,
        actions=actions,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
    )
