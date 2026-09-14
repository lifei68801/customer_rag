import aiosqlite
import pytest

from app.graphrag.alias_usage import ensure_alias_usage_schema, record_review_alias
from app.graphrag.attribute_conflicts import ensure_attribute_conflicts_schema, record_conflict
from app.graphrag.duplicate_review_queue import (
    enqueue_duplicate_suggestion,
    ensure_duplicate_review_schema,
)
from app.graphrag.ontology_change_log import ensure_change_log_schema
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.term_edits_store import FIELD_CREATED, FIELD_DELETED, ensure_term_edits_schema
from app.graphrag.term_purge import (
    GRAPH_BATCH_SIZE,
    PurgeCountChangedError,
    list_stored_term_types,
    plan_purge_term_type,
    purge_term_type,
)
from app.graphrag.terms_store import ensure_terms_schema, list_terms_merged

T = "t1"


class FakeGraph:
    def __init__(self, fail_on_batch: int | None = None) -> None:
        self.batches: list[list[str]] = []
        self._fail_on_batch = fail_on_batch

    async def delete_term_nodes(self, *, tenant_id: str, node_keys: list[str]) -> None:
        if self._fail_on_batch is not None and len(self.batches) == self._fail_on_batch:
            raise RuntimeError("图谱挂了")
        self.batches.append(list(node_keys))

    @property
    def deleted(self) -> list[str]:
        return [k for batch in self.batches for k in batch]


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_alias_usage_schema(conn)
    await ensure_attribute_conflicts_schema(conn)
    await ensure_duplicate_review_schema(conn)
    await ensure_change_log_schema(conn)
    return conn


async def _term(conn, node_key: str, term_type: str, tenant: str = T) -> None:
    await conn.execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, extra_properties, source) "
        "VALUES (?, ?, ?, '[]', ?, '{}', 'etl')",
        (tenant, node_key, node_key, term_type),
    )


async def _edit(conn, node_key: str, field: str, value: str, tenant: str = T) -> None:
    await conn.execute(
        "INSERT INTO term_edits (tenant_id, node_key, field, value, edited_at, edited_by) "
        "VALUES (?, ?, ?, ?, '2026-09-14T00:00:00', 'admin')",
        (tenant, node_key, field, value),
    )


async def _count(conn, sql: str, *params) -> int:
    cursor = await conn.execute(sql, params)
    return (await cursor.fetchone())[0]


async def test_stored_types_include_types_whose_every_entity_was_soft_deleted():
    """实体列表的类型摘要走合并视图，全删光的类型不出现——而那正是最该清空的。"""
    conn = await _conn()
    await _term(conn, "Order:1", "Order ID")
    await _term(conn, "Order:2", "Order ID")
    await _term(conn, "City:1", "Customer City")
    await _edit(conn, "Order:1", FIELD_DELETED, "true")
    await _edit(conn, "Order:2", FIELD_DELETED, "true")
    await conn.commit()

    stored = {s.term_type: (s.stored, s.visible) for s in await list_stored_term_types(conn, T)}

    assert stored == {"Order ID": (2, 0), "Customer City": (1, 1)}
    await conn.close()


async def test_purge_removes_rows_edits_satellites_and_graph_nodes_of_that_type_only():
    conn = await _conn()
    await _term(conn, "Order:1", "Order ID")
    await _term(conn, "Order:2", "Order ID")
    await _term(conn, "City:1", "Customer City")
    await _edit(conn, "Order:1", FIELD_DELETED, "true")
    await _edit(conn, "City:1", "standard_name", '"Houston"')
    await record_review_alias(conn, tenant_id=T, node_key="Order:2", alias="O-2", created_by="admin")
    await record_review_alias(conn, tenant_id=T, node_key="City:1", alias="HOU", created_by="admin")
    await record_conflict(
        conn, tenant_id=T, node_key="Order:2", field="Revenue",
        kept_value="1", kept_source="a", incoming_value="2", incoming_source="b",
    )
    await enqueue_duplicate_suggestion(
        conn, tenant_id=T, candidate_a_node_key="City:1", candidate_b_node_key="Order:1",
        similarity_score=0.9, reason="test",
    )
    await conn.commit()
    graph = FakeGraph()

    result = await purge_term_type(conn, graph, T, "Order ID", expected_node_count=2, actor="admin")

    assert sorted(graph.deleted) == ["Order:1", "Order:2"]
    assert result.node_count == 2
    assert await _count(conn, "SELECT COUNT(*) FROM terms WHERE term_type = 'Order ID'") == 0
    assert await _count(conn, "SELECT COUNT(*) FROM term_edits WHERE node_key LIKE 'Order:%'") == 0
    assert await _count(conn, "SELECT COUNT(*) FROM review_alias_usage WHERE node_key = 'Order:2'") == 0
    assert await _count(conn, "SELECT COUNT(*) FROM attribute_conflicts WHERE node_key = 'Order:2'") == 0
    # 一端是被清空的实体的疑似重复建议，另一端还在也要清：那条建议已经无从审起。
    assert await _count(conn, "SELECT COUNT(*) FROM duplicate_review_queue") == 0
    # 别的类型原封不动。
    assert await _count(conn, "SELECT COUNT(*) FROM terms WHERE node_key = 'City:1'") == 1
    assert await _count(conn, "SELECT COUNT(*) FROM term_edits WHERE node_key = 'City:1'") == 1
    assert await _count(conn, "SELECT COUNT(*) FROM review_alias_usage WHERE node_key = 'City:1'") == 1
    # 留痕：不可逆操作必须有人、有时间、有数量。
    assert await _count(
        conn, "SELECT COUNT(*) FROM ontology_change_log WHERE action = 'purge' AND object_id = 'Order ID'"
    ) == 1
    await conn.close()


async def test_reimport_after_purge_is_visible_again():
    """这正是清空要解决的事：软删除之后重导看不见，清空之后重导看得见。"""
    conn = await _conn()
    await _term(conn, "Order:1", "Order ID")
    await _edit(conn, "Order:1", FIELD_DELETED, "true")
    await conn.commit()
    assert await list_terms_merged(conn, T) == []

    await purge_term_type(conn, FakeGraph(), T, "Order ID", expected_node_count=1, actor="admin")
    # 模拟 ETL 重新写入同一个 node_key。
    await _term(conn, "Order:1", "Order ID")
    await conn.commit()

    assert [t.node_key for t in await list_terms_merged(conn, T)] == ["Order:1"]
    await conn.close()


async def test_edit_layer_created_entities_of_that_type_are_purged_too():
    """后台新建的实体只在编辑层，列表里挂在这个类型下。不带上它，清空之后还剩几条。"""
    conn = await _conn()
    await _term(conn, "Order:1", "Order ID")
    await _edit(conn, "Order:new", FIELD_CREATED,
                '{"standard_name": "新", "term_type": "Order ID", "aliases": [], "extra_properties": {}}')
    await _edit(conn, "City:new", FIELD_CREATED,
                '{"standard_name": "城", "term_type": "Customer City", "aliases": [], "extra_properties": {}}')
    await conn.commit()

    plan = await plan_purge_term_type(conn, T, "Order ID")
    assert sorted(plan.node_keys) == ["Order:1", "Order:new"]
    assert (plan.stored_rows, plan.created_only) == (1, 1)

    await purge_term_type(conn, FakeGraph(), T, "Order ID", expected_node_count=2, actor="admin")

    assert [t.node_key for t in await list_terms_merged(conn, T)] == ["City:new"]
    await conn.close()


async def test_refuses_when_count_changed_since_preview_and_touches_nothing():
    """预览之后有人导入了新数据：删的会比用户看到的多。不可逆的操作不能按新数字悄悄执行。"""
    conn = await _conn()
    await _term(conn, "Order:1", "Order ID")
    await _term(conn, "Order:2", "Order ID")
    await conn.commit()
    graph = FakeGraph()

    with pytest.raises(PurgeCountChangedError):
        await purge_term_type(conn, graph, T, "Order ID", expected_node_count=1, actor="admin")

    assert graph.batches == []
    assert await _count(conn, "SELECT COUNT(*) FROM terms") == 2
    await conn.close()


async def test_other_tenant_with_same_type_and_keys_is_untouched():
    conn = await _conn()
    await _term(conn, "Order:1", "Order ID")
    await _term(conn, "Order:1", "Order ID", tenant="other")
    await _edit(conn, "Order:1", FIELD_DELETED, "true", tenant="other")
    await conn.commit()

    await purge_term_type(conn, FakeGraph(), T, "Order ID", expected_node_count=1, actor="admin")

    assert await _count(conn, "SELECT COUNT(*) FROM terms WHERE tenant_id = 'other'") == 1
    assert await _count(conn, "SELECT COUNT(*) FROM term_edits WHERE tenant_id = 'other'") == 1
    await conn.close()


async def test_graph_failure_leaves_sqlite_intact_so_a_rerun_converges():
    """先删图谱、再删 SQLite：图谱删到一半失败时，候选集还在 SQLite 里，重跑能收尾。"""
    conn = await _conn()
    for i in range(GRAPH_BATCH_SIZE + 5):
        await _term(conn, f"Order:{i}", "Order ID")
    await conn.commit()

    with pytest.raises(RuntimeError):
        await purge_term_type(
            conn, FakeGraph(fail_on_batch=1), T, "Order ID",
            expected_node_count=GRAPH_BATCH_SIZE + 5, actor="admin",
        )
    assert await _count(conn, "SELECT COUNT(*) FROM terms") == GRAPH_BATCH_SIZE + 5

    graph = FakeGraph()
    await purge_term_type(conn, graph, T, "Order ID", expected_node_count=GRAPH_BATCH_SIZE + 5, actor="admin")
    assert len(graph.batches) == 2
    assert await _count(conn, "SELECT COUNT(*) FROM terms") == 0
    await conn.close()


async def test_purges_more_keys_than_the_sqlite_parameter_limit():
    """几万个实体的类型（demo 的 Order ID 就是一万个）不能撞 SQLite 的参数上限。"""
    conn = await _conn()
    n = 33_000
    await conn.executemany(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, extra_properties, source) "
        "VALUES (?, ?, ?, '[]', 'Order ID', '{}', 'etl')",
        [(T, f"Order:{i}", f"O{i}") for i in range(n)],
    )
    await conn.executemany(
        "INSERT INTO term_edits (tenant_id, node_key, field, value, edited_at, edited_by) "
        "VALUES (?, ?, ?, 'true', '2026-09-14T00:00:00', 'admin')",
        [(T, f"Order:{i}", FIELD_DELETED) for i in range(n)],
    )
    await conn.commit()

    result = await purge_term_type(conn, FakeGraph(), T, "Order ID", expected_node_count=n, actor="admin")

    assert result.removed_by_table["terms"] == n
    assert result.removed_by_table["term_edits"] == n
    await conn.close()
