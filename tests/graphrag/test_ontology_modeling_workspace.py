from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.ontology_modeling_workspace import (
    InvalidWorkspaceStateError,
    WorkspaceConflictError,
    WorkspaceExistsError,
    WorkspaceNotFoundError,
    create_workspace,
    delete_workspace,
    ensure_modeling_workspace_schema,
    get_workspace,
    initial_state_from_skill,
    save_workspace,
    validate_state,
)
from app.graphrag.ontology_skills import discover_skills

pytestmark = pytest.mark.anyio

BUILTIN_DIR = Path(__file__).resolve().parents[2] / "app" / "ontology_skills"


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_modeling_workspace_schema(conn)
    return conn


def _retail():
    return discover_skills(BUILTIN_DIR).get("consumer_retail")


def test_initial_state_from_skill_copies_skeleton_as_pending():
    state = initial_state_from_skill(_retail())
    sku = next(t for t in state["term_types"] if t["value"] == "SKU")
    assert sku["provenance"] == "skill"
    assert sku["review"] == "pending"
    assert sku["display_name"] == "商品"
    # 别名跟着进工作区：对齐在前端做，前端只拿得到工作区，拿不到 skill
    assert "jan" in sku["key_aliases"]
    assert sku["field_aliases"]["color"]
    # extra_fields 用本体表的键名（name/value_type/label），应用到草稿时直接透传
    assert sku["extra_fields"][0]["label"] == "颜色"
    assert sku["clues"] == []
    assert sku["data_match"] is None
    assert {c["relation"] for c in state["constraints"]} == {"BELONGS_TO_CATEGORY", "SOLD_AT"}
    assert state["sources"] == []
    assert state["unmatched_columns"] == {}
    # v1 不做问题清单，但这个键要在，前端不必判 undefined
    assert state["questions"] == []


def test_initial_state_without_skill_is_empty_but_well_formed():
    state = initial_state_from_skill(None)
    assert state == {
        "term_types": [],
        "relation_types": [],
        "constraints": [],
        "sources": [],
        "unmatched_columns": {},
        "questions": [],
    }


@pytest.mark.parametrize(
    "bad,fragment",
    [
        ([], "映射"),
        ({"term_types": {}}, "term_types"),
        ({"term_types": [{"value": "SKU", "provenance": "llm", "review": "pending"}]}, "llm"),
        ({"term_types": [{"value": "SKU", "provenance": "skill", "review": "maybe"}]}, "maybe"),
        ({"term_types": [{"provenance": "skill", "review": "pending"}]}, "value"),
        ({"relation_types": [{"relation_type": "SOLD_AT", "provenance": "skill", "review": "x"}]}, "x"),
        ({"constraints": [{"subject": "SKU", "relation": "SOLD_AT"}]}, "object"),
        ({"sources": [{"sheet": 0}]}, "file"),
        ({"unmatched_columns": {"a.csv": "md_no"}}, "unmatched_columns"),
    ],
)
def test_validate_state_rejects_malformed_state(bad, fragment):
    with pytest.raises(InvalidWorkspaceStateError) as exc_info:
        validate_state(bad)
    assert fragment in str(exc_info.value)


def test_validate_state_fills_missing_top_level_keys():
    # 前端少传一个键不该 500：补齐成空值，语义等同"这一类什么都没有"
    state = validate_state({"term_types": []})
    assert state["relation_types"] == []
    assert state["questions"] == []


async def test_create_then_get_round_trips():
    conn = await _conn()
    created = await create_workspace(
        conn, "t1", skill=_retail(), actor="alice", now="2026-09-16T10:00:00"
    )
    assert created.skill_name == "consumer_retail"
    assert created.skill_version == "1"
    assert created.updated_by == "alice"
    loaded = await get_workspace(conn, "t1")
    assert loaded == created
    assert any(t["value"] == "SKU" for t in loaded.state["term_types"])


async def test_get_returns_none_when_absent():
    conn = await _conn()
    assert await get_workspace(conn, "t1") is None


async def test_create_twice_raises():
    conn = await _conn()
    await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-16T10:00:00")
    with pytest.raises(WorkspaceExistsError):
        await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-16T10:01:00")


async def test_save_requires_matching_updated_at():
    conn = await _conn()
    created = await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-16T10:00:00")
    saved = await save_workspace(
        conn,
        "t1",
        state={"term_types": [{"value": "SKU", "provenance": "manual", "review": "accepted"}]},
        expected_updated_at=created.updated_at,
        actor="bob",
        now="2026-09-16T10:05:00",
    )
    assert saved.updated_at == "2026-09-16T10:05:00"
    assert saved.updated_by == "bob"
    # 拿旧时间戳再写一次：两个人同时开着工作台，后写的人不该静默盖掉前一个人
    with pytest.raises(WorkspaceConflictError):
        await save_workspace(
            conn,
            "t1",
            state={"term_types": []},
            expected_updated_at=created.updated_at,
            actor="carol",
            now="2026-09-16T10:06:00",
        )
    # 冲突之后库里还是 bob 那一版，没有被改动
    current = await get_workspace(conn, "t1")
    assert current.updated_by == "bob"
    assert current.state["term_types"][0]["value"] == "SKU"


async def test_save_absent_workspace_raises():
    conn = await _conn()
    with pytest.raises(WorkspaceNotFoundError):
        await save_workspace(
            conn, "t1", state={}, expected_updated_at="whatever", actor="a", now="b"
        )


async def test_save_absent_workspace_with_invalid_state_still_raises_not_found():
    # 不存在 + state 也非法时，客户端要拿到 404（该重新起步）而不是 400
    # （会误以为是自己传的 state 有问题，去改 state 而不是重新创建）。
    conn = await _conn()
    with pytest.raises(WorkspaceNotFoundError):
        await save_workspace(
            conn,
            "t1",
            state={"term_types": [{"value": "SKU", "provenance": "llm", "review": "pending"}]},
            expected_updated_at="whatever",
            actor="a",
            now="b",
        )


async def test_save_rejects_malformed_state_without_writing():
    conn = await _conn()
    created = await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-16T10:00:00")
    with pytest.raises(InvalidWorkspaceStateError):
        await save_workspace(
            conn,
            "t1",
            state={"term_types": [{"value": "SKU", "provenance": "llm", "review": "pending"}]},
            expected_updated_at=created.updated_at,
            actor="bob",
            now="2026-09-16T10:05:00",
        )
    assert (await get_workspace(conn, "t1")).updated_by == "alice"


async def test_delete_workspace_is_idempotent():
    conn = await _conn()
    await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-16T10:00:00")
    await delete_workspace(conn, "t1")
    assert await get_workspace(conn, "t1") is None
    await delete_workspace(conn, "t1")  # 再删一次不报错


async def test_save_keeps_extra_keys_inside_sources_entries():
    """前端会往 sources[] 条目里多放 columns（列角色/依据），后端只校验外形，
    多出来的键必须原样存取——丢了的话工作台重新打开时未接住列旁的依据全没了。"""
    conn = await _conn()
    created = await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-17T10:00:00")
    state = {
        "sources": [
            {
                "file": "a.csv",
                "header_row": 6,
                "columns": [{"name": "JAN", "role": "identifier", "reason": "100/100", "inferred_type": "string"}],
            }
        ]
    }
    saved = await save_workspace(
        conn, "t1", state=state, expected_updated_at=created.updated_at, actor="alice", now="2026-09-17T10:01:00"
    )
    assert saved.state["sources"][0]["columns"][0]["reason"] == "100/100"
    assert (await get_workspace(conn, "t1")).state["sources"][0]["columns"][0]["role"] == "identifier"


async def test_ensure_ontology_schema_creates_the_workspace_table():
    """建表挂进统一入口。不挂的话，真实的 get_review_conn 开出来的连接上没有
    这张表，工作台第一次请求就是 no such table。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_modeling_workspaces'"
    )
    assert await cursor.fetchone() is not None
