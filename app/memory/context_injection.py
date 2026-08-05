from __future__ import annotations

from typing import Any

import aiosqlite

from app.memory.compaction import compact_messages
from app.memory.memory_store import list_active_memory_items
from app.memory.session_window import get_recent_turns


async def inject_memory_context(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    user_id: str,
    recent_turn_limit: int = 10,
    compaction_preserve_recent_messages: int = 8,
    memory_item_limit: int = 20,
) -> list[dict[str, Any]]:
    """组装记忆上下文消息：长期记忆条目 + （压缩后的）近期会话轮次。"""
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
