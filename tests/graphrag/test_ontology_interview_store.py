from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_interview_store import (
    OPENING_QUESTION,
    InterviewConflictError,
    InterviewExistsError,
    InterviewNotFoundError,
    InvalidInterviewStateError,
    create_session,
    delete_session,
    ensure_interview_schema,
    get_session,
    initial_state,
    save_session,
    validate_state,
)
from app.graphrag.ontology_lifecycle import ensure_ontology_schema

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_interview_schema(conn)
    return conn


def test_initial_state_starts_with_the_fixed_opening_question():
    state = initial_state()
    # 第一问写死：省一次 LLM 调用，而且第一问没有猜错的空间
    assert state["turns"] == [{"role": "assistant", "text": OPENING_QUESTION}]
    assert state["skeleton"] == {"term_types": [], "relation_types": [], "constraints": []}
    assert state["questions"] == []
    assert state["done"] is False


@pytest.mark.parametrize(
    "bad,fragment",
    [
        ([], "映射"),
        ({"turns": {}}, "turns"),
        ({"turns": [{"role": "system", "text": "x"}]}, "system"),
        ({"turns": [{"role": "user"}]}, "text"),
        ({"skeleton": []}, "skeleton"),
        ({"skeleton": {"term_types": [{"value": "SKU", "review": "maybe"}]}}, "maybe"),
        ({"skeleton": {"term_types": [{"review": "pending"}]}}, "value"),
        ({"skeleton": {"relation_types": [{"relation_type": "X"}]}}, "review"),
        ({"skeleton": {"constraints": [{"subject": "A", "relation": "R", "review": "pending"}]}}, "object"),
        ({"questions": [{"needs": {}}]}, "text"),
        ({"done": "yes"}, "done"),
    ],
)
def test_validate_state_rejects_malformed_state(bad, fragment):
    with pytest.raises(InvalidInterviewStateError) as exc_info:
        validate_state(bad)
    assert fragment in str(exc_info.value)


def test_validate_state_fills_missing_keys():
    state = validate_state({"turns": []})
    assert state["skeleton"] == {"term_types": [], "relation_types": [], "constraints": []}
    assert state["questions"] == []
    assert state["done"] is False


async def test_create_then_get_round_trips():
    conn = await _conn()
    created = await create_session(conn, "t1", actor="alice", now="2026-09-17T10:00:00")
    assert created.updated_by == "alice"
    assert created.state["turns"][0]["text"] == OPENING_QUESTION
    assert await get_session(conn, "t1") == created


async def test_get_absent_returns_none():
    conn = await _conn()
    assert await get_session(conn, "t1") is None


async def test_create_twice_raises():
    conn = await _conn()
    await create_session(conn, "t1", actor="a", now="n")
    with pytest.raises(InterviewExistsError):
        await create_session(conn, "t1", actor="a", now="n2")


async def test_save_is_optimistically_locked_and_atomic():
    conn = await _conn()
    created = await create_session(conn, "t1", actor="alice", now="2026-09-17T10:00:00")
    saved = await save_session(
        conn, "t1",
        state={"turns": [{"role": "assistant", "text": "q"}, {"role": "user", "text": "a"}]},
        expected_updated_at=created.updated_at, actor="bob", now="2026-09-17T10:01:00",
    )
    assert saved.updated_by == "bob"
    with pytest.raises(InterviewConflictError):
        await save_session(
            conn, "t1", state={"turns": []},
            expected_updated_at=created.updated_at, actor="carol", now="2026-09-17T10:02:00",
        )
    assert (await get_session(conn, "t1")).updated_by == "bob"


async def test_save_absent_raises_not_found_even_with_invalid_state():
    conn = await _conn()
    with pytest.raises(InterviewNotFoundError):
        await save_session(conn, "t1", state={"turns": {}}, expected_updated_at="x", actor="a", now="n")


async def test_save_rejects_invalid_state_without_writing():
    conn = await _conn()
    created = await create_session(conn, "t1", actor="alice", now="n")
    with pytest.raises(InvalidInterviewStateError):
        await save_session(conn, "t1", state={"done": "yes"}, expected_updated_at=created.updated_at, actor="bob", now="n2")
    assert (await get_session(conn, "t1")).updated_by == "alice"


async def test_delete_is_idempotent():
    conn = await _conn()
    await create_session(conn, "t1", actor="a", now="n")
    await delete_session(conn, "t1")
    assert await get_session(conn, "t1") is None
    await delete_session(conn, "t1")


async def test_ensure_ontology_schema_creates_the_table():
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_interview_sessions'"
    )
    assert await cursor.fetchone() is not None
