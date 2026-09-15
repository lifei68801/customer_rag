from __future__ import annotations

from typing import Any

import aiosqlite

from app.memory.compaction import compact_messages
from app.memory.memory_store import list_active_memory_items
from app.memory.recall import recall_memory_items
from app.memory.session_summary import get_session_summary
from app.memory.session_window import get_turns_with_ids
from app.providers.embedding import EmbeddingRegistry
from app.safety.rules import UNSAFE_INPUT_MESSAGE, UNSAFE_OUTPUT_MESSAGE

# 历史里的安全兜底文案，喂给 LLM 之前换成这句说明。
#
# 兜底文案会原样作为助手回答存进会话轮次（memory_save_node 写的是用户实际
# 看到的 final_text，会话列表也靠它回放）。原样喂回去的话，模型会把它当成
# 自己上一轮的回答照抄：demo 租户一次回答被语义审查误拦之后，同一会话里
# 再问同一个问题，模型一个工具都没调，直接复述"抱歉，生成的回答未通过安全
# 审查"——那一轮根本没有经过拦截，日志里也没有任何审查记录。
#
# 替换而不是删掉：删掉会让历史里出现连续两条 user 消息，一问一答的交替
# 断掉。替换成的是一句事实陈述，不是指令，也不含原文案里的任何字句。
_SAFETY_FALLBACK_REPLIES = frozenset({UNSAFE_OUTPUT_MESSAGE, UNSAFE_INPUT_MESSAGE})
SAFETY_FALLBACK_HISTORY_NOTE = "（这一轮没有给出回答：内容被安全检查拦下了。）"


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
    use_embedding_recall: bool = True,
) -> list[dict[str, Any]]:
    """组装记忆上下文消息：长期记忆条目 + （压缩后的）近期会话轮次。

    question 提供时，长期记忆条目改用 recall_memory_items() 的多源召回
    融合+MMR 结果（跟当前问题的相关性排序），而不是"全部 active 条目按
    更新时间截断"；question 缺失则保留旧行为，向后兼容不传这个参数的
    调用方。

    use_embedding_recall=False 时 recall_memory_items 只用 BM25 关键词排名，
    不需要 embedding_registry/embedding_provider_name（可以不传）——见
    MemorySettings.recall_use_embedding 的说明。
    """
    if question is not None and (
        use_embedding_recall is False
        or (embedding_registry is not None and embedding_provider_name is not None)
    ):
        memory_items = await recall_memory_items(
            conn,
            tenant_id=tenant_id,
            user_id=user_id,
            question=question,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            top_k=memory_item_limit,
            use_embedding=use_embedding_recall,
        )
    else:
        memory_items = await list_active_memory_items(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        memory_items = memory_items[:memory_item_limit]

    turns = await get_turns_with_ids(
        conn, tenant_id=tenant_id, session_id=session_id, limit=recent_turn_limit
    )
    # 已经进了摘要的轮次不再原样带进来——否则同一句话在上下文里出现两次
    # （一次在摘要里，一次是原文）。摘要缺失时 covered=0，等于全都保留，
    # 行为跟接入摘要之前逐字相同。
    summary = await get_session_summary(conn, tenant_id=tenant_id, session_id=session_id)
    covered = summary.covered_through_turn_id if summary else 0
    turn_messages = [
        {
            "role": t["role"],
            "content": (
                SAFETY_FALLBACK_HISTORY_NOTE
                if t["role"] == "assistant" and t["content"] in _SAFETY_FALLBACK_REPLIES
                else t["content"]
            ),
        }
        for t in turns
        if int(t["id"]) > covered
    ]
    if summary is not None:
        # 有 LLM 摘要就用它，不再叠统计摘要：两条摘要说同一段历史，其中一条
        # 只说得出条数，放在一起只会稀释另一条。
        compacted_turns = [
            {"role": "system", "content": f"前面的对话摘要：{summary.summary}"},
            *turn_messages,
        ]
    else:
        # 没有摘要（worker 还没跑过、或这个会话还没长到要压缩）时，退回统计
        # 摘要——降级后的行为跟接入前逐字相同。
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
