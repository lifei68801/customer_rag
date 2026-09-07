"""本体变更日志：谁、什么时候、把什么改成了什么。

为什么是一张独立的日志表而不是给三张本体表各加 edited_by/edited_at 两列：
ontology_term_types / tenant_relation_types / term_type_relation_allowlist
的删除都是真 DELETE，行没了、挂在行上的审计字段跟着没了，而"谁删了这个
分类"恰恰是最需要事后追查的那个问题。行上的字段结构性地覆盖不了删除。
"""

from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag import ontology_change_log
from app.graphrag.ontology_categories import (
    ExtraFieldSpec,
    create_term_type,
    delete_term_type,
    list_term_types,
    update_term_type,
)
from app.graphrag.ontology_change_log import (
    ensure_change_log_schema,
    list_ontology_changes,
)
from app.graphrag.ontology_constraints import (
    add_allowed_combination,
    list_allowed_combinations,
    remove_allowed_combination,
)
from app.graphrag.ontology_lifecycle import (
    confirm_ontology,
    ensure_ontology_schema,
    replace_draft,
)
from app.graphrag.ontology_relations import (
    create_relation_type,
    delete_relation_type,
    list_relation_types,
    update_relation_type,
)
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema

pytestmark = pytest.mark.anyio


async def _open_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    # delete_term_type 的引用检查走合并视图（terms 叠加 term_edits），要读
    # 这张表；生产环境由 open_ontology_store_conn 统一建。
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    return conn


@pytest.fixture
async def conn():
    """必须在 finally 里关：aiosqlite 的工作线程不是 daemon 线程，某条
    断言失败时泄漏一个未关闭的连接，会让 pytest 跑完全部用例后卡死在
    解释器退出阶段（tests/api/conftest.py 里记过同一个坑）。"""
    connection = await _open_conn()
    try:
        yield connection
    finally:
        await connection.close()


# ---------------------------------------------------------------------------
# 分类（ontology_term_types）
# ---------------------------------------------------------------------------


async def test_create_term_type_records_who_created_what(conn):
    await create_term_type(
        conn, "t1", value="错误码",
        extra_fields=[ExtraFieldSpec(name="severity", value_type="string")],
        actor="alice",
    )

    changes = await list_ontology_changes(conn, "t1")

    assert len(changes) == 1
    change = changes[0]
    assert change.actor == "alice"
    assert change.action == "create"
    assert change.object_kind == "term_type"
    assert change.object_id == "错误码"
    assert change.details["extra_fields"] == [
        {"name": "severity", "value_type": "string", "label": ""}
    ]
    assert change.changed_at != ""


async def test_update_term_type_records_the_new_shape_not_just_the_old_name(conn):
    """update 的日志要能回答"改成了什么"——只记被改对象的旧名字，事后
    看到的是"bob 动过 错误码"，动成什么样仍然不知道。"""
    await create_term_type(conn, "t1", value="错误码", actor="alice")
    await update_term_type(
        conn, "t1", value="错误码", new_value="故障码",
        extra_fields=[ExtraFieldSpec(name="severity", value_type="integer")],
        standard_name_value_type="string",
        actor="bob",
    )

    change = (await list_ontology_changes(conn, "t1"))[-1]

    assert change.actor == "bob"
    assert change.action == "update"
    assert change.object_kind == "term_type"
    assert change.object_id == "错误码"
    assert change.details["new_value"] == "故障码"
    assert change.details["extra_fields"] == [
        {"name": "severity", "value_type": "integer", "label": ""}
    ]


async def test_delete_term_type_leaves_a_log_row_after_the_row_itself_is_gone(conn):
    """这条是这张表存在的全部理由：分类行被真删了，"谁删的"只能问日志。"""
    await create_term_type(conn, "t1", value="错误码", actor="alice")
    await delete_term_type(conn, "t1", "错误码", actor="bob")

    assert await list_term_types(conn, "t1", status="draft") == []
    change = (await list_ontology_changes(conn, "t1"))[-1]
    assert change.actor == "bob"
    assert change.action == "delete"
    assert change.object_kind == "term_type"
    assert change.object_id == "错误码"


# ---------------------------------------------------------------------------
# 关系类型（tenant_relation_types）
# ---------------------------------------------------------------------------


async def test_create_relation_type_records_who_created_what(conn):
    await create_relation_type(
        conn, "t1", relation_type="SOLD_BY", example_phrase="产品 SOLD_BY 公司",
        description="销售方", allow_chain_query=True, actor="alice",
    )

    change = (await list_ontology_changes(conn, "t1"))[-1]

    assert change.actor == "alice"
    assert change.action == "create"
    assert change.object_kind == "relation_type"
    assert change.object_id == "SOLD_BY"
    assert change.details["example_phrase"] == "产品 SOLD_BY 公司"
    assert change.details["allow_chain_query"] is True


async def test_update_relation_type_records_the_new_shape(conn):
    await create_relation_type(
        conn, "t1", relation_type="SOLD_BY", example_phrase="产品 SOLD_BY 公司",
        actor="alice",
    )
    await update_relation_type(
        conn, "t1", relation_type="SOLD_BY", new_relation_type="SUPPLIED_BY",
        example_phrase="产品 SUPPLIED_BY 公司", description="供货方",
        allow_chain_query=False, actor="bob",
    )

    change = (await list_ontology_changes(conn, "t1"))[-1]

    assert change.actor == "bob"
    assert change.action == "update"
    assert change.object_kind == "relation_type"
    assert change.object_id == "SOLD_BY"
    assert change.details["new_relation_type"] == "SUPPLIED_BY"
    assert change.details["description"] == "供货方"
    assert change.details["allow_chain_query"] is False


async def test_delete_relation_type_leaves_a_log_row_after_the_row_itself_is_gone(conn):
    await create_relation_type(
        conn, "t1", relation_type="SOLD_BY", example_phrase="产品 SOLD_BY 公司",
        actor="alice",
    )
    await delete_relation_type(conn, "t1", "SOLD_BY", actor="bob")

    assert await list_relation_types(conn, "t1", status="draft") == []
    change = (await list_ontology_changes(conn, "t1"))[-1]
    assert change.actor == "bob"
    assert change.action == "delete"
    assert change.object_kind == "relation_type"
    assert change.object_id == "SOLD_BY"


# ---------------------------------------------------------------------------
# 约束（term_type_relation_allowlist）
# ---------------------------------------------------------------------------


async def _seed_constraint_prerequisites(conn: aiosqlite.Connection) -> None:
    await create_term_type(conn, "t1", value="产品", actor="alice")
    await create_term_type(conn, "t1", value="公司", actor="alice")
    await create_relation_type(
        conn, "t1", relation_type="SOLD_BY", example_phrase="产品 SOLD_BY 公司",
        actor="alice",
    )


async def test_add_allowed_combination_records_the_whole_triple(conn):
    await _seed_constraint_prerequisites(conn)
    await add_allowed_combination(
        conn, "t1", subject_term_type="产品", relation_type="SOLD_BY",
        object_term_type="公司", actor="bob",
    )

    change = (await list_ontology_changes(conn, "t1"))[-1]

    assert change.actor == "bob"
    assert change.action == "create"
    assert change.object_kind == "constraint"
    assert change.object_id == "产品 -SOLD_BY-> 公司"
    assert change.details["subject_term_type"] == "产品"
    assert change.details["relation_type"] == "SOLD_BY"
    assert change.details["object_term_type"] == "公司"


async def test_remove_allowed_combination_leaves_a_log_row_after_the_row_is_gone(conn):
    await _seed_constraint_prerequisites(conn)
    await add_allowed_combination(
        conn, "t1", subject_term_type="产品", relation_type="SOLD_BY",
        object_term_type="公司", actor="alice",
    )
    await remove_allowed_combination(
        conn, "t1", subject_term_type="产品", relation_type="SOLD_BY",
        object_term_type="公司", actor="bob",
    )

    assert await list_allowed_combinations(conn, "t1", status="draft") == []
    change = (await list_ontology_changes(conn, "t1"))[-1]
    assert change.actor == "bob"
    assert change.action == "delete"
    assert change.object_kind == "constraint"
    assert change.object_id == "产品 -SOLD_BY-> 公司"


# ---------------------------------------------------------------------------
# 批量：确认整份草稿 / 整份替换草稿
# ---------------------------------------------------------------------------


async def test_confirm_ontology_records_one_summary_row(conn):
    await _seed_constraint_prerequisites(conn)
    await add_allowed_combination(
        conn, "t1", subject_term_type="产品", relation_type="SOLD_BY",
        object_term_type="公司", actor="alice",
    )
    await confirm_ontology(conn, "t1", actor="bob")

    change = (await list_ontology_changes(conn, "t1"))[-1]

    assert change.actor == "bob"
    assert change.action == "confirm"
    assert change.object_kind == "ontology"
    assert change.details["term_types"] == 2
    assert change.details["relation_types"] == 1
    assert change.details["constraints"] == 1


async def test_confirm_ontology_without_any_draft_records_nothing(conn):
    """没有草稿时 confirm 直接早退、什么都没改，日志也不该多出一条——
    否则日志里全是"bob 确认了本体"，却没有任何一次真的改变了什么。"""
    await confirm_ontology(conn, "t1", actor="bob")

    assert await list_ontology_changes(conn, "t1") == []


async def test_replace_draft_records_one_summary_row_with_counts(conn):
    await replace_draft(
        conn, "t1",
        term_types=[{"value": "产品"}, {"value": "公司"}],
        relation_types=[
            {"relation_type": "SOLD_BY", "example_phrase": "产品 SOLD_BY 公司"}
        ],
        constraints=[
            {
                "subject_term_type": "产品",
                "relation_type": "SOLD_BY",
                "object_term_type": "公司",
            }
        ],
        actor="alice",
    )

    changes = await list_ontology_changes(conn, "t1")

    assert len(changes) == 1
    change = changes[0]
    assert change.actor == "alice"
    assert change.action == "replace"
    assert change.object_kind == "ontology_draft"
    assert change.details["term_types"] == 2
    assert change.details["relation_types"] == 1
    assert change.details["constraints"] == 1
    assert change.details["term_type_values"] == ["产品", "公司"]
    assert change.details["relation_type_names"] == ["SOLD_BY"]


# ---------------------------------------------------------------------------
# 操作者必填：漏传立刻 TypeError，不给默认值
# ---------------------------------------------------------------------------


async def test_every_ontology_write_refuses_to_run_without_an_actor(conn):
    """actor 是 keyword-only 且没有默认值。给默认值的话，某个调用点会悄悄
    把变更记在一个假身份上——那正是 _EDITED_BY = "admin" 当年的毛病，
    这里不重复一遍。"""
    with pytest.raises(TypeError):
        await create_term_type(conn, "t1", value="错误码")
    with pytest.raises(TypeError):
        await update_term_type(conn, "t1", value="a", new_value="b", extra_fields=[])
    with pytest.raises(TypeError):
        await delete_term_type(conn, "t1", "错误码")
    with pytest.raises(TypeError):
        await create_relation_type(conn, "t1", relation_type="R", example_phrase="x R y")
    with pytest.raises(TypeError):
        await update_relation_type(
            conn, "t1", relation_type="R", example_phrase="x R y",
            description="", allow_chain_query=False,
        )
    with pytest.raises(TypeError):
        await delete_relation_type(conn, "t1", "R")
    with pytest.raises(TypeError):
        await add_allowed_combination(
            conn, "t1", subject_term_type="a", relation_type="R", object_term_type="b"
        )
    with pytest.raises(TypeError):
        await remove_allowed_combination(
            conn, "t1", subject_term_type="a", relation_type="R", object_term_type="b"
        )
    with pytest.raises(TypeError):
        await confirm_ontology(conn, "t1")
    with pytest.raises(TypeError):
        await replace_draft(conn, "t1", term_types=[], relation_types=[], constraints=[])


# ---------------------------------------------------------------------------
# 审计写入和业务写入在同一个事务里
# ---------------------------------------------------------------------------


def _break_the_log(monkeypatch) -> None:
    """让日志写入失败，业务写入本身照常执行。"""

    async def _boom(*args, **kwargs):
        raise RuntimeError("日志表写不进去")

    monkeypatch.setattr(ontology_change_log, "record_ontology_change", _boom)


async def test_a_failed_log_write_takes_the_create_down_with_it(conn, monkeypatch):
    _break_the_log(monkeypatch)

    with pytest.raises(RuntimeError):
        await create_term_type(conn, "t1", value="错误码", actor="alice")

    assert await list_term_types(conn, "t1", status="draft") == []


async def test_a_failed_log_write_takes_the_delete_down_with_it(conn, monkeypatch):
    """业务改了但日志没记，比不记更糟——事后会以为那次改动没发生过。
    删除这一侧尤其要紧：分类行一旦真删掉，没有日志就再也没有第二个地方
    能说出它曾经存在过。"""
    await create_term_type(conn, "t1", value="错误码", actor="alice")
    _break_the_log(monkeypatch)

    with pytest.raises(RuntimeError):
        await delete_term_type(conn, "t1", "错误码", actor="bob")

    assert [t.value for t in await list_term_types(conn, "t1", status="draft")] == ["错误码"]


async def test_a_failed_log_write_takes_the_confirm_down_with_it(conn, monkeypatch):
    await create_term_type(conn, "t1", value="错误码", actor="alice")
    _break_the_log(monkeypatch)

    with pytest.raises(RuntimeError):
        await confirm_ontology(conn, "t1", actor="bob")

    assert await list_term_types(conn, "t1", status="confirmed") == []
    assert [t.value for t in await list_term_types(conn, "t1", status="draft")] == ["错误码"]


# ---------------------------------------------------------------------------
# 隔离与建表
# ---------------------------------------------------------------------------


async def test_changes_are_scoped_to_one_tenant(conn):
    await create_term_type(conn, "t1", value="错误码", actor="alice")
    await create_term_type(conn, "t2", value="工单", actor="bob")

    assert [c.object_id for c in await list_ontology_changes(conn, "t1")] == ["错误码"]
    assert [c.object_id for c in await list_ontology_changes(conn, "t2")] == ["工单"]


async def test_ensure_change_log_schema_is_idempotent_on_an_existing_database(conn):
    """存量库升级：表已经在、还带着数据，再建一次不能炸也不能清空。"""
    await create_term_type(conn, "t1", value="错误码", actor="alice")

    await ensure_change_log_schema(conn)
    await ensure_change_log_schema(conn)

    assert len(await list_ontology_changes(conn, "t1")) == 1


async def test_a_database_that_predates_the_change_log_gets_the_table_on_upgrade(conn):
    """存量库里根本没有这张表。ensure_ontology_schema 建出来，随后的写入
    不该撞上 no such table。"""
    await conn.execute("DROP TABLE ontology_change_log")
    await conn.commit()

    await ensure_ontology_schema(conn)
    await create_term_type(conn, "t1", value="错误码", actor="alice")

    assert [c.object_id for c in await list_ontology_changes(conn, "t1")] == ["错误码"]
