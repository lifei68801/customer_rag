from __future__ import annotations

import json
from typing import Any

import aiosqlite

#: 单个工具结果的 content 上限（字符）。structured_filter_query 命中上千条
#: 时 content 会很大，全量存会让库迅速膨胀。截断而不是丢弃：工具名、参数、
#: 前面那部分结果通常已经够定位问题。
CONTENT_LIMIT = 8192

#: 每个租户保留的诊断记录条数。无上限增长会撑爆内存库，而诊断的对象是
#: 「最近答错的那次」——三个月前的问答已经无从对照当时的数据了。
RETENTION_PER_TENANT = 500


def _truncate(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """超长的 content 截断并打标。

    标记是必须的：排查的人看到一段结果，会默认那就是全部，据此得出「只匹配
    到 3 条」这样的结论。被截断却不说，比不存更糟。

    没被截断的**不带**这个字段——每条都带的话，这个信号就没有意义了。
    """
    out: list[dict[str, Any]] = []
    for result in tool_results:
        content = str(result.get("content", ""))
        if len(content) <= CONTENT_LIMIT:
            out.append(dict(result))
            continue
        item = dict(result)
        item["content"] = content[:CONTENT_LIMIT]
        item["content_truncated"] = True
        out.append(item)
    return out


async def record_diagnostic(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    question: str,
    resolved_question: str | None,
    answer: str,
    used_sources: list[str],
    tool_results: list[dict[str, Any]],
) -> int:
    """存一次问答的诊断快照。

    「答错了」反查实体是这个项目里发现数据问题的主路径，但当时用了哪些工具、
    匹配到哪些实体，此前只活在内存里，一轮对话结束就没了。重跑不能替代——
    LLM 非确定性，可能复现不出那个错误，你会对着一个正确的结果找不到问题。

    存全量（只截超长的 content）而不是预先挑字段：诊断的场景就是「不知道
    问题在哪」，预先裁剪等于预判了问题在哪。
    """
    cursor = await conn.execute(
        "INSERT INTO qa_diagnostics (tenant_id, session_id, question, resolved_question,"
        " answer, used_sources, tool_results) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            tenant_id,
            session_id,
            question,
            resolved_question,
            answer,
            json.dumps(used_sources, ensure_ascii=False),
            json.dumps(_truncate(tool_results), ensure_ascii=False),
        ),
    )
    diagnostic_id = cursor.lastrowid
    # 按租户各自保留，一个高频租户不该把别人的记录挤掉。
    await conn.execute(
        "DELETE FROM qa_diagnostics WHERE tenant_id = ? AND id NOT IN ("
        "  SELECT id FROM qa_diagnostics WHERE tenant_id = ? ORDER BY id DESC LIMIT ?"
        ")",
        (tenant_id, tenant_id, RETENTION_PER_TENANT),
    )
    await conn.commit()
    return int(diagnostic_id or 0)


async def list_diagnostics(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    """列出诊断记录，最近的排最前面——入口是「刚才那次答错了」。

    列表不带 tool_results：那是详情才需要的大字段，列进来会让每次翻页都
    传上几百 KB。
    """
    conn.row_factory = aiosqlite.Row
    where = "tenant_id = ?"
    params: list[Any] = [tenant_id]
    if session_id is not None:
        where += " AND session_id = ?"
        params.append(session_id)
    # SQLite 的 LIMIT 取负数表示不限制，用 -1 承载 limit=None，跟
    # terms_store.list_terms / tracking.list_tracked_files 一致。
    params.append(limit if limit is not None else -1)
    cursor = await conn.execute(
        "SELECT id, session_id, question, answer, created_at FROM qa_diagnostics "
        f"WHERE {where} ORDER BY id DESC LIMIT ?",
        params,
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_diagnostic(
    conn: aiosqlite.Connection, *, tenant_id: str, diagnostic_id: int
) -> dict[str, Any] | None:
    """取一条诊断的完整内容。tenant_id 是条件不是断言——拿别的租户的 id
    过来必须查不到，不能只靠调用方自觉。"""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT id, session_id, question, resolved_question, answer, used_sources,"
        " tool_results, created_at FROM qa_diagnostics WHERE tenant_id = ? AND id = ?",
        (tenant_id, diagnostic_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    record = dict(row)
    record["used_sources"] = json.loads(record["used_sources"])
    record["tool_results"] = json.loads(record["tool_results"])
    return record
