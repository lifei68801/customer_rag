"""人工新建一条 Term 的完整编排。

## 为什么从路由里搬出来

这段编排此前整个住在 `admin_terms_routes.py::create_new_term` 里：152 行，
一路穿过相似度扫描、node_key 拼装、祖父豁免、分类校验、编辑层写入、撤销
删除标记、读回合并视图、同步进图谱。

后果有两条。

一是**测不动**：要验"node_key 撞上已有实体该怎么办""分类被删之后还能不能
重建"这类规则，只能构造完整的 HTTP → SQLite → 图客户端场景，而这些规则跟
HTTP 没有任何关系。

二是**一致性的决定落在了最不该落的那一层**：写完 SQLite 再同步图谱，同步
失败时两侧已经不一致——这件事该由知道数据模型的这一层决定怎么办，而不是
由一个负责把 JSON 翻译成 Python 的路由函数决定。

现在路由只做它该做的：把请求翻译进来、把结果和拒绝理由翻译回 HTTP 状态码。

## 这个模块不做的事

不做权限判定，不认识租户是否启用（那是 `require_active_tenant_or_404` 的
职责，留在路由层——它要回 404，是一个 HTTP 层面的判断）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

from app.graphrag.duplicate_detection import find_similar_terms
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.term_edits_store import (
    FIELD_CREATED,
    FIELD_DELETED,
    delete_term_edit,
    list_term_edits_for_node_key,
    upsert_term_edit,
)
from app.graphrag.terms_store import (
    InvalidExtraPropertyTypeError,
    TermNotFoundError,
    UnknownCategoryError,
    get_term_by_node_key,
    get_term_merged_by_node_key,
    is_tombstoned,
    list_terms_merged,
    validate_term_categories,
)

logger = logging.getLogger(__name__)


class TermCreateRejected(Exception):
    """这次创建被规则拒绝。消息是写给管理员看的，可以直接呈现。

    跟 `UnknownCategoryError` / `InvalidExtraPropertyTypeError` 收成同一个
    类型：对调用方来说它们是同一件事（提交的内容不合规，改了再来），分开
    只会让每个调用方都抄一遍同样的三个 except 分支。
    """


@dataclass(frozen=True)
class TermCreated:
    """创建成功。"""

    term: Term
    node_key: str

    #: 同租户同类型里名字跟它相近的现有术语，按相似度排好。给管理员一个
    #: "是不是已经有一个很像的了"的提示，不是拒绝理由。
    similar_terms: list[tuple[Term, float]]


def build_node_key(term_type: str, standard_name: str) -> str:
    """人工创建路径的 node_key 拼装规则。

    人工创建没有外部系统分配的稳定码，创建时直接取当时的展示名（ADR-0003
    说的"LLM 抽取场景里 node_key 在创建时可直接取当时的 standard_name"）。
    创建之后它就固定了，之后改名只改展示名。
    """
    return f"{term_type}:{standard_name}"


async def create_term_from_admin(
    review_conn: aiosqlite.Connection,
    graph_client,
    *,
    tenant_id: str,
    standard_name: str,
    term_type: str,
    aliases: list[str],
    extra_properties: dict | None,
    source: str,
    actor: str,
) -> TermCreated:
    """人工新建一条 Term：写编辑层，然后把合并结果投影进图谱。

    不往 terms 表插入新行，而是给 node_key 写一条 `__created__` 编辑——
    terms 表在 ETL 产出同 node_key 的行之前永远没有这一行（见
    `term_merge._synthesize_created`，合并视图会把它合成出来，source 固定
    标 "review"）。

    **不做名字冲突检查。** 这是刻意的：standard_name 早已不是身份键
    （2026-08-30 起同一 term_type 下允许重名），编辑层路径上"名字撞了"不再
    是数据完整性问题。如果这次创建的 node_key 恰好和已有的一行（不管是 ETL
    产出的还是别的编辑层创建的）相同，合并视图会把 `__created__` 的字段降级
    成对那一行的普通字段级编辑（见 `term_merge.apply_edits`），不报错、也不会
    产生第二条记录。

    规则不通过时抛 `TermCreateRejected`。图谱同步失败时原样抛出去——见函数
    末尾那段说明。
    """
    extra_properties = extra_properties or {}
    node_key = build_node_key(term_type, standard_name)

    similar = await _scan_for_similar_terms(
        review_conn, tenant_id=tenant_id, term_type=term_type, standard_name=standard_name
    )

    existing_term, existing_extra_property_keys = await _load_existing_row(
        review_conn, tenant_id=tenant_id, node_key=node_key
    )

    if existing_term is not None:
        await _guard_against_reviving_into_a_deleted_category(
            review_conn, tenant_id=tenant_id, node_key=node_key, existing_term=existing_term
        )

    try:
        await validate_term_categories(
            review_conn,
            tenant_id=tenant_id,
            term_type=term_type,
            extra_properties=extra_properties,
            existing_extra_property_keys=existing_extra_property_keys,
        )
    except (UnknownCategoryError, InvalidExtraPropertyTypeError) as exc:
        raise TermCreateRejected(str(exc)) from exc

    await upsert_term_edit(
        review_conn,
        tenant_id=tenant_id,
        node_key=node_key,
        field=FIELD_CREATED,
        value={
            "standard_name": standard_name,
            "term_type": term_type,
            "aliases": aliases,
            "extra_properties": extra_properties,
        },
        edited_by=actor,
    )
    # 人工重建一个曾被人工删除的 node_key：撤掉那条 __deleted__ 编辑，让它
    # 重新可见。这不违反"人工删除不可被恢复"——那条规矩的准确表述是
    # Foundry 的「Deletions aren't reversible by datasource updates」，禁的是
    # **数据源更新**把人删掉的东西带回来（ETL 重跑仍然做不到，见
    # term_merge.apply_edits 里 FIELD_DELETED 的短路），而不是禁止人自己撤销
    # 自己的删除。
    #
    # 顺序：先写 __created__ 再撤 __deleted__。反过来的话，中间一步失败会让
    # 实体带着删除前的旧值重新可见；现在这个顺序下中间失败则维持删除状态
    # 不变，是安全的那一侧。
    #
    # 不这样做的后果不是"静默成功"而是 500：下面那句 get_term_merged_by_node_key
    # 会因 __deleted__ 抛 TermNotFoundError。
    await delete_term_edit(
        review_conn, tenant_id=tenant_id, node_key=node_key, field=FIELD_DELETED
    )

    # 写完编辑层后从合并视图取回同步进图谱——图谱应当是合并结果的投影。
    merged_term = await get_term_merged_by_node_key(
        review_conn, tenant_id=tenant_id, node_key=node_key
    )
    # 返回给调用方的这一份用请求里的 source，不是合并视图里那个固定的
    # "review"：调用方问的是"我刚提交的这条长什么样"。
    term_to_return = Term(
        tenant_id=tenant_id,
        node_key=node_key,
        standard_name=standard_name,
        aliases=aliases,
        term_type=term_type,
        extra_properties=extra_properties,
        source=source,
    )

    # 立即同步进图谱，不留图谱异步落后的窗口。
    #
    # 失败时原样抛出去，不吞。这一步之前 SQLite 已经写了，抛出去意味着调用方
    # 看到一次失败、而编辑层其实已经生效——两侧不一致。这是有意选的那一侧：
    # 反过来（吞掉异常、报成功）会让不一致**无人知晓**，而日志里这条 exception
    # 是唯一能让人去核对的线索。
    try:
        await graph_client.sync_term(merged_term)
    except Exception:
        logger.exception(
            "术语 %r（租户 %r）已写入 SQLite 但同步进图谱失败——两侧数据已不一致，需要人工核对",
            standard_name,
            tenant_id,
        )
        raise

    return TermCreated(term=term_to_return, node_key=node_key, similar_terms=similar)


async def _scan_for_similar_terms(
    review_conn: aiosqlite.Connection, *, tenant_id: str, term_type: str, standard_name: str
) -> list[tuple[Term, float]]:
    """同租户、同类型里名字跟它相近的现有术语。

    限定同 term_type，避免不同类型之间凑巧撞名字的噪声提示。走合并视图，
    这样刚被人工编辑过（改名/属性）的术语也能算进比对。
    """
    existing_terms = await list_terms_merged(review_conn, tenant_id, source=None)
    # 已经被合并过的墓碑行（duplicate_review_queue.approve_duplicate_suggestion
    # 打上的标记）排除在外——它的 standard_name 字面包含被合并前的原名，不该
    # 被当成"这个新名字看起来很像"的提示对象，见 is_tombstoned() 的说明。
    same_type_terms = [
        t for t in existing_terms if t.term_type == term_type and not is_tombstoned(t)
    ]
    return find_similar_terms(standard_name, same_type_terms)


async def _load_existing_row(
    review_conn: aiosqlite.Connection, *, tenant_id: str, node_key: str
) -> tuple[Term | None, frozenset[str]]:
    """terms 表里这个 node_key 的原始行（没有就是 None），以及它现有的属性键。

    祖父豁免用的是**原始行**而不是合并视图：它关心的是"这个实体在 terms 表里
    实际存在的属性键"——那些键可能因为 term_type 声明变更而成了"废弃字段"，
    这次 POST 不该因为它们被拒。
    """
    try:
        existing_term = await get_term_by_node_key(
            review_conn, tenant_id=tenant_id, node_key=node_key
        )
    except TermNotFoundError:
        # 查不到原始行 = 纯新建，无需豁免
        return None, frozenset()
    return existing_term, frozenset(existing_term.extra_properties)


async def _guard_against_reviving_into_a_deleted_category(
    review_conn: aiosqlite.Connection, *, tenant_id: str, node_key: str, existing_term: Term
) -> None:
    """这次创建会顺带复活一行曾被人工删除的 terms 行时，先看它的 term_type
    还在不在已确认 schema 里。

    分类删除的守卫走的是合并视图，被人工删空的类型可以被删掉——于是"实体被删
    → 分类被删 → 实体被重建"这条链会让一行 term_type 指向不存在分类的实体
    重新可见，悬空。

    校验口径跟 ETL 写入那道一致（`schema_etl.py::_write_entity_mapping` 也是
    `list_term_types(status="confirmed")` 里没有就拒），这里补的是同一套逻辑
    漏掉的那个入口。

    只在真的要复活时检查：没有 `__deleted__` 编辑的行本来就一直可见，这次
    创建不改变它的可见性，在这里拦住只会挡掉一次合法的编辑。

    注意校验的是 terms 行自己的 term_type，不是这次提交的那个（那个由
    `validate_term_categories` 负责）——node_key 的类型前缀只反映创建时的
    类型，两者可以不一致，见 `terms_store.update_term`。
    """
    existing_edits = await list_term_edits_for_node_key(
        review_conn, tenant_id=tenant_id, node_key=node_key
    )
    if FIELD_DELETED not in existing_edits:
        return
    confirmed_types = await list_term_types(review_conn, tenant_id, status="confirmed")
    if existing_term.term_type in {t.value for t in confirmed_types}:
        return
    raise TermCreateRejected(
        f"无法重建 {node_key!r}：它在 terms 表里的 term_type "
        f"{existing_term.term_type!r} 不在已确认 schema 里（分类已被删除）。"
        f"要恢复这条实体，先把这个分类加回来。"
    )
