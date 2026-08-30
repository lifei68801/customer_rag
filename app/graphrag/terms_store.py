from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

from app.db_migrations import add_column_if_missing
from app.graphrag.ontology import Term, load_terminology
from app.graphrag.ontology_categories import (
    ensure_categories_schema,
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
    extra_properties  TEXT NOT NULL DEFAULT '{}',
    source            TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (tenant_id, node_key)
);
CREATE INDEX IF NOT EXISTS idx_terms_tenant_standard_name
    ON terms(tenant_id, term_type, standard_name);
"""


class TermNotFoundError(Exception):
    """指定的 standard_name 在术语表里不存在。"""


class TermNameConflictError(Exception):
    """提交的 standard_name 或某个 alias，跟同一租户、同一 term_type 下
    另一个已存在的术语的 standard_name/alias 重复——所有"按名字查 Term"
    的调用路径（`app/graphrag/ontology.py::resolve_term`，供 LLM 抽取
    归一化、人工审核批准、RAG 检索工具统一复用）都按"这个名字在目标
    类型下只对应一条术语才算解析成功，出现两条以上视为歧义"的策略消歧；
    一旦同一类型内允许两条术语共享同一个名字/
    别名，这些调用路径就会把它们判定为无法解析（返回 None/"未找到"），
    而不是随便选中其中一个——这条约束就是防止这种歧义在写入时就产生，
    而不是留到查询时才发现。"""


class UnknownCategoryError(Exception):
    """提交的 term_type 不在全局分类枚举表里，或 extra_properties
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
            extra_properties  TEXT NOT NULL DEFAULT '{}',
            source            TEXT NOT NULL DEFAULT 'unknown',
            PRIMARY KEY (tenant_id, node_key)
        );
        """
    )
    await conn.execute(
        "INSERT INTO terms_new "
        "(tenant_id, node_key, standard_name, aliases, term_type, extra_properties, source) "
        "SELECT 'default', standard_name, standard_name, aliases, term_type, "
        "extra_properties, source FROM terms"
    )
    await conn.executescript(
        "DROP TABLE terms; ALTER TABLE terms_new RENAME TO terms; "
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_tenant_standard_name "
        "ON terms(tenant_id, standard_name);"
    )
    await conn.commit()


async def _migrate_terms_drop_product_line_column_if_needed(
    conn: aiosqlite.Connection,
) -> None:
    """把仍带着 product_line 列的 terms 表（tenant_id 已存在，只是还没删这一列
    的库——本项目实际开发库/生产库的常见情况）原地去掉这一列。SQLite 3.35+
    原生支持 ALTER TABLE ... DROP COLUMN（本项目实测 SQLite 3.49.1），不需要
    像 _migrate_terms_table_to_tenant_scoped_if_needed 那样建新表搬数据——
    product_line 只是普通 TEXT NOT NULL 列，不是主键的一部分、没有 CHECK/
    UNIQUE 约束、不被任何生成列引用，满足原生语法的适用条件。幂等：列已经
    不存在时直接跳过。不做删除前的数据备份，见
    docs/superpowers/specs/2026-08-19-remove-product-line-design.md 决策 2
    （这批数据本身没有实际区分意义，备份没有价值）。
    """
    cursor = await conn.execute("PRAGMA table_info(terms)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "product_line" not in existing_columns:
        return
    await conn.execute("ALTER TABLE terms DROP COLUMN product_line")
    await conn.commit()


async def _migrate_terms_standard_name_index_to_type_scoped_if_needed(
    conn: aiosqlite.Connection,
) -> None:
    """把 2026-08-22 之前"(tenant_id, standard_name)"这个跨类型全局唯一索引，
    收窄成"(tenant_id, term_type, standard_name)"——允许不同 term_type 的
    术语共享同一个 standard_name（真实场景：ETL 导入时"产品"类目下的
    "Coffee"和"类目"类目下的"Coffee"是两个不同的实体，见 2026-08-22 的
    bug 调查记录）。用 PRAGMA index_info 探测当前索引的列数，2 列（旧
    版本）就重建成 3 列，已经是 3 列或索引还不存在（全新库，稍后
    _SCHEMA_SQL 会按新定义建）都直接跳过——幂等，模式跟
    _migrate_terms_drop_product_line_column_if_needed 一致。
    """
    cursor = await conn.execute("PRAGMA index_info('idx_terms_tenant_standard_name')")
    columns = await cursor.fetchall()
    if len(columns) != 2:
        return
    await conn.execute("DROP INDEX idx_terms_tenant_standard_name")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_tenant_standard_name "
        "ON terms(tenant_id, term_type, standard_name)"
    )
    await conn.commit()


async def _migrate_terms_standard_name_index_drop_unique_if_needed(
    conn: aiosqlite.Connection,
) -> None:
    """把 (tenant_id, term_type, standard_name) 的唯一索引降级成普通索引。

    唯一性下沉到 node_key：terms 表的主键本来就是 (tenant_id, node_key)，
    身份约束已经在那里；standard_name 是展示名，同一 term_type 下允许重复
    （两个同名不同人的客户，各自有不同的 node_key）。见
    docs/superpowers/specs/2026-08-30-name-uniqueness-to-node-key-design.md。

    用 PRAGMA index_list 探测 unique 标志，已经是普通索引或索引不存在都
    直接跳过——幂等，模式跟上面两个迁移一致。

    注意这是一道单向门：降级之后如果真的写入了同类型同名的多条 Term，
    想回滚重建唯一索引会失败，必须先人工处理重名。
    """
    cursor = await conn.execute("PRAGMA index_list('terms')")
    rows = await cursor.fetchall()
    is_unique = any(
        row[1] == "idx_terms_tenant_standard_name" and row[2] == 1 for row in rows
    )
    if not is_unique:
        return
    await conn.execute("DROP INDEX idx_terms_tenant_standard_name")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_terms_tenant_standard_name "
        "ON terms(tenant_id, term_type, standard_name)"
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
    上线时term_type 还是自由文本，没有枚举表），自动把
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
        await add_column_if_missing(
            conn, table="terms", column="source", ddl="TEXT NOT NULL DEFAULT 'unknown'"
        )
        await _migrate_terms_table_to_tenant_scoped_if_needed(conn)
        await _migrate_terms_drop_product_line_column_if_needed(conn)
        await _migrate_terms_standard_name_index_to_type_scoped_if_needed(conn)
        await _migrate_terms_standard_name_index_drop_unique_if_needed(conn)
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    if not table_already_existed and seed_yaml_path is not None and seed_yaml_path.exists():
        try:
            for term in load_terminology(seed_yaml_path):
                await conn.execute(
                    "INSERT OR IGNORE INTO terms "
                    "(tenant_id, node_key, standard_name, aliases, term_type, "
                    "extra_properties) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        term.tenant_id,
                        term.node_key,
                        term.standard_name,
                        json.dumps(term.aliases, ensure_ascii=False),
                        term.term_type,
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
    出现过的去重实体类型值导入枚举表。按租户隔离，每次调用只处理一个租户。
    """
    known_types = await list_term_types(conn, tenant_id, status="confirmed")
    if known_types:
        return
    cursor = await conn.execute(
        "SELECT DISTINCT term_type FROM terms WHERE tenant_id = ?", (tenant_id,)
    )
    distinct_types = [row[0] for row in await cursor.fetchall()]
    if not distinct_types:
        return
    for value in distinct_types:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_term_types (tenant_id, value, extra_fields, status) "
            "VALUES (?, ?, '[]', 'confirmed')",
            (tenant_id, value),
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
        extra_properties=json.loads(row["extra_properties"]),
        source=row["source"],
    )


async def list_terms(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
    source: str | None = None,
) -> list[Term]:
    """limit=None（默认）返回该租户全部术语，保持既有调用方（agent 检索、
    摄取管线、eval runner、review_cli 等，见 app/api/agent_routes.py 等处
    直接调用本函数的调用点）不传这两个参数时的行为不变；管理后台
    分页时显式传入具体的 limit/offset。哨兵模式与
    app/graphrag/review_queue.py::list_pending_reviews 一致：SQLite 的
    LIMIT 取负数即表示不限制行数，用 -1 承载 limit=None 这个语义。

    source=None（默认）不按来源过滤；传具体值（manual/etl/review/unknown）
    只返回该来源的行，供"实体列表"页的来源筛选用。
    """
    conn.row_factory = aiosqlite.Row
    if source is None:
        cursor = await conn.execute(
            "SELECT tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source FROM terms WHERE tenant_id = ? "
            "ORDER BY standard_name LIMIT ? OFFSET ?",
            (tenant_id, limit if limit is not None else -1, offset),
        )
    else:
        cursor = await conn.execute(
            "SELECT tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source FROM terms WHERE tenant_id = ? AND source = ? "
            "ORDER BY standard_name LIMIT ? OFFSET ?",
            (tenant_id, source, limit if limit is not None else -1, offset),
        )
    rows = await cursor.fetchall()
    return [_row_to_term(row) for row in rows]


async def count_terms(
    conn: aiosqlite.Connection, tenant_id: str, *, source: str | None = None
) -> int:
    if source is None:
        cursor = await conn.execute("SELECT COUNT(*) FROM terms WHERE tenant_id = ?", (tenant_id,))
        row = await cursor.fetchone()
        return row[0]
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE tenant_id = ? AND source = ?", (tenant_id, source)
    )
    row = await cursor.fetchone()
    return row[0]


async def list_node_keys_by_term_type(
    conn: aiosqlite.Connection, tenant_id: str, term_type: str
) -> set[str]:
    """该租户、该类型下已存在的全部 node_key。

    给 ETL 关系写入路径做"端点实体是否真的写进来过"的批量校验用：调用方
    在处理某个关系映射的整个源文件之前查一次、拿着这个集合逐行判断，
    不在行循环里反复查库——跟 _write_entity_mapping 预取 extra_field_specs
    是同一个模式（设计文档第 6.4 节给出的真实规模是 18 万+ 行）。

    只 SELECT node_key，不走 list_terms：那个函数会把 aliases /
    extra_properties 一并读出来反序列化成 Term 对象，而这里只需要身份键。
    """
    cursor = await conn.execute(
        "SELECT node_key FROM terms WHERE tenant_id = ? AND term_type = ?",
        (tenant_id, term_type),
    )
    return {row[0] for row in await cursor.fetchall()}


async def get_term(
    conn: aiosqlite.Connection,
    tenant_id: str,
    standard_name: str,
    term_type: str | None = None,
) -> Term:
    """term_type 不传（默认，向后兼容旧调用方）：按 (tenant_id,
    standard_name) 查，不区分类型——多个同名不同类型的术语存在时返回
    其中任意一条（哪条由 SQLite 的行序决定，不保证稳定）。传了
    term_type：精确按 (tenant_id, term_type, standard_name) 定位，同名
    不同类型也能查到正确的那一条。新的调用方（admin_terms_routes.py
    的编辑/删除路由）应该总是传这个参数。
    """
    conn.row_factory = aiosqlite.Row
    if term_type is None:
        cursor = await conn.execute(
            "SELECT tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source FROM terms WHERE tenant_id = ? AND standard_name = ?",
            (tenant_id, standard_name),
        )
    else:
        cursor = await conn.execute(
            "SELECT tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source FROM terms WHERE tenant_id = ? AND standard_name = ? "
            "AND term_type = ?",
            (tenant_id, standard_name, term_type),
        )
    row = await cursor.fetchone()
    if row is None:
        raise TermNotFoundError(f"术语不存在: {standard_name}")
    return _row_to_term(row)


async def get_term_by_node_key(
    conn: aiosqlite.Connection, tenant_id: str, node_key: str
) -> Term:
    """按 (tenant_id, node_key) 精确定位一条术语。

    node_key 是主键，永远唯一；get_term 按 standard_name 定位，在
    standard_name 唯一索引降级之后（2026-08-30）已经不再能唯一确定一条
    记录。所有"定位某一条具体术语"的调用都应该用这个函数。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id, node_key, standard_name, aliases, term_type, "
        "extra_properties, source FROM terms WHERE tenant_id = ? AND node_key = ?",
        (tenant_id, node_key),
    )
    row = await cursor.fetchone()
    if row is None:
        raise TermNotFoundError(f"术语不存在: node_key={node_key!r}")
    return _row_to_term(row)


async def _check_name_conflict(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    term_type: str,
    standard_name: str,
    aliases: list[str],
    exclude_node_key: str | None = None,
) -> None:
    """检查 standard_name 和 aliases 有没有跟同一租户、同一 term_type 下别的
    术语（编辑时排除自己）的 standard_name/alias 重叠。按 (租户, 类型)
    扫描——不同类型之间允许共享同一个名字/别名（2026-08-22 起，见
    idx_terms_tenant_standard_name 的新定义），不同租户之间也允许，见
    Global Constraints"标准名同类型内唯一"。

    exclude_node_key 按身份（node_key）排除"自己"，不能按名字排除——
    2026-08-22 起 standard_name 不再租户内全局唯一，如果编辑操作同时改了
    term_type（该记录第一次进入目标类型的 same_type_terms 集合），按旧名字
    排除会误伤一个只是恰好同名、但其实是完全不相关的术语（该术语在目标
    类型下可能早就存在，旧的按名字排除会让这次冲突检查对它视而不见），
    见 2026-08-23 C1 修复的调查记录。
    """
    tenant_terms = await list_terms(conn, tenant_id)
    same_type_terms = [t for t in tenant_terms if t.term_type == term_type]
    candidate_names = {standard_name, *aliases}
    for term in same_type_terms:
        if term.node_key == exclude_node_key:
            continue
        existing_names = {term.standard_name, *term.aliases}
        overlap = candidate_names & existing_names
        if overlap:
            conflicting = next(iter(overlap))
            raise TermNameConflictError(
                f"{conflicting!r} 已经是同类型（{term_type!r}）术语 "
                f"{term.standard_name!r} 的别名/标准名，不能重复使用"
            )


async def _validate_categories(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    term_type: str,
    extra_properties: dict[str, object],
    existing_extra_property_keys: frozenset[str] = frozenset(),
) -> None:
    """term_type 校验按租户过滤——每个租户只能使用该租户下注册的分类。

    字段名校验（是否在白名单里）和字段值类型校验（是否匹配声明的
    value_type）是两道独立的检查：existing_extra_property_keys 里的
    "已废弃字段"只豁免字段名检查，不再做类型检查（因为它已经不在
    declared_by_name 里，无法判断"应该是什么类型"）——这是延续本体
    基座计划"移除字段声明不触碰已有数据"的原则，见 Global Constraints。
    """
    types = await list_term_types(conn, tenant_id, status="confirmed")
    types_by_value = {t.value: t for t in types}
    if term_type not in types_by_value:
        raise UnknownCategoryError(f"未知分类: {term_type!r}")
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
    extra_properties: dict[str, object] | None = None,
    source: str = "manual",
) -> None:
    """node_key 取 "{term_type}:{standard_name}"（2026-08-22 起，与 ETL
    引擎 compute_node_key 的格式风格一致——冒号分隔、term_type 打头），
    保证不同类型即使标准名相同也不会撞主键 (tenant_id, node_key)。
    历史数据（2026-08-22 之前创建）的 node_key 不回填，仍是不带前缀的
    纯 standard_name，见 Global Constraints。

    source 记录这条术语最初是通过哪个渠道创建的（manual/etl/review），
    默认值 "manual" 只是为了不用逐个改动测试里大量既有的 create_term()
    调用——本计划里唯一真正的生产调用点是 admin_terms_routes.py 的
    create_new_term，它现在只会被"知识图谱审核"页的内联创建调用，会显式
    传 source="review"。见
    docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 C。
    """
    extra_properties = extra_properties or {}
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type,
        extra_properties=extra_properties,
    )
    await _check_name_conflict(
        conn, tenant_id=tenant_id, term_type=term_type,
        standard_name=standard_name, aliases=aliases,
    )
    node_key = f"{term_type}:{standard_name}"
    try:
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                node_key,
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                json.dumps(extra_properties, ensure_ascii=False),
                source,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(
            f"{standard_name!r} 已经是同类型（{term_type!r}）术语的标准名，不能重复创建"
        )
    await conn.commit()


async def update_term(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    new_standard_name: str,
    aliases: list[str],
    term_type: str,
    extra_properties: dict[str, object] | None = None,
) -> None:
    """node_key 是这条记录的身份键，必须精确定位到唯一一行——不能再像
    2026-08-30 之前那样按 standard_name 用 get_term()（内部 fetchone()）
    查回来：那次改造之后同一 term_type 下允许同名多条，按名字查回来的
    是哪一条完全由 SQLite 内部行序决定，管理员编辑 A 有可能实际改到了
    同名的 B（见该缺陷的调查记录）。new_standard_name 是提交的新名字，
    允许和当前名字相同（即不改名）；term_type 是改名后要写入的目标类型
    （可能与这条记录改之前的类型不同，即这次编辑同时改了类型），用于
    校验新名字在目标类型下是否冲突。

    node_key 本身不受这次调用影响，UPDATE 语句不写这一列——ADR-0003 的
    核心断言：身份键创建后永不改变。如果这次编辑同时改了 term_type，这条
    术语的 node_key（如果是 2026-08-22 之后创建、带类型前缀的）里的类型
    前缀会跟新的 term_type 不一致——这是已知、可接受的行为，node_key 前缀
    只反映创建时的类型，不是实时准确的。

    UPDATE 语句不写 source 列——这是刻意的：source 只记录创建时的渠道，
    人工编辑（无论改名、改别名还是改属性）都不改变它，见
    docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 C.4。
    """
    extra_properties = extra_properties or {}
    existing_term = await get_term_by_node_key(conn, tenant_id, node_key)
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type,
        extra_properties=extra_properties,
        existing_extra_property_keys=frozenset(existing_term.extra_properties),
    )
    await _check_name_conflict(
        conn, tenant_id=tenant_id, term_type=term_type,
        standard_name=new_standard_name, aliases=aliases,
        exclude_node_key=node_key,
    )
    try:
        await conn.execute(
            "UPDATE terms SET standard_name=?, aliases=?, term_type=?, "
            "extra_properties=? WHERE tenant_id=? AND node_key=?",
            (
                new_standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                json.dumps(extra_properties, ensure_ascii=False),
                tenant_id,
                node_key,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(
            f"{new_standard_name!r} 已经是同类型（{term_type!r}）术语的标准名，不能重复使用"
        )
    await conn.commit()


async def delete_term(
    conn: aiosqlite.Connection,
    tenant_id: str,
    node_key: str,
) -> None:
    """按 (tenant_id, node_key) 精确定位并删除这一条记录——不能再按
    standard_name 查（见 update_term 的同一段说明）：2026-08-30 之后同一
    term_type 下允许同名多条，按名字查回来的是哪一条不确定，删错一条会
    绕过路由层按图谱边数做的 409 安全检查，在 Neo4j 留下孤儿边。
    """
    term = await get_term_by_node_key(conn, tenant_id, node_key)
    await conn.execute(
        "DELETE FROM terms WHERE tenant_id=? AND node_key=?", (tenant_id, term.node_key)
    )
    await conn.commit()


async def migrate_term_type(
    conn: aiosqlite.Connection, tenant_id: str, *, old_type: str, new_type: str
) -> int:
    """把该租户 terms 表里 term_type 从旧值批量改成新值，返回受影响的行数。
    供"迁移实体类型"工具用——改名一个已确认的实体类型不会自动级联到这张
    表（见 ontology_categories.py::update_term_type 的说明），需要业务显式
    触发这个函数才会同步。
    """
    cursor = await conn.execute(
        "UPDATE terms SET term_type = ? WHERE tenant_id = ? AND term_type = ?",
        (new_type, tenant_id, old_type),
    )
    await conn.commit()
    return cursor.rowcount


async def upsert_term_with_node_key(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    extra_properties: dict[str, object] | None = None,
    source: str = "etl",
) -> None:
    """ETL 专用的幂等写入：按 (tenant_id, node_key) 判定冲突，已存在就更新，不存在
    就插入——不是 create_term/update_term 那种"创建 xor 更新"两态分支，是真正的
    upsert，与 Neo4j 侧 merge_relation/sync_term 的 MERGE 语义一致（见
    docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 5 节）。

    node_key 由调用方显式提供（ETL 场景下按每个租户 ETL 配置里声明的
    node_key_parts 算出，见 app/graphrag/schema_etl_row_processing.py::
    compute_node_key），不像 create_term 那样自动取 standard_name 的值——
    这是与 create_term/update_term 唯一的本质区别。

    standard_name 不再受唯一性约束——2026-08-30 起
    idx_terms_tenant_standard_name 降级为普通索引，唯一性下沉到
    (tenant_id, node_key)（表的主键）。同一 term_type 下两个 node_key
    不同的术语允许共享同一个 standard_name（复合 node_key 场景下这是
    合法状态：两个真实存在、同名的不同实体），见
    docs/superpowers/specs/2026-08-30-name-uniqueness-to-node-key-design.md。

    source 默认值 "etl"——这个函数目前唯一的生产调用点就是
    schema_etl.py::_write_entity_mapping，不需要显式传参也总是正确的。

    注意：ON CONFLICT ... DO UPDATE SET 故意不包含 source = excluded.source——
    已存在的行（哪怕是被 ETL 再次 upsert）保留它最初的 source，这与
    update_term 不碰 source 列是同一个道理的两种写法（这里是 upsert 语句
    层面的对应处理）。
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
        conn, tenant_id=tenant_id, term_type=term_type,
        extra_properties=extra_properties,
        existing_extra_property_keys=existing_extra_property_keys,
    )
    try:
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (tenant_id, node_key) DO UPDATE SET "
            "standard_name = excluded.standard_name, aliases = excluded.aliases, "
            "term_type = excluded.term_type, "
            "extra_properties = excluded.extra_properties",
            (
                tenant_id,
                node_key,
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                json.dumps(extra_properties, ensure_ascii=False),
                source,
            ),
        )
    except aiosqlite.IntegrityError:
        # standard_name 的唯一索引在 2026-08-30 已降级为普通索引，重名不再
        # 触发这里；剩下能命中的只有 NOT NULL 之类的约束违例，原样抛出，
        # 不要再冒充"标准名冲突"——那会把一个 schema 问题误报成数据问题。
        raise
    await conn.commit()


_TOMBSTONE_PREFIX = "[已合并] "


def _tombstone_name(node_key: str) -> str:
    """merge_terms 把被合并那条 Term 的 standard_name 重写成这个格式
    （"合并进了别的术语"的墓碑标记）——这是这个格式唯一的生成位置，其它
    需要构造/识别这个字符串的地方（is_tombstoned 本身、merge_terms 内部
    墓碑化/恢复两处调用）都通过这里，不重复拼字面量。"""
    return f"{_TOMBSTONE_PREFIX}{node_key}"


def is_tombstoned(term: Term) -> bool:
    """term 是否已经是一条被 merge_terms 合并掉的墓碑行。

    墓碑行的 standard_name 字面包含了被合并前的原始名字（"[已合并]
    {node_key}"，node_key 通常带着原 standard_name），如果不过滤掉，
    duplicate_detection_worker.py 的批量扫描、admin_terms_routes.py
    创建术语时的相似度提示，都可能把墓碑行的字符串当成一个正常术语去跟
    别的术语比相似度——同类型里名字凑巧是墓碑串子串的术语很容易因此
    被算出很高的相似度分（甚至 1.0），造成"建议合并一个已经被合并过的
    行"这种垃圾建议，一旦被批准还会把墓碑串本身当垃圾数据写进另一条
    术语的 aliases。所有需要判断"这条术语是不是已经不该再参与重复检测"
    的地方都应该调这个函数，不要自己写 standard_name.startswith(...)。"""
    return term.standard_name.startswith(_TOMBSTONE_PREFIX)


async def merge_terms(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    keep_node_key: str,
    merged_node_key: str,
) -> None:
    """把 merged_node_key 这条 Term 合并进 keep_node_key 那条：merged 那条的
    standard_name，连同它自己已有的全部 aliases，一起追加进 keep 那条的
    aliases（去重）——不是只追加 standard_name，否则 merged 那条自己的
    别名会变成孤儿，resolve_term() 再也找不回它们。merged 那条本身不删除
    （node_key 可能已经被 Neo4j 图数据引用，删除会破坏引用完整性），改成
    "墓碑化"：把它的 standard_name 重写成一个不会跟真实术语碰撞的占位名、
    aliases 清空，之后 resolve_term() 统一路由到 keep 那条。

    两个 node_key 有任意一个在这个租户下不存在，抛 TermNotFoundError。

    实现是"先墓碑化 merged 那条，再追加到 keep 那条"两步 update_term()
    调用，不是真正的数据库事务（update_term() 内部各自 commit）：必须先
    墓碑化，否则 merged 那条自己的 standard_name/aliases 还在，会跟马上
    要追加到 keep 那条上的同样字符串撞上 update_term() 的别名冲突检查
    （_check_name_conflict）。第二步失败时会尝试把 merged 那条恢复成
    合并前的状态，再重新抛出原始异常；如果连恢复本身也失败（比如恢复的
    目标名字这期间被并发写入抢占），会记一条 ERROR 日志留下人工核对/
    手动恢复所需的全部信息，然后依然重新抛出触发这整条回滚路径的原始
    异常（不是恢复失败的异常）——调用方关心的是"为什么合并失败"，恢复
    失败是已经被记录下来的次要问题，不应该掩盖掉主要异常。
    """
    terms = await list_terms(conn, tenant_id)
    terms_by_node_key = {t.node_key: t for t in terms}
    keep_term = terms_by_node_key.get(keep_node_key)
    merged_term = terms_by_node_key.get(merged_node_key)
    if keep_term is None or merged_term is None:
        raise TermNotFoundError(
            f"待合并的术语不存在: keep={keep_node_key!r}, merged={merged_node_key!r}"
        )

    merged_original_standard_name = merged_term.standard_name
    merged_original_aliases = list(merged_term.aliases)

    # Step 1: 墓碑化 merged 那条——先清空它的名字/别名占用，避免 Step 2
    # 追加同样的字符串到 keep 那条时撞上别名冲突检查。
    await update_term(
        conn,
        tenant_id=tenant_id,
        node_key=merged_term.node_key,
        new_standard_name=_tombstone_name(merged_term.node_key),
        aliases=[],
        term_type=merged_term.term_type,
        extra_properties=merged_term.extra_properties,
    )

    # Step 2: 把 merged 那条合并前的 standard_name/aliases 追加到 keep 那条。
    merged_aliases = list(dict.fromkeys(
        [*keep_term.aliases, merged_original_standard_name, *merged_original_aliases]
    ))
    try:
        await update_term(
            conn,
            tenant_id=tenant_id,
            node_key=keep_term.node_key,
            new_standard_name=keep_term.standard_name,
            aliases=merged_aliases,
            term_type=keep_term.term_type,
            extra_properties=keep_term.extra_properties,
        )
    except Exception as append_exc:
        try:
            await update_term(
                conn,
                tenant_id=tenant_id,
                node_key=merged_term.node_key,
                new_standard_name=merged_original_standard_name,
                aliases=merged_original_aliases,
                term_type=merged_term.term_type,
                extra_properties=merged_term.extra_properties,
            )
        except Exception as compensation_exc:
            logger.error(
                "合并术语失败且补偿恢复也失败：tenant_id=%r keep_node_key=%r "
                "merged_node_key=%r 的原始 standard_name=%r、aliases=%r 已经从"
                "数据库丢失（该行目前仍是墓碑状态 standard_name=%r, aliases=[]），"
                "需要人工核对/手动恢复。触发合并失败的原始异常=%r，"
                "补偿恢复自身失败的异常=%r",
                tenant_id, keep_node_key, merged_node_key,
                merged_original_standard_name, merged_original_aliases,
                _tombstone_name(merged_term.node_key), append_exc, compensation_exc,
                exc_info=True,
            )
            raise append_exc from compensation_exc
        logger.warning(
            "合并术语 tenant_id=%r keep_node_key=%r merged_node_key=%r 追加别名"
            "步骤失败，已回滚：把 merged 那条的 standard_name/aliases 恢复为"
            "合并前的状态（standard_name=%r, aliases=%r）。原始异常=%r",
            tenant_id, keep_node_key, merged_node_key,
            merged_original_standard_name, merged_original_aliases, append_exc,
        )
        raise
