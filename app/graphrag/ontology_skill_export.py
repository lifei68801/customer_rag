"""把一个租户做完的本体导出成 skill YAML，供人工审阅、脱敏、补别名之后
提交进 app/ontology_skills/。

**不自动入仓**（spec 决策 6）：导出的产物带着这个租户的真实列名（JAN、
STORE_CD 这类还好，客户自定义的列名可能含业务机密），必须有人看过才能
进代码仓给所有租户用。

导出是纯机械操作，因为 skill 格式的 term_types/relation_types/constraints
三段跟本体表一一对应；唯一需要"翻译"的是别名——它在本体里不存在，用 ETL
映射里实际用到的列名当第一个（也是唯一一个）别名。这是一个真实的线索：
那一列就是这个客户对这个概念的叫法。
"""

from __future__ import annotations

import re

import aiosqlite
import yaml

from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations
from app.graphrag.ontology_etl_mapping import get_etl_mapping
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.schema_etl_config import (
    ColumnNodeKeyPart,
    InvalidSchemaETLConfigError,
    parse_schema_etl_config,
)

# 与 ontology_skills._SKILL_NAME_PATTERN 用的是同一条规则：skill 名要能直接
# 当 app/ontology_skills/<name>/ 的目录名用。那边的正则是模块私有名，这里
# 故意复制一份而不是跨模块 import——代价是两处规则要同步，漂移的后果是
# 导出这边放行了一个名字，提交进代码仓后 load_skill 却拒绝装载。
_SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")


class NothingToExportError(Exception):
    """这个租户还没有已确认的本体。"""


async def export_skill_yaml(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    skill_name: str,
    display_name: str,
    today: str,
) -> str:
    if not _SKILL_NAME_PATTERN.match(skill_name):
        raise ValueError(
            f"skill 名 {skill_name!r} 不合法，必须满足 ^[a-z][a-z0-9_]{{0,63}}$（要当目录名用）"
        )
    term_types = await list_term_types(conn, tenant_id, status="confirmed")
    if not term_types:
        raise NothingToExportError(
            f"租户 {tenant_id} 还没有已确认的本体，导出的会是一份空骨架，装不回去。"
        )
    relation_types = await list_relation_types(conn, tenant_id, status="confirmed")
    combinations = await list_allowed_combinations(conn, tenant_id, status="confirmed")

    key_aliases: dict[str, list[str]] = {}
    field_aliases: dict[str, dict[str, list[str]]] = {}
    mapping = await get_etl_mapping(conn, tenant_id, status="confirmed")
    if mapping is not None:
        try:
            config = parse_schema_etl_config(
                mapping.config_yaml, origin=f"{tenant_id} 的已确认 ETL 映射"
            )
        except (InvalidSchemaETLConfigError, yaml.YAMLError):
            # 映射坏了不该挡住导出：本体本身是完整的，别名空着让人工补，
            # 比让"导出"这个只读动作报错更有用。写入侧（set_draft_etl_mapping）
            # 不校验 YAML 合法性，所以语法损坏（截断、手改出错）的 config_yaml
            # 是能落库的可达状态——parse_schema_etl_config 内部的 yaml.safe_load
            # 没有 try 包裹，语法错误会抛 yaml.YAMLError 而不是
            # InvalidSchemaETLConfigError，两种都要接住。
            config = None
        if config is not None:
            for entity in config.entities:
                columns = [
                    part.column
                    for part in entity.node_key_parts
                    if isinstance(part, ColumnNodeKeyPart)
                ]
                if columns:
                    key_aliases.setdefault(entity.term_type, []).extend(columns)
                for field_name, column in entity.field_mappings.items():
                    field_aliases.setdefault(entity.term_type, {}).setdefault(
                        field_name, []
                    ).append(column)

    document = {
        "name": skill_name,
        "version": "1",
        "display_name": display_name,
        "description": (
            f"从租户 {tenant_id} 的已确认本体导出于 {today}。"
            f"别名取自该租户 ETL 映射里实际用到的列名，提交进代码仓前请人工审阅、"
            f"脱敏，并补上其它常见叫法。"
        ),
        "term_types": [
            {
                "value": t.value,
                "display_name": t.value,
                "standard_name_value_type": t.standard_name_value_type,
                "extra_fields": [
                    {"name": f.name, "value_type": f.value_type, "display_name": f.display_name}
                    for f in t.extra_fields
                ],
                "key_aliases": key_aliases.get(t.value, []),
                # 按这次导出的 extra_fields 名字集合过滤：本体改了字段但 ETL
                # 映射没跟着改是系统认可的可达状态（replace_draft 的
                # etl_mapping 是可选参数），漂移时 field_mappings 里可能还
                # 留着一个本体已不再声明的字段名。不过滤的话产物会带上一条
                # 指向未声明字段的 field_aliases，load_skill._parse_term_type
                # 会因此拒绝装载——导出出来的东西装不回去。
                "field_aliases": {
                    field_name: aliases
                    for field_name, aliases in field_aliases.get(t.value, {}).items()
                    if field_name in {f.name for f in t.extra_fields}
                },
            }
            for t in term_types
        ],
        "relation_types": [
            {
                "relation_type": r.relation_type,
                "example_phrase": r.example_phrase,
                "description": r.description,
            }
            for r in relation_types
        ],
        "constraints": [
            [c.subject_term_type, c.relation_type, c.object_term_type] for c in combinations
        ],
        # v2 才读；导出时留空，由人工按这个领域的真实业务问题填。
        "questions": [],
    }
    # sort_keys=False 保留上面这个顺序：产物是给人读、给人改的，name 在最前面
    # 比按字母序排完 constraints 排在 description 前面更好读。
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100)
