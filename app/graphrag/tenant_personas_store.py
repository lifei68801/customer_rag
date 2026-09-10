from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from app.db_migrations import add_column_if_missing

logger = logging.getLogger(__name__)

#: 没指定 persona_id 时用的那张脸。
#:
#: 存量调用方一行不用改：它们不传 persona_id，落到这张脸上——而在解耦之前，
#: 那本来就是这个租户唯一的一张脸。这也是这次改动能做到零迁移的原因。
DEFAULT_PERSONA_ID = "default"


class InvalidPersonaError(ValueError):
    """persona_id 或 name 是空的 / 纯空白。"""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenant_personas (
    tenant_id  TEXT NOT NULL,
    persona_id TEXT NOT NULL DEFAULT 'default',
    name       TEXT NOT NULL DEFAULT '',
    avatar     TEXT NOT NULL DEFAULT '',
    tagline    TEXT NOT NULL DEFAULT '',
    questions  TEXT NOT NULL DEFAULT '[]',
    -- 「这张脸显式配过引导问题吗」。跟 questions 列分开存，是因为
    -- 「配了一个空列表」（我不要引导问题）和「从没碰过」（用自动兜底）
    -- 在 questions 列里长得一模一样，而这两者前台的行为完全相反。
    questions_set INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, persona_id)
);
"""
# 数字人的脸：前台右栏和欢迎语要用的那几样，外加引导问题（questions 列）。
#
# **一个租户可以挂多张脸**（ADR-0004）。主键是 (tenant_id, persona_id)：
# 脸是展示单元，租户才是隔离单元；把两者绑死会让「要几张脸」决定
# 「切几刀隔离」，而那两件事在业务上独立。
#
# **persona_id 绝不进任何权限判据。** 谁能看什么仍然只由 tenant_id 和
# require_tenant_access 决定。这一列只用来定位一张脸，以及标记一次会话是
# 「跟谁聊的」——那是归属，不是可见范围。一旦有人拿它写
# `WHERE tenant_id = ? AND persona_id = ?` 来限制能看到的数据，
# ADR-0004 明确否掉的第二条隔离维度就从展示层的后门被放进来了。


async def ensure_tenant_personas_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    # 已经建过表的部署不会被上面的 CREATE TABLE IF NOT EXISTS 改到。
    # 补这一列时默认 0（从没配过）对老数据是对的：老行里 questions 非空的
    # 那些仍按"配过"处理（判定见 get_questions_setting），只有 '[]' 那些
    # 会落回自动兜底——而那正是它们今天的行为。
    await add_column_if_missing(
        conn, table="tenant_personas", column="questions_set",
        ddl="INTEGER NOT NULL DEFAULT 0",
    )
    await _migrate_to_composite_key_if_needed(conn)


async def _migrate_to_composite_key_if_needed(conn: aiosqlite.Connection) -> None:
    """把主键从 tenant_id 单列换成 (tenant_id, persona_id)。

    SQLite 改不了主键，只能建新表 + 拷 + RENAME。存量行全部落到
    DEFAULT_PERSONA_ID 那张脸上——它们本来就是那个租户唯一的一张脸，
    这是准确的回填，不是猜的。

    **这是重建型迁移，后面再加列的话必须排在它之后**，否则新加的列会被这次
    重建原地丢掉（terms_store.py 里那段注释解释过同样的坑）。
    """
    cursor = await conn.execute("PRAGMA table_info(tenant_personas)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "persona_id" in columns:
        return
    await conn.executescript(
        """
        CREATE TABLE tenant_personas_new (
            tenant_id  TEXT NOT NULL,
            persona_id TEXT NOT NULL DEFAULT 'default',
            name       TEXT NOT NULL DEFAULT '',
            avatar     TEXT NOT NULL DEFAULT '',
            tagline    TEXT NOT NULL DEFAULT '',
            questions  TEXT NOT NULL DEFAULT '[]',
            questions_set INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, persona_id)
        );
        INSERT INTO tenant_personas_new
            (tenant_id, persona_id, name, avatar, tagline, questions, questions_set, updated_at)
        SELECT tenant_id, 'default', '', avatar, tagline, questions, questions_set, updated_at
        FROM tenant_personas;
        DROP TABLE tenant_personas;
        ALTER TABLE tenant_personas_new RENAME TO tenant_personas;
        """
    )
    await conn.commit()


def _require_non_blank(value: str, what: str) -> str:
    if not value.strip():
        raise InvalidPersonaError(f"{what}不能为空")
    return value


async def create_persona(
    conn: aiosqlite.Connection, *, tenant_id: str, persona_id: str, name: str
) -> None:
    """建一张脸。

    空 / 纯空白的 persona_id 拒掉：空串是合法的主键值，建出来之后在任何
    按 id 定位的地方都跟"没指定"撞车——而"没指定"走的是 default 那张脸。
    """
    _require_non_blank(persona_id, "数字人 ID")
    _require_non_blank(name, "数字人名字")
    await conn.execute(
        "INSERT INTO tenant_personas (tenant_id, persona_id, name) VALUES (?, ?, ?) "
        "ON CONFLICT (tenant_id, persona_id) DO UPDATE SET "
        "name = excluded.name, updated_at = datetime('now')",
        (tenant_id, persona_id, name),
    )
    await conn.commit()


async def list_personas(conn: aiosqlite.Connection, tenant_id: str) -> list[dict[str, Any]]:
    """这个租户的全部脸，按建的顺序。

    tenant_id 是查询条件不是断言：拿别的租户的过来必须查不到。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id, persona_id, name, avatar, tagline FROM tenant_personas "
        "WHERE tenant_id = ? ORDER BY rowid",
        (tenant_id,),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def delete_persona(
    conn: aiosqlite.Connection, *, tenant_id: str, persona_id: str
) -> None:
    """删一张脸。别的租户的删不掉。

    「不许删到零张」的判断在路由层：删光了这个租户在右栏就消失了，用户会
    以为租户没了——但那是一条产品规则，不是这一层的事，store 只负责删。
    """
    await conn.execute(
        "DELETE FROM tenant_personas WHERE tenant_id = ? AND persona_id = ?",
        (tenant_id, persona_id),
    )
    await conn.commit()


async def upsert_persona(
    conn: aiosqlite.Connection, *, tenant_id: str, avatar: str, tagline: str,
    persona_id: str = DEFAULT_PERSONA_ID,
) -> None:
    """写脸。questions 不在 DO UPDATE 的列里——它有自己的写入口 set_questions，
    这里顺手把它清空的话，改一次头像就会把引导问题全删掉。

    跟 set_questions 反过来对称：set_questions 写引导问题时不碰 avatar/tagline，
    这里写脸时也不碰 questions，两个编辑动作互不清空对方。"""
    await conn.execute(
        "INSERT INTO tenant_personas (tenant_id, persona_id, avatar, tagline) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (tenant_id, persona_id) DO UPDATE SET "
        "avatar = excluded.avatar, tagline = excluded.tagline, updated_at = datetime('now')",
        (tenant_id, persona_id, avatar, tagline),
    )
    await conn.commit()


async def get_persona(
    conn: aiosqlite.Connection, tenant_id: str, *, persona_id: str = DEFAULT_PERSONA_ID
) -> dict[str, Any] | None:
    """自己设 row_factory，跟本仓库其它 store 的做法一致（如
    app/auth/user_tenants_store.py）。生产路径上 review_conn 是进程内单例
    （见 app/api/deps.py::get_review_conn），启动阶段 seed_admin_user →
    get_admin_user 会先把它的 row_factory 设成 aiosqlite.Row，这里目前是
    "碰巧"能按列名取值；不自设的话，一旦调用顺序被打破，`dict(row)` 会以
    ValueError 收场（已用未设 row_factory 的连接验证过），而不是静默返回
    错的数据。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id, persona_id, name, avatar, tagline FROM tenant_personas "
        "WHERE tenant_id = ? AND persona_id = ?",
        (tenant_id, persona_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def get_personas(
    conn: aiosqlite.Connection, tenant_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """一次问一批。右栏有 N 个数字人，逐个问就是 N 次查询。

    空列表直接返回、不发查询：拼出来的 `IN ()` 在 SQLite 上是语法错误。

    row_factory 的理由同 get_persona：不自设的话 `row["tenant_id"]` 会以
    TypeError 收场（同样已验证过），而不是拿到错的键。
    """
    conn.row_factory = aiosqlite.Row
    if not tenant_ids:
        return {}
    placeholders = ",".join("?" for _ in tenant_ids)
    cursor = await conn.execute(
        f"SELECT tenant_id, persona_id, name, avatar, tagline FROM tenant_personas "
        f"WHERE tenant_id IN ({placeholders}) AND persona_id = 'default'",
        tuple(tenant_ids),
    )
    return {row["tenant_id"]: dict(row) for row in await cursor.fetchall()}


async def set_questions(
    conn: aiosqlite.Connection, *, tenant_id: str, questions: list[str],
    persona_id: str = DEFAULT_PERSONA_ID,
) -> None:
    """写引导问题。存 JSON 数组而不是另开一张行表：它是一个有序的短列表，
    整体读整体写，拆成行表只会让「顺序」需要一个额外的列来维护。

    avatar/tagline 不在 DO UPDATE 的列里——跟 upsert_persona 反过来对称：
    upsert_persona 写脸时不碰 questions，这里写 questions 时也不碰脸，
    两个编辑动作互不清空对方。

    这里不做校验——校验在路由层（保存时要把「哪几条不通过」告诉用户，
    而这个函数只能返回成功或抛异常，说不出是哪几条）。
    """
    await conn.execute(
        "INSERT INTO tenant_personas (tenant_id, persona_id, questions, questions_set) "
        "VALUES (?, ?, ?, 1) "
        "ON CONFLICT (tenant_id, persona_id) DO UPDATE SET "
        "questions = excluded.questions, questions_set = 1, updated_at = datetime('now')",
        (tenant_id, persona_id, json.dumps(questions, ensure_ascii=False)),
    )
    await conn.commit()


async def get_questions_setting(
    conn: aiosqlite.Connection, tenant_id: str, *, persona_id: str = DEFAULT_PERSONA_ID
) -> list[str] | None:
    """读引导问题，并区分「配了个空的」和「从没配过」。

    返回 `None` 表示从没配过——调用方该走自动兜底。返回 `[]` 表示这个租户
    显式地说了"我不要引导问题"，兜底必须让路：否则管理员刚删光的内容会
    原样冒回前台，而他唯一的出路是留一条自己不想要的问题。

    判据是 `questions_set == 0 且列表为空`。不单看 `questions_set`，是为了
    照顾这一列被加进来之前就存在的行：它们的标志位是补列时的默认 0，但
    里面确实存着手写的问题，只看标志位会把它们一起冲回自动兜底。
    非空永远算配过，跟标志位无关。

    自己设 row_factory，理由同 get_persona：不自设的话，一旦调用顺序被打破
    （生产路径上依赖 seed_admin_user → get_admin_user 先把进程内单例连接的
    row_factory 设成 aiosqlite.Row 这个"碰巧"），`row["questions"]` 会以
    TypeError 收场，而不是取到错的数据。

    JSON 解析失败时按「从没配过」处理并告警：这一列是人写进去的，历史上
    手工改库留下一个坏值是可能的，而它不该让整个前台首屏 500。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT questions, questions_set FROM tenant_personas "
        "WHERE tenant_id = ? AND persona_id = ?",
        (tenant_id, persona_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        parsed = json.loads(row["questions"])
    except (TypeError, ValueError):
        logger.warning("租户 %r 的 questions 列不是合法 JSON，按「从没配过」处理", tenant_id)
        return None
    questions = [q for q in parsed if isinstance(q, str)] if isinstance(parsed, list) else []
    if not questions and not row["questions_set"]:
        return None
    return questions


async def get_questions(
    conn: aiosqlite.Connection, tenant_id: str, *, persona_id: str = DEFAULT_PERSONA_ID
) -> list[str]:
    """读手写的引导问题，「从没配过」和「配了个空的」都返回空列表。

    给不关心这个区别的调用方用（比如失效检测：两种情况下都没有手写问题
    可查）。要区分的调用方用 get_questions_setting。
    """
    return await get_questions_setting(conn, tenant_id, persona_id=persona_id) or []
