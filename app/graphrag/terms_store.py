from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

from app.db_migrations import add_column_if_missing
from app.graphrag.ontology import Term, load_terminology
from app.graphrag.ontology_categories import (
    ensure_categories_schema,
    list_product_lines,
    list_term_types,
)

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terms (
    tenant_id         TEXT NOT NULL,
    node_key          TEXT NOT NULL,
    standard_name     TEXT NOT NULL,
    aliases           TEXT NOT NULL,
    term_type         TEXT NOT NULL,
    product_line      TEXT NOT NULL,
    extra_properties  TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, node_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_tenant_standard_name
    ON terms(tenant_id, standard_name);
"""


class TermNotFoundError(Exception):
    """指定的 standard_name 在术语表里不存在。"""


class TermNameConflictError(Exception):
    """提交的 standard_name 或某个 alias，跟另一个已存在的术语的
    standard_name/alias 重复——resolve_to_standard_name() 按顺序遍历命中
    第一个匹配就返回，允许重叠会让抽取结果变成"看列表顺序"决定的、
    不可预测。"""


class UnknownCategoryError(Exception):
    """提交的 term_type/product_line 不在全局分类枚举表里，或 extra_properties
    里出现了该 term_type 没有声明过的字段名——本体 schema 基座计划把这两项从
    "自由文本、无校验" 收紧成硬约束，理由见
    docs/superpowers/specs/2026-08-14-ontology-schema-design.md 第 3 节。"""


class InvalidExtraPropertyTypeError(Exception):
    """extra_properties 里某个值不匹配该字段在 term_type 上声明的 value_type。"""


async def _migrate_terms_table_to_tenant_scoped_if_needed(
    conn: aiosqlite.Connection,
) -> None:
    """把 2026-08-15 之前的 terms 表（standard_name 主键，无 tenant_id/
    node_key）原地迁移成按租户隔离的新结构。只在表已存在且还是老结构时
    执行，幂等——已经是新结构（有 tenant_id 列）直接跳过。存量数据统一
    归到 tenant_id='default'，node_key 回填成当时的 standard_name 值
    （Global Constraints 的 node_key 生成规则）。SQLite 不支持 ALTER
    TABLE 改主键，只能建新表 + 搬数据 + 删旧表 + 改名。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='terms'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute("PRAGMA table_info(terms)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "tenant_id" in existing_columns:
        return
    await conn.executescript(
        """
        CREATE TABLE terms_new (
            tenant_id         TEXT NOT NULL,
            node_key          TEXT NOT NULL,
            standard_name     TEXT NOT NULL,
            aliases           TEXT NOT NULL,
            term_type         TEXT NOT NULL,
            product_line      TEXT NOT NULL,
            extra_properties  TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (tenant_id, node_key)
        );
        """
    )
    await conn.execute(
        "INSERT INTO terms_new "
        "(tenant_id, node_key, standard_name, aliases, term_type, product_line, extra_properties) "
        "SELECT 'default', standard_name, standard_name, aliases, term_type, product_line, "
        "extra_properties FROM terms"
    )
    await conn.executescript(
        "DROP TABLE terms; ALTER TABLE terms_new RENAME TO terms; "
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_tenant_standard_name "
        "ON terms(tenant_id, standard_name);"
    )
    await conn.commit()


async def ensure_terms_schema(
    conn: aiosqlite.Connection, *, seed_yaml_path: Path | None = None
) -> None:
    """幂等建表/迁移。

    seed_yaml_path 只在传入且指向一个存在的文件、同时这张表是刚刚第一次
    被创建（不是已经存在）时才生效：从这个 YAML 文件里一次性导入内容，
    此后这份 YAML 不再被任何代码路径读取。导入的每条术语 tenant_id 固定
    是 "default"（见 ontology.py::load_terminology 的说明）。

    向后兼容桥接：分类枚举表为空、但 terms 表已经有历史数据（老版本
    上线时term_type/product_line 还是自由文本，没有枚举表），自动把
    历史数据里出现过的去重值导入枚举表——_bridge_seed_categories_from_
    existing_terms 现在按租户隔离（只处理传入的单个 tenant_id，查询/
    写入 ontology_term_types 时带 tenant_id 过滤），这里固定传
    tenant_id="default"，因为桥接的历史数据本来就是迁移前统一归属
    "default" 租户的存量数据（见上面 seed_yaml_path 段落的说明）。
    """
    await ensure_categories_schema(conn)
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='terms'"
    )
    table_already_existed = await cursor.fetchone() is not None
    if table_already_existed:
        await add_column_if_missing(
            conn, table="terms", column="extra_properties", ddl="TEXT NOT NULL DEFAULT '{}'"
        )
        await _migrate_terms_table_to_tenant_scoped_if_needed(conn)
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    if not table_already_existed and seed_yaml_path is not None and seed_yaml_path.exists():
        try:
            for term in load_terminology(seed_yaml_path):
                await conn.execute(
                    "INSERT OR IGNORE INTO terms "
                    "(tenant_id, node_key, standard_name, aliases, term_type, product_line, "
                    "extra_properties) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        term.tenant_id,
                        term.node_key,
                        term.standard_name,
                        json.dumps(term.aliases, ensure_ascii=False),
                        term.term_type,
                        term.product_line,
                        json.dumps(term.extra_properties, ensure_ascii=False),
                    ),
                )
            await conn.commit()
            cursor = await conn.execute("SELECT COUNT(*) FROM terms")
            row = await cursor.fetchone()
            logger.info("术语表首次建表：从 %s 导入了 %d 条术语", seed_yaml_path, row[0])
        except Exception:
            logger.warning(
                "术语表首次建表，种子文件 %s 解析/导入失败，术语表保持为空",
                seed_yaml_path, exc_info=True,
            )
    elif not table_already_existed:
        logger.warning(
            "术语表首次建表，但未找到种子文件%s——术语表当前为空，"
            "需要通过管理后台手动添加术语，否则知识图谱抽取的术语归一化"
            "将始终落到人工审核队列",
            f"（{seed_yaml_path}）" if seed_yaml_path is not None else "",
        )
    await _bridge_seed_categories_from_existing_terms(conn, tenant_id="default")


async def _bridge_seed_categories_from_existing_terms(
    conn: aiosqlite.Connection, *, tenant_id: str
) -> None:
    """桥接函数：分类枚举表为空、但 terms 表已经有历史数据时，把该租户历史数据里
    出现过的去重分类值导入枚举表。按租户隔离，每次调用只处理一个租户。
    """
    known_types = await list_term_types(conn, tenant_id)
    known_lines = await list_product_lines(conn)
    if known_types or known_lines:
        return
    cursor = await conn.execute(
        "SELECT DISTINCT term_type FROM terms WHERE tenant_id = ?", (tenant_id,)
    )
    distinct_types = [row[0] for row in await cursor.fetchall()]
    cursor = await conn.execute(
        "SELECT DISTINCT product_line FROM terms WHERE tenant_id = ?", (tenant_id,)
    )
    distinct_lines = [row[0] for row in await cursor.fetchall()]
    if not distinct_types and not distinct_lines:
        return
    for value in distinct_types:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_term_types "
            "(tenant_id, value, extra_fields, node_key_template) VALUES (?, ?, '[]', '')",
            (tenant_id, value),
        )
    for value in distinct_lines:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_product_lines (value) VALUES (?)", (value,)
        )
    await conn.commit()


def _extra_property_value_matches_type(value: object, value_type: str) -> bool:
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number[]":
        return isinstance(value, list) and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
        )
    return False


def _row_to_term(row: aiosqlite.Row) -> Term:
    return Term(
        tenant_id=row["tenant_id"],
        node_key=row["node_key"],
        standard_name=row["standard_name"],
        aliases=json.loads(row["aliases"]),
        term_type=row["term_type"],
        product_line=row["product_line"],
        extra_properties=json.loads(row["extra_properties"]),
    )


async def list_terms(conn: aiosqlite.Connection, tenant_id: str) -> list[Term]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id, node_key, standard_name, aliases, term_type, product_line, "
        "extra_properties FROM terms WHERE tenant_id = ? ORDER BY standard_name",
        (tenant_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_term(row) for row in rows]


async def get_term(conn: aiosqlite.Connection, tenant_id: str, standard_name: str) -> Term:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id, node_key, standard_name, aliases, term_type, product_line, "
        "extra_properties FROM terms WHERE tenant_id = ? AND standard_name = ?",
        (tenant_id, standard_name),
    )
    row = await cursor.fetchone()
    if row is None:
        raise TermNotFoundError(f"术语不存在: {standard_name}")
    return _row_to_term(row)


async def _check_name_conflict(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    standard_name: str,
    aliases: list[str],
    exclude_standard_name: str | None = None,
) -> None:
    """检查 standard_name 和 aliases 有没有跟同一租户下别的术语（编辑时
    排除自己）的 standard_name/alias 重叠。按租户扫描，不同租户之间允许
    使用相同的名字/别名——见 Global Constraints"node_key/standard_name
    只需租户内唯一"。
    """
    tenant_terms = await list_terms(conn, tenant_id)
    candidate_names = {standard_name, *aliases}
    for term in tenant_terms:
        if term.standard_name == exclude_standard_name:
            continue
        existing_names = {term.standard_name, *term.aliases}
        overlap = candidate_names & existing_names
        if overlap:
            conflicting = next(iter(overlap))
            raise TermNameConflictError(
                f"{conflicting!r} 已经是术语 {term.standard_name!r} 的别名/标准名，不能重复使用"
            )


async def _validate_categories(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    term_type: str,
    product_line: str,
    extra_properties: dict[str, object],
    existing_extra_property_keys: frozenset[str] = frozenset(),
) -> None:
    """product_line 校验保持全局（不受本次改造影响）。term_type 校验
    按租户过滤——每个租户只能使用该租户下注册的分类。

    字段名校验（是否在白名单里）和字段值类型校验（是否匹配声明的
    value_type）是两道独立的检查：existing_extra_property_keys 里的
    "已废弃字段"只豁免字段名检查，不再做类型检查（因为它已经不在
    declared_by_name 里，无法判断"应该是什么类型"）——这是延续本体
    基座计划"移除字段声明不触碰已有数据"的原则，见 Global Constraints。
    """
    types = await list_term_types(conn, tenant_id)
    types_by_value = {t.value: t for t in types}
    if term_type not in types_by_value:
        raise UnknownCategoryError(f"未知分类: {term_type!r}")
    if product_line not in await list_product_lines(conn):
        raise UnknownCategoryError(f"未知产品线: {product_line!r}")
    declared_by_name = {f.name: f for f in types_by_value[term_type].extra_fields}
    declared_fields = set(declared_by_name)
    unknown = set(extra_properties) - declared_fields - existing_extra_property_keys
    if unknown:
        raise UnknownCategoryError(
            f"分类 {term_type!r} 没有声明这些属性字段: {sorted(unknown)}"
        )
    for key, value in extra_properties.items():
        if key not in declared_fields:
            continue
        spec = declared_by_name[key]
        if not _extra_property_value_matches_type(value, spec.value_type):
            raise InvalidExtraPropertyTypeError(
                f"字段 {key!r} 的值 {value!r} 不符合声明的类型 {spec.value_type!r}"
            )


async def create_term(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
    extra_properties: dict[str, object] | None = None,
) -> None:
    """node_key 创建时直接取 standard_name 的值（Global Constraints 的
    node_key 生成规则：extraction 模式下没有外部稳定码来源）。"""
    extra_properties = extra_properties or {}
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type, product_line=product_line,
        extra_properties=extra_properties,
    )
    await _check_name_conflict(conn, tenant_id=tenant_id, standard_name=standard_name, aliases=aliases)
    try:
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "product_line, extra_properties) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                standard_name,
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                json.dumps(extra_properties, ensure_ascii=False),
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{standard_name!r} 已经是已有术语的标准名，不能重复创建")
    await conn.commit()


async def update_term(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    standard_name: str,
    new_standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
    extra_properties: dict[str, object] | None = None,
) -> None:
    """standard_name 是当前（改名前）的名字，用来定位这条记录；
    new_standard_name 是提交的新名字，允许和 standard_name 相同（即不改名）。
    node_key 不受影响，UPDATE 语句不写这一列——ADR-0003 的核心断言：
    身份键创建后永不改变，即使术语被改名。
    """
    extra_properties = extra_properties or {}
    existing_term = await get_term(conn, tenant_id, standard_name)
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type, product_line=product_line,
        extra_properties=extra_properties,
        existing_extra_property_keys=frozenset(existing_term.extra_properties),
    )
    await _check_name_conflict(
        conn, tenant_id=tenant_id, standard_name=new_standard_name, aliases=aliases,
        exclude_standard_name=standard_name,
    )
    try:
        await conn.execute(
            "UPDATE terms SET standard_name=?, aliases=?, term_type=?, product_line=?, "
            "extra_properties=? WHERE tenant_id=? AND node_key=?",
            (
                new_standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                json.dumps(extra_properties, ensure_ascii=False),
                tenant_id,
                existing_term.node_key,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{new_standard_name!r} 已经是已有术语的标准名，不能重复使用")
    await conn.commit()


async def delete_term(conn: aiosqlite.Connection, tenant_id: str, standard_name: str) -> None:
    await get_term(conn, tenant_id, standard_name)
    await conn.execute(
        "DELETE FROM terms WHERE tenant_id=? AND standard_name=?", (tenant_id, standard_name)
    )
    await conn.commit()


async def upsert_term_with_node_key(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
    extra_properties: dict[str, object] | None = None,
) -> None:
    """ETL 专用的幂等写入：按 (tenant_id, node_key) 判定冲突，已存在就更新，不存在
    就插入——不是 create_term/update_term 那种"创建 xor 更新"两态分支，是真正的
    upsert，与 Neo4j 侧 merge_relation/sync_term 的 MERGE 语义一致（见
    docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 5 节）。

    node_key 由调用方显式提供（按 node_key_template 算出），不像 create_term 那样
    自动取 standard_name 的值——这是与 create_term/update_term 唯一的本质区别。

    standard_name 的租户内唯一性约束（idx_terms_tenant_standard_name）仍然生效：
    如果这个 standard_name 已经被另一个 node_key 占用，抛 TermNameConflictError。
    """
    extra_properties = extra_properties or {}
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT extra_properties FROM terms WHERE tenant_id = ? AND node_key = ?",
        (tenant_id, node_key),
    )
    existing_row = await cursor.fetchone()
    existing_extra_property_keys = (
        frozenset(json.loads(existing_row["extra_properties"]))
        if existing_row is not None else frozenset()
    )
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type, product_line=product_line,
        extra_properties=extra_properties,
        existing_extra_property_keys=existing_extra_property_keys,
    )
    try:
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "product_line, extra_properties) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (tenant_id, node_key) DO UPDATE SET "
            "standard_name = excluded.standard_name, aliases = excluded.aliases, "
            "term_type = excluded.term_type, product_line = excluded.product_line, "
            "extra_properties = excluded.extra_properties",
            (
                tenant_id,
                node_key,
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                json.dumps(extra_properties, ensure_ascii=False),
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(
            f"{standard_name!r} 已经是租户 {tenant_id!r} 下另一个术语的标准名，无法写入"
        )
    await conn.commit()
