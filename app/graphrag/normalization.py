from __future__ import annotations

import difflib
import logging
from datetime import datetime
from typing import Any, Protocol

import aiosqlite

from app.graphrag.ontology import Term, resolve_term
from app.graphrag.provenance import AUTO_MERGED
from app.graphrag.relation_writer import RelationWriterProtocol
from app.graphrag.review_queue import enqueue_for_review

logger = logging.getLogger(__name__)


class GraphWriteClientProtocol(RelationWriterProtocol, Protocol):
    """摄取管道用到的两个图写方法。merge_relation 的签名继承自
    RelationWriterProtocol——三条写入路径共用同一份声明，见那个模块。"""

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None: ...


def resolve_to_standard_name(
    name: str, terms: list[Term], *, term_type_hint: str | None = None
) -> str | None:
    """精确匹配单个候选实体名（可以是标准名或别名）到术语表标准名，未命中
    返回 None。

    这里用精确匹配（等于标准名或某个别名），而非 term_matcher 的子串
    包含匹配——候选名来自 LLM 抽取，通常已经是较短的实体名，用更严格
    的精确匹配降低误对齐风险。

    term_type_hint 传了：优先只在该类型的术语里找精确匹配，命中就直接
    返回；该类型下没有命中，退回下面"不分类型"的逻辑。

    不分类型的匹配：候选名对应的（标准名或别名意义上的）术语只有一个时
    才返回，命中 0 个或 2 个以上都返回 None——避免"这个名字本身在多个
    类型下都存在"时静默选中错误的那一个。这比 2026-08-22 之前"遍历全部、
    返回第一个命中"的旧行为更严格：旧行为在名字唯一时结果不变，只有在
    名字确实有歧义时行为才不同（以前静默选一个，现在明确返回 None，
    交给调用方的模糊匹配/人工审核兜底路径处理），见该 bug 的调查记录。

    实际消歧逻辑在 `app.graphrag.ontology.resolve_term`（返回 Term 对象）
    里；这个函数只是取 `.standard_name`。normalize_and_write_relations
    直接调用 `resolve_term` 而不是这个函数，为的是同一次查找结果既能拿到
    standard_name 也能拿到 node_key，见 `resolve_term` 的说明。
    """
    term = resolve_term(name, terms, term_type_hint=term_type_hint)
    return term.standard_name if term is not None else None


def find_fuzzy_candidate_standard_name(
    name: str, terms: list[Term], *, threshold: float = 0.75, term_type_hint: str | None = None
) -> str | None:
    """精确匹配失败后的模糊匹配兜底：找相似度最高的单一标准名建议。

    和 term_matcher.py::match_terms()（TermGuard）"任意命中就收集一组
    术语"不同——那边是往上下文里塞信息，多塞几个无妨；这里要给人工审核
    一个具体的对齐建议，必须是单一最优解。因为 name 本身就是 LLM 抽取
    出的完整候选实体名（不是需要在长文本里扫描的段落），直接整串比较，
    不需要滑动窗口。

    threshold 默认 0.75（沿用 TermGuard 的保守取值）——这是参考起点，
    需要结合真实数据调整，不是权威值。返回值只是"建议"，调用方（见
    normalize_and_write_relations）不会拿这个结果自动写入图谱，而是
    连同建议一起进人工审核队列，由人工最终确认——正因为最终有人工把关，
    这里不套用 resolve_to_standard_name 那套"歧义就拒绝"的严格策略，
    term_type_hint 传了就只在该类型内找，没传就在全部术语里找相似度
    最高的一个（哪怕有同名不同类型的术语存在，模糊匹配本来就只是排序
    取最优，不是精确判定"是不是这个"）。
    """
    candidates = terms if term_type_hint is None else [
        t for t in terms if t.term_type == term_type_hint
    ]
    # 比较前统一小写：大小写差异不该参与相似度打分。归一化前
    # difflib("coca-cola", "Coca-Cola") 只有 0.7778（勉强过 0.75 纯属
    # 巧合），"COCA-COLA" 直接是 0.0——同一个名字在召回阶段能匹配上、
    # 到这里却给不出对齐建议。跟 ontology.resolve_term 的
    # _name_matches、term_matcher、ontology_recall 保持同一条归一化约定。
    name_lower = name.lower()
    best_name: str | None = None
    best_ratio = 0.0
    for term in candidates:
        for candidate in [term.standard_name, *term.aliases]:
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, name_lower, candidate.lower()).ratio()
            if ratio >= threshold and ratio > best_ratio:
                best_ratio = ratio
                best_name = term.standard_name
    return best_name


async def normalize_and_write_relations(
    relations: list[dict[str, str]],
    *,
    terms: list[Term],
    graph_client: GraphWriteClientProtocol,
    source: str,
    tenant_id: str,
    now: datetime,
    confirmed_relation_types: set[str],
    allowed_combinations: set[tuple[str, str, str]],
    review_conn: aiosqlite.Connection | None = None,
) -> int:
    """候选关系归一化对齐术语表后写入图谱，返回成功写入数。

    任一侧未能对齐标准术语、关系类型不合法、或关系类型/实体类型组合不在
    该租户已确认范围内的候选都不会自动入库。

    confirmed_relation_types/allowed_combinations 是调用方（见
    graph_extraction.py）预先查好的该租户 status="confirmed" 范围——
    AUTO_MERGED 直写路径（两侧实体都精确对齐时）过去会跳过这层校验直接
    写图谱，现在两侧对齐之后还要再过一遍这层检查：relation_type 必须在
    confirmed_relation_types 里，且 (subject_type, relation_type,
    object_type) 必须在 allowed_combinations 里，任一条件不满足就降级
    转人工审核（reason="not_in_confirmed_ontology"），不再直接写图谱。
    见 docs/superpowers/specs/2026-08-19-data-entry-unification-design.md
    决策 E.3。

    review_conn 为可选项：
    - 不传（默认）：候选只记日志后丢弃，保持阶段3落地时的行为不变；
    - 传入：候选改为写入持久化的人工待审核队列（见 review_queue.py），
      而不是随日志一起消失——对应架构文档"低置信度新实体进入人工待
      审核队列，而非直接自动入库/直接丢弃"的完整实现。

    这条路径写入的边一律标记 provenance=AUTO_MERGED（见
    app/graphrag/provenance.py）——两侧实体精确对齐了术语表，不代表 LLM
    抽取出的这条关系本身就是对的，只是它没有进人工审核；调用方传入的
    now 统一作为这批关系的写入时间（不在循环内部逐条调用 datetime.now()，
    避免同一批次内的边打上有细微先后差异的时间戳，也方便测试用固定时钟）。
    """
    written = 0
    for relation in relations:
        subject_type_hint = relation.get("subject_type") or None
        object_type_hint = relation.get("object_type") or None
        # 用 resolve_term（app/graphrag/ontology.py）而不是
        # resolve_to_standard_name：后面写图谱时要用这条候选实际命中的
        # Term 的 node_key（见下方 try 块），如果这里只留下 standard_name
        # 字符串，后面就得按字符串重新查一次 Term——2026-08-22 起
        # standard_name 允许跨 term_type 重复，"按已知 standard_name 反查
        # Term"和"按候选名 name-or-alias 解析 Term"是两套不同的判重规则，
        # 可能对同一条候选给出不同答案（见 2026-08-22 Fix round 1 的调查
        # 记录）。这里保留 Term 对象本身，全程只查一次，避免这种"两次
        # 查找互相打架"的可能性。
        subject_term = resolve_term(
            relation["subject"], terms, term_type_hint=subject_type_hint
        )
        object_term = resolve_term(
            relation["object"], terms, term_type_hint=object_type_hint
        )
        subject_std = subject_term.standard_name if subject_term is not None else None
        object_std = object_term.standard_name if object_term is not None else None
        if subject_std is None or object_std is None:
            suggested_subject = (
                None
                if subject_std is not None
                else find_fuzzy_candidate_standard_name(
                    relation["subject"], terms, term_type_hint=subject_type_hint
                )
            )
            suggested_object = (
                None
                if object_std is not None
                else find_fuzzy_candidate_standard_name(
                    relation["object"], terms, term_type_hint=object_type_hint
                )
            )
            if suggested_subject is not None or suggested_object is not None:
                logger.info(
                    "关系候选模糊匹配到建议标准名，转人工审核 subject=%s "
                    "(建议=%s) object=%s (建议=%s)",
                    relation["subject"],
                    suggested_subject,
                    relation["object"],
                    suggested_object,
                )
                if review_conn is not None:
                    await enqueue_for_review(
                        review_conn,
                        subject_candidate=relation["subject"],
                        object_candidate=relation["object"],
                        relation_type=relation["relation_type"],
                        reason="fuzzy_match_needs_confirmation",
                        source=source,
                        tenant_id=tenant_id,
                        suggested_subject_standard_name=suggested_subject,
                        suggested_object_standard_name=suggested_object,
                        evidence=relation.get("evidence", ""),
                        subject_type_candidate=relation.get("subject_type") or None,
                        object_type_candidate=relation.get("object_type") or None,
                    )
                continue
            logger.info(
                "关系候选未能对齐术语表，丢弃 subject=%s object=%s",
                relation["subject"],
                relation["object"],
            )
            if review_conn is not None:
                reason = "subject_unresolved" if subject_std is None else "object_unresolved"
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason=reason,
                    source=source,
                    tenant_id=tenant_id,
                    evidence=relation.get("evidence", ""),
                    subject_type_candidate=relation.get("subject_type") or None,
                    object_type_candidate=relation.get("object_type") or None,
                )
            continue
        subject_type = relation.get("subject_type", "")
        object_type = relation.get("object_type", "")
        combo = (subject_type, relation["relation_type"], object_type)
        if relation["relation_type"] not in confirmed_relation_types or combo not in allowed_combinations:
            logger.info(
                "关系候选两侧已对齐术语表，但类型组合不在已确认本体范围内，转人工审核 "
                "subject=%s(%s) object=%s(%s) relation_type=%s",
                relation["subject"], subject_type, relation["object"], object_type,
                relation["relation_type"],
            )
            if review_conn is not None:
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason="not_in_confirmed_ontology",
                    source=source,
                    tenant_id=tenant_id,
                    suggested_subject_standard_name=subject_std,
                    suggested_object_standard_name=object_std,
                    evidence=relation.get("evidence", ""),
                    subject_type_candidate=subject_type or None,
                    object_type_candidate=object_type or None,
                )
            continue
        try:
            # merge_relation 现在按 {tenant_id, node_key} MERGE 端点节点
            # （node_key 是创建时固定的身份键，改名后不变——ADR-0003），不能
            # 直接传 standard_name 展示名（改名后就不等于 node_key 了）。
            # subject_term/object_term 就是上面 resolve_term 解析
            # subject_std/object_std 时命中的那个 Term 对象本身（同一次
            # 查找，没有再按 standard_name 反查一遍）——subject_std 不是
            # None 就意味着 subject_term 也不是 None，这里的
            # assert 只是把这个不变量写清楚，不是靠它兜底一次可能失败的
            # 二次查找（2026-08-22 Fix round 1 之前的版本是靠 assert 兜底
            # find_term_by_type_hint 的二次查找，那次查找用的是跟这里不同
            # 的判重规则，可能查不到人，assert 因此真的会炸——2026-08-23
            # 起这两次查找已经合并成同一个 resolve_term，不再可能出现
            # 这种分叉——见调查记录）。
            assert subject_term is not None and object_term is not None
            subject_node_key = subject_term.node_key
            object_node_key = object_term.node_key
            await graph_client.merge_relation(
                subject_standard_name=subject_node_key,
                object_standard_name=object_node_key,
                relation_type=relation["relation_type"],
                source=source,
                tenant_id=tenant_id,
                provenance=AUTO_MERGED,
                recorded_at=now,
            )
        except ValueError:
            logger.warning(
                "关系类型不合法，丢弃该候选 relation_type=%s",
                relation["relation_type"],
            )
            if review_conn is not None:
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason="invalid_relation_type",
                    source=source,
                    tenant_id=tenant_id,
                    # 走到这个分支说明两侧都已经精确对齐过术语表了（见函数
                    # 顶部 subject_std/object_std 的计算），只是 relation_type
                    # 不合法——不是"建议"而是已知事实，直接回传，省得审核员
                    # 重新输入系统已经算出来的正确答案
                    suggested_subject_standard_name=subject_std,
                    suggested_object_standard_name=object_std,
                    evidence=relation.get("evidence", ""),
                    subject_type_candidate=relation.get("subject_type") or None,
                    object_type_candidate=relation.get("object_type") or None,
                )
            continue
        written += 1
    return written
