from datetime import datetime

import aiosqlite

from app.graphrag.normalization import (
    find_fuzzy_candidate_standard_name,
    normalize_and_write_relations,
)
from app.graphrag.ontology import Term
from app.graphrag.review_queue import ensure_review_schema, list_pending_reviews

_NOW = datetime(2026, 8, 12, 12, 0, 0)

_CONFIRMED_RELATION_TYPES = {
    "RELATED_TO", "PART_OF", "IS_A", "REQUIRES", "ALTERNATIVE_TO",
    "CAUSES", "ADDRESSED_BY", "LOCATED_IN", "APPLIES_TO", "PRECEDES",
}
_ALLOWED_COMBINATIONS = {
    ("error_code", rt, "module") for rt in _CONFIRMED_RELATION_TYPES
} | {
    ("module", rt, "error_code") for rt in _CONFIRMED_RELATION_TYPES
}

_TERMS = [
    Term(
        tenant_id="t1",
        node_key="错误码E502",
        standard_name="错误码E502",
        aliases=["网关超时"],
        term_type="error_code",
    ),
    Term(
        tenant_id="t1",
        node_key="登录模块",
        standard_name="登录模块",
        aliases=["认证模块"],
        term_type="module",
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
        {
            "subject": "网关超时", "subject_type": "error_code",
            "object": "认证模块", "object_type": "module",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW, confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS,
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
        now=_NOW, confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert written == 0
    assert graph_client.written == []


async def test_drops_relation_with_invalid_relation_type_without_crashing_batch():
    """走 merge_relation 的 except ValueError 兜底分支（正则/保留名校验，
    与"是否在已确认本体范围内"是两层独立校验，见 spec 决策 E.3）。第一条
    关系用 "ALIAS_OF" 而非任意字符串——它格式合法、也是本测试局部确认
    范围内允许的类型（真实场景里租户完全可以把 tenant_relation_types
    确认成这个名字，ontology_relations.py 的创建/确认路径不会拦它），
    但 FakeGraphClient.merge_relation 模拟的是保留名校验，固定拒绝它——
    这样它能先通过新加的确认范围校验，再在 merge_relation 里被拒绝，
    真正测到 except ValueError 这层，而不是被更早的确认范围校验拦下来
    （那样 reason 会是 "not_in_confirmed_ontology"，测不到这条分支）。
    """
    graph_client = FakeGraphClient()
    relations = [
        {
            "subject": "网关超时", "subject_type": "error_code",
            "object": "认证模块", "object_type": "module",
            "relation_type": "ALIAS_OF",
        },
        {
            "subject": "网关超时", "subject_type": "error_code",
            "object": "认证模块", "object_type": "module",
            "relation_type": "RELATED_TO",
        },
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW, confirmed_relation_types=_CONFIRMED_RELATION_TYPES | {"ALIAS_OF"},
        allowed_combinations=_ALLOWED_COMBINATIONS | {("error_code", "ALIAS_OF", "module")},
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
        confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS,
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["subject_candidate"] == "网关超时"
    assert pending[0]["object_candidate"] == "不存在的实体"
    assert pending[0]["reason"] == "object_unresolved"


async def test_enqueues_invalid_relation_type_for_review_when_review_conn_provided():
    """走 merge_relation 的 except ValueError 兜底分支（正则/保留名校验，
    与"是否在已确认本体范围内"是两层独立校验，见 spec 决策 E.3）。这里
    刻意在本用例范围内把 "非法类型" 临时加进已确认范围（confirmed_relation_types/
    allowed_combinations 局部覆盖，只在本测试生效），让它先通过新加的
    确认范围校验，再在 merge_relation 里被 FakeGraphClient 的固定白名单
    拒绝——否则它会在更早的确认范围校验处就被拦下，reason 变成
    "not_in_confirmed_ontology"，测不到 except ValueError 这层了。
    """
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {
            "subject": "网关超时", "subject_type": "error_code",
            "object": "认证模块", "object_type": "module",
            "relation_type": "非法类型",
        }
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        confirmed_relation_types=_CONFIRMED_RELATION_TYPES | {"非法类型"},
        allowed_combinations=_ALLOWED_COMBINATIONS | {("error_code", "非法类型", "module")},
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
    # Fix 7 回归测试：这是本函数里唯一一处 enqueue_for_review 调用曾经
    # 没有透传 subject_type_candidate/object_type_candidate 的地方，跟
    # 同一函数里其它三处调用不一致；现在补齐后行为应该一致——候选实体
    # 类型能预填进审核页内联创建实体表单的下拉框。
    assert pending[0]["subject_type_candidate"] == "error_code"
    assert pending[0]["object_type_candidate"] == "module"


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
        now=_NOW, confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS,
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
        confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS,
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
        confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS,
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
            tenant_id="t1",
            node_key="错误码E503",
            standard_name="错误码E503",
            aliases=["网关超时中"],
            term_type="error_code",
        ),
        Term(
            tenant_id="t1",
            node_key="错误码E502",
            standard_name="错误码E502",
            aliases=["网关超时"],
            term_type="error_code",
        ),
    ]

    result = find_fuzzy_candidate_standard_name("网关超时了", multi_candidate_terms)

    assert result == "错误码E502"


async def test_writes_node_key_not_standard_name_after_term_was_renamed():
    """Fix 2 回归测试：merge_relation 现在按 {tenant_id, node_key} MERGE
    端点节点（node_key 是创建时固定的身份键，改名后不变——ADR-0003）。
    这里构造一个"已改名"的术语——node_key（创建时的原始值）与当前
    standard_name（改名后的展示名）不同——验证写入图谱时传给
    merge_relation 的是 node_key，而不是 resolve_to_standard_name()
    返回的当前展示名，否则改名后会在 Neo4j 里 MERGE 出一个没有
    standard_name 属性的幽灵节点，而不是命中真实节点。
    """
    graph_client = FakeGraphClient()
    renamed_terms = [
        Term(
            tenant_id="t1",
            # node_key 是术语创建时的原始名字，改名后不再更新；
            # standard_name 是改名后的当前展示名，两者刻意不同。
            node_key="错误码E502_原始名",
            standard_name="错误码E502",
            aliases=["网关超时"],
            term_type="error_code",
        ),
        Term(
            tenant_id="t1",
            node_key="登录模块_原始名",
            standard_name="登录模块",
            aliases=["认证模块"],
            term_type="module",
        ),
    ]
    relations = [
        {
            "subject": "网关超时", "subject_type": "error_code",
            "object": "认证模块", "object_type": "module",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations, terms=renamed_terms, graph_client=graph_client, source="a.md",
        tenant_id="t1", now=_NOW, confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert written == 1
    assert graph_client.written[0]["subject"] == "错误码E502_原始名"
    assert graph_client.written[0]["object"] == "登录模块_原始名"


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
        confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS,
        review_conn=review_conn,
    )

    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert pending[0]["evidence"] == "文档中提到网关超时时会影响不存在的实体"


async def test_downgrades_to_review_when_relation_type_not_confirmed():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "subject_type": "error_code",
         "object": "认证模块", "object_type": "module",
         "relation_type": "UNCONFIRMED_TYPE"},
    ]
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW, confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS, review_conn=conn,
    )

    assert written == 0
    assert graph_client.written == []
    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["reason"] == "not_in_confirmed_ontology"


async def test_downgrades_to_review_when_type_combination_not_allowed():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "subject_type": "module",  # 故意传反类型
         "object": "认证模块", "object_type": "error_code",
         "relation_type": "RELATED_TO"},
    ]
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW, confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations={("error_code", "RELATED_TO", "module")},  # 只允许一个方向
        review_conn=conn,
    )

    assert written == 0
    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["reason"] == "not_in_confirmed_ontology"
