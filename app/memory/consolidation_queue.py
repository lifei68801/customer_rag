from __future__ import annotations

import hashlib
import uuid
from typing import Any

import aiosqlite

from app.memory.consolidation import run_memory_consolidation
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry


def _compute_dedupe_key(
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    user_input: str,
    assistant_output: str,
) -> str:
    raw = f"{tenant_id}:{user_id}:{session_id}:{user_input}:{assistant_output}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def enqueue_consolidation_job(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    user_input: str,
    assistant_output: str,
) -> str:
    """入队一个 consolidation 任务，幂等：同一个 (tenant_id, user_id, session_id,
    user_input, assistant_output) 组合重复入队只会创建一条记录（同一轮对话
    因为请求重试等原因被调用两次时，不会产生两条重复的待处理任务）。
    """
    dedupe_key = _compute_dedupe_key(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        user_input=user_input,
        assistant_output=assistant_output,
    )
    job_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO consolidation_jobs "
        "(job_id, dedupe_key, tenant_id, user_id, session_id, user_input, assistant_output) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(dedupe_key) DO NOTHING",
        (job_id, dedupe_key, tenant_id, user_id, session_id, user_input, assistant_output),
    )
    await conn.commit()
    cursor = await conn.execute(
        "SELECT job_id FROM consolidation_jobs WHERE dedupe_key = ?", (dedupe_key,)
    )
    row = await cursor.fetchone()
    return row[0]


async def list_pending_jobs(
    conn: aiosqlite.Connection, *, limit: int = 10
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM consolidation_jobs WHERE status = 'pending' "
        "ORDER BY created_at LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def mark_job_completed(conn: aiosqlite.Connection, job_id: str) -> None:
    await conn.execute(
        "UPDATE consolidation_jobs SET status='completed', updated_at=datetime('now') "
        "WHERE job_id=?",
        (job_id,),
    )
    await conn.commit()


async def mark_job_failed(
    conn: aiosqlite.Connection,
    job_id: str,
    *,
    error: str,
    max_attempts: int = 3,
) -> None:
    """失败重试计数：attempts 达到上限前退回 pending 供下次 worker 扫描重试，
    达到上限后转 dead（死信），不再被 list_pending_jobs 捞到，避免坏任务
    无限重试拖垮整个队列。
    """
    cursor = await conn.execute(
        "SELECT attempts FROM consolidation_jobs WHERE job_id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    attempts = (row[0] if row else 0) + 1
    status = "dead" if attempts >= max_attempts else "pending"
    await conn.execute(
        "UPDATE consolidation_jobs SET status=?, attempts=?, last_error=?, "
        "updated_at=datetime('now') WHERE job_id=?",
        (status, attempts, error, job_id),
    )
    await conn.commit()


async def process_pending_jobs(
    conn: aiosqlite.Connection,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    embedding_registry: EmbeddingRegistry | None = None,
    embedding_provider_name: str | None = None,
    limit: int = 10,
    max_attempts: int = 3,
) -> int:
    """扫描并处理一批待处理任务，返回成功完成的数量。

    每个任务的失败都单独捕获、单独判定重试/死信，不会因为一条任务出错
    影响同批次其它任务——这是这个 worker 函数存在的核心原因：
    memory_save_node 只管快速入队（一次 INSERT），真正耗时的事实抽取+
    冲突决策+写入由这里异步处理，不阻塞对话响应。
    """
    jobs = await list_pending_jobs(conn, limit=limit)
    processed = 0
    for job in jobs:
        try:
            await run_memory_consolidation(
                conn,
                tenant_id=job["tenant_id"],
                user_id=job["user_id"],
                user_input=job["user_input"],
                assistant_output=job["assistant_output"],
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                embedding_registry=embedding_registry,
                embedding_provider_name=embedding_provider_name,
            )
        except Exception as exc:  # noqa: BLE001 - 任何异常都要落到重试/死信逻辑
            await mark_job_failed(
                conn, job["job_id"], error=str(exc), max_attempts=max_attempts
            )
            continue
        await mark_job_completed(conn, job["job_id"])
        processed += 1
    return processed
