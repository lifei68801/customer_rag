from datetime import datetime

import aiosqlite

from app.graphrag.normalization import (
    find_fuzzy_candidate_standard_name,
    normalize_and_write_relations,
)
from app.graphrag.ontology import Term
from app.graphrag.review_queue import ensure_review_schema, list_pending_reviews

_NOW = datetime(2026, 8, 12, 12, 0, 0)

_TERMS = [
    Term(
        standard_name="错误码E502",
        aliases=["网关超时"],
        term_type="error_code",
        product_line="核心平台",
    ),
    Term(
        standard_name="登录模块",
        aliases=["认证模块"],
        term_type="module",
        product_line="核心平台",
    ),
]


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []
        self.deleted_sources: list[str] = []

    async def merge_relation(
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
        provenance,
        recorded_at,
    ) -> None:
        if relation_type not in {
            "RELATED_TO", "PART_OF", "IS_A", "REQUIRES", "ALTERNATIVE_TO",
            "CAUSES", "ADDRESSED_BY", "LOCATED_IN", "APPLIES_TO", "PRECEDES",
        }:
            raise ValueError("不允许的关系类型")
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
                "provenance": provenance,
                "recorded_at": recorded_at,
            }
        )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        self.deleted_sources.append(source)


async def test_writes_relation_when_both_sides_resolve_via_alias():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "RELATED_TO"}
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW,
    )

    assert written == 1
    assert graph_client.written == [
        {
            "subject": "错误码E502",
            "object": "登录模块",
            "relation_type": "RELATED_TO",
            "source": "a.md",
            "tenant_id": "t1",
            "provenance": "auto_merged",
            "recorded_at": _NOW,
        }
    ]


async def test_drops_relation_when_one_side_unresolved():
    graph_client = FakeGraphClient()
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW,
    )

    assert written == 0
    assert graph_client.written == []


async def test_drops_relation_with_invalid_relation_type_without_crashing_batch():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "非法类型"},
        {"subject": "网关超时", "object": "认证模块", "relation_type": "RELATED_TO"},
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW,
    )

    assert written == 1


async def test_enqueues_unresolved_candidate_for_review_when_review_conn_provided():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["subject_candidate"] == "网关超时"
    assert pending[0]["object_candidate"] == "不存在的实体"
    assert pending[0]["reason"] == "object_unresolved"


async def test_enqueues_invalid_relation_type_for_review_when_review_conn_provided():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "非法类型"}
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["reason"] == "invalid_relation_type"
    # 两侧实体在这个分支里已经精确对齐过术语表了（只是 relation_type 不
    # 合法才被拦下来），这两个标准名不是"建议"而是已知事实，审核员不该
    # 再重新输入一遍系统已经算出来的正确答案
    assert pending[0]["suggested_subject_standard_name"] == "错误码E502"
    assert pending[0]["suggested_object_standard_name"] == "登录模块"


async def test_does_not_enqueue_when_review_conn_not_provided():
    """默认行为保持不变：不传 review_conn 时仍然只是丢弃+记日志，不建表不写库。"""
    graph_client = FakeGraphClient()
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW,
    )

    assert written == 0


def test_find_fuzzy_candidate_standard_name_matches_via_alias_typo():
    # "网关超时了" 比别名"网关超时"多了一个字，difflib 相似度约 0.8889，
    # 高于默认阈值 0.75，应该建议对齐到"错误码E502"。
    result = find_fuzzy_candidate_standard_name("网关超时了", _TERMS)

    assert result == "错误码E502"


def test_find_fuzzy_candidate_standard_name_matches_at_exact_threshold_boundary():
    # "认正模块"是别名"认证模块"打错1字，difflib 相似度恰好等于默认阈值
    # 0.75，应该命中（>= 判断，不是 >）。
    result = find_fuzzy_candidate_standard_name("认正模块", _TERMS)

    assert result == "登录模块"


def test_find_fuzzy_candidate_standard_name_returns_none_when_below_threshold():
    # 完全不相关的候选名，所有术语的相似度都是 0，远低于阈值。
    result = find_fuzzy_candidate_standard_name("不存在的实体", _TERMS)

    assert result is None


def test_find_fuzzy_candidate_standard_name_respects_custom_threshold():
    # "认正模块" vs 别名"认证模块"相似度 0.75；传入更严格的阈值 0.9 时
    # 不应该命中——验证 threshold 参数真的生效，不是死参数。
    result = find_fuzzy_candidate_standard_name(
        "认正模块", _TERMS, threshold=0.9
    )

    assert result is None


async def test_fuzzy_candidate_goes_to_review_queue_instead_of_auto_writing():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {
            "subject": "网关超时了",  # 模糊匹配"错误码E502"（经由别名"网关超时"）
            "object": "认证模块",  # 精确匹配"登录模块"
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        review_conn=review_conn,
    )

    assert written == 0
    assert graph_client.written == []
    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["reason"] == "fuzzy_match_needs_confirmation"
    assert pending[0]["subject_candidate"] == "网关超时了"
    assert pending[0]["object_candidate"] == "认证模块"
    assert pending[0]["suggested_subject_standard_name"] == "错误码E502"
    assert pending[0]["suggested_object_standard_name"] is None


async def test_totally_unresolved_candidate_still_uses_unresolved_reason_not_fuzzy():
    # "不存在的实体"和任何术语的相似度都是 0，没有模糊候选——必须继续走
    # 原有的 reason="object_unresolved" 分支，不能被误判成模糊匹配。
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["reason"] == "object_unresolved"
    assert pending[0]["suggested_subject_standard_name"] is None
    assert pending[0]["suggested_object_standard_name"] is None


def test_find_fuzzy_candidate_standard_name_picks_highest_ratio_not_first_match():
    # 两个术语的别名都超过默认阈值 0.75，但相似度不同（"网关超时中" 0.8，
    # "网关超时" 0.8889）——刻意把相似度更低的那个放在遍历顺序的第一位，
    # 验证函数返回的是相似度更高的那个，而不是"遍历到的第一个达标候选"
    # （TermGuard 的 match_terms() 是后者语义，这里必须不一样）。
    multi_candidate_terms = [
        Term(
            standard_name="错误码E503",
            aliases=["网关超时中"],
            term_type="error_code",
            product_line="核心平台",
        ),
        Term(
            standard_name="错误码E502",
            aliases=["网关超时"],
            term_type="error_code",
            product_line="核心平台",
        ),
    ]

    result = find_fuzzy_candidate_standard_name("网关超时了", multi_candidate_terms)

    assert result == "错误码E502"


async def test_enqueued_review_carries_evidence_from_relation_candidate():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
            "evidence": "文档中提到网关超时时会影响不存在的实体",
        }
    ]

    await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        review_conn=review_conn,
    )

    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert pending[0]["evidence"] == "文档中提到网关超时时会影响不存在的实体"
