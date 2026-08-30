from __future__ import annotations

import argparse
import asyncio
import logging
from collections import defaultdict

import aiosqlite

from app.config.settings import Settings
from app.graphrag.duplicate_detection import find_duplicate_pairs
from app.graphrag.duplicate_review_queue import (
    enqueue_duplicate_suggestion,
    has_any_duplicate_record,
)
from app.graphrag.ontology_store import open_ontology_store_conn
from app.graphrag.tenants_store import list_tenants
from app.graphrag.terms_store import is_tombstoned, list_terms

logger = logging.getLogger(__name__)

# 每个 term_type 分组允许两两比对的最大术语数——find_duplicate_pairs 是
# O(n²) 的暴力两两比对，本仓库真实租户有 10 万+ 量级的术语（见
# neo4j_client.py "MUJI 的 SKU 18万+ 行" 的说明），不加上限的话单个大分组
# 会让一次扫描长时间不返回甚至耗尽内存。这只是一个止损性质的临时上限，
# 不是真正的可扩展匹配方案——真正的方案需要索引/分批比对，超出这次修复
# 的范围，这里只做到"超过上限就跳过整组并留日志"，不崩溃、不假装比对过了。
_MAX_BUCKET_SIZE_FOR_PAIRWISE_SCAN = 2000


async def _scan_tenant(conn: aiosqlite.Connection, tenant_id: str) -> int:
    terms = await list_terms(conn, tenant_id)
    by_term_type: dict[str, list] = defaultdict(list)
    for term in terms:
        # 已经被合并过的墓碑行（approve_duplicate_suggestion 打上的标记）不该
        # 再作为候选参与比对——它的 standard_name 字面包含被合并前的原名，
        # 短名字很容易跟它算出很高的相似度，见 is_tombstoned() 的说明。
        if is_tombstoned(term):
            continue
        by_term_type[term.term_type].append(term)

    enqueued = 0
    for term_type, term_type_terms in by_term_type.items():
        if len(term_type_terms) > _MAX_BUCKET_SIZE_FOR_PAIRWISE_SCAN:
            logger.warning(
                "租户 %r 的实体类型 %r 下有 %d 条术语，超过两两比对的临时上限 "
                "%d，本轮跳过整个分组、不做重复检测（不是真正的可扩展匹配"
                "方案，只是止损）",
                tenant_id, term_type, len(term_type_terms),
                _MAX_BUCKET_SIZE_FOR_PAIRWISE_SCAN,
            )
            continue
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
    resolved_settings = settings or Settings()
    conn = review_conn
    # 只有这次调用自己开的连接才由自己负责关闭——传入 review_conn 的调用方
    # （测试、FastAPI 依赖注入）拥有那个连接的生命周期，不该被这里关掉。
    # aiosqlite 的后台工作线程不是 daemon 线程，泄漏一个未关闭的连接会让
    # CLI 进程在逻辑上跑完之后还挂在解释器退出阶段不返回（同
    # tests/api/test_admin_duplicate_review_routes.py 的 review_conn fixture
    # 里记录的那个症状），这是这个 worker 目前唯一的生产入口，必须关。
    opened_conn_here = conn is None
    if conn is None:
        conn = await open_ontology_store_conn(resolved_settings)

    try:
        if tenant_id is not None:
            tenant_ids = [tenant_id]
        else:
            tenants = await list_tenants(conn, include_disabled=False)
            tenant_ids = [t["tenant_id"] for t in tenants]
            if not tenant_ids:
                # 租户注册表的存量回填由 app/main.py 的 lifespan 在启动时做
                # （它要同时读本体库和 ingestion 库才能发现历史 tenant_id）。
                # 这个 worker 是独立 CLI 进程，不走 lifespan——跑在一个 API
                # 进程从没启动过的库文件上时，注册表就是空的。不出声地"扫描
                # 0 个租户"跟"扫完了、确实没有重复"在输出上无法区分，是个
                # 静默失效的安全网，比没有更糟，所以这里必须留下痕迹。
                logger.warning(
                    "租户注册表为空，本次没有扫描任何租户。如果这不是预期结果，"
                    "通常说明这个库文件还没被 API 进程启动过（存量租户的回填在 "
                    "app/main.py 的 lifespan 里），或者所有租户都已被停用；"
                    "也可以用 --tenant-id 显式指定要扫描的租户绕过注册表。"
                )

        total = 0
        for tid in tenant_ids:
            total += await _scan_tenant(conn, tid)
        print(f"本次扫描 {len(tenant_ids)} 个租户，新增 {total} 条疑似重复合并建议")
        return total
    finally:
        if opened_conn_here:
            await conn.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量扫描术语表，生成疑似重复实体的合并建议")
    parser.add_argument("--tenant-id", type=str, default=None, help="只扫描指定租户，不传则扫描全部")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(tenant_id=args.tenant_id))
