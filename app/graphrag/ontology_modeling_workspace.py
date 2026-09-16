"""建模工作区：一个租户一份、长期存在的建模过程状态。

它**不是**本体草稿——骨架里的元素要用户显式"应用"才写进
ontology_term_types 等三张草稿表。两者分开的理由（spec 决策 9/10）：工作区
要留住"这个我拒过""这个还没数据"这类过程信息，而本体表是结果，多存一列
过程状态就会被 ETL、问答、结构页各自解释一遍。

叫"工作区"不叫"会话"：代码库里"会话"已经指登录会话（AdminSession）和前台
聊天会话两样东西，这份状态跟两者都无关——不随登录失效、不属于某个用户，
跟租户的本体同寿。

state_json 对本模块是**不透明**的：只校验外形（键在不在、枚举值合不合法、
列表还是映射），不校验语义（元素之间引不引用得上、别名对不对得上列）。
语义检查在两个更合适的地方各做一次：前端做，因为它拿得到用户正在看的界面；
replace_draft 做，因为那是真正会写进本体的那一刻，它已经有一整套引用检查。
在这里再做第三遍只会让"保存一下"这个动作变得可能失败。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

from app.graphrag.ontology_skills import OntologySkill

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_modeling_workspaces (
    tenant_id     TEXT NOT NULL PRIMARY KEY,
    skill_name    TEXT,
    skill_version TEXT,
    state_json    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    updated_by    TEXT NOT NULL
);
"""

#: 来源。v1 只会产生 skill / data / manual 三种；llm / document / question 是
#: v2 的三条来源，现在拒掉——放行的话前端会收到自己不认识的标，而"标是哪来的"
#: 没有任何地方说得清。
_PROVENANCES = frozenset({"skill", "data", "manual"})
_REVIEWS = frozenset({"pending", "accepted", "rejected"})
#: 顶层键 -> 它该是什么容器。值同时当"缺省值工厂"用（list() / dict()）。
_TOP_LEVEL_DEFAULTS: dict[str, type] = {
    "term_types": list,
    "relation_types": list,
    "constraints": list,
    "sources": list,
    "unmatched_columns": dict,
    "questions": list,
}


class WorkspaceExistsError(Exception):
    """这个租户已经有工作区了。"""


class WorkspaceNotFoundError(Exception):
    """这个租户还没有工作区。"""


class WorkspaceConflictError(Exception):
    """带来的 updated_at 不是库里那一版——期间有别人存过。"""


class InvalidWorkspaceStateError(Exception):
    """state_json 外形不合法。"""


@dataclass(frozen=True)
class ModelingWorkspace:
    tenant_id: str
    skill_name: str | None
    skill_version: str | None
    state: dict
    updated_at: str
    updated_by: str

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "state": self.state,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


async def ensure_modeling_workspace_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def initial_state_from_skill(skill: OntologySkill | None) -> dict:
    """把 skill 的骨架抄成工作区初始状态，每个元素 provenance=skill、review=pending。

    别名（key_aliases / field_aliases）一起抄进来，不是留在 skill 里按名字回查：
    数据对齐在前端做，前端只拿得到工作区；而且用户改过名之后，按 value 回查
    skill 会查不到，别名会静默消失，表现为"改了个名字，数据就对不上了"。
    """
    if skill is None:
        return {
            "term_types": [],
            "relation_types": [],
            "constraints": [],
            "sources": [],
            "unmatched_columns": {},
            "questions": [],
        }
    return {
        "term_types": [
            {
                "value": t.value,
                "display_name": t.display_name,
                "provenance": "skill",
                "review": "pending",
                "standard_name_value_type": t.standard_name_value_type,
                # 键名跟着本体表走（name/value_type/label），应用到草稿时原样
                # 透传给 replace_draft，不需要中间再翻译一次。
                "extra_fields": [
                    {"name": f.name, "value_type": f.value_type, "label": f.display_name}
                    for f in t.extra_fields
                ],
                "key_aliases": list(t.key_aliases),
                "field_aliases": {k: list(v) for k, v in t.field_aliases.items()},
                "clues": [],
                "data_match": None,
            }
            for t in skill.term_types
        ],
        "relation_types": [
            {
                "relation_type": r.relation_type,
                "example_phrase": r.example_phrase,
                "description": r.description,
                "provenance": "skill",
                "review": "pending",
                "clues": [],
                "data_match": None,
            }
            for r in skill.relation_types
        ],
        "constraints": [
            {
                "subject": c.subject,
                "relation": c.relation,
                "object": c.object,
                "provenance": "skill",
                "review": "pending",
            }
            for c in skill.constraints
        ],
        "sources": [],
        "unmatched_columns": {},
        "questions": [],
    }


def _require_keys(item: object, keys: tuple[str, ...], *, where: str) -> dict:
    if not isinstance(item, dict):
        raise InvalidWorkspaceStateError(f"{where} 的每一项要是映射，收到: {item!r}")
    for key in keys:
        if not isinstance(item.get(key), str) or not item[key]:
            raise InvalidWorkspaceStateError(f"{where} 缺少非空字符串字段 {key}: {item!r}")
    return item


def _check_review_and_provenance(item: dict, *, where: str) -> None:
    provenance = item.get("provenance")
    if provenance not in _PROVENANCES:
        raise InvalidWorkspaceStateError(
            f"{where} 的 provenance {provenance!r} 不合法，v1 只接受 {sorted(_PROVENANCES)}"
        )
    review = item.get("review")
    if review not in _REVIEWS:
        raise InvalidWorkspaceStateError(
            f"{where} 的 review {review!r} 不合法，只接受 {sorted(_REVIEWS)}"
        )


def validate_state(state: object) -> dict:
    """校验外形并补齐缺失的顶层键，返回可以直接 json.dumps 的 dict。

    缺键补齐而不是报错：前端少传一类（比如从来没做过数据发现，不传
    unmatched_columns）语义就是"这一类什么都没有"，让它 400 只会逼前端
    每次都构造完整对象。
    """
    if not isinstance(state, dict):
        raise InvalidWorkspaceStateError(f"工作区状态要是映射，收到: {type(state).__name__}")
    normalized: dict = {}
    for key, kind in _TOP_LEVEL_DEFAULTS.items():
        value = state.get(key, kind())
        if not isinstance(value, kind):
            raise InvalidWorkspaceStateError(
                f"工作区状态的 {key} 要是{'列表' if kind is list else '映射'}，收到: {value!r}"
            )
        normalized[key] = value

    for item in normalized["term_types"]:
        _require_keys(item, ("value",), where="term_types")
        _check_review_and_provenance(item, where=f"term_types[{item.get('value')!r}]")
    for item in normalized["relation_types"]:
        _require_keys(item, ("relation_type",), where="relation_types")
        _check_review_and_provenance(item, where=f"relation_types[{item.get('relation_type')!r}]")
    for item in normalized["constraints"]:
        _require_keys(item, ("subject", "relation", "object"), where="constraints")
        _check_review_and_provenance(item, where=f"constraints[{item!r}]")
    for item in normalized["sources"]:
        _require_keys(item, ("file",), where="sources")
    for file_name, columns in normalized["unmatched_columns"].items():
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise InvalidWorkspaceStateError(
                f"unmatched_columns[{file_name!r}] 要是字符串列表，收到: {columns!r}"
            )
    return normalized


def _row_to_workspace(tenant_id: str, row) -> ModelingWorkspace:
    return ModelingWorkspace(
        tenant_id=tenant_id,
        skill_name=row[0],
        skill_version=row[1],
        state=json.loads(row[2]),
        updated_at=row[3],
        updated_by=row[4],
    )


async def get_workspace(conn: aiosqlite.Connection, tenant_id: str) -> ModelingWorkspace | None:
    cursor = await conn.execute(
        "SELECT skill_name, skill_version, state_json, updated_at, updated_by "
        "FROM ontology_modeling_workspaces WHERE tenant_id = ?",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else _row_to_workspace(tenant_id, row)


async def create_workspace(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    skill: OntologySkill | None,
    actor: str,
    now: str,
) -> ModelingWorkspace:
    if await get_workspace(conn, tenant_id) is not None:
        raise WorkspaceExistsError(f"租户 {tenant_id} 已经有建模工作区了")
    state = initial_state_from_skill(skill)
    await conn.execute(
        "INSERT INTO ontology_modeling_workspaces "
        "(tenant_id, skill_name, skill_version, state_json, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            tenant_id,
            None if skill is None else skill.name,
            None if skill is None else skill.version,
            json.dumps(state, ensure_ascii=False),
            now,
            actor,
        ),
    )
    await conn.commit()
    return ModelingWorkspace(
        tenant_id=tenant_id,
        skill_name=None if skill is None else skill.name,
        skill_version=None if skill is None else skill.version,
        state=state,
        updated_at=now,
        updated_by=actor,
    )


async def save_workspace(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    state: dict,
    expected_updated_at: str,
    actor: str,
    now: str,
) -> ModelingWorkspace:
    """整份写回，乐观锁。

    带时间戳而不是无脑覆盖：工作台是长期存在的（spec 决策 10），同一个租户
    的两个人同时开着它很正常。无锁覆盖时后点保存的人会静默抹掉前一个人刚
    做完的一批审阅，而两边界面都显示"已保存"。
    """
    current = await get_workspace(conn, tenant_id)
    if current is None:
        raise WorkspaceNotFoundError(f"租户 {tenant_id} 还没有建模工作区")
    if current.updated_at != expected_updated_at:
        raise WorkspaceConflictError(
            f"工作区在 {current.updated_at} 被 {current.updated_by} 改过，"
            f"你手上这份是 {expected_updated_at} 的。刷新后重试。"
        )
    # 校验放在写之前：校验失败时库里还是上一版，不需要事务回滚（理由同
    # replace_draft 的 docstring：单例连接上不能用显式事务）。
    normalized = validate_state(state)
    await conn.execute(
        "UPDATE ontology_modeling_workspaces SET state_json = ?, updated_at = ?, updated_by = ? "
        "WHERE tenant_id = ?",
        (json.dumps(normalized, ensure_ascii=False), now, actor, tenant_id),
    )
    await conn.commit()
    return ModelingWorkspace(
        tenant_id=tenant_id,
        skill_name=current.skill_name,
        skill_version=current.skill_version,
        state=normalized,
        updated_at=now,
        updated_by=actor,
    )


async def delete_workspace(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """删掉工作区。幂等——"重新起步"按钮会先删再建，删一个不存在的不该报错。"""
    await conn.execute("DELETE FROM ontology_modeling_workspaces WHERE tenant_id = ?", (tenant_id,))
    await conn.commit()
