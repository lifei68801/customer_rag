"""引导流程产出的 ETL 映射，与本体同生命周期。

为什么挂在本体上而不是让用户保管一个下载下来的 YAML：这份映射描述的是
"这个本体的实体从哪张表的哪几列来"，它本来就是本体定义的一部分。放在
用户的下载目录里会有一个没人管的问题——用户重跑引导覆盖了草稿，旧映射
还躺在磁盘上，两者已经对不上，而没有任何东西告诉他。

表结构与三张本体表同构（tenant_id + status 两列），因此加进
ontology_lifecycle._TABLES_WITH_TENANT_LIFECYCLE 就能白拿 confirm_ontology
的原子提升：那个循环对每张表做"删 confirmed + 把 draft 提升成 confirmed"，
对任何带这两列的表都成立。
"""

from dataclasses import dataclass

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_etl_mapping (
    tenant_id        TEXT NOT NULL,
    status           TEXT NOT NULL,
    config_yaml      TEXT NOT NULL,
    source_file_name TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, status)
);
"""


@dataclass(frozen=True)
class EtlMapping:
    config_yaml: str
    source_file_name: str
    created_at: str


async def ensure_etl_mapping_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def set_draft_etl_mapping(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    config_yaml: str,
    source_file_name: str,
    created_at: str,
    commit: bool = True,
) -> None:
    """整份替换草稿映射。一个租户的草稿只有一份，没有增量语义。

    commit=False 给的是"这次写入只是某个更大的写入阶段里的一步"的调用方：
    replace_draft 的 docstring 论证过它为什么只在末尾提交一次（先做完全部
    校验、写入阶段不会再失败），本函数在它中间自带一次 commit 会把那个论证
    作废——三张草稿表已经写完、checkout 标记还没写，崩在这个窗口里下一次
    checkout_draft 会把 confirmed 行复制回来盖在引导刚写的草稿上。
    """
    await conn.execute(
        "INSERT OR REPLACE INTO ontology_etl_mapping "
        "(tenant_id, status, config_yaml, source_file_name, created_at) "
        "VALUES (?, 'draft', ?, ?, ?)",
        (tenant_id, config_yaml, source_file_name, created_at),
    )
    if commit:
        await conn.commit()


async def get_etl_mapping(
    conn: aiosqlite.Connection, tenant_id: str, *, status: str
) -> EtlMapping | None:
    cursor = await conn.execute(
        "SELECT config_yaml, source_file_name, created_at FROM ontology_etl_mapping "
        "WHERE tenant_id = ? AND status = ?",
        (tenant_id, status),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return EtlMapping(config_yaml=row[0], source_file_name=row[1], created_at=row[2])
