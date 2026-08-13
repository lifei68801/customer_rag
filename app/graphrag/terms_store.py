from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

from app.graphrag.ontology import Term, load_terminology

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terms (
    standard_name TEXT PRIMARY KEY,
    aliases TEXT NOT NULL,
    term_type TEXT NOT NULL,
    product_line TEXT NOT NULL
);
"""


class TermNotFoundError(Exception):
    """指定的 standard_name 在术语表里不存在。"""


class TermNameConflictError(Exception):
    """提交的 standard_name 或某个 alias，跟另一个已存在的术语的
    standard_name/alias 重复——resolve_to_standard_name() 按顺序遍历命中
    第一个匹配就返回，允许重叠会让抽取结果变成"看列表顺序"决定的、
    不可预测。"""


async def ensure_terms_schema(
    conn: aiosqlite.Connection, *, seed_yaml_path: Path | None = None
) -> None:
    """幂等建表。

    seed_yaml_path 只在传入且指向一个存在的文件、同时这张表是刚刚第一次
    被创建（不是已经存在）时才生效：从这个 YAML 文件里一次性导入内容，
    此后这份 YAML 不再被任何代码路径读取（术语表迁移到这张表之后的过渡
    措施）。不传（默认 None）只是单纯建表，不做任何导入——所有测试固定
    用这个默认行为，不会被本机真实存在的 terminology_seed.yaml 意外
    带入示例数据。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='terms'"
    )
    table_already_existed = await cursor.fetchone() is not None
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    if not table_already_existed and seed_yaml_path is not None and seed_yaml_path.exists():
        try:
            for term in load_terminology(seed_yaml_path):
                await conn.execute(
                    "INSERT OR IGNORE INTO terms "
                    "(standard_name, aliases, term_type, product_line) VALUES (?, ?, ?, ?)",
                    (
                        term.standard_name,
                        json.dumps(term.aliases, ensure_ascii=False),
                        term.term_type,
                        term.product_line,
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


def _row_to_term(row: aiosqlite.Row) -> Term:
    return Term(
        standard_name=row["standard_name"],
        aliases=json.loads(row["aliases"]),
        term_type=row["term_type"],
        product_line=row["product_line"],
    )


async def list_terms(conn: aiosqlite.Connection) -> list[Term]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT standard_name, aliases, term_type, product_line "
        "FROM terms ORDER BY standard_name"
    )
    rows = await cursor.fetchall()
    return [_row_to_term(row) for row in rows]


async def get_term(conn: aiosqlite.Connection, standard_name: str) -> Term:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT standard_name, aliases, term_type, product_line "
        "FROM terms WHERE standard_name = ?",
        (standard_name,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise TermNotFoundError(f"术语不存在: {standard_name}")
    return _row_to_term(row)


async def _check_name_conflict(
    conn: aiosqlite.Connection,
    *,
    standard_name: str,
    aliases: list[str],
    exclude_standard_name: str | None = None,
) -> None:
    """检查 standard_name 和 aliases 有没有跟别的术语（编辑时排除自己）
    的 standard_name/alias 重叠。术语表规模是人工维护的封闭词表，量级
    不大，直接全表扫描比维护一张单独的"已用名字"索引表更简单，跟
    resolve_to_standard_name() 现有的 O(n) 扫描方式保持一致的复杂度假设。
    """
    all_terms = await list_terms(conn)
    candidate_names = {standard_name, *aliases}
    for term in all_terms:
        if term.standard_name == exclude_standard_name:
            continue
        existing_names = {term.standard_name, *term.aliases}
        overlap = candidate_names & existing_names
        if overlap:
            conflicting = next(iter(overlap))
            raise TermNameConflictError(
                f"{conflicting!r} 已经是术语 {term.standard_name!r} 的别名/标准名，不能重复使用"
            )


async def create_term(
    conn: aiosqlite.Connection,
    *,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
) -> None:
    await _check_name_conflict(conn, standard_name=standard_name, aliases=aliases)
    try:
        await conn.execute(
            "INSERT INTO terms (standard_name, aliases, term_type, product_line) "
            "VALUES (?, ?, ?, ?)",
            (standard_name, json.dumps(aliases, ensure_ascii=False), term_type, product_line),
        )
    except aiosqlite.IntegrityError:
        # _check_name_conflict 已经检查过 standard_name 冲突，这里是防御性
        # 兜底（比如并发写入的极端情况），不是主要校验路径。
        raise TermNameConflictError(f"{standard_name!r} 已经是已有术语的标准名，不能重复创建")
    await conn.commit()


async def update_term(
    conn: aiosqlite.Connection,
    *,
    standard_name: str,
    new_standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
) -> None:
    """standard_name 是当前（改名前）的名字，用来定位这条记录；
    new_standard_name 是提交的新名字，允许和 standard_name 相同（即不改名）。
    """
    await get_term(conn, standard_name)
    await _check_name_conflict(
        conn, standard_name=new_standard_name, aliases=aliases,
        exclude_standard_name=standard_name,
    )
    try:
        await conn.execute(
            "UPDATE terms SET standard_name=?, aliases=?, term_type=?, product_line=? "
            "WHERE standard_name=?",
            (
                new_standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                standard_name,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{new_standard_name!r} 已经是已有术语的标准名，不能重复使用")
    await conn.commit()


async def delete_term(conn: aiosqlite.Connection, standard_name: str) -> None:
    await get_term(conn, standard_name)
    await conn.execute("DELETE FROM terms WHERE standard_name=?", (standard_name,))
    await conn.commit()
