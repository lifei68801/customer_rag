"""管道产出与人工编辑的合并（Apply User Edits）。

合并策略：**人工编辑对被编辑的字段永远优先，未被编辑的字段正常接受
管道更新**。不采用按时间戳比较的策略——那要求背书数据带时间戳列，而
源文件是客户上传的 xlsx，不保证有这一列。
见 docs/superpowers/specs/2026-08-30-manual-edits-layer-design.md。

刻意做成不碰数据库的纯函数：合并语义有六七种组合（字段编辑 / 删除 /
创建 / 创建后管道又产出同 node_key / 属性字段单独编辑 / 孤儿编辑），
只有跟查询分开才能穷举地单测。
"""

from __future__ import annotations

from dataclasses import replace

from app.graphrag.ontology import Term
from app.graphrag.term_edits_store import (
    EXTRA_PROPERTY_PREFIX,
    FIELD_CREATED,
    FIELD_DELETED,
)

# Term 上可以被整字段替换的编辑字段。extra_properties 不在其中——它按
# "extra_properties.<name>" 的形式逐个属性编辑，见下面的说明。
_REPLACEABLE_FIELDS = ("standard_name", "aliases", "term_type")


def _apply_field_edits(term: Term, edits: dict[str, object]) -> Term:
    """把普通字段级编辑叠加到一个 Term 上，返回新的 Term。

    extra_properties 走单独的路径：编辑的 field 形如
    "extra_properties.revenue"，只覆盖字典里的那一个键，同一个字典里
    其余属性仍然跟随管道——这正是"字段级而不是整行级"的要点，整行覆盖
    会让人工只改了展示名却导致该实体的金额再也不跟着数据源更新。
    """
    changes: dict[str, object] = {}
    for field in _REPLACEABLE_FIELDS:
        if field in edits:
            changes[field] = edits[field]

    property_edits = {
        key[len(EXTRA_PROPERTY_PREFIX):]: value
        for key, value in edits.items()
        if key.startswith(EXTRA_PROPERTY_PREFIX)
    }
    if property_edits:
        changes["extra_properties"] = {**term.extra_properties, **property_edits}

    return replace(term, **changes) if changes else term


def _created_to_edits(created: dict[str, object]) -> dict[str, object]:
    """把 __created__ 的字段对象摊平成普通字段级编辑。

    这是"管道后来产出了同 node_key"那条语义的实现：__created__ 的每个
    字段等价于一条同字段的普通编辑，于是管道的行接管存在性、而人当初
    填的那些值继续按字段级优先。
    """
    flattened: dict[str, object] = {}
    for field in _REPLACEABLE_FIELDS:
        if field in created:
            flattened[field] = created[field]
    for name, value in (created.get("extra_properties") or {}).items():
        flattened[f"{EXTRA_PROPERTY_PREFIX}{name}"] = value
    return flattened


def _synthesize_created(
    node_key: str, created: dict[str, object], *, tenant_id: str
) -> Term:
    """terms 表里没有对应行时，由 __created__ 合成一个 Term。

    source 固定为 "review"：这条路径就是审核界面批准关系时现场创建端点
    实体用的，沿用既有的来源标记，让"哪些实体不是管道产出的"这个问题
    在合并视图上仍然可答。
    """
    return Term(
        tenant_id=tenant_id,
        node_key=node_key,
        standard_name=str(created.get("standard_name", "")),
        aliases=list(created.get("aliases") or []),
        term_type=str(created.get("term_type", "")),
        extra_properties=dict(created.get("extra_properties") or {}),
        source="review",
    )


def apply_edits(
    terms: list[Term], edits: dict[str, dict[str, object]], *, tenant_id: str
) -> list[Term]:
    """把编辑叠加到管道产出上，返回合并后的术语列表。

    terms 和 edits 都应当已经是同一个租户的（调用方负责按 tenant_id 查）。

    产出顺序：先是 terms 的顺序（调用方通常按 standard_name 排过），再是
    纯由编辑层创建、terms 表里没有对应行的那些。
    """
    merged: list[Term] = []
    seen: set[str] = set()

    for term in terms:
        node_edits = edits.get(term.node_key)
        seen.add(term.node_key)
        if node_edits is None:
            merged.append(term)
            continue
        if FIELD_DELETED in node_edits:
            # 人工删除不可被管道恢复。terms 表里的行仍然存在（ETL 还在
            # 维护它），只是对所有读路径不可见。
            continue
        effective = dict(node_edits)
        created = effective.pop(FIELD_CREATED, None)
        if created is not None:
            # 管道后来产出了同 node_key：管道的行接管存在性，__created__
            # 里记录的字段降级为普通字段级编辑。普通编辑优先级更高——它
            # 是在创建之后发生的更新。
            effective = {**_created_to_edits(created), **effective}
        merged.append(_apply_field_edits(term, effective))

    for node_key, node_edits in edits.items():
        if node_key in seen:
            continue
        if FIELD_DELETED in node_edits:
            continue
        created = node_edits.get(FIELD_CREATED)
        if created is None:
            # 孤儿编辑：挂在一个 terms 表里不存在、也没有 __created__ 的
            # node_key 上。不凭空造实体——这种编辑通常来自实体被 ETL 的
            # sweep 清理之后，源里若再出现同 node_key，它会自动重新生效。
            continue
        synthesized = _synthesize_created(node_key, created, tenant_id=tenant_id)
        rest = {k: v for k, v in node_edits.items() if k != FIELD_CREATED}
        merged.append(_apply_field_edits(synthesized, rest) if rest else synthesized)

    return merged
