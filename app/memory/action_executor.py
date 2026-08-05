from __future__ import annotations

import uuid

import aiosqlite

from app.memory.memory_store import append_history, mark_deleted, upsert_memory_item


async def apply_memory_actions(
    conn: aiosqlite.Connection,
    *,
    user_id: str,
    actions: list[dict[str, str]],
) -> list[dict[str, str]]:
    """执行冲突决策器给出的记忆动作，写入 memory_items 并记审计日志。"""
    applied: list[dict[str, str]] = []
    for action in actions:
        event = action.get("event", "").upper()
        text = action.get("text", "")
        memory_id = action.get("memory_id", "")
        reason = action.get("reason") or None

        if event == "ADD":
            if not text:
                continue
            resolved_id = memory_id or str(uuid.uuid4())
            await upsert_memory_item(
                conn, memory_id=resolved_id, user_id=user_id, text=text
            )
            await append_history(
                conn,
                memory_id=resolved_id,
                user_id=user_id,
                event="ADD",
                old_text=None,
                new_text=text,
                reason=reason,
            )
            applied.append({"event": "ADD", "memory_id": resolved_id, "text": text})

        elif event == "UPDATE":
            if not text or not memory_id:
                continue
            await upsert_memory_item(
                conn, memory_id=memory_id, user_id=user_id, text=text
            )
            await append_history(
                conn,
                memory_id=memory_id,
                user_id=user_id,
                event="UPDATE",
                old_text=None,
                new_text=text,
                reason=reason,
            )
            applied.append({"event": "UPDATE", "memory_id": memory_id, "text": text})

        elif event == "DELETE":
            if not memory_id:
                continue
            await mark_deleted(conn, memory_id=memory_id, user_id=user_id)
            await append_history(
                conn,
                memory_id=memory_id,
                user_id=user_id,
                event="DELETE",
                old_text=None,
                new_text=None,
                reason=reason,
            )
            applied.append({"event": "DELETE", "memory_id": memory_id, "text": ""})

    return applied
