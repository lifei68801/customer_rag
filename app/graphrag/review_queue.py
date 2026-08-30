from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

import aiosqlite

from app.db_migrations import add_column_if_missing
from app.graphrag.provenance import HUMAN_APPROVED
from app.graphrag.relation_writer import RelationWriterProtocol
from app.graphrag.ontology import Term, find_candidate_term_types, resolve_term

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_review_queue (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_candidate TEXT NOT NULL,
    object_candidate TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    suggested_subject_standard_name TEXT,
    suggested_object_standard_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_review_queue_status
    ON graph_review_queue (status);
"""


class ReviewGraphClientProtocol(RelationWriterProtocol, Protocol):
    """人工审核批准路径只需要写一条关系边，方法集合恰好就是基协议本身。
    保留这个消费方自己的名字（而不是直接用 RelationWriterProtocol），是为了
    让 approve_review 的签名继续说明"这里要的是审核路径的图客户端"。"""


class ReviewNotFoundError(Exception):
    """指定的 review_id 在该租户下不存在（包括存在于别的租户名下的情况——
    不做区分，统一按"不存在"处理，避免向调用方泄露"这个 id 属于别的租户"
    这个信息）。"""


class ReviewAlreadyResolvedError(Exception):
    """指定的 review_id 已经被批准或驳回过，不能重复处理。"""


class StandardNameNotInTermsError(Exception):
    """人工确认的标准名（subject 或 object）没能唯一解析到术语表里的一条
    术语——阻止绕开封闭词表的强约束、把术语表里没有的任意字符串当成新
    术语写进图谱。消息文案会区分三种情况：这个名字（或别名）在术语表里
    根本不存在；或者存在，但同名/同别名分布在多个 term_type 下、没有
    （或没有匹配上）类型提示时无法确定唯一一条；或者存在，但集中在同一个
    term_type 下（术语表本身出现了别名冲突），传类型提示也解决不了，见
    _standard_name_not_found_message 的说明。前端自动补全只是体验层面的
    约束，这里才是真正的安全边界：API 路由和 review_cli.py 两个批准入口
    都调用同一个 approve_review()，校验只需要加在这一处。"""


class RelationNotInConfirmedOntologyError(Exception):
    """人工确认的候选关系，其 relation_type 不在该租户已确认的
    tenant_relation_types 里，或 (subject.term_type, relation_type,
    object.term_type) 组合不在已确认的 term_type_relation_allowlist 里。

    自动写入路径（normalize_and_write_relations）早就做了同一层校验——
    两侧实体精确对齐术语表之后，还要求关系类型/类型组合落在租户已确认
    的本体范围内，不满足就降级转人工审核而不是直接写图谱。approve_review
    是候选关系最终真正写入图谱的唯一入口，人工批准同样不能绕开这条闸门，
    否则审核员可以把任意关系类型/类型组合写进图谱，让"只写入已确认本体
    范围内的关系"这条不变量在人工审核路径上形同虚设。校验失败时不写图谱、
    也不改变 review 状态（仍是 pending），与 StandardNameNotInTermsError
    的行为保持一致，方便审核员改对后重新提交。"""


def _standard_name_not_found_message(field_name: str, standard_name: str, terms: list[Term]) -> str:
    """给 StandardNameNotInTermsError 生成消息：区分三种情况——这个名字
    在术语表里根本不存在；存在，但同名/同别名分布在多个 term_type 下，
    需要传入 term_type 提示才能确定唯一一条；存在，但集中在同一个
    term_type 下（比如术语表里出现了跨条目的别名冲突），传类型提示也
    解决不了，需要人工核对术语表本身。"""
    candidate_types = find_candidate_term_types(standard_name, terms)
    if not candidate_types:
        return f"{field_name} 不在术语表中: {standard_name!r}"
    if len(candidate_types) == 1:
        return (
            f"{field_name} {standard_name!r} 在类型 {candidate_types[0]!r} 下存在多条同名/同别名的术语，"
            "无法自动确定唯一一条，需要人工核对术语表、修正冲突后再重试"
        )
    return (
        f"{field_name} {standard_name!r} 存在歧义：同名/同别名的术语分布在 "
        f"{candidate_types} 多个类型下，需要传入对应的 term_type 提示才能确定唯一一条"
    )


async def ensure_review_schema(conn: aiosqlite.Connection) -> None:
    """幂等建表+迁移，可重复调用。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    # tenant_id 迁移历史数据默认回填 'demo'——项目里目前唯一真实产生过
    # 数据的租户就是 demo，见 docs/superpowers/specs/2026-08-08-admin-backend-design.md 第2节。
    await add_column_if_missing(
        conn, table="graph_review_queue", column="tenant_id",
        ddl="TEXT NOT NULL DEFAULT 'demo'",
    )
    # source 记录候选关系抽取自哪个文档，approve_review 批准时要把它传给
    # graph_client.merge_relation()（写入图谱边的 source 属性，用于文档
    # 重新摄取时按 source 清理旧边）。历史数据没有这个信息，回填空字符串
    # （不是 NULL，避免下游拼接/比较时到处判空）。
    await add_column_if_missing(
        conn, table="graph_review_queue", column="source",
        ddl="TEXT NOT NULL DEFAULT ''",
    )
    # evidence 记录支持这条候选关系的原文引用（见 llm_extractor.py），
    # 人工审核时用来判断"文档里是不是真的这么说的"——历史数据没有这个
    # 信息，回填空字符串（不是 NULL，跟 source 同样的理由：下游拼接/
    # 展示时不用到处判空）。
    await add_column_if_missing(
        conn, table="graph_review_queue", column="evidence",
        ddl="TEXT NOT NULL DEFAULT ''",
    )
    # subject_type_candidate/object_type_candidate 记录 LLM 抽取阶段给出的
    # 候选实体类型（已经是从该租户已确认 term_type 集合里选出的合法值，见
    # llm_extractor.py）——供审核页内联创建实体时预填表单的 term_type 下拉。
    # 历史候选行没有这个信息，回填 NULL（不是空字符串：空字符串会被
    # 内联创建表单误当成"选中了一个空选项"，NULL 更准确地表达"未知/不适用"）。
    await add_column_if_missing(
        conn, table="graph_review_queue", column="subject_type_candidate", ddl="TEXT",
    )
    await add_column_if_missing(
        conn, table="graph_review_queue", column="object_type_candidate", ddl="TEXT",
    )
    # tenant_id 是后加的列，不在 _SCHEMA_SQL 的建表语句里——这个复合索引
    # 必须放在上面两次 add_column_if_missing 之后创建，否则全新数据库上
    # 建表时 tenant_id 列还不存在，CREATE INDEX 会报 "no such column"。
    # list_pending_reviews/list_resolved_reviews 的查询都是
    # WHERE tenant_id = ? AND status = ...，复合索引比单独的 status 索引更贴合。
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_review_queue_tenant_status "
        "ON graph_review_queue (tenant_id, status)"
    )
    # 分页查询历史记录时 ORDER BY resolved_at DESC 需要排序，上面的
    # (tenant_id, status) 索引只能命中过滤条件、排序仍需额外一步；这个
    # 三列复合索引让 tenant_id+status 精确匹配的历史记录分页查询可以直接
    # 走索引有序扫描，不用每次都对候选行做一次排序。
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_review_queue_tenant_status_resolved "
        "ON graph_review_queue (tenant_id, status, resolved_at)"
    )
    await conn.commit()


async def enqueue_for_review(
    conn: aiosqlite.Connection,
    *,
    subject_candidate: str,
    object_candidate: str,
    relation_type: str,
    reason: str,
    source: str,
    tenant_id: str,
    suggested_subject_standard_name: str | None = None,
    suggested_object_standard_name: str | None = None,
    evidence: str = "",
    subject_type_candidate: str | None = None,
    object_type_candidate: str | None = None,
) -> int:
    """把未能对齐术语表（或关系类型不合法）的候选关系存入待审核队列，返回 review_id。

    source/tenant_id 是批准时写入图谱边所必需的信息，来自调用方
    normalize_and_write_relations() 本身已有的同名参数，这里改为必填，
    不给默认值——遗漏它们会让批准动作在写图谱这一步直接失败。

    evidence 是抽取阶段 LLM 给出的原文引用（见 llm_extractor.py），给
    人工审核用；默认空字符串——不是所有调用方都一定拿得到这个信息
    （比如未来可能有的非 LLM 抽取来源），不强制要求。

    subject_type_candidate/object_type_candidate 是 LLM 抽取阶段给出的候选
    实体类型（见 llm_extractor.py 的动态 prompt 改造），默认 None——不是所有
    调用方都一定拿得到（比如未来可能有的非 LLM 抽取来源），不强制要求。
    """
    cursor = await conn.execute(
        "INSERT INTO graph_review_queue "
        "(subject_candidate, object_candidate, relation_type, reason, "
        "suggested_subject_standard_name, suggested_object_standard_name, "
        "source, tenant_id, evidence, subject_type_candidate, object_type_candidate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            subject_candidate,
            object_candidate,
            relation_type,
            reason,
            suggested_subject_standard_name,
            suggested_object_standard_name,
            source,
            tenant_id,
            evidence,
            subject_type_candidate,
            object_type_candidate,
        ),
    )
    await conn.commit()
    return cursor.lastrowid


async def list_pending_reviews(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """limit=None（默认）返回该租户全部待审核记录，保持 review_cli.py 等
    既有调用方不传这两个参数时的行为不变；管理后台分页时显式传入具体的
    limit/offset。SQLite 的 LIMIT 取负数即表示不限制行数，用 -1 承载
    limit=None 这个语义，不需要为"要不要拼 LIMIT 子句"写分支 SQL。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT review_id, subject_candidate, object_candidate, relation_type, "
        "reason, suggested_subject_standard_name, suggested_object_standard_name, "
        "source, evidence, created_at, subject_type_candidate, object_type_candidate "
        "FROM graph_review_queue "
        "WHERE status = 'pending' AND tenant_id = ? ORDER BY review_id LIMIT ? OFFSET ?",
        (tenant_id, limit if limit is not None else -1, offset),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def list_resolved_reviews(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """查已批准/已驳回的历史记录，按 resolved_at 倒序（最近处理的排前面）。

    status 为 None 时返回 approved+rejected 两种都算"已处理"的记录；
    传 'approved'/'rejected' 时只看其中一种。
    """
    conn.row_factory = aiosqlite.Row
    if status is None:
        cursor = await conn.execute(
            "SELECT review_id, subject_candidate, object_candidate, relation_type, "
            "reason, status, resolved_at, resolved_note, source, evidence, created_at, "
            "subject_type_candidate, object_type_candidate "
            "FROM graph_review_queue "
            "WHERE tenant_id = ? AND status IN ('approved', 'rejected') "
            "ORDER BY resolved_at DESC, review_id DESC LIMIT ? OFFSET ?",
            (tenant_id, limit, offset),
        )
    else:
        cursor = await conn.execute(
            "SELECT review_id, subject_candidate, object_candidate, relation_type, "
            "reason, status, resolved_at, resolved_note, source, evidence, created_at, "
            "subject_type_candidate, object_type_candidate "
            "FROM graph_review_queue "
            "WHERE tenant_id = ? AND status = ? "
            "ORDER BY resolved_at DESC, review_id DESC LIMIT ? OFFSET ?",
            (tenant_id, status, limit, offset),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def count_pending_reviews(conn: aiosqlite.Connection, *, tenant_id: str) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM graph_review_queue WHERE status = 'pending' AND tenant_id = ?",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return row[0]


async def count_resolved_reviews(
    conn: aiosqlite.Connection, *, tenant_id: str, status: str | None = None
) -> int:
    """status=None 统计 approved+rejected 两种之和，语义与 list_resolved_reviews 一致。"""
    if status is None:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM graph_review_queue "
            "WHERE tenant_id = ? AND status IN ('approved', 'rejected')",
            (tenant_id,),
        )
    else:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM graph_review_queue WHERE tenant_id = ? AND status = ?",
            (tenant_id, status),
        )
    row = await cursor.fetchone()
    return row[0]


async def _fetch_pending_row(
    conn: aiosqlite.Connection, review_id: int, *, tenant_id: str
) -> dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM graph_review_queue WHERE review_id = ? AND tenant_id = ?",
        (review_id, tenant_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ReviewNotFoundError(f"待审核记录不存在: {review_id}")
    row_dict = dict(row)
    if row_dict["status"] != "pending":
        raise ReviewAlreadyResolvedError(
            f"待审核记录已处理过 (status={row_dict['status']}): {review_id}"
        )
    return row_dict


async def approve_review(
    conn: aiosqlite.Connection,
    *,
    review_id: int,
    subject_standard_name: str,
    object_standard_name: str,
    tenant_id: str,
    graph_client: ReviewGraphClientProtocol,
    terms: list[Term],
    now: datetime,
    confirmed_relation_types: set[str],
    allowed_combinations: set[tuple[str, str, str]],
    subject_term_type_hint: str | None = None,
    object_term_type_hint: str | None = None,
) -> None:
    """人工确认候选关系对应的标准名称后，写入图谱并把队列状态标记为已批准。

    subject_term_type_hint/object_term_type_hint 是可选的类型提示（通常
    来自 LLM 抽取时给出的 subject_type_candidate/object_type_candidate，
    见 app/api/admin_graph_review_routes.py 的调用方）：传了且该类型下
    确实存在这个 standard_name（本身或某个 alias），就精确解析到那一条；
    没传、或者传了但那个类型下没有，退回"这个名字（或某个别名）在全部
    术语里是否唯一"——唯一就照样解析成功（2026-08-22 之前的默认行为，
    标准名当年不可能重复，这个场景下结果不变），不唯一就拒绝
    （StandardNameNotInTermsError），不会像旧实现那样从多个候选里随便
    选一个。见 app/graphrag/ontology.py::resolve_term 的完整说明。

    写入的边标记 provenance=HUMAN_APPROVED（见 app/graphrag/provenance.py）
    ——这是这条边第一次、也是唯一一次被写入图谱的时刻（进了审核队列的
    候选，在此之前从未调用过 merge_relation），与自动写入路径共用同一个
    Neo4jGraphClient.merge_relation，只是 provenance 标记不同。

    confirmed_relation_types/allowed_combinations 是调用方预先查好的该
    租户 status="confirmed" 范围，与 normalize_and_write_relations() 要求
    的同名参数是同一类校验：relation_type 必须在 confirmed_relation_types
    里，且 (subject.term_type, relation_type, object.term_type) 必须在
    allowed_combinations 里，任一条件不满足就抛
    RelationNotInConfirmedOntologyError——人工批准同样不能绕开"只写入已
    确认本体范围内的关系"这条不变量，见该异常类的说明。

    以上任一校验失败都不写图谱、也不改变 review 状态（仍是 pending），
    方便审核员改对后重新提交，而不是必须先驳回再重新走一遍抽取流程。
    """
    row = await _fetch_pending_row(conn, review_id, tenant_id=tenant_id)
    subject_term = resolve_term(subject_standard_name, terms, term_type_hint=subject_term_type_hint)
    if subject_term is None:
        raise StandardNameNotInTermsError(
            _standard_name_not_found_message("subject_standard_name", subject_standard_name, terms)
        )
    object_term = resolve_term(object_standard_name, terms, term_type_hint=object_term_type_hint)
    if object_term is None:
        raise StandardNameNotInTermsError(
            _standard_name_not_found_message("object_standard_name", object_standard_name, terms)
        )
    # merge_relation 现在按 {tenant_id, node_key} MERGE 端点节点（node_key
    # 是创建时固定的身份键，改名后不变——ADR-0003），不能直接传人工确认的
    # 展示名 subject_standard_name/object_standard_name（改名后就不等于
    # node_key 了）。resolve_term 已经返回了完整的 Term，直接
    # 取 node_key，与 app/graphrag/normalization.py 的做法一致。
    subject_node_key = subject_term.node_key
    object_node_key = object_term.node_key
    combo = (subject_term.term_type, row["relation_type"], object_term.term_type)
    if row["relation_type"] not in confirmed_relation_types or combo not in allowed_combinations:
        raise RelationNotInConfirmedOntologyError(
            f"关系类型 {row['relation_type']!r} 或类型组合 "
            f"{(subject_term.term_type, row['relation_type'], object_term.term_type)!r} "
            "不在该租户已确认的本体范围内"
        )
    await graph_client.merge_relation(
        subject_standard_name=subject_node_key,
        object_standard_name=object_node_key,
        relation_type=row["relation_type"],
        source=row["source"],
        tenant_id=tenant_id,
        provenance=HUMAN_APPROVED,
        recorded_at=now,
    )
    # WHERE 里重复加 tenant_id：单看这条语句本身，_fetch_pending_row 已经
    # 校验过 review_id 属于这个 tenant_id，此刻再查一次理论上多余；但两条
    # 语句一旦将来被拆开（比如中间插入别的逻辑），少了这层防御就会变成
    # "校验和实际更新对不上号" 的隐患，多写一个条件的成本很低。
    await conn.execute(
        "UPDATE graph_review_queue SET status='approved', "
        "resolved_at=datetime('now'), resolved_note=? "
        "WHERE review_id=? AND tenant_id=?",
        (f"{subject_standard_name} -> {object_standard_name}", review_id, tenant_id),
    )
    await conn.commit()


async def reject_review(
    conn: aiosqlite.Connection,
    *,
    review_id: int,
    tenant_id: str,
    note: str | None = None,
) -> None:
    """人工判定该候选是噪声/误抽取，标记驳回，不写入图谱。"""
    await _fetch_pending_row(conn, review_id, tenant_id=tenant_id)
    await conn.execute(
        "UPDATE graph_review_queue SET status='rejected', "
        "resolved_at=datetime('now'), resolved_note=? "
        "WHERE review_id=? AND tenant_id=?",
        (note, review_id, tenant_id),
    )
    await conn.commit()
