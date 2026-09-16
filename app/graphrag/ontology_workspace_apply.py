"""把工作台将要提交的一份草案，跟当前草稿比一比。

存在的理由：replace_draft 是**整份替换**——工作台提交什么，草稿就变成什么。
草稿里可能有用户在「本体结构」页手工加的东西（spec 决策 10 明确要保留这条
路径），它们不在工作区里，会被这次替换删掉。不先告诉用户就是静默删数据。

只算差异、不做决定：这里不阻止任何一种差异，用户看过 diff 自己点。
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations
from app.graphrag.ontology_relations import list_relation_types


@dataclass(frozen=True)
class DraftDiff:
    added_term_types: list[str]
    removed_term_types: list[str]
    #: 已有但内容变了的实体类型，每条是一句人话，说清变的是什么。
    changed_term_types: list[str]
    added_relation_types: list[str]
    removed_relation_types: list[str]
    added_constraints: list[str]
    removed_constraints: list[str]

    def to_dict(self) -> dict:
        return {
            "added_term_types": self.added_term_types,
            "removed_term_types": self.removed_term_types,
            "changed_term_types": self.changed_term_types,
            "added_relation_types": self.added_relation_types,
            "removed_relation_types": self.removed_relation_types,
            "added_constraints": self.added_constraints,
            "removed_constraints": self.removed_constraints,
        }


def _field_signature(extra_fields: list) -> dict[str, tuple[str, str]]:
    """字段签名：name -> (value_type, label)。

    用映射而不是列表：顺序在本体表里没有语义（读的地方都按 name 取），
    按列表比的话，用户在工作台调一下字段顺序就会报一条假变更。
    """
    signature: dict[str, tuple[str, str]] = {}
    for item in extra_fields:
        if isinstance(item, dict):
            signature[item["name"]] = (item.get("value_type", ""), item.get("label", ""))
        else:  # ExtraFieldSpec
            signature[item.name] = (item.value_type, item.label)
    return signature


def _describe_field_changes(before: dict, after: dict) -> list[str]:
    changes: list[str] = []
    for name in sorted(set(after) - set(before)):
        changes.append(f"新增字段 {name}")
    for name in sorted(set(before) - set(after)):
        changes.append(f"删除字段 {name}")
    for name in sorted(set(before) & set(after)):
        if before[name] != after[name]:
            changes.append(f"字段 {name} 由 {before[name]} 改成 {after[name]}")
    return changes


def _constraint_label(subject: str, relation: str, obj: str) -> str:
    return f"{subject} -{relation}-> {obj}"


async def diff_against_draft(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    term_types: list[dict],
    relation_types: list[dict],
    constraints: list[dict],
) -> DraftDiff:
    current_terms = {t.value: t for t in await list_term_types(conn, tenant_id, status="draft")}
    current_relations = {
        r.relation_type for r in await list_relation_types(conn, tenant_id, status="draft")
    }
    current_constraints = {
        _constraint_label(c.subject_term_type, c.relation_type, c.object_term_type)
        for c in await list_allowed_combinations(conn, tenant_id, status="draft")
    }

    submitted_terms = {t["value"]: t for t in term_types}
    submitted_relations = {r["relation_type"] for r in relation_types}
    submitted_constraints = {
        _constraint_label(c["subject_term_type"], c["relation_type"], c["object_term_type"])
        for c in constraints
    }

    changed: list[str] = []
    for value in sorted(set(current_terms) & set(submitted_terms)):
        before = current_terms[value]
        after = submitted_terms[value]
        descriptions = _describe_field_changes(
            _field_signature(before.extra_fields), _field_signature(after.get("extra_fields", []))
        )
        after_value_type = after.get("standard_name_value_type", "string")
        if before.standard_name_value_type != after_value_type:
            descriptions.append(
                f"标准名类型由 {before.standard_name_value_type} 改成 {after_value_type}"
            )
        if descriptions:
            changed.append(f"{value}：" + "；".join(descriptions))

    return DraftDiff(
        added_term_types=sorted(set(submitted_terms) - set(current_terms)),
        removed_term_types=sorted(set(current_terms) - set(submitted_terms)),
        changed_term_types=changed,
        added_relation_types=sorted(submitted_relations - current_relations),
        removed_relation_types=sorted(current_relations - submitted_relations),
        added_constraints=sorted(submitted_constraints - current_constraints),
        removed_constraints=sorted(current_constraints - submitted_constraints),
    )
