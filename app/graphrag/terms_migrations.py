"""terms 表的 schema 演进史——一条有序的迁移链。

## 为什么单独有这个模块

这条链此前住在 `terms_store.py` 里，跟二十几个读写函数混在一起。读业务
函数的人得先读完四个 `_migrate_*_if_needed`，而迁移之间还有一条**只写在
注释里**的顺序约束：

> `extra_property_sources` 必须排在重建型迁移之后。
> `_migrate_terms_table_to_tenant_scoped_if_needed` 会 CREATE terms_new +
> 拷数据 + RENAME，而它那份 DDL 里没有这一列——排在它前面的话，刚加的列
> 会被那次重建原地丢掉，而 `_SCHEMA_SQL` 是 CREATE TABLE IF NOT EXISTS，
> 表已经在了，补不回来。

后果是本次启动之后每一次 upsert 都在 SELECT 那一行抛 no such column，重启
才自愈——而且只有最老的那批存量库会踩，它们恰恰是最没人盯着的。

## 真正的规则是什么

不是"新列都排在后面"。`extra_properties` 和 `source` 就排在重建之前，而且
是对的——因为**重建那份 DDL 里有这两列**，重建会把它们一起造出来。

规则是：**任何不在重建 DDL 里的列，必须加在重建之后。**

所以这里把"这一步是不是重建型"记成 `rebuilds_table`，把每一步的名字记成
数据，让顺序可以被检查，而不是靠下一个改这个文件的人读到那段注释。

真正钉住这条规则的是 `tests/graphrag/test_terms_migrations.py`：从最老的
那版 schema 建库、跑完整条链、断言当前 `_SCHEMA_SQL` 声明的每一列都在。
顺序排错时那条测试会红，而按模块各自为政的单元测试不会。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

from app.db_migrations import add_column_if_missing


@dataclass(frozen=True)
class TermsMigration:
    """一步迁移。每一步都必须幂等——整条链每次启动都从头跑一遍。"""

    name: str

    #: 这一步会不会**重建整张表**（CREATE 新表 + 拷数据 + RENAME）。
    #: 重建会把重建 DDL 里没有的列丢掉，所以它是这条链的分水岭：
    #: 加列的步骤只要那一列不在重建 DDL 里，就必须排在最后一个重建之后。
    rebuilds_table: bool

    apply: Callable[[aiosqlite.Connection], Awaitable[None]]


async def _add_extra_properties(conn: aiosqlite.Connection) -> None:
    await add_column_if_missing(
        conn, table="terms", column="extra_properties", ddl="TEXT NOT NULL DEFAULT '{}'"
    )


async def _add_source(conn: aiosqlite.Connection) -> None:
    await add_column_if_missing(
        conn, table="terms", column="source", ddl="TEXT NOT NULL DEFAULT 'unknown'"
    )


async def _to_tenant_scoped(conn: aiosqlite.Connection) -> None:
    """把 2026-08-15 之前的 terms 表（standard_name 主键，无 tenant_id/
    node_key）原地迁移成按租户隔离的新结构。

    只在表已存在且还是老结构时执行，幂等——已经是新结构（有 tenant_id 列）
    直接跳过。存量数据统一归到 tenant_id='default'，node_key 回填成当时的
    standard_name 值。SQLite 不支持 ALTER TABLE 改主键，只能建新表 + 搬数据
    + 删旧表 + 改名——这就是这一步 rebuilds_table=True 的原因，也是这条链
    有顺序约束的全部来源。
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


async def _drop_product_line(conn: aiosqlite.Connection) -> None:
    """把仍带着 product_line 列的 terms 表原地去掉这一列。

    SQLite 3.35+ 原生支持 ALTER TABLE ... DROP COLUMN（本项目实测 3.49.1），
    不需要像 `_to_tenant_scoped` 那样建新表搬数据——product_line 只是普通
    TEXT NOT NULL 列，不是主键的一部分、没有 CHECK/UNIQUE 约束、不被任何
    生成列引用，满足原生语法的适用条件。所以这一步 rebuilds_table=False。

    幂等：列已经不存在时直接跳过。不做删除前的数据备份，见
    docs/superpowers/specs/2026-08-19-remove-product-line-design.md 决策 2
    （这批数据本身没有实际区分意义，备份没有价值）。
    """
    cursor = await conn.execute("PRAGMA table_info(terms)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "product_line" not in existing_columns:
        return
    await conn.execute("ALTER TABLE terms DROP COLUMN product_line")
    await conn.commit()


async def _index_to_type_scoped(conn: aiosqlite.Connection) -> None:
    """把 2026-08-22 之前 (tenant_id, standard_name) 这个跨类型全局唯一索引，
    收窄成 (tenant_id, term_type, standard_name)。

    允许不同 term_type 的术语共享同一个 standard_name——真实场景：ETL 导入
    时"产品"类目下的 Coffee 和"类目"类目下的 Coffee 是两个不同的实体。

    用 PRAGMA index_info 探测当前索引的列数，2 列（旧版本）就重建成 3 列，
    已经是 3 列或索引还不存在（全新库，稍后 _SCHEMA_SQL 会按新定义建）都
    直接跳过。重建的是索引不是表，所以 rebuilds_table=False。
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


async def _index_drop_unique(conn: aiosqlite.Connection) -> None:
    """把 (tenant_id, term_type, standard_name) 的唯一索引降级成普通索引。

    唯一性下沉到 node_key：terms 表的主键本来就是 (tenant_id, node_key)，
    身份约束已经在那里；standard_name 是展示名，同一 term_type 下允许重复
    （两个同名不同人的客户，各自有不同的 node_key）。见
    docs/superpowers/specs/2026-08-30-name-uniqueness-to-node-key-design.md。

    用 PRAGMA index_list 探测 unique 标志，已经是普通索引或索引不存在都
    直接跳过。

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


async def _add_extra_property_sources(conn: aiosqlite.Connection) -> None:
    """每个属性字段各自来自哪次导入。JSON 对象 {字段名: 来源}。

    为什么逐字段而不是整行记一个"最近导入来源"：属性冲突页要显示
    「39 来自 商品表.xlsx」。整行只记一个的话，A 表写了售价、B 表写了产地
    之后那个值是 B——于是售价那条冲突会声称 39 来自 B 表，而它其实来自 A 表。
    一个看起来精确、实际是错的来源比不显示来源更糟：审核员会据此挑错边。

    只有带 incoming_source 调用 upsert_term_with_node_key 时才写这一列。
    这次改动之前的存量行留空，读的时候退回行级的 source 渠道值。

    **这一列不在 `_to_tenant_scoped` 的重建 DDL 里，所以必须排在它之后。**
    """
    await add_column_if_missing(
        conn,
        table="terms",
        column="extra_property_sources",
        ddl="TEXT NOT NULL DEFAULT '{}'",
    )


async def _add_extra_property_source_rows(conn: aiosqlite.Connection) -> None:
    """逐字段：这个值来自源文件的第几行。跟 extra_property_sources 平行的
    一份 {field: row_number}，冲突页据此显示「39 来自 商品表.xlsx 第 88 行」。

    同上，同一个理由：不在重建 DDL 里，必须排在重建之后。
    """
    await add_column_if_missing(
        conn,
        table="terms",
        column="extra_property_source_rows",
        ddl="TEXT NOT NULL DEFAULT '{}'",
    )


#: terms 表的迁移链，**顺序即契约**。
#:
#: 前两步排在重建之前是对的：重建那份 DDL 里有 extra_properties 和 source，
#: 重建会把它们一起造出来。后两步不在那份 DDL 里，所以必须排在重建之后。
TERMS_MIGRATIONS: tuple[TermsMigration, ...] = (
    TermsMigration("add_extra_properties", False, _add_extra_properties),
    TermsMigration("add_source", False, _add_source),
    TermsMigration("to_tenant_scoped", True, _to_tenant_scoped),
    TermsMigration("drop_product_line", False, _drop_product_line),
    TermsMigration("index_to_type_scoped", False, _index_to_type_scoped),
    TermsMigration("index_drop_unique", False, _index_drop_unique),
    TermsMigration("add_extra_property_sources", False, _add_extra_property_sources),
    TermsMigration("add_extra_property_source_rows", False, _add_extra_property_source_rows),
)

#: 重建 DDL 里有的列。加新列时，只有名字在这个集合里的才可以排在重建之前
#: ——而实际上不会有新的了：这份 DDL 是历史快照，不随当前 schema 变化。
REBUILD_DDL_COLUMNS = frozenset(
    {
        "tenant_id",
        "node_key",
        "standard_name",
        "aliases",
        "term_type",
        "extra_properties",
        "source",
    }
)


async def apply_terms_migrations(conn: aiosqlite.Connection) -> None:
    """按顺序跑完整条链。只对**已存在**的 terms 表调用。

    全新库不需要走这里：`_SCHEMA_SQL` 一次就建出当前结构。
    """
    for migration in TERMS_MIGRATIONS:
        await migration.apply(conn)
