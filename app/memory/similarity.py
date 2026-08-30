from __future__ import annotations

from typing import Any

import aiosqlite

from app.memory.memory_store import list_active_memory_items
from app.retrieval.vector_math import cosine_similarity



async def find_similar_memory_items(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    query_vector: list[float],
    top_k: int,
) -> list[dict[str, Any]]:
    """从该用户全部 active 记忆条目里，按 embedding 余弦相似度取 Top-K 最相似的。

    没有引入独立的向量库/ANN 索引——记忆条目通常是单用户量级（几十到几百条），
    全量拉取后在 Python 里算余弦相似度足够快；如果未来单用户记忆条目规模膨胀
    到需要专门的近似最近邻索引，再引入向量库，不为此提前上基础设施。

    没有 embedding 的条目（历史遗留数据、或从未写过向量的记录）直接跳过，
    不参与排序，不会因为"缺向量"而拿到一个错误的相似度分数。
    """
    items = await list_active_memory_items(conn, tenant_id=tenant_id, user_id=user_id)
    scored = [
        (cosine_similarity(query_vector, item["embedding"]), item)
        for item in items
        if item.get("embedding") is not None
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:top_k]]
