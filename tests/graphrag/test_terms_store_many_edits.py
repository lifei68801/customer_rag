"""编辑层条数超过 SQLite 单条语句参数上限时，合并视图的读写不能崩。

真实事故：demo 租户在实体明细页批量删除了 39362 个实体（软删除，每个在
term_edits 里记一行 __deleted__）。之后实体列表、类型摘要、导航角标全部
500：`sqlite3.OperationalError: too many SQL variables`。这几个函数把"所有
被编辑过的 node_key"逐个展开成 `IN (?, ?, ...)` 的参数，而 SQLite 单条语句
最多 32766 个参数（3.32 之前是 999）。

删除本身是成功的——崩的是之后每一次读。这类故障不会在小数据上出现，所以
这里用真的超过上限的条数去撞，而不是 mock 一个"很大的数"。
"""
import aiosqlite

from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.term_edits_store import FIELD_CREATED, FIELD_DELETED, ensure_term_edits_schema
from app.graphrag.terms_store import (
    count_and_sample_terms_merged_by_term_type,
    count_terms_merged,
    count_terms_merged_by_term_type,
    delete_terms_by_node_keys,
    ensure_terms_schema,
)

#: 比 SQLite 3.32+ 的默认上限 32766 多出一截。上限是编译期参数，不同构建可能
#: 更小（老版本 999），但不会更大——撞得过这个数就撞得过所有常见构建。
OVER_LIMIT = 33_000
TENANT = "t1"


async def _connect_with_many_deleted(*, kept: int, deleted: int) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await ensure_ontology_schema(conn)
    rows = [
        (TENANT, f"Order:{i}", f"O{i}", "[]", "Order", "{}", "etl")
        for i in range(kept + deleted)
    ]
    await conn.executemany(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
        "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    # 直接批量写编辑层：逐条走 upsert_term_edit 每条都 commit，33000 条要跑很久，
    # 而这里要测的是读路径，不是写路径。
    await conn.executemany(
        "INSERT INTO term_edits (tenant_id, node_key, field, value, edited_at, edited_by) "
        "VALUES (?, ?, ?, ?, '2026-09-14T00:00:00', 'admin')",
        [(TENANT, f"Order:{i}", FIELD_DELETED, "true") for i in range(kept, kept + deleted)],
    )
    await conn.commit()
    return conn


async def test_count_terms_merged_survives_more_deletions_than_the_parameter_limit():
    conn = await _connect_with_many_deleted(kept=5, deleted=OVER_LIMIT)

    assert await count_terms_merged(conn, TENANT) == 5
    assert await count_terms_merged(conn, TENANT, source="etl") == 5
    await conn.close()


async def test_count_by_term_type_survives_more_deletions_than_the_parameter_limit():
    conn = await _connect_with_many_deleted(kept=5, deleted=OVER_LIMIT)

    assert await count_terms_merged_by_term_type(conn, TENANT) == {"Order": 5}
    await conn.close()


async def test_count_and_sample_survives_more_deletions_than_the_parameter_limit():
    conn = await _connect_with_many_deleted(kept=3, deleted=OVER_LIMIT)

    total, sample = await count_and_sample_terms_merged_by_term_type(
        conn, TENANT, "Order", sample_limit=10
    )

    assert total == 3
    assert sorted(t.node_key for t in sample) == ["Order:0", "Order:1", "Order:2"]
    await conn.close()


async def test_created_entities_still_counted_alongside_a_huge_deleted_set():
    """子查询只取 __deleted__/__created__ 那两类编辑时，纯编辑层创建的实体仍要加上。"""
    conn = await _connect_with_many_deleted(kept=2, deleted=OVER_LIMIT)
    await conn.execute(
        "INSERT INTO term_edits (tenant_id, node_key, field, value, edited_at, edited_by) "
        "VALUES (?, 'Order:new', ?, ?, '2026-09-14T00:00:00', 'admin')",
        (TENANT, FIELD_CREATED, '{"standard_name": "新", "term_type": "Order", "aliases": [], "extra_properties": {}}'),
    )
    await conn.commit()

    assert await count_terms_merged(conn, TENANT) == 3
    assert await count_terms_merged(conn, TENANT, source="review") == 1
    assert await count_terms_merged_by_term_type(conn, TENANT) == {"Order": 3}
    await conn.close()


async def test_edits_of_another_tenant_do_not_leak_into_the_count():
    """别的租户删掉了同名 node_key，这个租户的计数不受影响。

    注意这条**不**钉子查询里的 tenant_id 过滤：三个函数做增减判断用的都是
    按租户读出来的编辑字典，子查询只决定"去 terms 捞哪些行"。去掉那个过滤
    只会多捞几行，结果不变（变异测试验证过）——那个过滤是为了不在大租户上
    白扫别人的编辑，是性能上的，不是正确性上的。
    """
    conn = await _connect_with_many_deleted(kept=0, deleted=0)
    await conn.executemany(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, extra_properties, source) "
        "VALUES (?, 'Order:1', 'O1', '[]', 'Order', '{}', 'etl')",
        [(TENANT,), ("other",)],
    )
    await conn.execute(
        "INSERT INTO term_edits (tenant_id, node_key, field, value, edited_at, edited_by) "
        "VALUES ('other', 'Order:1', ?, 'true', '2026-09-14T00:00:00', 'admin')",
        (FIELD_DELETED,),
    )
    await conn.commit()

    assert await count_terms_merged(conn, TENANT) == 1
    assert await count_terms_merged_by_term_type(conn, TENANT) == {"Order": 1}
    total, _ = await count_and_sample_terms_merged_by_term_type(conn, TENANT, "Order", sample_limit=5)
    assert total == 1
    await conn.close()


async def test_delete_terms_by_node_keys_accepts_more_keys_than_the_parameter_limit():
    conn = await _connect_with_many_deleted(kept=OVER_LIMIT + 10, deleted=0)
    keys = {f"Order:{i}" for i in range(OVER_LIMIT)}

    removed = await delete_terms_by_node_keys(conn, TENANT, keys)

    assert removed == OVER_LIMIT
    cursor = await conn.execute("SELECT COUNT(*) FROM terms WHERE tenant_id = ?", (TENANT,))
    assert (await cursor.fetchone())[0] == 10
    await conn.close()


# ── 类型摘要要跟 apply_edits 的合并结果逐条对得上 ─────────────────────────
#
# 下面几条跟参数上限无关，是修上面那个问题时撞出来的：后台手工新建的实体
# 只写一条 __created__（类型在它的整对象里），而 count_terms_merged_by_term_type
# 先取裸 term_type 字段、取不到就跳过，读 __created__ 的分支永远走不到——
# 摘要里的数字比点进去看到的少。对照物用 list_terms_merged 本身：摘要必须
# 等于"合并视图按类型数一遍"。


async def _edit(conn, node_key: str, field: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO term_edits (tenant_id, node_key, field, value, edited_at, edited_by) "
        "VALUES (?, ?, ?, ?, '2026-09-14T00:00:00', 'admin')",
        (TENANT, node_key, field, value),
    )


async def _by_type_from_merged_view(conn) -> dict[str, int]:
    from app.graphrag.terms_store import list_terms_merged

    counts: dict[str, int] = {}
    for term in await list_terms_merged(conn, TENANT):
        counts[term.term_type] = counts.get(term.term_type, 0) + 1
    return counts


async def test_by_type_summary_matches_merged_view_for_every_edit_shape():
    conn = await _connect_with_many_deleted(kept=0, deleted=0)
    await conn.executemany(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, extra_properties, source) "
        "VALUES (?, ?, ?, '[]', ?, '{}', 'etl')",
        [
            (TENANT, "k:plain", "plain", "Order"),
            (TENANT, "k:retyped", "retyped", "Order"),
            (TENANT, "k:taken_over", "taken_over", "Order"),
        ],
    )
    # 后台新建：只有 __created__。
    await _edit(conn, "k:created", FIELD_CREATED,
                '{"standard_name": "c", "term_type": "Product", "aliases": [], "extra_properties": {}}')
    # 后台新建之后又改了类型：裸字段盖在 __created__ 上。
    await _edit(conn, "k:created_then_retyped", FIELD_CREATED,
                '{"standard_name": "cr", "term_type": "Product", "aliases": [], "extra_properties": {}}')
    await _edit(conn, "k:created_then_retyped", "term_type", '"Company"')
    # 管道行上改类型。
    await _edit(conn, "k:retyped", "term_type", '"Company"')
    # 管道后来产出了同 node_key：__created__ 的类型降级成字段编辑，盖在管道值上。
    await _edit(conn, "k:taken_over", FIELD_CREATED,
                '{"standard_name": "t", "term_type": "Product", "aliases": [], "extra_properties": {}}')
    # 孤儿编辑：terms 无行、也没有 __created__——合并视图里不存在。
    await _edit(conn, "k:orphan", "term_type", '"Order"')
    await conn.commit()

    expected = await _by_type_from_merged_view(conn)
    assert expected == {"Order": 1, "Product": 2, "Company": 2}, expected
    assert await count_terms_merged_by_term_type(conn, TENANT) == expected
    await conn.close()
