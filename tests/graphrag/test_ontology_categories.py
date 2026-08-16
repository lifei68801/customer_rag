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
    create_product_line,
    create_term_type,
    delete_product_line,
    delete_term_type,
    ensure_categories_schema,
    list_product_lines,
    list_term_types,
    update_product_line,
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


async def test_create_and_list_term_type_with_extra_fields():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="default", value="错误码",
        extra_fields=[
            ExtraFieldSpec(name="严重等级", value_type="string"),
            ExtraFieldSpec(name="影响范围", value_type="string"),
        ],
    )

    result = await list_term_types(conn, tenant_id="default")

    assert result == [TermTypeCategory(
        value="错误码",
        extra_fields=[
            ExtraFieldSpec(name="严重等级", value_type="string"),
            ExtraFieldSpec(name="影响范围", value_type="string"),
        ],
        node_key_template="",
    )]


async def test_create_term_type_without_extra_fields_defaults_to_empty_list():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="地点")

    result = await list_term_types(conn, tenant_id="default")

    assert result == [TermTypeCategory(value="地点", extra_fields=[], node_key_template="")]


async def test_create_and_list_product_line():
    conn = await _conn()
    await create_product_line(conn, value="示例产品线")

    assert await list_product_lines(conn) == ["示例产品线"]


async def test_create_duplicate_term_type_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")

    with pytest.raises(CategoryNameConflictError):
        await create_term_type(conn, tenant_id="default", value="错误码")


async def test_update_term_type_renames_without_referencing_terms():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="default", value="错误码",
        extra_fields=[ExtraFieldSpec(name="严重等级", value_type="string")],
    )
    await conn.execute(
        "CREATE TABLE terms (tenant_id TEXT NOT NULL, standard_name TEXT NOT NULL, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', node_key TEXT NOT NULL, PRIMARY KEY (tenant_id, standard_name))"
    )
    await conn.commit()

    await update_term_type(
        conn, tenant_id="default", value="错误码", new_value="故障码",
        extra_fields=[
            ExtraFieldSpec(name="严重等级", value_type="string"),
            ExtraFieldSpec(name="影响范围", value_type="string"),
        ],
        node_key_template="",
    )

    result = await list_term_types(conn, tenant_id="default")
    assert result == [TermTypeCategory(
        value="故障码",
        extra_fields=[
            ExtraFieldSpec(name="严重等级", value_type="string"),
            ExtraFieldSpec(name="影响范围", value_type="string"),
        ],
        node_key_template="",
    )]


async def test_update_term_type_cascades_rename_to_referencing_terms():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await conn.execute(
        "CREATE TABLE terms (tenant_id TEXT NOT NULL, standard_name TEXT NOT NULL, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', node_key TEXT NOT NULL, PRIMARY KEY (tenant_id, standard_name))"
    )
    await conn.execute(
        "INSERT INTO terms (tenant_id, standard_name, term_type, product_line, node_key) VALUES (?, ?, ?, ?, ?)",
        ("default", "错误码E502", "错误码", "示例产品线", "错误码E502"),
    )
    await conn.commit()

    await update_term_type(conn, tenant_id="default", value="错误码", new_value="故障码", extra_fields=[], node_key_template="")

    cursor = await conn.execute("SELECT term_type FROM terms WHERE tenant_id = ? AND standard_name = ?", ("default", "错误码E502",))
    row = await cursor.fetchone()
    assert row[0] == "故障码"


async def test_update_term_type_into_existing_name_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await create_term_type(conn, tenant_id="default", value="模块")

    with pytest.raises(CategoryNameConflictError):
        await update_term_type(conn, tenant_id="default", value="错误码", new_value="模块", extra_fields=[], node_key_template="")


async def test_delete_term_type_not_in_use_succeeds():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await conn.execute(
        "CREATE TABLE terms (tenant_id TEXT NOT NULL, standard_name TEXT NOT NULL, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', node_key TEXT NOT NULL, PRIMARY KEY (tenant_id, standard_name))"
    )
    await conn.commit()

    await delete_term_type(conn, tenant_id="default", value="错误码")

    assert await list_term_types(conn, tenant_id="default") == []


async def test_delete_term_type_in_use_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await conn.execute(
        "CREATE TABLE terms (tenant_id TEXT NOT NULL, standard_name TEXT NOT NULL, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', node_key TEXT NOT NULL, PRIMARY KEY (tenant_id, standard_name))"
    )
    await conn.execute(
        "INSERT INTO terms (tenant_id, standard_name, term_type, product_line, node_key) VALUES (?, ?, ?, ?, ?)",
        ("default", "错误码E502", "错误码", "示例产品线", "错误码E502"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, tenant_id="default", value="错误码")


async def test_update_term_type_cascades_rename_to_allowlist_references():
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="客房")
    await create_term_type(conn, tenant_id="t1", value="酒店")
    await conn.executescript(
        "CREATE TABLE terms (tenant_id TEXT NOT NULL, standard_name TEXT NOT NULL, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', node_key TEXT NOT NULL, PRIMARY KEY (tenant_id, standard_name));"
    )
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "客房", "PART_OF", "酒店", "confirmed"),
    )
    await conn.commit()

    await update_term_type(conn, tenant_id="t1", value="客房", new_value="大床房", extra_fields=[], node_key_template="")

    cursor = await conn.execute(
        "SELECT subject_term_type FROM term_type_relation_allowlist WHERE tenant_id = 't1'"
    )
    row = await cursor.fetchone()
    assert row[0] == "大床房"


async def test_delete_term_type_referenced_only_by_allowlist_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="客房")
    await create_term_type(conn, tenant_id="t1", value="酒店")
    await conn.executescript(
        "CREATE TABLE terms (tenant_id TEXT NOT NULL, standard_name TEXT NOT NULL, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', node_key TEXT NOT NULL, PRIMARY KEY (tenant_id, standard_name));"
    )
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "客房", "PART_OF", "酒店", "draft"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, tenant_id="t1", value="客房")


async def test_delete_product_line_in_use_raises_conflict():
    conn = await _conn()
    await create_product_line(conn, value="示例产品线")
    await conn.execute(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]')"
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, term_type, product_line) VALUES (?, ?, ?)",
        ("错误码E502", "错误码", "示例产品线"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_product_line(conn, "示例产品线")


async def test_ensure_categories_schema_migrates_legacy_term_types_table():
    """模拟 2026-08-15 之前的 ontology_term_types 表（value 主键，没有
    tenant_id/node_key_template），验证迁移把存量数据归到 tenant_id='default'。"""
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

    types = await list_term_types(conn, tenant_id="default")
    assert len(types) == 1
    assert types[0].value == "error_code"
    assert types[0].node_key_template == ""


async def test_create_and_list_term_types_isolated_per_tenant():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="tenant_a", value="错误码",
        extra_fields=[ExtraFieldSpec(name="严重等级", value_type="string")],
    )
    await create_term_type(conn, tenant_id="tenant_b", value="VariantValue", extra_fields=[])

    types_a = await list_term_types(conn, tenant_id="tenant_a")
    types_b = await list_term_types(conn, tenant_id="tenant_b")
    assert [t.value for t in types_a] == ["错误码"]
    assert [t.value for t in types_b] == ["VariantValue"]


async def test_create_term_type_with_node_key_template():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="muji", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="string")],
        node_key_template="Variant:{dim_code}:{value_code}",
    )

    types = await list_term_types(conn, tenant_id="muji")
    assert types[0].node_key_template == "Variant:{dim_code}:{value_code}"


async def test_update_term_type_rename_cascades_within_same_tenant_only():
    """改名级联到 terms/term_type_relation_allowlist，必须只影响同一
    租户的行——term_type 按租户隔离后，不该波及其它租户。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="tenant_a", value="客房", extra_fields=[])
    await create_term_type(conn, tenant_id="tenant_a", value="酒店", extra_fields=[])
    await create_term_type(conn, tenant_id="tenant_b", value="客房", extra_fields=[])
    await create_term_type(conn, tenant_id="tenant_b", value="酒店", extra_fields=[])
    await create_product_line(conn, value="示例产品线")
    from app.graphrag.terms_store import create_term, ensure_terms_schema, get_term
    await ensure_terms_schema(conn)
    await create_term(
        conn, tenant_id="tenant_a", standard_name="A栋客房", aliases=[],
        term_type="客房", product_line="示例产品线",
    )
    await create_term(
        conn, tenant_id="tenant_b", standard_name="B栋客房", aliases=[],
        term_type="客房", product_line="示例产品线",
    )

    await update_term_type(
        conn, tenant_id="tenant_a", value="客房", new_value="客房间",
        extra_fields=[], node_key_template="",
    )

    term_a = await get_term(conn, tenant_id="tenant_a", standard_name="A栋客房")
    term_b = await get_term(conn, tenant_id="tenant_b", standard_name="B栋客房")
    assert term_a.term_type == "客房间"
    assert term_b.term_type == "客房"  # tenant_b 不受 tenant_a 改名影响


async def test_delete_term_type_in_use_by_constraint_returns_error():
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="客房", extra_fields=[])
    await create_term_type(conn, tenant_id="t1", value="酒店", extra_fields=[])
    from app.graphrag.ontology_relations import seed_default_relation_types
    from app.graphrag.ontology_constraints import add_allowed_combination
    from app.graphrag.terms_store import ensure_terms_schema
    await ensure_terms_schema(conn)
    await seed_default_relation_types(conn, "t1")
    await add_allowed_combination(
        conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店",
    )

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, tenant_id="t1", value="客房")


async def test_create_term_type_with_typed_extra_fields():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="t1", value="错误码",
        extra_fields=[
            ExtraFieldSpec(name="严重等级", value_type="string"),
            ExtraFieldSpec(name="影响范围人数", value_type="integer"),
        ],
    )

    types = await list_term_types(conn, tenant_id="t1")
    assert types[0].extra_fields == [
        ExtraFieldSpec(name="严重等级", value_type="string"),
        ExtraFieldSpec(name="影响范围人数", value_type="integer"),
    ]


async def test_create_term_type_rejects_invalid_value_type():
    conn = await _conn()
    with pytest.raises(InvalidExtraFieldTypeError):
        await create_term_type(
            conn, tenant_id="t1", value="错误码",
            extra_fields=[ExtraFieldSpec(name="严重等级", value_type="不存在的类型")],
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
        node_key_template="",
    )

    types = await list_term_types(conn, tenant_id="t1")
    assert types[0].extra_fields == [
        ExtraFieldSpec(name="numeric_value", value_type="number"),
        ExtraFieldSpec(name="dims", value_type="number[]"),
    ]


async def test_ensure_categories_schema_migrates_legacy_extra_fields_shape():
    """模拟 2026-08-16 之前写入的 extra_fields（纯字符串列表，无类型信息），
    验证迁移把它升级成 [{"name":..., "value_type":"string"}] 形态，旧字段
    统一按 "string" 类型对待（Global Constraints 的迁移规则）。"""
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

    types = await list_term_types(conn, tenant_id="default")
    assert types[0].extra_fields == [
        ExtraFieldSpec(name="严重等级", value_type="string"),
        ExtraFieldSpec(name="影响范围", value_type="string"),
    ]


async def test_ensure_categories_schema_extra_fields_migration_is_idempotent():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="t1", value="错误码",
        extra_fields=[ExtraFieldSpec(name="严重等级", value_type="string")],
    )

    await ensure_categories_schema(conn)
    await ensure_categories_schema(conn)

    types = await list_term_types(conn, tenant_id="t1")
    assert types[0].extra_fields == [ExtraFieldSpec(name="严重等级", value_type="string")]
