from __future__ import annotations

from typing import Any

import aiosqlite

from app.memory.compaction import compact_messages
from app.memory.memory_store import list_active_memory_items
from app.memory.recall import recall_memory_items
from app.memory.session_window import get_recent_turns
from app.providers.embedding import EmbeddingRegistry


async def inject_memory_context(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    user_id: str,
    recent_turn_limit: int = 10,
    compaction_preserve_recent_messages: int = 8,
    memory_item_limit: int = 20,
    question: str | None = None,
    embedding_registry: EmbeddingRegistry | None = None,
    embedding_provider_name: str | None = None,
) -> list[dict[str, Any]]:
    """组装记忆上下文消息：长期记忆条目 + （压缩后的）近期会话轮次。

    question/embedding_registry/embedding_provider_name 均提供时，长期记忆
    条目改用 recall_memory_items() 的多源召回融合+MMR 结果（跟当前问题的
    相关性排序），而不是"全部 active 条目按更新时间截断"；三者任一缺失
    则保留旧行为，向后兼容不传这些参数的调用方。
    """
    if question is not None and embedding_registry is not None and embedding_provider_name is not None:
        memory_items = await recall_memory_items(
            conn,
            tenant_id=tenant_id,
            user_id=user_id,
            question=question,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            top_k=memory_item_limit,
        )
    else:
        memory_items = await list_active_memory_items(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        memory_items = memory_items[:memory_item_limit]

    turns = await get_recent_turns(
        conn, tenant_id=tenant_id, session_id=session_id, limit=recent_turn_limit
    )
    turn_messages = [{"role": t["role"], "content": t["content"]} for t in turns]
    compacted_turns = compact_messages(
        turn_messages, preserve_recent_messages=compaction_preserve_recent_messages
    )

    messages: list[dict[str, Any]] = []
    if memory_items:
        lines = "\n".join(f"- {item['text']}" for item in memory_items)
        messages.append(
            {"role": "system", "content": f"长期记忆条目：\n{lines}"}
        )
    messages.extend(compacted_turns)
    return messages
