from __future__ import annotations

import argparse
import asyncio

import aiosqlite

from app.config.settings import Settings
from app.memory.consolidation_queue import process_pending_jobs
from app.memory.factory import build_memory_conn_from_settings
from app.providers.embedding import EmbeddingRegistry
from app.providers.factory import (
    DEFAULT_EMBEDDING_PROVIDER_NAME,
    DEFAULT_LLM_PROVIDER_NAME,
    build_embedding_registry_from_settings,
    build_llm_registry_from_settings,
)
from app.providers.registry import ProviderRegistry


async def main(
    *,
    settings: Settings | None = None,
    memory_conn: aiosqlite.Connection | None = None,
    llm_registry: ProviderRegistry | None = None,
    embedding_registry: EmbeddingRegistry | None = None,
    limit: int = 10,
    max_attempts: int = 3,
) -> int:
    """异步 consolidation 队列的处理入口：跑一批（受 limit 限制）待处理任务。

    这是独立的后台 worker，不在 FastAPI 请求路径里——memory_save_node 只做
    快速入队（一次 SQLite INSERT），真正耗时的事实抽取+冲突决策+写入由这个
    脚本异步处理，不阻塞对话响应。建议部署为 cron/systemd timer 周期性
    调用，或用 supervisor 起一个常驻循环反复调用；本仓库不内置常驻循环，
    只提供"跑一批就退出"的单次入口，循环调度是部署层的决定。

    用法：python -m app.memory.consolidation_worker
    """
    resolved_settings = settings or Settings()
    conn = memory_conn or await build_memory_conn_from_settings(resolved_settings)
    llm = llm_registry or build_llm_registry_from_settings(resolved_settings)
    embedding = embedding_registry or build_embedding_registry_from_settings(
        resolved_settings
    )

    processed = await process_pending_jobs(
        conn,
        llm_registry=llm,
        llm_provider_name=DEFAULT_LLM_PROVIDER_NAME,
        embedding_registry=embedding,
        embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        limit=limit,
        max_attempts=max_attempts,
    )
    print(f"本次处理 {processed} 个 consolidation 任务")
    return processed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="处理待处理的记忆 consolidation 队列任务")
    parser.add_argument("--limit", type=int, default=10, help="单次最多处理的任务数")
    parser.add_argument(
        "--max-attempts", type=int, default=3, help="失败重试上限，超过转入死信"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(limit=args.limit, max_attempts=args.max_attempts))
