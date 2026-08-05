from __future__ import annotations

import uuid

import aiosqlite

from app.memory.memory_store import append_history, mark_deleted, upsert_memory_item
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest


async def _embed_text(
    text: str,
    *,
    embedding_registry: EmbeddingRegistry | None,
    embedding_provider_name: str | None,
) -> list[float] | None:
    """embedding_registry 未提供时返回 None，保持"不做向量投影"这个旧行为
    不变——只有明确要用相似度窄化候选的调用方才需要传这两个参数。"""
    if embedding_registry is None or embedding_provider_name is None:
        return None
    result = await embedding_registry.run(
        EmbeddingRequest(texts=[text]), provider_name=embedding_provider_name
    )
    return result.vectors[0]


async def apply_memory_actions(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    actions: list[dict[str, str]],
    embedding_registry: EmbeddingRegistry | None = None,
    embedding_provider_name: str | None = None,
) -> list[dict[str, str]]:
    """执行冲突决策器给出的记忆动作，写入 memory_items 并记审计日志。

    embedding_registry/embedding_provider_name 均为可选：提供时，ADD/UPDATE
    写入的文本会被同步 embedding，供 similarity.py 做相似度候选窄化；不提供
    则保持不写向量的旧行为。
    """
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
            embedding = await _embed_text(
                text,
                embedding_registry=embedding_registry,
                embedding_provider_name=embedding_provider_name,
            )
            await upsert_memory_item(
                conn,
                memory_id=resolved_id,
                tenant_id=tenant_id,
                user_id=user_id,
                text=text,
                embedding=embedding,
            )
            await append_history(
                conn,
                memory_id=resolved_id,
                tenant_id=tenant_id,
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
            embedding = await _embed_text(
                text,
                embedding_registry=embedding_registry,
                embedding_provider_name=embedding_provider_name,
            )
            await upsert_memory_item(
                conn,
                memory_id=memory_id,
                tenant_id=tenant_id,
                user_id=user_id,
                text=text,
                embedding=embedding,
            )
            await append_history(
                conn,
                memory_id=memory_id,
                tenant_id=tenant_id,
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
            await mark_deleted(
                conn, memory_id=memory_id, tenant_id=tenant_id, user_id=user_id
            )
            await append_history(
                conn,
                memory_id=memory_id,
                tenant_id=tenant_id,
                user_id=user_id,
                event="DELETE",
                old_text=None,
                new_text=None,
                reason=reason,
            )
            applied.append({"event": "DELETE", "memory_id": memory_id, "text": ""})

    return applied
