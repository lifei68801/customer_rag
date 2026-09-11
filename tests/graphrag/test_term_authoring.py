"""人工新建 Term 的规则测试。

这些规则此前只能通过完整的 HTTP → SQLite → 图客户端场景来验，因为编排整个
住在路由函数里——而它们跟 HTTP 没有任何关系。搬进 `term_authoring` 之后，
测试面就是这个模块的接口本身。

`tests/api/test_admin_terms_routes.py` 仍然覆盖端点的 HTTP 行为（状态码、
响应体形状）。这里覆盖的是规则。
"""

import asyncio

import aiosqlite
import pytest

from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import create_term_type, delete_term_type
from app.graphrag.ontology_lifecycle import (
    checkout_draft,
    confirm_ontology,
    ensure_ontology_schema,
)
from app.graphrag.term_authoring import (
    TermCreateRejected,
    build_node_key,
    create_term_from_admin,
)
from app.graphrag.term_edits_store import (
    FIELD_CREATED,
    FIELD_DELETED,
    ensure_term_edits_schema,
    list_term_edits_for_node_key,
    upsert_term_edit,
)
from app.graphrag.terms_store import ensure_terms_schema


class FakeGraph:
    """记下同步过哪些 Term。"""

    def __init__(self, fail: bool = False) -> None:
        self.synced: list[Term] = []
        self.fail = fail

    async def sync_term(self, term: Term) -> None:
        if self.fail:
            raise RuntimeError("图谱连不上")
        self.synced.append(term)


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await ensure_ontology_schema(conn)
    await create_term_type(conn, tenant_id="t1", value="产品", actor="alice")
    await create_term_type(conn, tenant_id="t1", value="客户", actor="alice")
    await confirm_ontology(conn, "t1", actor="alice")
    return conn


@pytest.fixture()
def conn():
    c = asyncio.run(_connect())
    try:
        yield c
    finally:
        asyncio.run(c.close())


def _create(conn, graph, **overrides):
    kwargs = {
        "tenant_id": "t1",
        "standard_name": "拿铁",
        "term_type": "产品",
        "aliases": [],
        "extra_properties": None,
        "source": "manual",
        "actor": "alice",
    }
    kwargs.update(overrides)
    return asyncio.run(create_term_from_admin(conn, graph, **kwargs))


def test_creates_an_edit_not_a_terms_row(conn):
    """写的是编辑层，不是 terms 表。

    terms 表在 ETL 产出同 node_key 的行之前永远没有这一行——合并视图把它
    合成出来。这条钉住的是"人工创建走编辑层"这个设计本身。
    """
    graph = FakeGraph()
    created = _create(conn, graph)

    assert created.node_key == "产品:拿铁"
    edits = asyncio.run(
        list_term_edits_for_node_key(conn, tenant_id="t1", node_key="产品:拿铁")
    )
    assert FIELD_CREATED in edits

    async def _count_rows():
        cursor = await conn.execute("SELECT COUNT(*) FROM terms")
        return (await cursor.fetchone())[0]

    assert asyncio.run(_count_rows()) == 0


def test_the_merged_term_is_projected_into_the_graph(conn):
    """图谱拿到的是**合并视图**的结果，不是请求里那份。"""
    graph = FakeGraph()
    _create(conn, graph)

    assert len(graph.synced) == 1
    assert graph.synced[0].node_key == "产品:拿铁"


def test_the_returned_term_keeps_the_requested_source(conn):
    """返回给调用方的那份用请求里的 source，不是合并视图里固定的 "review"。

    调用方问的是"我刚提交的这条长什么样"。
    """
    created = _create(conn, FakeGraph(), source="manual")

    assert created.term.source == "manual"
    # 而进图谱的那份走合并语义，source 是 review。
    assert created.term.source != "review"


def test_an_unknown_term_type_is_rejected(conn):
    with pytest.raises(TermCreateRejected) as exc:
        _create(conn, FakeGraph(), term_type="不存在的类型")

    assert "不存在的类型" in str(exc.value)


def test_an_undeclared_extra_property_is_rejected(conn):
    with pytest.raises(TermCreateRejected):
        _create(conn, FakeGraph(), extra_properties={"没声明过的字段": "x"})


def test_reviving_a_row_whose_own_category_is_gone_is_refused(conn):
    """「实体被删 → 分类被删 → 实体被重建」这条链要被拦住。

    分类删除的守卫走合并视图，被人工删空的类型可以被删掉——不拦的话，一行
    term_type 指向不存在分类的实体会重新可见，悬空。

    关键在于这道守卫校验的是 **terms 行自己的 term_type**，不是这次提交的
    那个。所以场景必须是两者不一致：行自己的类型（客户）已被删除，而提交
    用的类型（产品）仍然有效——否则 validate_term_categories 会先一步拒掉，
    这条测试就会因为别的原因变绿，守卫拿掉也不会红。

    node_key 的类型前缀只反映创建时的类型，跟行自己的 term_type 可以不一致
    （见 terms_store.update_term），所以这一行是能真实存在的。
    """
    asyncio.run(
        conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t1", "产品:张三", "张三", "[]", "客户", "{}", "etl"),
        )
    )
    asyncio.run(conn.commit())
    asyncio.run(
        upsert_term_edit(
            conn,
            tenant_id="t1",
            node_key="产品:张三",
            field=FIELD_DELETED,
            value=True,
            edited_by="alice",
        )
    )
    # 把「客户」这个分类删掉。「产品」还在——这次提交用的就是它。
    asyncio.run(checkout_draft(conn, "t1"))
    asyncio.run(delete_term_type(conn, tenant_id="t1", value="客户", actor="alice"))
    asyncio.run(confirm_ontology(conn, "t1", actor="alice"))

    with pytest.raises(TermCreateRejected) as exc:
        _create(conn, FakeGraph(), standard_name="张三", term_type="产品")

    message = str(exc.value)
    # 要点名是哪一行、哪个类型、以及怎么办，否则管理员只知道点不动。
    assert "产品:张三" in message
    assert "客户" in message
    assert "分类" in message


def test_a_live_row_with_the_same_node_key_is_not_blocked_by_that_guard(conn):
    """没有被删过的同 node_key 行照常写得进去。

    那道守卫只在"真的要复活一行被删的"时才检查——对一直可见的行拦一道，
    挡掉的是一次合法的编辑。没有这一条的话，"永远拒绝"也能让上面那条通过。
    """
    asyncio.run(
        conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t1", "产品:拿铁", "拿铁", "[]", "产品", "{}", "etl"),
        )
    )
    asyncio.run(conn.commit())

    created = _create(conn, FakeGraph())

    assert created.node_key == "产品:拿铁"


def test_reviving_a_deleted_term_clears_the_deletion(conn):
    """重建一个被人工删除的 node_key，要撤掉那条 __deleted__。

    不撤的话读合并视图会抛 TermNotFoundError，一次合法的重建变成 500。
    """
    asyncio.run(
        upsert_term_edit(
            conn,
            tenant_id="t1",
            node_key="产品:拿铁",
            field=FIELD_DELETED,
            value=True,
            edited_by="alice",
        )
    )

    _create(conn, FakeGraph())

    edits = asyncio.run(
        list_term_edits_for_node_key(conn, tenant_id="t1", node_key="产品:拿铁")
    )
    assert FIELD_DELETED not in edits


def test_similar_existing_terms_come_back_as_a_hint_not_a_rejection(conn):
    """同类型下名字相近的现有术语要报出来，但不拦。

    standard_name 早已不是身份键，重名不是错误——它只是一件值得管理员看一眼
    的事。
    """
    _create(conn, FakeGraph(), standard_name="拿铁")
    created = _create(conn, FakeGraph(), standard_name="拿铁咖啡")

    names = {term.standard_name for term, _ in created.similar_terms}
    assert "拿铁" in names


def test_similar_terms_do_not_cross_term_types(conn):
    """不同 term_type 之间凑巧撞名字不算相近——那只是噪声。"""
    _create(conn, FakeGraph(), standard_name="拿铁", term_type="产品")
    created = _create(conn, FakeGraph(), standard_name="拿铁", term_type="客户")

    assert created.similar_terms == []


def test_a_graph_sync_failure_propagates_instead_of_being_swallowed(conn):
    """图谱同步失败要抛出去，不能吞掉报成功。

    这一步之前 SQLite 已经写了，抛出去意味着调用方看到失败而编辑层其实已经
    生效——两侧不一致。这是有意选的那一侧：吞掉的话不一致就**无人知晓**。
    """
    with pytest.raises(RuntimeError, match="图谱连不上"):
        _create(conn, FakeGraph(fail=True))

    # 编辑确实已经写进去了——这正是"不一致"的那一半，钉住它是为了让下一个
    # 改这里的人知道现状是什么，而不是以为失败会回滚。
    edits = asyncio.run(
        list_term_edits_for_node_key(conn, tenant_id="t1", node_key="产品:拿铁")
    )
    assert FIELD_CREATED in edits


def test_node_key_is_built_from_type_and_name():
    """人工创建路径没有外部稳定码，node_key 取创建时的展示名（ADR-0003）。"""
    assert build_node_key("产品", "拿铁") == "产品:拿铁"
