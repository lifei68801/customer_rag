"""离线刷新会话摘要：把每个会话新压缩掉的那几轮追加进它的摘要。

跟 consolidation_worker.py 同一个部署模式：不内置常驻循环，只提供"跑一批
就退出"的单次入口，循环调度是部署层的决定（cron / systemd timer /
supervisor 反复调用）。

用法：python -m app.memory.session_summary_worker [--limit N]

为什么是离线的：问答链路上 inject_memory_context 是回答生成之前必须等完的
一跳，在那里同步摘要等于给每一轮超出滑窗的对话加一次模型往返。摘要在这里
生成、存进 session_summaries，问答时只做一次主键查询；这里没跑或跑失败时，
问答会回落到统计摘要，不会失败。
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import aiosqlite

from app.config.settings import Settings
from app.memory.factory import build_memory_conn_from_settings
from app.memory.session_summary import (
    ensure_session_summary_schema,
    get_session_summary,
    plan_summary_update,
    save_session_summary,
    summarize_turns,
)
from app.memory.session_window import get_turns_with_ids
from app.providers.factory import (
    DEFAULT_LLM_PROVIDER_NAME,
    build_llm_registry_from_settings,
)
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

#: 跟 inject_memory_context 的默认值一致。两边不一致会让同一条轮次要么在
#: 上下文里出现两次（摘要摘早了），要么消失（摘晚了）。
DEFAULT_PRESERVE_RECENT_MESSAGES = 8


async def _list_sessions(conn: aiosqlite.Connection) -> list[tuple[str, str]]:
    """有对话轮次的 (tenant_id, session_id)，按最近活跃排前面。

    从 conversation_turns 取而不是 chat_sessions：摘要的输入就是轮次，
    chat_sessions 是会话列表的元信息表，两者可能不同步（历史数据里有轮次
    但没有会话行）。
    """
    cursor = await conn.execute(
        "SELECT tenant_id, session_id, MAX(id) AS last_id FROM conversation_turns "
        "GROUP BY tenant_id, session_id ORDER BY last_id DESC"
    )
    return [(row[0], row[1]) for row in await cursor.fetchall()]


async def refresh_session_summary(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    preserve_recent_messages: int = DEFAULT_PRESERVE_RECENT_MESSAGES,
) -> bool:
    """刷新一个会话的摘要。返回是否真的写入了新摘要。

    LLM 失败时**不推进** covered_through_turn_id：下次重试会把这几条连同
    再新的一起摘，不会漏掉任何一段历史。
    """
    turns = await get_turns_with_ids(conn, tenant_id=tenant_id, session_id=session_id)
    existing = await get_session_summary(conn, tenant_id=tenant_id, session_id=session_id)
    covered = existing.covered_through_turn_id if existing else 0
    pending = plan_summary_update(
        turns,
        preserve_recent_messages=preserve_recent_messages,
        covered_through_turn_id=covered,
    )
    if not pending:
        return False

    summary = await summarize_turns(
        previous_summary=existing.summary if existing else None,
        turns=pending,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
    )
    if summary is None:
        return False

    await save_session_summary(
        conn,
        tenant_id=tenant_id,
        session_id=session_id,
        summary=summary,
        covered_through_turn_id=int(pending[-1]["id"]),
    )
    logger.info(
        "会话摘要已更新：tenant=%s session=%s 新摘入 %d 条，摘到 id=%s",
        tenant_id, session_id, len(pending), pending[-1]["id"],
    )
    return True


async def main(
    *,
    settings: Settings | None = None,
    memory_conn: aiosqlite.Connection | None = None,
    llm_registry: ProviderRegistry | None = None,
    limit: int = 20,
    preserve_recent_messages: int = DEFAULT_PRESERVE_RECENT_MESSAGES,
) -> int:
    """跑一批：最多刷新 limit 个会话的摘要，返回真正更新了几个。"""
    resolved_settings = settings or Settings()
    conn = memory_conn or await build_memory_conn_from_settings(resolved_settings)
    llm = llm_registry or build_llm_registry_from_settings(resolved_settings)
    await ensure_session_summary_schema(conn)

    updated = 0
    for tenant_id, session_id in (await _list_sessions(conn))[:limit]:
        if await refresh_session_summary(
            conn,
            tenant_id=tenant_id,
            session_id=session_id,
            llm_registry=llm,
            llm_provider_name=DEFAULT_LLM_PROVIDER_NAME,
            preserve_recent_messages=preserve_recent_messages,
        ):
            updated += 1
    print(f"本次更新 {updated} 个会话摘要")
    return updated


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线刷新会话摘要")
    parser.add_argument("--limit", type=int, default=20, help="单次最多处理的会话数")
    parser.add_argument(
        "--preserve-recent",
        type=int,
        default=DEFAULT_PRESERVE_RECENT_MESSAGES,
        help="最近多少条保留原文、不进摘要（必须跟问答侧一致）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(limit=args.limit, preserve_recent_messages=args.preserve_recent))
