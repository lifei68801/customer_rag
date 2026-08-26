from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

import aiosqlite

from app.config.settings import Settings
from app.graphrag.duplicate_detection import find_duplicate_pairs
from app.graphrag.duplicate_review_queue import (
    enqueue_duplicate_suggestion,
    has_any_duplicate_record,
)
from app.graphrag.terms_store import list_terms


async def _scan_tenant(conn: aiosqlite.Connection, tenant_id: str) -> int:
    terms = await list_terms(conn, tenant_id)
    by_term_type: dict[str, list] = defaultdict(list)
    for term in terms:
        by_term_type[term.term_type].append(term)

    enqueued = 0
    for term_type_terms in by_term_type.values():
        for term_a, term_b, score in find_duplicate_pairs(term_type_terms):
            already_exists = await has_any_duplicate_record(
                conn, tenant_id=tenant_id,
                candidate_a_node_key=term_a.node_key, candidate_b_node_key=term_b.node_key,
            )
            if already_exists:
                continue
            await enqueue_duplicate_suggestion(
                conn, tenant_id=tenant_id,
                candidate_a_node_key=term_a.node_key, candidate_b_node_key=term_b.node_key,
                similarity_score=score,
                reason=f"相似度 {score:.2f}：{term_a.standard_name!r} / {term_b.standard_name!r}",
            )
            enqueued += 1
    return enqueued


async def main(
    *,
    settings: Settings | None = None,
    review_conn: aiosqlite.Connection | None = None,
    tenant_id: str | None = None,
) -> int:
    """扫描全量术语表，按 term_type 分组两两比对相似度，超阈值且尚未有
    pending/rejected 记录的候选对写入 duplicate_review_queue。返回本次
    新增的建议条数。tenant_id=None 时遍历全部启用租户。

    用法：python -m app.graphrag.duplicate_detection_worker
    """
    from app.api.deps import get_review_conn  # 延迟 import 避免循环依赖

    resolved_settings = settings or Settings()
    conn = review_conn
    if conn is None:
        conn = await get_review_conn(resolved_settings)

    if tenant_id is not None:
        tenant_ids = [tenant_id]
    else:
        from app.graphrag.tenants_store import list_tenants

        tenants = await list_tenants(conn, include_disabled=False)
        tenant_ids = [t["tenant_id"] for t in tenants]

    total = 0
    for tid in tenant_ids:
        total += await _scan_tenant(conn, tid)
    return total


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量扫描术语表，生成疑似重复实体的合并建议")
    parser.add_argument("--tenant-id", type=str, default=None, help="只扫描指定租户，不传则扫描全部")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(tenant_id=args.tenant_id))
