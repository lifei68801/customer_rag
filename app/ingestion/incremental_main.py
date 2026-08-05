from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import aiosqlite

from app.config.settings import Settings
from app.graphrag.factory import build_graph_client_from_settings, load_terms_from_settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.review_factory import build_review_conn_from_settings
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema, process_pending_jobs
from app.ingestion.ocr_parser import OcrFunction
from app.ingestion.scan_and_enqueue import scan_and_enqueue
from app.ingestion.tracking import ensure_tracking_schema
from app.providers.embedding import EmbeddingRegistry
from app.providers.factory import (
    DEFAULT_EMBEDDING_PROVIDER_NAME,
    DEFAULT_LLM_PROVIDER_NAME,
    build_embedding_registry_from_settings,
    build_llm_registry_from_settings,
)
from app.providers.registry import ProviderRegistry
from app.retrieval.factory import build_vector_store_from_settings
from app.retrieval.vector_store import VectorStore


async def main(
    *,
    directory: Path,
    tenant_id: str,
    build_graph: bool = False,
    settings: Settings | None = None,
    ingestion_conn: aiosqlite.Connection | None = None,
    embedding_registry: EmbeddingRegistry | None = None,
    vector_store: VectorStore | None = None,
    graph_llm_registry: ProviderRegistry | None = None,
    graph_terms: list[Term] | None = None,
    graph_client: Neo4jGraphClient | None = None,
    graph_review_conn: aiosqlite.Connection | None = None,
    ocr: OcrFunction | None = None,
    limit: int = 100,
) -> dict:
    """增量摄取脚本入口：扫描目录 -> 只对新增/变更/已删除文件入队 -> 处理队列。

    和 app/ingestion/main.py（全量批处理脚本）的区别：main.py 每次都
    重新摄取目录下的所有文件；这个脚本靠 app/ingestion/tracking.py 的
    内容哈希对比，跳过没变化的文件，只处理真正变化的部分——对应架构文档
    §8"只处理变化部分，避免全量重建成本"。

    设计上不内置常驻文件监听（inotify/watchdog 那种），而是"跑一次、
    扫一次目录、处理完就退出"的脚本，供部署方用 cron/systemd timer 周期
    性调用——和 app/memory/consolidation_worker.py 是同一个模式，调度
    循环是部署层的决定，不在本仓库里内置。

    用法：
      python -m app.ingestion.incremental_main --dir path/to/docs --tenant-id t1
      python -m app.ingestion.incremental_main --dir path/to/docs --tenant-id t1 --build-graph
    """
    resolved_settings = settings or Settings()
    conn = ingestion_conn
    if conn is None:
        db_path = Path(resolved_settings.ingestion_db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(db_path))
    await ensure_tracking_schema(conn)
    await ensure_ingestion_queue_schema(conn)

    registry = embedding_registry or build_embedding_registry_from_settings(
        resolved_settings
    )
    store = vector_store or build_vector_store_from_settings(resolved_settings)

    resolved_graph_llm_registry = None
    resolved_graph_terms = None
    resolved_graph_client = None
    resolved_graph_review_conn = None
    if build_graph:
        resolved_graph_llm_registry = (
            graph_llm_registry or build_llm_registry_from_settings(resolved_settings)
        )
        resolved_graph_terms = graph_terms or load_terms_from_settings(resolved_settings)
        resolved_graph_client = graph_client or build_graph_client_from_settings(
            resolved_settings
        )
        resolved_graph_review_conn = (
            graph_review_conn or await build_review_conn_from_settings(resolved_settings)
        )

    scan_summary = await scan_and_enqueue(directory, tenant_id=tenant_id, conn=conn)
    processed = await process_pending_jobs(
        conn,
        embedding_registry=registry,
        embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=store,
        graph_llm_registry=resolved_graph_llm_registry,
        graph_llm_provider_name=DEFAULT_LLM_PROVIDER_NAME if build_graph else None,
        graph_terms=resolved_graph_terms,
        graph_client=resolved_graph_client,
        graph_review_conn=resolved_graph_review_conn,
        ocr=ocr,
        limit=limit,
    )

    print(
        f"扫描结果: 新增{scan_summary['new']} 变更{scan_summary['changed']} "
        f"删除{scan_summary['deleted']} 未变{scan_summary['unchanged']}；"
        f"本次处理完成 {processed} 个任务"
    )
    return {"scanned": scan_summary, "processed": processed}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="增量摄取：只处理变化过的文件")
    parser.add_argument("--dir", required=True, type=Path, help="待摄取的文档目录")
    parser.add_argument("--tenant-id", required=True, help="本次摄取归属的租户/产品线标识")
    parser.add_argument(
        "--build-graph",
        action="store_true",
        help="同步做 LLM 关系抽取+术语表归一化+写入 Neo4j",
    )
    parser.add_argument(
        "--limit", type=int, default=100, help="单次最多处理的任务数"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        main(
            directory=args.dir,
            tenant_id=args.tenant_id,
            build_graph=args.build_graph,
            limit=args.limit,
        )
    )
