from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_categories import (
    CategoryInUseError,
    CategoryNameConflictError,
    CategoryNotFoundError,
    ExtraFieldSpec,
    InvalidExtraFieldTypeError,
    TermTypeCategory,
    create_term_type,
    delete_term_type,
    ensure_categories_schema,
    list_term_types,
    update_term_type,
)
from app.graphrag.ontology_lifecycle import ensure_ontology_schema

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    # 生产环境里分类表从不单独建——deps.py::get_review_conn 总是紧接着调用
    # ensure_ontology_schema，把 term_type_relation_allowlist 一起建出来（见该
    # 函数的说明）。update_term_type/delete_term_type 的 allowlist 级联依赖这张
    # 表存在，测试这里也建整套本体 schema 而不是只建分类表，跟生产环境的调用
    # 顺序保持一致。
    await ensure_ontology_schema(conn)
    return conn


# 注意：这个文件里凡是需要一张 terms 表来模拟"真实术语引用"的测试，一律用
# 手写 CREATE TABLE + 原生 SQL INSERT，不调用 terms_store.py::create_term/
# ensure_terms_schema。terms_store.py 里 _validate_categories/
# _bridge_seed_categories_from_existing_terms 目前调用 list_term_types(conn,
# tenant_id) 时还没有传 status（这是本次改造范围之外的调用点，属于同一个
# plan 后续任务负责更新，见 docs/superpowers/specs/
# 2026-08-19-term-type-draft-lifecycle-design.md 的调用点表格），在那些调用点
# 更新之前直接调用 create_term/ensure_terms_schema 会因为 status 变成必填
# 参数而抛 TypeError。手写 SQL 完全绕开这条路径，不依赖那些尚未更新的调用点。
_TERMS_TABLE_SQL = (
    "CREATE TABLE terms (tenant_id TEXT NOT NULL, standard_name TEXT NOT NULL, "
    "term_type TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', "
    "node_key TEXT NOT NULL, PRIMARY KEY (tenant_id, standard_name))"
)


async def test_create_and_list_term_type_with_extra_fields():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="default", value="错误码",
        extra_fields=[
            ExtraFieldSpec(name="severity_level", value_type="string"),
            ExtraFieldSpec(name="impact_scope", value_type="string"),
        ],
    )

    result = await list_term_types(conn, tenant_id="default", status="draft")

    assert result == [TermTypeCategory(
        value="错误码",
        extra_fields=[
            ExtraFieldSpec(name="severity_level", value_type="string"),
            ExtraFieldSpec(name="impact_scope", value_type="string"),
        ],
    )]


async def test_create_term_type_without_extra_fields_defaults_to_empty_list():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="地点")

    result = await list_term_types(conn, tenant_id="default", status="draft")

    assert result == [TermTypeCategory(value="地点", extra_fields=[])]


async def test_create_term_type_is_draft_only_not_visible_in_confirmed():
    """create_term_type 现在固定写 status='draft'——新建的分类要能在草稿
    列表里看到，但在已确认列表里看不到，直到走确认流程（本任务不实现确认
    流程本身，只验证 create 这一步的落点符合预期）。对照
    test_ontology_relations.py::test_create_relation_type_with_valid_name
    的模式。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")

    draft_result = await list_term_types(conn, tenant_id="default", status="draft")
    confirmed_result = await list_term_types(conn, tenant_id="default", status="confirmed")

    assert [t.value for t in draft_result] == ["错误码"]
    assert confirmed_result == []


async def test_create_duplicate_term_type_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")

    with pytest.raises(CategoryNameConflictError):
        await create_term_type(conn, tenant_id="default", value="错误码")


async def test_update_term_type_renames_draft_category():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="default", value="错误码",
        extra_fields=[ExtraFieldSpec(name="severity_level", value_type="string")],
    )

    await update_term_type(
        conn, tenant_id="default", value="错误码", new_value="故障码",
        extra_fields=[
            ExtraFieldSpec(name="severity_level", value_type="string"),
            ExtraFieldSpec(name="impact_scope", value_type="string"),
        ],
    )

    result = await list_term_types(conn, tenant_id="default", status="draft")
    assert result == [TermTypeCategory(
        value="故障码",
        extra_fields=[
            ExtraFieldSpec(name="severity_level", value_type="string"),
            ExtraFieldSpec(name="impact_scope", value_type="string"),
        ],
    )]


async def test_update_term_type_rename_does_not_cascade_to_terms():
    """原名 test_update_term_type_cascades_rename_to_referencing_terms，断言
    反过来：改名只改草稿定义，不再级联更新 terms 表——真实术语只引用已确认
    类型，草稿改名不影响它们（决策 4，见 spec 文档）。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await conn.execute(_TERMS_TABLE_SQL)
    await conn.execute(
        "INSERT INTO terms (tenant_id, standard_name, term_type, node_key) VALUES (?, ?, ?, ?)",
        ("default", "错误码E502", "错误码", "错误码E502"),
    )
    await conn.commit()

    await update_term_type(conn, tenant_id="default", value="错误码", new_value="故障码", extra_fields=[])

    cursor = await conn.execute(
        "SELECT term_type FROM terms WHERE tenant_id = ? AND standard_name = ?", ("default", "错误码E502",)
    )
    row = await cursor.fetchone()
    assert row[0] == "错误码"  # 不级联——terms 表里的值保持不变


async def test_update_term_type_into_existing_name_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await create_term_type(conn, tenant_id="default", value="模块")

    with pytest.raises(CategoryNameConflictError):
        await update_term_type(conn, tenant_id="default", value="错误码", new_value="模块", extra_fields=[])


async def test_update_term_type_on_confirmed_only_row_raises_not_found():
    """update_term_type 只操作 status='draft' 的行——一个分类值只有已确认
    版本、没有草稿版本时（比如确认后没有重新 checkout），必须报
    CategoryNotFoundError，不能误改已确认的行。"""
    conn = await _conn()
    await conn.execute(
        "INSERT INTO ontology_term_types (tenant_id, value, extra_fields, status) "
        "VALUES ('default', '错误码', '[]', 'confirmed')"
    )
    await conn.commit()

    with pytest.raises(CategoryNotFoundError):
        await update_term_type(conn, tenant_id="default", value="错误码", new_value="故障码", extra_fields=[])


async def test_delete_term_type_not_in_use_succeeds():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await conn.execute(_TERMS_TABLE_SQL)
    await conn.commit()

    await delete_term_type(conn, tenant_id="default", value="错误码")

    assert await list_term_types(conn, tenant_id="default", status="draft") == []


async def test_delete_term_type_in_use_raises_conflict():
    """删除保护测试：草稿中的实体类型删除时，如果 terms 表里有真实术语引用
    它（模拟"已确认版本在用"的场景），删除必须被 CategoryInUseError 拦住。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await conn.execute(_TERMS_TABLE_SQL)
    await conn.execute(
        "INSERT INTO terms (tenant_id, standard_name, term_type, node_key) VALUES (?, ?, ?, ?)",
        ("default", "错误码E502", "错误码", "错误码E502"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, tenant_id="default", value="错误码")


async def test_delete_term_type_in_use_by_confirmed_counterpart_raises_conflict():
    """同一个 value 同时存在 draft 行和 confirmed 行时（比如已经确认过、又
    重新 checkout 出一份草稿在编辑），terms 表引用的是已确认版本，删除草稿行
    仍然要被这份引用拦住——terms 表使用量检查不区分 ontology_term_types 自己
    的 status。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await conn.execute(
        "INSERT INTO ontology_term_types (tenant_id, value, extra_fields, status) "
        "VALUES ('default', '错误码', '[]', 'confirmed')"
    )
    await conn.execute(_TERMS_TABLE_SQL)
    await conn.execute(
        "INSERT INTO terms (tenant_id, standard_name, term_type, node_key) VALUES (?, ?, ?, ?)",
        ("default", "错误码E502", "错误码", "错误码E502"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, tenant_id="default", value="错误码")


async def test_delete_term_type_only_deletes_draft_row_leaves_confirmed_row():
    """delete_term_type 只删 status='draft' 的行，不影响同名的
    status='confirmed' 行。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await conn.execute(
        "INSERT INTO ontology_term_types (tenant_id, value, extra_fields, status) "
        "VALUES ('default', '错误码', '[]', 'confirmed')"
    )
    await conn.execute(_TERMS_TABLE_SQL)
    await conn.commit()

    await delete_term_type(conn, tenant_id="default", value="错误码")

    assert await list_term_types(conn, tenant_id="default", status="draft") == []
    confirmed = await list_term_types(conn, tenant_id="default", status="confirmed")
    assert [t.value for t in confirmed] == ["错误码"]


async def test_update_term_type_cascades_rename_to_draft_allowlist_references():
    """改名级联到 term_type_relation_allowlist 的 draft 行——新语义下这是
    唯一还保留的级联范围（不再级联 terms 表，见
    test_update_term_type_rename_does_not_cascade_to_terms）。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="客房")
    await create_term_type(conn, tenant_id="t1", value="酒店")
    await conn.executescript(_TERMS_TABLE_SQL + ";")
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "客房", "PART_OF", "酒店", "draft"),
    )
    await conn.commit()

    await update_term_type(conn, tenant_id="t1", value="客房", new_value="大床房", extra_fields=[])

    cursor = await conn.execute(
        "SELECT subject_term_type FROM term_type_relation_allowlist WHERE tenant_id = 't1'"
    )
    row = await cursor.fetchone()
    assert row[0] == "大床房"


async def test_update_term_type_rename_does_not_cascade_to_confirmed_allowlist_references():
    """改名级联范围收窄到只更新 draft 行——confirmed 的约束条目不受草稿改名
    影响。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="客房")
    await create_term_type(conn, tenant_id="t1", value="酒店")
    await conn.executescript(_TERMS_TABLE_SQL + ";")
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "客房", "PART_OF", "酒店", "confirmed"),
    )
    await conn.commit()

    await update_term_type(conn, tenant_id="t1", value="客房", new_value="大床房", extra_fields=[])

    cursor = await conn.execute(
        "SELECT subject_term_type FROM term_type_relation_allowlist WHERE tenant_id = 't1'"
    )
    row = await cursor.fetchone()
    assert row[0] == "客房"  # confirmed 行不受影响


async def test_delete_term_type_referenced_only_by_draft_allowlist_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="客房")
    await create_term_type(conn, tenant_id="t1", value="酒店")
    await conn.executescript(_TERMS_TABLE_SQL + ";")
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "客房", "PART_OF", "酒店", "draft"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, tenant_id="t1", value="客房")


async def test_delete_term_type_referenced_only_by_confirmed_allowlist_succeeds():
    """delete_term_type 的 allowlist 引用检查加了 status='draft'——只拦草稿
    自洽性相关的引用，跟本次删除无关的 confirmed 约束不挡删除。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="客房")
    await create_term_type(conn, tenant_id="t1", value="酒店")
    await conn.executescript(_TERMS_TABLE_SQL + ";")
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "客房", "PART_OF", "酒店", "confirmed"),
    )
    await conn.commit()

    await delete_term_type(conn, tenant_id="t1", value="客房")

    remaining = {t.value for t in await list_term_types(conn, tenant_id="t1", status="draft")}
    assert "客房" not in remaining


async def test_ensure_categories_schema_migrates_legacy_term_types_table():
    """模拟 2026-08-15 之前的 ontology_term_types 表（value 主键，没有
    tenant_id 列），验证迁移把存量数据归到 tenant_id='default'，并且（这一步
    紧跟在 tenant_id 迁移之后）落 status='confirmed'。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        "CREATE TABLE ontology_term_types (value TEXT PRIMARY KEY, "
        "extra_fields TEXT NOT NULL DEFAULT '[]');"
    )
    await conn.execute(
        "INSERT INTO ontology_term_types (value, extra_fields) VALUES ('error_code', '[]')"
    )
    await conn.commit()

    from app.graphrag.ontology_categories import ensure_categories_schema
    await ensure_categories_schema(conn)

    types = await list_term_types(conn, tenant_id="default", status="confirmed")
    assert len(types) == 1
    assert types[0].value == "error_code"
    assert await list_term_types(conn, tenant_id="default", status="draft") == []


async def test_ensure_categories_schema_migrates_legacy_table_without_status_column():
    """专门覆盖 _migrate_term_types_add_status_if_needed：手工建一张已经有
    tenant_id 列、但没有 status 列的旧表（2026-08-15~2026-08-19 之间的形态），
    调用 ensure_categories_schema，断言迁移后所有行 status='confirmed'。
    对照 test_ensure_categories_schema_migrates_legacy_term_types_table 的
    手法。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        "CREATE TABLE ontology_term_types (tenant_id TEXT NOT NULL, value TEXT NOT NULL, "
        "extra_fields TEXT NOT NULL DEFAULT '[]', node_key_template TEXT NOT NULL DEFAULT '', "
        "PRIMARY KEY (tenant_id, value));"
    )
    await conn.execute(
        "INSERT INTO ontology_term_types (tenant_id, value, extra_fields) VALUES (?, ?, ?)",
        ("default", "错误码", "[]"),
    )
    await conn.execute(
        "INSERT INTO ontology_term_types (tenant_id, value, extra_fields) VALUES (?, ?, ?)",
        ("t1", "客房", "[]"),
    )
    await conn.commit()

    await ensure_categories_schema(conn)

    cursor = await conn.execute("SELECT tenant_id, value, status FROM ontology_term_types ORDER BY tenant_id")
    rows = await cursor.fetchall()
    assert rows == [("default", "错误码", "confirmed"), ("t1", "客房", "confirmed")]
    confirmed = await list_term_types(conn, tenant_id="default", status="confirmed")
    assert [t.value for t in confirmed] == ["错误码"]
    assert await list_term_types(conn, tenant_id="default", status="draft") == []


async def test_create_and_list_term_types_isolated_per_tenant():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="tenant_a", value="错误码",
        extra_fields=[ExtraFieldSpec(name="severity_level", value_type="string")],
    )
    await create_term_type(conn, tenant_id="tenant_b", value="VariantValue", extra_fields=[])

    types_a = await list_term_types(conn, tenant_id="tenant_a", status="draft")
    types_b = await list_term_types(conn, tenant_id="tenant_b", status="draft")
    assert [t.value for t in types_a] == ["错误码"]
    assert [t.value for t in types_b] == ["VariantValue"]


async def test_update_term_type_allowlist_cascade_scoped_to_same_tenant_only():
    """改名级联到 term_type_relation_allowlist，必须只影响同一租户的
    draft 行——term_type 按租户隔离后，不该波及其它租户。原测试名
    test_update_term_type_rename_cascades_within_same_tenant_only 用
    terms_store.py::create_term 验证跨表级联，但新语义下 update_term_type
    不再级联 terms 表，这里改成验证 allowlist 级联的租户隔离，terms 表
    改成手写 SQL 直接插入（绕开 create_term 内部对 list_term_types 的调用
    ——那个调用点的 status 参数补齐属于本 plan 后续任务范围）。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="tenant_a", value="客房", extra_fields=[])
    await create_term_type(conn, tenant_id="tenant_a", value="酒店", extra_fields=[])
    await create_term_type(conn, tenant_id="tenant_b", value="客房", extra_fields=[])
    await create_term_type(conn, tenant_id="tenant_b", value="酒店", extra_fields=[])
    await conn.execute(_TERMS_TABLE_SQL)
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("tenant_a", "客房", "PART_OF", "酒店", "draft"),
    )
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("tenant_b", "客房", "PART_OF", "酒店", "draft"),
    )
    await conn.execute(
        "INSERT INTO terms (tenant_id, standard_name, term_type, node_key) VALUES (?, ?, ?, ?)",
        ("tenant_a", "A栋客房", "客房", "A栋客房"),
    )
    await conn.execute(
        "INSERT INTO terms (tenant_id, standard_name, term_type, node_key) VALUES (?, ?, ?, ?)",
        ("tenant_b", "B栋客房", "客房", "B栋客房"),
    )
    await conn.commit()

    await update_term_type(
        conn, tenant_id="tenant_a", value="客房", new_value="客房间",
        extra_fields=[],
    )

    cursor = await conn.execute(
        "SELECT subject_term_type FROM term_type_relation_allowlist WHERE tenant_id = 'tenant_a'"
    )
    assert (await cursor.fetchone())[0] == "客房间"
    cursor = await conn.execute(
        "SELECT subject_term_type FROM term_type_relation_allowlist WHERE tenant_id = 'tenant_b'"
    )
    assert (await cursor.fetchone())[0] == "客房"  # tenant_b 不受 tenant_a 改名影响

    cursor = await conn.execute(
        "SELECT term_type FROM terms WHERE tenant_id = ? AND standard_name = ?", ("tenant_a", "A栋客房")
    )
    assert (await cursor.fetchone())[0] == "客房"  # terms 表不再被级联，两租户都保持原值


async def test_delete_term_type_in_use_by_constraint_returns_error():
    """原来用 ontology_constraints.py::add_allowed_combination 写入约束，
    但那个函数内部的 _validate_references 目前还在用
    list_term_types(conn, tenant_id)（没有 status 参数，属于本 plan 后续
    任务负责更新的调用点），直接调用会因为 status 变成必填参数抛
    TypeError。改成手写 SQL 直接插入 draft 约束行，绕开这条尚未更新的
    调用路径，断言逻辑不变：草稿中的实体类型被草稿约束引用时，删除被
    CategoryInUseError 拦住。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="客房", extra_fields=[])
    await create_term_type(conn, tenant_id="t1", value="酒店", extra_fields=[])
    await conn.execute(_TERMS_TABLE_SQL)
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "客房", "PART_OF", "酒店", "draft"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, tenant_id="t1", value="客房")


async def test_create_term_type_with_typed_extra_fields():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="t1", value="错误码",
        extra_fields=[
            ExtraFieldSpec(name="severity_level", value_type="string"),
            ExtraFieldSpec(name="impact_count", value_type="integer"),
        ],
    )

    types = await list_term_types(conn, tenant_id="t1", status="draft")
    assert types[0].extra_fields == [
        ExtraFieldSpec(name="severity_level", value_type="string"),
        ExtraFieldSpec(name="impact_count", value_type="integer"),
    ]


async def test_create_term_type_rejects_invalid_value_type():
    conn = await _conn()
    with pytest.raises(InvalidExtraFieldTypeError):
        await create_term_type(
            conn, tenant_id="t1", value="错误码",
            extra_fields=[ExtraFieldSpec(name="severity_level", value_type="不存在的类型")],
        )


async def test_update_term_type_with_typed_extra_fields():
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="VariantValue", extra_fields=[])

    await update_term_type(
        conn, tenant_id="t1", value="VariantValue", new_value="VariantValue",
        extra_fields=[
            ExtraFieldSpec(name="numeric_value", value_type="number"),
            ExtraFieldSpec(name="dims", value_type="number[]"),
        ],
    )

    types = await list_term_types(conn, tenant_id="t1", status="draft")
    assert types[0].extra_fields == [
        ExtraFieldSpec(name="numeric_value", value_type="number"),
        ExtraFieldSpec(name="dims", value_type="number[]"),
    ]


async def test_ensure_categories_schema_migrates_legacy_extra_fields_shape():
    """模拟 2026-08-16 之前写入的 extra_fields（纯字符串列表，无类型信息），
    验证迁移把它升级成 [{"name":..., "value_type":"string"}] 形态，旧字段
    统一按 "string" 类型对待（Global Constraints 的迁移规则）。这张手写表
    还没有 status 列（模拟 2026-08-19 之前的形态），ensure_categories_schema
    连带把它也迁移成 status='confirmed'，所以查询要用 status="confirmed"。
    """
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        "CREATE TABLE ontology_term_types (tenant_id TEXT NOT NULL, value TEXT NOT NULL, "
        "extra_fields TEXT NOT NULL DEFAULT '[]', node_key_template TEXT NOT NULL DEFAULT '', "
        "PRIMARY KEY (tenant_id, value));"
    )
    await conn.execute(
        "INSERT INTO ontology_term_types (tenant_id, value, extra_fields, node_key_template) "
        "VALUES ('default', '错误码', '[\"严重等级\", \"影响范围\"]', '')"
    )
    await conn.commit()

    await ensure_categories_schema(conn)

    types = await list_term_types(conn, tenant_id="default", status="confirmed")
    assert types[0].extra_fields == [
        ExtraFieldSpec(name="严重等级", value_type="string"),
        ExtraFieldSpec(name="影响范围", value_type="string"),
    ]


async def test_ensure_categories_schema_extra_fields_migration_is_idempotent():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="t1", value="错误码",
        extra_fields=[ExtraFieldSpec(name="severity_level", value_type="string")],
    )

    await ensure_categories_schema(conn)
    await ensure_categories_schema(conn)

    types = await list_term_types(conn, tenant_id="t1", status="draft")
    assert types[0].extra_fields == [ExtraFieldSpec(name="severity_level", value_type="string")]


async def test_create_term_type_rejects_extra_field_with_invalid_name_characters():
    conn = await _conn()
    with pytest.raises(InvalidExtraFieldTypeError):
        await create_term_type(
            conn, tenant_id="t1", value="Product",
            extra_fields=[ExtraFieldSpec(name="numeric value", value_type="number")],
        )


async def test_create_term_type_accepts_extra_field_with_underscore_name():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="t1", value="Product",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    result = await list_term_types(conn, "t1", status="draft")
    assert result[0].extra_fields[0].name == "numeric_value"
