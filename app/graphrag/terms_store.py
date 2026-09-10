from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from app.db_migrations import add_column_if_missing
from app.graphrag.attribute_conflicts import record_conflict
from app.graphrag.date_normalization import is_normalized_date
from app.graphrag.ontology import Term, load_terminology
from app.graphrag.ontology_categories import (
    ensure_categories_schema,
    list_term_types,
)
from app.graphrag.term_edits_store import (
    FIELD_CREATED,
    FIELD_DELETED,
    list_term_edits,
    list_term_edits_for_node_key,
    upsert_term_edit,
)
from app.graphrag.term_merge import apply_edits

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
    -- 每个属性字段各自来自哪次导入：{字段名: 来源}。见
    -- ensure_terms_schema 里那条 add_column_if_missing 上方的说明。
    extra_property_sources TEXT NOT NULL DEFAULT '{}',
    -- 逐字段：这个值来自源文件的第几行。跟 extra_property_sources 平行的一份
    -- {field: row_number}，冲突页据此显示「39 来自 商品表.xlsx 第 88 行」。
    extra_property_source_rows TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, node_key)
);
CREATE INDEX IF NOT EXISTS idx_terms_tenant_standard_name
    ON terms(tenant_id, term_type, standard_name);
"""


class TermNotFoundError(Exception):
    """按 node_key 或 standard_name 定位的术语在术语表里不存在。"""


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
        # 每个属性字段各自来自哪次导入。JSON 对象 {字段名: 来源}。
        #
        # 为什么逐字段而不是整行记一个"最近导入来源"：属性冲突页要显示
        # 「39 来自 商品表.xlsx」。整行只记一个的话，A 表写了售价、B 表写了
        # 产地之后那个值是 B——于是售价那条冲突会声称 39 来自 B 表，而它其实
        # 来自 A 表。一个看起来精确、实际是错的来源比不显示来源更糟：审核员
        # 会据此挑错边。
        #
        # 只有带 incoming_source 调用 upsert_term_with_node_key 时才写这一列。
        # 这次改动之前的存量行留空，读的时候退回行级的 source 渠道值。
        #
        # **必须排在上面那几条重建型迁移之后。**
        # _migrate_terms_table_to_tenant_scoped_if_needed 会 CREATE terms_new
        # + 拷数据 + RENAME，而它那份 DDL 里没有这一列——排在它前面的话，
        # 刚加的列会被那次重建原地丢掉，而下面的 _SCHEMA_SQL 是
        # CREATE TABLE IF NOT EXISTS，表已经在了，补不回来。结果是本次启动
        # 之后每一次 upsert 都在 SELECT 那一行抛 no such column，重启才自愈
        # ——只有最老的那批存量库会踩，而它们恰恰是最没人盯着的。
        await add_column_if_missing(
            conn, table="terms", column="extra_property_sources",
            ddl="TEXT NOT NULL DEFAULT '{}'",
        )
        # 同上，同一个位置：也必须排在重建型迁移之后。
        await add_column_if_missing(
            conn, table="terms", column="extra_property_source_rows",
            ddl="TEXT NOT NULL DEFAULT '{}'",
        )
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
    if value_type == "date":
        # 判的是"已经是补零 ISO"，不是"能不能归一"——这是写库前的最后一道闸，
        # 放行一个没补零的值就等于把破坏字典序的数据放进图里（见
        # date_normalization.is_normalized_date 的说明）。ETL 路径下
        # convert_field_value 已经归一化过，这里理应总是符合；但校验不能因为
        # "调用方应该已经做对了"就跳过，别的写入路径（如 admin_terms_routes.py
        # 的手工编辑）不经过 convert_field_value。
        return isinstance(value, str) and is_normalized_date(value)
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


async def list_terms_merged(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
    source: str | None = None,
    term_type: str | None = None,
) -> list[Term]:
    """管道产出叠加人工编辑之后的术语列表——**所有读路径都该走这个**，
    而不是 list_terms。

    参数与 list_terms 一致。注意 limit/offset 作用在 terms 表的查询上，
    合并发生在之后：被 __deleted__ 排除掉的行会让这一页少几条，纯编辑层
    创建的实体则追加在末尾。分页的精确性让位于"读到的一定是合并结果"
    ——后者是本设计的保证，前者只是列表页的观感。

    source 过滤对**合并后的整体结果**生效，不只是 terms 表那次查询：
    apply_edits 追加的纯编辑层创建实体固定 source="review"（见
    term_merge._synthesize_created），不受上面那次 SQL 过滤约束——传了
    具体来源（比如管理后台的来源筛选传 "etl"）就不该把它们也带出来。
    传 source=None（默认）时这一步是空操作，不改变既有行为。
    """
    terms = await list_terms(conn, tenant_id, limit=limit, offset=offset, source=source)
    edits = await list_term_edits(conn, tenant_id)
    merged = apply_edits(terms, edits, tenant_id=tenant_id)
    if source is not None:
        merged = [term for term in merged if term.source == source]
    if term_type is not None:
        # 跟 source 同理，在合并之后过滤：人工把一个实体的类型从 A 改成 B
        # 之后，它就该出现在 B 下面。在 SQL 层筛会认原始值，等于让编辑层
        # 对这个视图失效。
        merged = [term for term in merged if term.term_type == term_type]
    return merged


async def get_term_merged_by_node_key(
    conn: aiosqlite.Connection, tenant_id: str, node_key: str
) -> Term:
    """按 node_key 取单条的合并结果。

    实体被 __deleted__ 编辑标记过时抛 TermNotFoundError——对读路径而言
    它就是不存在，跟 terms 表里根本没有这一行不该有可观测的区别。
    """
    edits = await list_term_edits_for_node_key(conn, tenant_id, node_key)
    try:
        term = await get_term_by_node_key(conn, tenant_id, node_key)
    except TermNotFoundError:
        merged = apply_edits([], {node_key: edits}, tenant_id=tenant_id)
        if not merged:
            raise
        return merged[0]
    merged = apply_edits([term], {node_key: edits}, tenant_id=tenant_id)
    if not merged:
        raise TermNotFoundError(f"术语已被人工删除: {node_key}")
    return merged[0]


async def count_terms_merged_by_term_type(
    conn: aiosqlite.Connection, tenant_id: str
) -> dict[str, int]:
    """合并视图下每个 term_type 有多少实体——**列表页的分组摘要用这个**。

    跟 count_terms_by_term_type 的区别跟 count_terms_merged 之于 count_terms
    一样：那个数的是原始表（"管道往这里写了多少"），这个数的是列表里看得见
    的条数。摘要行的数字必须跟点进去看到的条数对得上，对不上的话用户会以为
    自己漏看了几条。

    实现上不走"全量合并再分组"：那要把整张表载入内存。改为在 SQL 的分组
    基数上按编辑层增减——term_edits 只包含被人工碰过的行，通常远小于 terms。
    被删空的类型不出现在结果里：挂一个 0 条的类型在列表上是个死链，点进去
    什么都没有。
    """
    counts = dict(await count_terms_by_term_type(conn, tenant_id))
    edits = await list_term_edits(conn, tenant_id)
    if not edits:
        return counts

    # 只查被编辑过的那几行在 terms 里的原始 term_type——要知道它们该从哪个
    # 类型里减掉。
    keys = list(edits.keys())
    placeholders = ",".join("?" * len(keys))
    cursor = await conn.execute(
        f"SELECT node_key, term_type FROM terms WHERE tenant_id = ? AND node_key IN ({placeholders})",
        (tenant_id, *keys),
    )
    original = {row[0]: row[1] for row in await cursor.fetchall()}

    for node_key, fields in edits.items():
        base_type = original.get(node_key)
        if FIELD_DELETED in fields:
            if base_type is not None:
                counts[base_type] = counts.get(base_type, 0) - 1
            continue
        # 编辑层改过类型：从原类型减掉，加到新类型上。纯编辑层创建的实体
        # （terms 里没有对应行）只加不减。
        # 编辑层用裸字段名存可替换字段（见 term_merge._REPLACEABLE_FIELDS），
        # term_type 没有专门的常量。
        new_type = fields.get("term_type", base_type)
        if new_type is None:
            continue
        if base_type is None:
            # 纯编辑层创建：terms 里没有对应行，只加不减。创建时的类型在
            # __created__ 的整对象里，不在裸字段上。
            created = fields.get(FIELD_CREATED)
            if isinstance(created, dict):
                new_type = created.get("term_type", new_type)
            if new_type is None:
                continue
            counts[new_type] = counts.get(new_type, 0) + 1
        elif new_type != base_type:
            counts[base_type] = counts.get(base_type, 0) - 1
            counts[new_type] = counts.get(new_type, 0) + 1

    return {term_type: count for term_type, count in counts.items() if count > 0}


async def count_and_sample_terms_merged_by_term_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    term_type: str,
    *,
    sample_limit: int,
) -> tuple[int, list[Term]]:
    """合并视图下**单个** term_type 有多少实体，外加按 standard_name 排在
    最前面的几条——供"这个类型还有实体在用吗"这类守卫用（当前唯一调用方是
    ontology_categories.delete_term_type）。

    为什么不复用 count_terms_merged_by_term_type：那个函数按整个租户分组
    （一次全表 GROUP BY），只为取其中一个键；这里按 (tenant_id, term_type)
    直接命中 idx_terms_tenant_standard_name 前缀，两万条实体的租户上不会
    为了一个数字扫全表。守卫还需要"挡路的是谁"，计数和取样共用同一次编辑层
    合并，也不该拆成两次。

    合并语义不在这里重写：被人工碰过的那些行（term_edits 只包含它们，通常
    远小于 terms）整行取出来交给 apply_edits，再按类型过滤——人工删除的不
    算，人工改成别的类型的不算，人工改成这个类型的（含纯编辑层创建的）算。
    没被碰过的行的类型就是表里那个值，直接在 SQL 里数和取样。
    """
    conn.row_factory = aiosqlite.Row
    edits = await list_term_edits(conn, tenant_id)
    edited_keys = list(edits)
    exclusion = ""
    params: tuple[object, ...] = (tenant_id, term_type)
    if edited_keys:
        placeholders = ",".join("?" * len(edited_keys))
        exclusion = f" AND node_key NOT IN ({placeholders})"
        params = (tenant_id, term_type, *edited_keys)
    cursor = await conn.execute(
        f"SELECT COUNT(*) FROM terms WHERE tenant_id = ? AND term_type = ?{exclusion}", params
    )
    untouched_total = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        "SELECT tenant_id, node_key, standard_name, aliases, term_type, extra_properties, source "
        f"FROM terms WHERE tenant_id = ? AND term_type = ?{exclusion} "
        "ORDER BY standard_name LIMIT ?",
        (*params, sample_limit),
    )
    untouched_sample = [_row_to_term(row) for row in await cursor.fetchall()]

    edited_of_this_type: list[Term] = []
    if edited_keys:
        placeholders = ",".join("?" * len(edited_keys))
        cursor = await conn.execute(
            "SELECT tenant_id, node_key, standard_name, aliases, term_type, extra_properties, "
            f"source FROM terms WHERE tenant_id = ? AND node_key IN ({placeholders})",
            (tenant_id, *edited_keys),
        )
        touched = [_row_to_term(row) for row in await cursor.fetchall()]
        edited_of_this_type = [
            term for term in apply_edits(touched, edits, tenant_id=tenant_id)
            if term.term_type == term_type
        ]

    total = untouched_total + len(edited_of_this_type)
    sample = sorted(
        [*untouched_sample, *edited_of_this_type], key=lambda term: term.standard_name
    )[:sample_limit]
    return total, sample


async def count_terms_merged(
    conn: aiosqlite.Connection, tenant_id: str, *, source: str | None = None
) -> int:
    """合并视图下的术语总数——**分页器要用这个，不是 count_terms**。

    count_terms 数的是 terms 原始表，跟 list_terms_merged 返回的内容对不上：
    人工删除（__deleted__）的行仍在 terms 里但不出现在列表中，纯编辑层创建
    （__created__ 且 terms 无对应行）的实体则相反。两个偏差方向相反、会部分
    抵消——这比单纯多算更坏，抵消会让问题在小数据上看着"差不多对"，掩盖两个
    独立的错误。

    实现上不走"全量合并再数长度"：那要把整张表载入内存，而分页的意义正是
    不这么做。改为在 SQL 的基数上按编辑层做增减——term_edits 只包含被人工
    碰过的行，通常远小于 terms。
    """
    base = await count_terms(conn, tenant_id, source=source)
    edits = await list_term_edits(conn, tenant_id)
    if not edits:
        return base

    deleted_keys = {k for k, fields in edits.items() if FIELD_DELETED in fields}
    created_keys = {k for k, fields in edits.items() if FIELD_CREATED in fields}
    touched = deleted_keys | created_keys
    if not touched:
        return base

    # 只查被编辑过的那几个 node_key 在 terms 里的实际情况——要知道它们存不存在
    # 以及 source 是什么（source 过滤对合并结果整体生效，见 list_terms_merged）。
    keys = list(touched)
    placeholders = ",".join("?" * len(keys))
    cursor = await conn.execute(
        f"SELECT node_key, source FROM terms WHERE tenant_id = ? AND node_key IN ({placeholders})",
        (tenant_id, *keys),
    )
    source_by_key = {row[0]: row[1] for row in await cursor.fetchall()}

    total = base
    for key in deleted_keys:
        # 有 terms 行的才需要减：base 里本来就没数过纯编辑层创建的实体。
        existing_source = source_by_key.get(key)
        if existing_source is None:
            continue
        if source is None or existing_source == source:
            total -= 1
    for key in created_keys:
        if key in source_by_key or key in deleted_keys:
            # terms 里已有对应行时，那一行接管存在性、base 已经数过它；
            # 同时被删除的则根本不出现。两种都不该再加。
            continue
        # 纯编辑层创建的实体在合并视图里 source 固定为 "review"
        # （见 term_merge._synthesize_created）。
        if source is None or source == "review":
            total += 1
    return total


async def count_terms_by_term_type(
    conn: aiosqlite.Connection, tenant_id: str
) -> dict[str, int]:
    """该租户每个 term_type 下有多少实体，供本体图在节点上叠加数量用。

    数的是 terms 原始表，不是合并视图：被人工删除（__deleted__）的实体仍
    计入。这是刻意的——这个数字回答的是"管道往这个类型里写了多少"，是
    数据规模的信号；"用户看得见几条"是另一个问题，不该混在一个数字里。
    真要后者时应当另开一个接口，而不是让这一个含糊地兼顾两者。
    """
    cursor = await conn.execute(
        "SELECT term_type, COUNT(*) FROM terms WHERE tenant_id = ? GROUP BY term_type",
        (tenant_id,),
    )
    return {row[0]: row[1] for row in await cursor.fetchall()}


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


async def list_etl_node_keys_by_term_type(
    conn: aiosqlite.Connection, tenant_id: str, term_type: str
) -> set[str]:
    """该租户、该类型下、**由 ETL 写入**的全部 node_key。

    跟 list_node_keys_by_term_type 的区别只有 source 过滤，但这个区别是
    本质的：ETL 的 sweep（源里没有的实体要删掉）只能作用于 ETL 自己写进来
    的行。审核界面现场创建的（source='review'）和管理后台手工录入的
    （source='manual'）从来就不来自这个数据源，"源里没有"对它们不成立。

    不要把这个过滤加进 list_node_keys_by_term_type——那个函数服务的是关系
    写入的端点存在性守卫，它需要的正是"全部 node_key"，无论来源。
    """
    cursor = await conn.execute(
        "SELECT node_key FROM terms WHERE tenant_id = ? AND term_type = ? AND source = 'etl'",
        (tenant_id, term_type),
    )
    return {row[0] for row in await cursor.fetchall()}


async def delete_terms_by_node_keys(
    conn: aiosqlite.Connection, tenant_id: str, node_keys: set[str]
) -> int:
    """按 node_key 批量删除该租户的术语行，返回实际删除的行数。

    空集合是干净的空操作，直接返回 0——绝不能让它退化成一条没有有效 WHERE
    条件的 DELETE 把整张表清空。这是本函数最危险的失败形态，有测试钉住。

    只删 terms 行。图谱侧的节点删除由调用方另行调用 delete_term_node
    （它是 DETACH DELETE，会连边和别名节点一起清掉）。
    """
    if not node_keys:
        return 0
    keys = list(node_keys)
    placeholders = ",".join("?" * len(keys))
    cursor = await conn.execute(
        f"DELETE FROM terms WHERE tenant_id = ? AND node_key IN ({placeholders})",
        (tenant_id, *keys),
    )
    await conn.commit()
    return cursor.rowcount


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


async def validate_term_categories(
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

    这是 terms_store 的公开校验入口（2026-08-31 起去掉下划线前缀）：
    除了本模块内部的 create_term/update_term/upsert_term_with_node_key，
    app/api/admin_terms_routes.py 的写入端点（POST/PUT）也直接依赖它做
    分类/字段校验——这层跨模块契约现在是显式的，重构这个函数的签名或
    异常行为时需要同时检查 app/api 层的调用点。
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
    await validate_term_categories(
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
    await validate_term_categories(
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


async def _coerce_to_declared_type(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    term_type: str,
    field: str,
    value: str,
) -> object:
    """把界面上填进来的字符串转成这个字段声明的类型。

    界面上填什么都是字符串。原样存进去的话，一个声明为 number 的字段会得到
    `"45"`——而 ETL 写的是 `45`，下一次导入 `"45" != 45` 又记一条冲突，
    页面上两边 str() 之后都显示 45：审核员看到「用 45 / 用 45」，怎么点都
    消不掉，每天再多一条。

    转不过去时抛 ValueError 而不是硬写：硬写的话这个实体的那一列从此是脏的
    ——结构化查询按数字比会漏掉它，而界面上看起来完全正常。

    没声明过的字段（或没声明类型的分类）原样返回字符串：这里不承担
    validate_term_categories 的职责，它在别处已经把"字段没声明"挡住了。
    """
    types = await list_term_types(conn, tenant_id, status="confirmed")
    declared = {
        f.name: f.value_type
        for t in types
        if t.value == term_type
        for f in t.extra_fields
    }
    value_type = declared.get(field)
    if value_type in (None, "string"):
        return value
    try:
        if value_type == "integer":
            return int(value)
        if value_type == "number":
            parsed = float(value)
            # 42.0 存成 42：整数值存成浮点的话，下次 ETL 写 42（int）时
            # 42.0 != 42 又是一条冲突。
            return int(parsed) if parsed.is_integer() else parsed
    except ValueError:
        raise ValueError(
            f"{field!r} 声明的类型是 {value_type}，而 {value!r} 不是一个{value_type}。"
        ) from None
    # number[] 之类的复合类型：界面上还没有填它们的入口，走到这里说明
    # 调用方在用一个没设计过的路径，明确拒绝而不是猜一个解析方式。
    raise ValueError(f"{field!r} 声明的类型是 {value_type}，这个类型还不支持在界面上直接改。")


async def set_extra_property(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    field: str,
    value: str,
    value_source: str,
) -> None:
    """把一个属性字段的值定下来。属性冲突决议之后写回用。

    只改这一个字段，不碰别的：决议是对**一个字段**的判断，整份 extra_properties
    覆盖过去会把这次决议之外的字段一起改掉（而它们可能刚被另一次导入更新过）。

    实体不存在时抛 TermNotFoundError——静默 no-op 的话，审核员选完值、系统
    说成功，而那个值哪儿都没写进去。

    `value_source` 必填、无默认值：这个字段的来源必须跟着值一起更新。

    不更新的话那一列会**留着原来那次导入的来源**——决议成 42 之后，下一次
    冲突显示的是「用 42（来自 商品表.xlsx）」，而商品表说的是 39，它从没
    说过 42。这正是引入这一列时要防的那件事（整行只记一个来源会指名一个
    没说过这个值的文件），只不过换了个入口。
    （曾经在这里写过"留空由读的一侧退回行级 source"——那是错的：这一列
    对这个字段已经有值，不动它就是留着旧来源，根本走不到那个兜底。）

    调用方传的是一句人话，比如「人工决议：alice」——冲突页要把它显示出来，
    而审核员需要看出"这个值是人定的"跟"这个值来自某张表"是两回事。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT extra_properties, extra_property_sources, term_type FROM terms "
        "WHERE tenant_id = ? AND node_key = ?",
        (tenant_id, node_key),
    )
    row = await cursor.fetchone()
    if row is None:
        raise TermNotFoundError(f"术语 {node_key!r}（租户 {tenant_id!r}）不存在")
    extra = json.loads(row["extra_properties"])
    extra[field] = await _coerce_to_declared_type(
        conn, tenant_id=tenant_id, term_type=row["term_type"], field=field, value=value,
    )
    sources = json.loads(row["extra_property_sources"])
    sources[field] = value_source
    await conn.execute(
        "UPDATE terms SET extra_properties = ?, extra_property_sources = ? "
        "WHERE tenant_id = ? AND node_key = ?",
        (
            json.dumps(extra, ensure_ascii=False),
            json.dumps(sources, ensure_ascii=False),
            tenant_id,
            node_key,
        ),
    )
    await conn.commit()


async def _keep_old_values_and_record_conflicts(
    conflict_conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    existing_extra: dict[str, Any],
    existing_field_sources: dict[str, str],
    existing_field_rows: dict[str, int],
    existing_row_source: str,
    incoming_extra: dict[str, object],
    incoming_source: str,
    incoming_row_number: int | None,
) -> tuple[dict[str, object], dict[str, str], dict[str, int]]:
    """逐字段比对，冲突的字段换回旧值并记一条，返回该写进库的那份属性。

    三种情况分开处理，混在一起就是这个函数最容易出错的地方：

    - **旧记录里没有这个字段**：直接写，不算冲突。补充属性是多表导入最常见
      的用法（一张表给基础信息、另一张补价格），算成冲突的话审核页会被正常
      操作淹掉。
    - **值一样**：不算冲突。ETL 每天跑一次，记的话会积出一屏
      「39 和 39 冲突了」。
    - **值不一样**：保留旧值，记一条。

    逐**字段**记而不是逐行记：一行里两个字段都变了要记两条。合并成一条的话，
    审核员只能整行选 A 或选 B——而正确答案可能是「售价用 A、产地用 B」。
    """
    kept = dict(incoming_extra)
    sources = dict(existing_field_sources)
    # 行号跟来源走同一套规则：新字段/同源更新记这次的行，值没变不动，
    # 冲突保留旧行。行号不明（None）时不写，读出来就是"没有"，不是 0。
    rows = dict(existing_field_rows)

    def _note_row(field_name: str) -> None:
        if incoming_row_number is not None:
            rows[field_name] = incoming_row_number
        else:
            rows.pop(field_name, None)

    for field, incoming_value in incoming_extra.items():
        if field not in existing_extra:
            # 新字段：这次导入写进去的，来源就是这次的。
            sources[field] = incoming_source
            _note_row(field)
            continue
        old_value = existing_extra[field]
        if old_value == incoming_value:
            # 值没变。来源也不动：先写进来的那次才是这个值的出处，改成这次的
            # 会让"39 来自哪"随最后一次重跑漂移。
            continue
        if existing_field_sources.get(field) == incoming_source:
            # 同一个来源给出了新值 → **更新**，不是冲突。上游数据变了正是
            # 重跑 ETL 的目的。来源不变（还是这一个），值换成新的。
            sources[field] = incoming_source
            _note_row(field)
            continue
        kept[field] = old_value
        await record_conflict(
            conflict_conn,
            tenant_id=tenant_id,
            node_key=node_key,
            field=field,
            # 值统一转成字符串存：这张表是给人看的，两个 JSON 值长得一样
            # 但类型不同（39 与 "39"）在审核页上没法区分，也没法让审核员
            # 做出不同的决定。
            kept_value=str(old_value),
            # 逐字段的来源。存量行（这一列上线之前写的）没有记录，退回行级的
            # source 渠道值——那至少是真的，只是粗。
            kept_source=existing_field_sources.get(field, existing_row_source),
            incoming_value=str(incoming_value),
            incoming_source=incoming_source,
            kept_row_number=existing_field_rows.get(field),
            incoming_row_number=incoming_row_number,
        )
    return kept, sources, rows


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
    conflict_conn: aiosqlite.Connection | None = None,
    incoming_source: str = "unknown",
    incoming_row_number: int | None = None,
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

    ---

    **conflict_conn 不为 None 时，覆盖之前逐字段比对已有的 extra_properties：
    值不同的字段保留旧值，并往 attribute_conflicts 记一条。**

    这是 spec §1 点名的「今天最大的静默失败」：表格 A 说售价 39、表格 B 说
    45，后跑的赢，没有任何人知道发生过冲突。

    保留**旧**值而不是新值，是 Global Constraint 13（不确定的时候不擅自改
    数据）：改成新值赢的话，这次改动只是把静默覆盖换了个方向，一个问题都
    没解决。

    只比对 extra_properties，不比对 standard_name：改名走的是人工编辑层
    （term_edits）那条路，那件事已经在别处解决了。混进来的话，ETL 每次跑
    都会跟人工改过的名字冲突一次。

    conflict_conn 为 None 时行为**完全不变**——既有调用方一个字都不用改，
    也不会因为这次改动而行为变化。冲突表和术语表通常是同一个库，但参数分开
    留着：让"要不要记冲突"成为调用方的显式选择，而不是"只要那张表在就记"。

    incoming_source 是**这次导入的来源**（表名/文件名），跟 `source` 那个
    渠道字段（etl/manual/review）不是一回事——审核页要显示「39 来自
    商品表.xlsx」靠的是这个。两个名字容易看混，改这里时注意别串。

    incoming_row_number 是这一行在源文件里的行号（跟跳过行的记法一致，
    表头是第 1 行），冲突页据此显示「第 88 行」。不知道时传 None，不要传 0。
    """
    extra_properties = extra_properties or {}
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT extra_properties, source, extra_property_sources, extra_property_source_rows "
        "FROM terms WHERE tenant_id = ? AND node_key = ?",
        (tenant_id, node_key),
    )
    existing_row = await cursor.fetchone()
    existing_extra = (
        json.loads(existing_row["extra_properties"]) if existing_row is not None else {}
    )
    existing_extra_property_keys = frozenset(existing_extra)
    existing_field_sources: dict[str, str] = (
        json.loads(existing_row["extra_property_sources"]) if existing_row is not None else {}
    )
    existing_field_rows: dict[str, int] = (
        json.loads(existing_row["extra_property_source_rows"]) if existing_row is not None else {}
    )
    field_sources = dict(existing_field_sources)
    field_rows = dict(existing_field_rows)
    if conflict_conn is not None:
        if existing_row is not None:
            # 就地改 extra_properties：下面那条 INSERT ... DO UPDATE 用的就是
            # 这个字典，冲突字段在这里被换回旧值之后，写进库的自然是旧值。
            extra_properties, field_sources, field_rows = (
                await _keep_old_values_and_record_conflicts(
                    conflict_conn,
                    tenant_id=tenant_id,
                    node_key=node_key,
                    existing_extra=existing_extra,
                    existing_field_sources=existing_field_sources,
                    existing_field_rows=existing_field_rows,
                    existing_row_source=existing_row["source"],
                    incoming_extra=extra_properties,
                    incoming_source=incoming_source,
                    incoming_row_number=incoming_row_number,
                )
            )
        else:
            # 新行：每个字段都来自这次导入。
            field_sources = {field: incoming_source for field in extra_properties}
            field_rows = (
                {field: incoming_row_number for field in extra_properties}
                if incoming_row_number is not None
                else {}
            )
    await validate_term_categories(
        conn, tenant_id=tenant_id, term_type=term_type,
        extra_properties=extra_properties,
        existing_extra_property_keys=existing_extra_property_keys,
    )
    try:
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source, extra_property_sources, extra_property_source_rows) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (tenant_id, node_key) DO UPDATE SET "
            "standard_name = excluded.standard_name, aliases = excluded.aliases, "
            "term_type = excluded.term_type, "
            "extra_properties = excluded.extra_properties, "
            "extra_property_sources = excluded.extra_property_sources, "
            "extra_property_source_rows = excluded.extra_property_source_rows",
            (
                tenant_id,
                node_key,
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                json.dumps(extra_properties, ensure_ascii=False),
                source,
                json.dumps(field_sources, ensure_ascii=False),
                json.dumps(field_rows, ensure_ascii=False),
            ),
        )
    except aiosqlite.IntegrityError:
        # standard_name 的唯一索引在 2026-08-30 已降级为普通索引，重名不再
        # 触发这里；剩下能命中的只有 NOT NULL 之类的约束违例，原样抛出，
        # 不要再冒充"标准名冲突"——那会把一个 schema 问题误报成数据问题。
        raise
    await conn.commit()


_TOMBSTONE_PREFIX = "[已合并] "


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
    在编辑层写一条 __deleted__ 标记，使其在合并视图里虚拟不可见。

    两个 node_key 有任意一个在这个租户下不存在，抛 TermNotFoundError。

    实现是编辑层的两条 upsert_term_edit 调用：
    1. merged_node_key 写 __deleted__ 编辑——之后 apply_edits 会把这一行
       从合并结果里排除。
    2. keep_node_key 写 aliases 编辑 = keep 当前别名 + merged 的
       standard_name + merged 的全部别名（去重）。

    因为编辑层没有 _check_name_conflict 这道检查（旧实现的复杂性来源），
    也没有中间态——两条编辑要么都成功、要么都被原子地应用到读路径，不需要
    补偿回滚的中间状态处理。
    """
    # 当前值的获取使用 get_term_merged_by_node_key，它会把编辑层合并进去
    # ——如果某一方已经被删除过（有 __deleted__ 编辑），这里会抛
    # TermNotFoundError，满足"两个 node_key 有任意一个不存在时抛异常"的
    # 契约。
    keep_term = await get_term_merged_by_node_key(conn, tenant_id, keep_node_key)
    merged_term = await get_term_merged_by_node_key(conn, tenant_id, merged_node_key)

    # Step 1: 把 merged 那条标记为已删除。
    await upsert_term_edit(
        conn,
        tenant_id=tenant_id,
        node_key=merged_node_key,
        field=FIELD_DELETED,
        value=None,
        edited_by="admin",
    )

    # Step 2: 把 merged 那条的别名追加到 keep 那条。
    # 别名并集 = keep 当前别名 + merged 的 standard_name + merged 的全部别名
    # （去重，保持顺序）。
    merged_aliases = list(dict.fromkeys(
        [*keep_term.aliases, merged_term.standard_name, *merged_term.aliases]
    ))
    await upsert_term_edit(
        conn,
        tenant_id=tenant_id,
        node_key=keep_node_key,
        field="aliases",
        value=merged_aliases,
        edited_by="admin",
    )
