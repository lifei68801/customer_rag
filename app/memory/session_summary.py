"""会话滑窗被压缩掉的那段历史，用 LLM 摘成一段话存起来。

## 它替代了什么

`compaction.compact_messages` 把超出滑窗的消息压成一条统计摘要——"共压缩
12 条历史消息（user=6, assistant=6）"。那句话保住了条数，语义全丢：用户在
第 3 轮说过"我要的是 2024 年的数据"，到第 12 轮模型已经不知道了，而上下文
里只写着"压缩了 12 条"。

## 为什么不在回答链路里直接摘

`inject_memory_context` 跑在 `memory_recall_node` 里，是**回答生成之前必须
等完的一跳**。在那里同步调一次 LLM，等于给每一轮超出滑窗的对话都加上一次
模型往返——刚把首字延迟从 0.9 秒压到 0.4 秒，不该在这里加回去。

所以摘要是**离线生成、在线只读**：每轮对话之后由 worker 增量地把新压缩掉
的几条追加进已有摘要，存在 `session_summaries` 里；问答时只做一次主键查询。
缓存没跟上（worker 还没跑、或刚失败）时回落到统计摘要——降级后的行为跟
接入前逐字相同，不会因为摘要这条路不通就让问答失败。

## 增量而不是每次全量重摘

会话会一直变长，全量重摘的成本随轮数线性增长，而且每次摘出来的措辞都可能
不一样，同一段历史在不同轮次里对模型呈现出不同的说法。增量只摘"上次摘完
之后新压缩掉的那几条"，旧摘要原样带进提示词，成本恒定。

`covered_through_turn_id` 记的是"摘到哪一条为止"（conversation_turns.id）。
它同时是**去重依据**：问答时只把 id 大于它的轮次原样放进上下文，已经进了
摘要的那些不再重复出现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

from app.memory.llm_call import run_llm_text
from app.providers.base import ProviderRequest

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_summaries (
    tenant_id               TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    summary                 TEXT NOT NULL,
    -- conversation_turns.id：摘到这一条为止（含）。问答时只把 id 大于它的
    -- 轮次原样放进上下文，已经进了摘要的不再重复。
    covered_through_turn_id INTEGER NOT NULL,
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, session_id)
);
"""

_SYSTEM_PROMPT = (
    "你是对话摘要器。把下面这段客服对话压缩成一段中文摘要，供后续轮次作为上下文使用。"
    "必须保留：用户提出的诉求和约束条件、已经确认的事实和数字、做过的决定、"
    "还没解决的问题。可以丢弃：寒暄、重复表述、已经被后续内容推翻的说法。"
    "不要编造对话里没有的内容，不要输出任何解释或前缀，直接给摘要正文。"
    "控制在 300 字以内。"
)


@dataclass(frozen=True)
class SessionSummary:
    summary: str
    covered_through_turn_id: int


async def ensure_session_summary_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def get_session_summary(
    conn: aiosqlite.Connection, *, tenant_id: str, session_id: str
) -> SessionSummary | None:
    """读这个会话的摘要。表还没建时当作"没有摘要"，回落统计摘要。

    不在这里建表：问答链路上的读操作不该有建表这种副作用，而且它每轮都跑。
    """
    try:
        cursor = await conn.execute(
            "SELECT summary, covered_through_turn_id FROM session_summaries "
            "WHERE tenant_id = ? AND session_id = ?",
            (tenant_id, session_id),
        )
    except aiosqlite.OperationalError:
        return None
    row = await cursor.fetchone()
    if row is None:
        return None
    return SessionSummary(summary=row[0], covered_through_turn_id=int(row[1]))


async def save_session_summary(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    summary: str,
    covered_through_turn_id: int,
) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO session_summaries "
        "(tenant_id, session_id, summary, covered_through_turn_id, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (tenant_id, session_id, summary, covered_through_turn_id),
    )
    await conn.commit()


def plan_summary_update(
    turns: list[dict[str, Any]],
    *,
    preserve_recent_messages: int,
    covered_through_turn_id: int,
) -> list[dict[str, Any]]:
    """这一次该把哪几条追加进摘要。空列表表示无事可做。

    判据跟问答那一侧**必须一致**：最近 preserve_recent_messages 条原样保留，
    再往前的才进摘要。两边各写一遍的话，会出现"摘要已经摘了第 9 条、而问答
    仍然把第 9 条原样放进上下文"——同一句话在上下文里出现两次。

    turns 按 id 升序，包含这个会话的**全部**轮次（不是滑窗内的那些）。
    """
    if preserve_recent_messages < 0:
        raise ValueError("preserve_recent_messages 不能为负")
    cutoff = len(turns) - preserve_recent_messages
    if cutoff <= 0:
        return []
    return [t for t in turns[:cutoff] if int(t["id"]) > covered_through_turn_id]


def render_turns(turns: list[dict[str, Any]]) -> str:
    """把轮次渲染成喂给摘要器的文本。"""
    role_names = {"user": "用户", "assistant": "助手", "system": "系统"}
    return "\n".join(f"{role_names.get(t['role'], t['role'])}：{t['content']}" for t in turns)


async def summarize_turns(
    *,
    previous_summary: str | None,
    turns: list[dict[str, Any]],
    llm_registry,
    llm_provider_name: str,
    timeout_sec: float = 20.0,
) -> str | None:
    """把新压缩掉的几条追加进摘要，返回新摘要；失败/超时返回 None。

    超时给 20 秒而不是问答链路惯用的 2 秒：这是离线任务，没有用户在等，用
    更宽的超时换取更高的成功率是合算的（同 llm_extractor 里摄取路径的取舍）。

    返回 None 时调用方**不更新** covered_through_turn_id——下次重试会把这几条
    连同再新的一起摘，不会漏掉任何一段历史。
    """
    if not turns:
        return None
    parts = []
    if previous_summary:
        parts.append(f"已有摘要：\n{previous_summary}\n")
        parts.append("下面是这段摘要之后新发生的对话，请把它合并进摘要，输出合并后的完整摘要：")
    else:
        parts.append("请为下面这段对话生成摘要：")
    parts.append(render_turns(turns))

    text = await run_llm_text(
        llm_registry=llm_registry,
        request=ProviderRequest(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(parts)},
            ]
        ),
        provider_name=llm_provider_name,
        timeout_sec=timeout_sec,
        label="会话摘要",
        fallback_label="保留上一版摘要，下次重试",
    )
    if text is None:
        return None
    cleaned = text.strip()
    return cleaned or None
