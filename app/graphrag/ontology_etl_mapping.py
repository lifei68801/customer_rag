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


async def remember_mapping_used_for_import(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    config_yaml: str,
    source_file_name: str,
    created_at: str,
) -> bool:
    """把这次导入真正用的那份映射记成这个本体的默认映射。返回是否也更新了草稿。

    ## 为什么写的是 confirmed，而不是走草稿→确认那条路

    这份映射描述的是"这个本体的实体从哪张表的哪几列来"。这次导入跑完之后，
    图里的实体**就是**按这份映射建的——confirmed 那份如果还停在上一版，它
    描述的是一份已经不存在的数据。表格导入页读的也是 confirmed，用户下次进来
    看到的"沿用上次配好的映射"会是他上次明明改过、却没有被记住的那一份。

    走草稿再让用户去本体页确认一次不解决问题：那一步确认的是**本体**，而他
    刚才改的是映射；在本体页点确认来让映射生效，这个因果关系没有人能猜到。

    这不破坏本体生命周期的一致性：映射里能选的实体类型只来自已确认本体
    （映射编辑器的下拉就是拿 confirmed 填的），所以写进 confirmed 的映射不会
    引用草稿里才有的东西。

    ## 草稿只在它和旧的 confirmed 一致时才跟着改

    草稿跟 confirmed 不一样，说明有人正在改这个本体的映射（引导建模会整份
    写草稿）。这时把草稿覆盖掉就是把别人没提交的工作删了。一致才跟着走，
    不一致就只更新 confirmed，让那个人自己决定要不要合并。
    """
    cursor = await conn.execute(
        "SELECT status, config_yaml FROM ontology_etl_mapping WHERE tenant_id = ?",
        (tenant_id,),
    )
    existing = {row[0]: row[1] for row in await cursor.fetchall()}
    draft_follows = existing.get("draft") == existing.get("confirmed")

    await conn.execute(
        "INSERT OR REPLACE INTO ontology_etl_mapping "
        "(tenant_id, status, config_yaml, source_file_name, created_at) "
        "VALUES (?, 'confirmed', ?, ?, ?)",
        (tenant_id, config_yaml, source_file_name, created_at),
    )
    if draft_follows:
        await conn.execute(
            "INSERT OR REPLACE INTO ontology_etl_mapping "
            "(tenant_id, status, config_yaml, source_file_name, created_at) "
            "VALUES (?, 'draft', ?, ?, ?)",
            (tenant_id, config_yaml, source_file_name, created_at),
        )
    await conn.commit()
    return draft_follows
