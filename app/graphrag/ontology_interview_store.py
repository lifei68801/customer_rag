"""智能创建的访谈会话：一个租户一份、长期存在、可续。

跟建模工作区（ontology_modeling_workspace.py）同一个取向、同一套乐观锁，
但**不共用表、不共用代码**：spec 决策 2 定了三种构建方式各自独立，共用
存储会让"独立"在第一次 schema 改动时就名存实亡。

state_json 对本模块不透明：只校验外形，语义（元素之间引用得上不上）留给
写草稿那一刻的 replace_draft 去查。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

#: 第一问写死（spec 决策 4）：省一次 LLM 调用，而且第一问没有猜错的余地。
OPENING_QUESTION = "先说说你们主要做什么生意？卖什么、卖给谁、通过什么渠道。"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_interview_sessions (
    tenant_id   TEXT NOT NULL PRIMARY KEY,
    state_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);
"""

_ROLES = frozenset({"assistant", "user"})
_REVIEWS = frozenset({"pending", "accepted", "rejected"})


class InterviewExistsError(Exception):
    """这个租户已经有访谈会话了。"""


class InterviewNotFoundError(Exception):
    """这个租户还没有访谈会话。"""


class InterviewConflictError(Exception):
    """带来的 updated_at 不是库里那一版。"""


class InvalidInterviewStateError(Exception):
    """state_json 外形不合法。"""


@dataclass(frozen=True)
class InterviewSession:
    tenant_id: str
    state: dict
    updated_at: str
    updated_by: str

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "state": self.state,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


async def ensure_interview_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def initial_state() -> dict:
    return {
        "turns": [{"role": "assistant", "text": OPENING_QUESTION}],
        "skeleton": {"term_types": [], "relation_types": [], "constraints": []},
        "questions": [],
        "done": False,
    }


def _require_keys(item: object, keys: tuple[str, ...], *, where: str) -> dict:
    if not isinstance(item, dict):
        raise InvalidInterviewStateError(f"{where} 的每一项要是映射，收到: {item!r}")
    for key in keys:
        if not isinstance(item.get(key), str) or not item[key]:
            raise InvalidInterviewStateError(f"{where} 缺少非空字符串字段 {key}: {item!r}")
    return item


def _check_review(item: dict, *, where: str) -> None:
    review = item.get("review")
    if review not in _REVIEWS:
        raise InvalidInterviewStateError(f"{where} 的 review {review!r} 不合法，只接受 {sorted(_REVIEWS)}")


def validate_state(state: object) -> dict:
    """校验外形并补齐缺失的顶层键。语义不在这里查（理由见模块 docstring）。"""
    if not isinstance(state, dict):
        raise InvalidInterviewStateError(f"访谈状态要是映射，收到: {type(state).__name__}")
    turns = state.get("turns", [])
    if not isinstance(turns, list):
        raise InvalidInterviewStateError(f"访谈状态的 turns 要是列表，收到: {turns!r}")
    for turn in turns:
        _require_keys(turn, ("role", "text"), where="turns")
        if turn["role"] not in _ROLES:
            raise InvalidInterviewStateError(f"turns 的 role {turn['role']!r} 不合法，只接受 {sorted(_ROLES)}")

    skeleton = state.get("skeleton", {})
    if not isinstance(skeleton, dict):
        raise InvalidInterviewStateError(f"访谈状态的 skeleton 要是映射，收到: {skeleton!r}")
    normalized_skeleton: dict = {}
    for key in ("term_types", "relation_types", "constraints"):
        items = skeleton.get(key, [])
        if not isinstance(items, list):
            raise InvalidInterviewStateError(f"skeleton 的 {key} 要是列表，收到: {items!r}")
        normalized_skeleton[key] = items
    for item in normalized_skeleton["term_types"]:
        _require_keys(item, ("value",), where="skeleton.term_types")
        _check_review(item, where=f"skeleton.term_types[{item['value']!r}]")
    for item in normalized_skeleton["relation_types"]:
        _require_keys(item, ("relation_type",), where="skeleton.relation_types")
        _check_review(item, where=f"skeleton.relation_types[{item['relation_type']!r}]")
    for item in normalized_skeleton["constraints"]:
        _require_keys(item, ("subject", "relation", "object"), where="skeleton.constraints")
        _check_review(item, where=f"skeleton.constraints[{item!r}]")

    questions = state.get("questions", [])
    if not isinstance(questions, list):
        raise InvalidInterviewStateError(f"访谈状态的 questions 要是列表，收到: {questions!r}")
    for question in questions:
        _require_keys(question, ("text",), where="questions")

    done = state.get("done", False)
    if not isinstance(done, bool):
        raise InvalidInterviewStateError(f"访谈状态的 done 要是布尔，收到: {done!r}")

    return {"turns": turns, "skeleton": normalized_skeleton, "questions": questions, "done": done}


def _row_to_session(tenant_id: str, row) -> InterviewSession:
    return InterviewSession(tenant_id=tenant_id, state=json.loads(row[0]), updated_at=row[1], updated_by=row[2])


async def get_session(conn: aiosqlite.Connection, tenant_id: str) -> InterviewSession | None:
    cursor = await conn.execute(
        "SELECT state_json, updated_at, updated_by FROM ontology_interview_sessions WHERE tenant_id = ?",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else _row_to_session(tenant_id, row)


async def create_session(conn: aiosqlite.Connection, tenant_id: str, *, actor: str, now: str) -> InterviewSession:
    if await get_session(conn, tenant_id) is not None:
        raise InterviewExistsError(f"租户 {tenant_id} 已经有访谈会话了")
    state = initial_state()
    try:
        await conn.execute(
            "INSERT INTO ontology_interview_sessions (tenant_id, state_json, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            (tenant_id, json.dumps(state, ensure_ascii=False), now, actor),
        )
    except aiosqlite.IntegrityError as exc:
        # 上面那次查和这次插之间被别人抢先了：主键撞上，报"已存在"而不是 500。
        raise InterviewExistsError(f"租户 {tenant_id} 已经有访谈会话了") from exc
    await conn.commit()
    return InterviewSession(tenant_id=tenant_id, state=state, updated_at=now, updated_by=actor)


async def save_session(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    state: dict,
    expected_updated_at: str,
    actor: str,
    now: str,
) -> InterviewSession:
    """整份写回，原子乐观锁（UPDATE … WHERE updated_at = ?）。

    先查存在性再校验 state：客户端拿到 404 才知道该重新开始，拿到 400 会去
    改 state。校验放在写之前：失败时库里还是上一版，不需要事务。
    """
    if await get_session(conn, tenant_id) is None:
        raise InterviewNotFoundError(f"租户 {tenant_id} 还没有访谈会话")
    normalized = validate_state(state)
    cursor = await conn.execute(
        "UPDATE ontology_interview_sessions SET state_json = ?, updated_at = ?, updated_by = ? "
        "WHERE tenant_id = ? AND updated_at = ?",
        (json.dumps(normalized, ensure_ascii=False), now, actor, tenant_id, expected_updated_at),
    )
    if cursor.rowcount == 0:
        current = await get_session(conn, tenant_id)
        if current is None:
            raise InterviewNotFoundError(f"租户 {tenant_id} 还没有访谈会话")
        raise InterviewConflictError(
            f"访谈在 {current.updated_at} 被 {current.updated_by} 改过，你手上这份是 {expected_updated_at} 的。刷新后重试。"
        )
    await conn.commit()
    return InterviewSession(tenant_id=tenant_id, state=normalized, updated_at=now, updated_by=actor)


async def delete_session(conn: aiosqlite.Connection, tenant_id: str) -> None:
    await conn.execute("DELETE FROM ontology_interview_sessions WHERE tenant_id = ?", (tenant_id,))
    await conn.commit()
