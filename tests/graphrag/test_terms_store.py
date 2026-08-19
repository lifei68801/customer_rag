import aiosqlite
import pytest

from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import (
    create_product_line,
    create_term_type,
    list_product_lines,
    list_term_types,
    update_term_type,
)
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.terms_store import (
    InvalidExtraPropertyTypeError,
    TermNameConflictError,
    TermNotFoundError,
    UnknownCategoryError,
    count_terms,
    create_term,
    delete_term,
    ensure_terms_schema,
    get_term,
    list_terms,
    migrate_term_type,
    update_term,
    upsert_term_with_node_key,
)


def test_term_dataclass_has_tenant_id_and_node_key():
    term = Term(
        tenant_id="t1", node_key="k1", standard_name="错误码E502",
        aliases=[], term_type="error_code", product_line="核心平台",
    )
    assert term.tenant_id == "t1"
    assert term.node_key == "k1"


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    # confirm_ontology/checkout_draft 需要 tenant_relation_types/
    # term_type_relation_allowlist 等表存在——ensure_terms_schema 只建
    # ontology_term_types/ontology_product_lines 两张分类表，这里补齐
    # 完整的本体生命周期表结构（幂等，与 ensure_categories_schema 不冲突）。
    await ensure_ontology_schema(conn)
    # round-1 计划已写的既有测试直接用这些字面量当 term_type/product_line，
    # 早于分类枚举表存在——这里补齐分类，保持既有测试的字面量不变
    # （新增测试自己会为各自用到的分类调用 create_term_type/create_product_line，
    # 不依赖这份预置，两者字面量不重叠）。
    await create_term_type(conn, tenant_id="default", value="error_code")
    await create_term_type(conn, tenant_id="default", value="module")
    await create_term_type(conn, tenant_id="default", value="other")
    await create_term_type(conn, tenant_id="default", value="t")
    # 真实术语只认已确认的实体类型（见 _validate_categories），这里创建完就
    # 立刻确认，让共享 fixture 产出的类型对 create_term/update_term 可用。
    await confirm_ontology(conn, "default")
    await create_product_line(conn, value="核心平台")
    await create_product_line(conn, value="other")
    await create_product_line(conn, value="新产品线")
    await create_product_line(conn, value="p")
    return conn


async def _setup_default_categories(conn: aiosqlite.Connection) -> None:
    """Set up the standard categories for tests that use the new tenant-scoped functions."""
    # 调用方只 ensure_terms_schema 过——补齐 confirm_ontology 需要的表（幂等）。
    await ensure_ontology_schema(conn)
    await create_term_type(conn, tenant_id="default", value="error_code")
    await create_term_type(conn, tenant_id="default", value="module")
    await create_term_type(conn, tenant_id="default", value="other")
    await create_term_type(conn, tenant_id="default", value="t")
    await confirm_ontology(conn, "default")
    await create_product_line(conn, value="核心平台")
    await create_product_line(conn, value="other")
    await create_product_line(conn, value="新产品线")
    await create_product_line(conn, value="p")


async def test_ensure_terms_schema_migrates_legacy_table_to_tenant_scoped():
    """模拟一个 2026-08-15 之前建的 terms 表（老结构：standard_name 主键，
    没有 tenant_id/node_key 列），验证 ensure_terms_schema 能把它原地迁移
    成新结构，且存量数据全部归到 tenant_id='default'，node_key 回填成
    当时的 standard_name 值，不丢数据。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        """
        CREATE TABLE terms (
            standard_name TEXT PRIMARY KEY,
            aliases TEXT NOT NULL,
            term_type TEXT NOT NULL,
            product_line TEXT NOT NULL,
            extra_properties TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, aliases, term_type, product_line, extra_properties) "
        "VALUES ('错误码E502', '[\"网关超时\"]', 'error_code', '核心平台', '{}')"
    )
    await conn.commit()

    await ensure_terms_schema(conn)

    terms = await list_terms(conn, tenant_id="default")
    assert len(terms) == 1
    assert terms[0].tenant_id == "default"
    assert terms[0].node_key == "错误码E502"
    assert terms[0].standard_name == "错误码E502"
    assert terms[0].aliases == ["网关超时"]


async def test_ensure_terms_schema_migration_is_idempotent():
    """重复调用 ensure_terms_schema 不应该报错、不应该重复迁移导致数据翻倍。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await _setup_default_categories(conn)
    await create_term(
        conn, tenant_id="default", standard_name="A", aliases=[],
        term_type="t", product_line="p",
    )
    await ensure_terms_schema(conn)
    await ensure_terms_schema(conn)

    terms = await list_terms(conn, tenant_id="default")
    assert len(terms) == 1


async def test_create_term_is_isolated_per_tenant():
    """两个不同租户可以各自创建 standard_name 相同的术语，互不冲突——
    这是本次改造前不可能做到的（standard_name 曾经是全局主键）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await _setup_default_categories(conn)
    # Register categories for each tenant that will be used
    await create_term_type(conn, tenant_id="tenant_a", value="t")
    await create_term_type(conn, tenant_id="tenant_b", value="t")
    await confirm_ontology(conn, "tenant_a")
    await confirm_ontology(conn, "tenant_b")
    await create_term(
        conn, tenant_id="tenant_a", standard_name="错误码E502", aliases=[],
        term_type="t", product_line="p",
    )
    await create_term(
        conn, tenant_id="tenant_b", standard_name="错误码E502", aliases=[],
        term_type="t", product_line="p",
    )

    terms_a = await list_terms(conn, tenant_id="tenant_a")
    terms_b = await list_terms(conn, tenant_id="tenant_b")
    assert len(terms_a) == 1
    assert len(terms_b) == 1
    assert terms_a[0].tenant_id == "tenant_a"
    assert terms_b[0].tenant_id == "tenant_b"


async def test_update_term_rename_keeps_node_key_stable():
    """改名（standard_name 变化）不应该改变 node_key——这是 ADR-0003
    的核心断言：node_key 创建后永不变，即使术语被改名。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await _setup_default_categories(conn)
    # Register categories for t1 tenant
    await create_term_type(conn, tenant_id="t1", value="t")
    await confirm_ontology(conn, "t1")
    await create_term(
        conn, tenant_id="t1", standard_name="错误码E502", aliases=[],
        term_type="t", product_line="p",
    )
    original = await get_term(conn, tenant_id="t1", standard_name="错误码E502")

    await update_term(
        conn, tenant_id="t1", standard_name="错误码E502",
        new_standard_name="错误码E502v2", aliases=[], term_type="t", product_line="p",
    )

    renamed = await get_term(conn, tenant_id="t1", standard_name="错误码E502v2")
    assert renamed.node_key == original.node_key
    assert renamed.standard_name == "错误码E502v2"


async def test_check_name_conflict_does_not_cross_tenant_boundary():
    """租户 A 已经占用的 standard_name/alias，租户 B 应该可以自由使用——
    冲突检测必须按租户隔离，不能全局扫描。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await _setup_default_categories(conn)
    # Register categories for both tenants
    await create_term_type(conn, tenant_id="tenant_a", value="t")
    await create_term_type(conn, tenant_id="tenant_b", value="t")
    await confirm_ontology(conn, "tenant_a")
    await confirm_ontology(conn, "tenant_b")
    await create_term(
        conn, tenant_id="tenant_a", standard_name="登录模块", aliases=["认证模块"],
        term_type="t", product_line="p",
    )

    # 不应该抛 TermNameConflictError
    await create_term(
        conn, tenant_id="tenant_b", standard_name="登录模块", aliases=["认证模块"],
        term_type="t", product_line="p",
    )


async def test_delete_term_scoped_to_tenant():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await _setup_default_categories(conn)
    # Register categories for t1 tenant
    await create_term_type(conn, tenant_id="t1", value="t")
    await confirm_ontology(conn, "t1")
    await create_term(
        conn, tenant_id="t1", standard_name="待删除", aliases=[], term_type="t", product_line="p",
    )

    await delete_term(conn, "t1", "待删除")

    with pytest.raises(TermNotFoundError):
        await get_term(conn, tenant_id="t1", standard_name="待删除")


async def test_ensure_terms_schema_without_seed_path_creates_empty_table():
    conn = await _connect()

    assert await list_terms(conn, tenant_id="default") == []


async def test_ensure_terms_schema_seeds_from_yaml_only_on_first_creation(tmp_path):
    yaml_path = tmp_path / "seed.yaml"
    yaml_path.write_text(
        "terms:\n"
        "  - standard_name: 种子术语\n"
        "    aliases: [别名A]\n"
        "    term_type: type1\n"
        "    product_line: line1\n",
        encoding="utf-8",
    )
    conn = await aiosqlite.connect(":memory:")

    await ensure_terms_schema(conn, seed_yaml_path=yaml_path)
    seeded = await list_terms(conn, tenant_id="default")
    assert [t.standard_name for t in seeded] == ["种子术语"]

    # 再次调用（模拟第二次进程启动）：表已存在，即使 YAML 内容变了也不
    # 重新导入——只在首次建表时导入一次
    yaml_path.write_text(
        "terms:\n  - standard_name: 另一个术语\n    aliases: []\n"
        "    term_type: t\n    product_line: p\n",
        encoding="utf-8",
    )
    await ensure_terms_schema(conn, seed_yaml_path=yaml_path)
    after_second_call = await list_terms(conn, tenant_id="default")
    assert [t.standard_name for t in after_second_call] == ["种子术语"]


async def test_ensure_terms_schema_skips_seeding_when_yaml_path_missing(tmp_path):
    conn = await aiosqlite.connect(":memory:")
    missing_path = tmp_path / "does-not-exist.yaml"

    await ensure_terms_schema(conn, seed_yaml_path=missing_path)

    assert await list_terms(conn, tenant_id="default") == []


async def test_ensure_terms_schema_bridges_historical_term_type_and_product_line_into_categories():
    """向后兼容桥接的回归测试：老版本上线时 term_type/product_line 还是自由文本，
    没有分类枚举表这个概念——如果 terms 表已经有历史数据、但分类枚举表是空的，
    ensure_terms_schema 必须把历史数据里出现过的去重值自动导入枚举表，否则硬
    约束上线的第一刻，任何现有术语的编辑请求都会因为找不到匹配的枚举值报错
    （见 terms_store._bridge_seed_categories_from_existing_terms 的说明）。
    """
    conn = await aiosqlite.connect(":memory:")
    # 模拟老版本已经建过表、写过数据，此时还没有分类枚举表——直接用原始 SQL
    # 建表插入，绕开 create_term 的分类校验（那时压根不存在这层校验）。
    await conn.executescript(
        "CREATE TABLE terms ("
        "standard_name TEXT PRIMARY KEY, aliases TEXT NOT NULL, "
        "term_type TEXT NOT NULL, product_line TEXT NOT NULL"
        ");"
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, aliases, term_type, product_line) "
        "VALUES (?, ?, ?, ?)",
        ("历史术语", "[]", "error_code", "核心平台"),
    )
    await conn.commit()

    await ensure_terms_schema(conn)

    term_type_values = {
        t.value for t in await list_term_types(conn, tenant_id="default", status="confirmed")
    }
    product_line_values = set(await list_product_lines(conn))
    assert "error_code" in term_type_values
    assert "核心平台" in product_line_values
    # 历史行本身也要能正常读出来——extra_properties 是后补的列，历史行没有
    # 写过这个值，读出来应该是默认的空字典，而不是报错或缺列。
    term = await get_term(conn, tenant_id="default", standard_name="历史术语")
    assert term.extra_properties == {}


async def test_create_term_then_list_returns_it():
    conn = await _connect()

    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code", product_line="核心平台",
    )

    terms = await list_terms(conn, tenant_id="default")
    assert len(terms) == 1
    assert terms[0].tenant_id == "default"
    assert terms[0].standard_name == "错误码E502"
    assert terms[0].aliases == ["网关超时"]
    assert terms[0].term_type == "error_code"
    assert terms[0].product_line == "核心平台"


async def test_create_term_rejects_duplicate_standard_name():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=[],
        term_type="error_code", product_line="核心平台",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, tenant_id="default", standard_name="错误码E502", aliases=[],
            term_type="other", product_line="other",
        )


async def test_create_term_rejects_alias_that_collides_with_another_terms_standard_name():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="登录模块", aliases=[],
        term_type="module", product_line="核心平台",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, tenant_id="default", standard_name="错误码E502", aliases=["登录模块"],
            term_type="error_code", product_line="核心平台",
        )


async def test_create_term_rejects_alias_that_collides_with_another_terms_alias():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code", product_line="核心平台",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, tenant_id="default", standard_name="登录模块", aliases=["网关超时"],
            term_type="module", product_line="核心平台",
        )


async def test_get_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await get_term(conn, tenant_id="default", standard_name="不存在的术语")


async def test_update_term_without_rename_changes_fields_in_place():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code", product_line="核心平台",
    )

    await update_term(
        conn, tenant_id="default", standard_name="错误码E502", new_standard_name="错误码E502",
        aliases=["网关超时", "502错误"], term_type="error_code", product_line="新产品线",
    )

    term = await get_term(conn, tenant_id="default", standard_name="错误码E502")
    assert term.aliases == ["网关超时", "502错误"]
    assert term.product_line == "新产品线"


async def test_update_term_with_rename_moves_to_new_standard_name():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="旧名字", aliases=[],
        term_type="t", product_line="p",
    )

    await update_term(
        conn, tenant_id="default", standard_name="旧名字", new_standard_name="新名字",
        aliases=[], term_type="t", product_line="p",
    )

    with pytest.raises(TermNotFoundError):
        await get_term(conn, tenant_id="default", standard_name="旧名字")
    renamed = await get_term(conn, tenant_id="default", standard_name="新名字")
    assert renamed.standard_name == "新名字"


async def test_update_term_rejects_rename_into_an_existing_name():
    conn = await _connect()
    await create_term(conn, tenant_id="default", standard_name="A", aliases=[], term_type="t", product_line="p")
    await create_term(conn, tenant_id="default", standard_name="B", aliases=[], term_type="t", product_line="p")

    with pytest.raises(TermNameConflictError):
        await update_term(
            conn, tenant_id="default", standard_name="A", new_standard_name="B",
            aliases=[], term_type="t", product_line="p",
        )


async def test_update_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await update_term(
            conn, tenant_id="default", standard_name="不存在", new_standard_name="不存在",
            aliases=[], term_type="t", product_line="p",
        )


async def test_delete_term_removes_it():
    conn = await _connect()
    await create_term(conn, tenant_id="default", standard_name="待删除", aliases=[], term_type="t", product_line="p")

    await delete_term(conn, "default", "待删除")

    assert await list_terms(conn, tenant_id="default") == []


async def test_delete_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await delete_term(conn, "default", "不存在")


async def test_create_term_persists_extra_properties():
    from app.graphrag.ontology_categories import ExtraFieldSpec
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="错误码", extra_fields=[ExtraFieldSpec(name="severity_level", value_type="string")])
    await confirm_ontology(conn, "default")
    await create_product_line(conn, value="示例产品线")

    await create_term(
        conn,
        tenant_id="default",
        standard_name="错误码E502",
        aliases=[],
        term_type="错误码",
        product_line="示例产品线",
        extra_properties={"severity_level": "高"},
    )

    term = await get_term(conn, tenant_id="default", standard_name="错误码E502")
    assert term.extra_properties == {"severity_level": "高"}


async def test_create_term_rejects_unknown_term_type():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="default", standard_name="x", aliases=[], term_type="没有这个分类",
            product_line="示例产品线",
        )


async def test_create_term_rejects_unknown_product_line():
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="错误码")
    await confirm_ontology(conn, "default")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="default", standard_name="x", aliases=[], term_type="错误码",
            product_line="没有这个产品线",
        )


async def test_create_term_rejects_extra_property_not_declared_on_term_type():
    from app.graphrag.ontology_categories import ExtraFieldSpec
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="错误码", extra_fields=[ExtraFieldSpec(name="severity_level", value_type="string")])
    await confirm_ontology(conn, "default")
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="default", standard_name="x", aliases=[], term_type="错误码",
            product_line="示例产品线", extra_properties={"没声明过的字段": "值"},
        )


async def test_removing_extra_field_from_term_type_preserves_existing_term_value():
    from app.graphrag.ontology_categories import ExtraFieldSpec
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="错误码", extra_fields=[ExtraFieldSpec(name="severity_level", value_type="string"), ExtraFieldSpec(name="impact_scope", value_type="string")])
    await confirm_ontology(conn, "default")
    await create_product_line(conn, value="示例产品线")
    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=[], term_type="错误码",
        product_line="示例产品线", extra_properties={"severity_level": "高", "impact_scope": "全站不可用"},
    )

    # update_term_type 只操作草稿行，确认之后草稿已清空，需要先检出一份新草稿
    await checkout_draft(conn, "default")
    await update_term_type(conn, tenant_id="default", value="错误码", new_value="错误码", extra_fields=[ExtraFieldSpec(name="severity_level", value_type="string")])

    term = await get_term(conn, tenant_id="default", standard_name="错误码E502")
    assert term.extra_properties == {"severity_level": "高", "impact_scope": "全站不可用"}


async def test_update_term_resubmitting_undeclared_but_already_stored_key_succeeds():
    """回归测试：字段从 term_type 里被去掉之后，重新保存这条术语（哪怕值原样
    不改）不能因为这个字段"未声明"而被拒绝或静默丢弃——见
    _validate_categories 的 existing_extra_property_keys 参数说明。"""
    from app.graphrag.ontology_categories import ExtraFieldSpec
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="房型", extra_fields=[ExtraFieldSpec(name="area", value_type="string")])
    await confirm_ontology(conn, "default")
    await create_product_line(conn, value="示例产品线")
    await create_term(
        conn, tenant_id="default", standard_name="大床房", aliases=[], term_type="房型",
        product_line="示例产品线", extra_properties={"area": "30"},
    )

    # 业务把"area"从房型的声明字段里移除——update_term_type 只操作草稿行，
    # 确认之后草稿已清空，需要先检出一份新草稿；改完再确认一次，让
    # 下面 update_term 的 _validate_categories（查已确认声明）真正看到
    # "area 已不再声明"这个状态，否则测的就不是这里的豁免逻辑了。
    await checkout_draft(conn, "default")
    await update_term_type(conn, tenant_id="default", value="房型", new_value="房型", extra_fields=[])
    await confirm_ontology(conn, "default")

    # 重新保存这条术语，提交里仍然带着这个已经被去掉声明的字段——不应该报错
    await update_term(
        conn, tenant_id="default", standard_name="大床房", new_standard_name="大床房",
        aliases=["豪华大床房"], term_type="房型", product_line="示例产品线",
        extra_properties={"area": "30"},
    )

    term = await get_term(conn, tenant_id="default", standard_name="大床房")
    assert term.extra_properties == {"area": "30"}
    assert term.aliases == ["豪华大床房"]


async def test_update_term_rejects_genuinely_new_undeclared_key():
    """字段既不在 term_type 当前声明里，也从未在这条术语上出现过——不能因为
    "existing_extra_property_keys 放行"这条豁免被滥用成完全绕过校验。"""
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="房型", extra_fields=[])
    await confirm_ontology(conn, "default")
    await create_product_line(conn, value="示例产品线")
    await create_term(
        conn, tenant_id="default", standard_name="大床房", aliases=[], term_type="房型",
        product_line="示例产品线", extra_properties={},
    )

    with pytest.raises(UnknownCategoryError):
        await update_term(
            conn, tenant_id="default", standard_name="大床房", new_standard_name="大床房",
            aliases=[], term_type="房型", product_line="示例产品线",
            extra_properties={"从未出现过的字段": "值"},
        )


async def test_validate_categories_rejects_term_type_from_another_tenant():
    """term_type 校验闭环之后必须按租户过滤——tenant_a 注册的分类，
    tenant_b 提交同名 term_type 应该被拒绝（对 tenant_b 而言这是未知分类）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await create_term_type(conn, tenant_id="tenant_a", value="错误码")
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="tenant_b", standard_name="X", aliases=[],
            term_type="错误码", product_line="示例产品线",
        )


async def test_create_term_with_typed_extra_properties():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type, create_product_line
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[
            ExtraFieldSpec(name="numeric_value", value_type="number"),
            ExtraFieldSpec(name="dims", value_type="number[]"),
        ],
    )
    await confirm_ontology(conn, "t1")
    await create_product_line(conn, value="示例产品线")

    await create_term(
        conn, tenant_id="t1", standard_name="容量750ml", aliases=[],
        term_type="VariantValue", product_line="示例产品线",
        extra_properties={"numeric_value": 750, "dims": [20.5, 10.0]},
    )

    term = await get_term(conn, tenant_id="t1", standard_name="容量750ml")
    assert term.extra_properties == {"numeric_value": 750, "dims": [20.5, 10.0]}


async def test_create_term_rejects_extra_property_with_wrong_type():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type, create_product_line
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await confirm_ontology(conn, "t1")
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(InvalidExtraPropertyTypeError):
        await create_term(
            conn, tenant_id="t1", standard_name="容量750ml", aliases=[],
            term_type="VariantValue", product_line="示例产品线",
            extra_properties={"numeric_value": "不是数字"},
        )


async def test_create_term_rejects_bool_as_number():
    """bool 是 int 的子类，必须显式排除——见 Global Constraints。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type, create_product_line
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await confirm_ontology(conn, "t1")
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(InvalidExtraPropertyTypeError):
        await create_term(
            conn, tenant_id="t1", standard_name="X", aliases=[],
            term_type="VariantValue", product_line="示例产品线",
            extra_properties={"numeric_value": True},
        )


async def test_update_term_grandfathered_field_skips_type_check():
    """字段被从 term_type 移除后，已写在术语记录上的值不再做类型校验——
    延续本体基座计划"移除字段声明不触碰已有数据"的原则（见 Global
    Constraints）。这里验证：即使移除声明后重新提交同一个值，也不会因为
    "现在没有声明类型、无法判断类型是否匹配"而报错。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import (
        ExtraFieldSpec, create_term_type, create_product_line, update_term_type,
    )
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await confirm_ontology(conn, "t1")
    await create_product_line(conn, value="示例产品线")
    await create_term(
        conn, tenant_id="t1", standard_name="X", aliases=[],
        term_type="VariantValue", product_line="示例产品线",
        extra_properties={"numeric_value": 750},
    )
    await checkout_draft(conn, "t1")
    await update_term_type(
        conn, tenant_id="t1", value="VariantValue", new_value="VariantValue",
        extra_fields=[],
    )
    await confirm_ontology(conn, "t1")

    # 不应该抛 InvalidExtraPropertyTypeError 或 UnknownCategoryError
    await update_term(
        conn, tenant_id="t1", standard_name="X", new_standard_name="X",
        aliases=[], term_type="VariantValue", product_line="示例产品线",
        extra_properties={"numeric_value": 750},
    )


async def test_upsert_term_with_node_key_creates_new_row():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import create_term_type, create_product_line
    await create_term_type(conn, tenant_id="muji", value="Product")
    await confirm_ontology(conn, "muji")
    await create_product_line(conn, value="MUJI")

    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒",
        aliases=[], term_type="Product", product_line="MUJI",
    )

    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term.node_key == "Product:1001"


async def test_upsert_term_with_node_key_updates_existing_row_by_node_key():
    """再次 upsert 同一个 node_key、standard_name 变了——更新而不是报冲突，
    这是 upsert 和 create_term 的本质区别（见 terms_store.py 里的说明）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import create_term_type, create_product_line
    await create_term_type(conn, tenant_id="muji", value="Product")
    await confirm_ontology(conn, "muji")
    await create_product_line(conn, value="MUJI")
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒",
        aliases=[], term_type="Product", product_line="MUJI",
    )

    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒(新装)",
        aliases=[], term_type="Product", product_line="MUJI",
    )

    all_terms = await list_terms(conn, tenant_id="muji")
    assert len(all_terms) == 1
    assert all_terms[0].standard_name == "圆角收纳盒(新装)"
    assert all_terms[0].node_key == "Product:1001"


async def test_upsert_term_with_node_key_rejects_duplicate_standard_name_different_node_key():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import create_term_type, create_product_line
    await create_term_type(conn, tenant_id="muji", value="Product")
    await confirm_ontology(conn, "muji")
    await create_product_line(conn, value="MUJI")
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒",
        aliases=[], term_type="Product", product_line="MUJI",
    )

    with pytest.raises(TermNameConflictError):
        await upsert_term_with_node_key(
            conn, tenant_id="muji", node_key="Product:1002", standard_name="圆角收纳盒",
            aliases=[], term_type="Product", product_line="MUJI",
        )


async def test_upsert_term_with_node_key_typed_extra_properties():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type, create_product_line
    await create_term_type(
        conn, tenant_id="muji", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await confirm_ontology(conn, "muji")
    await create_product_line(conn, value="MUJI")

    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Variant:dim_007:00001", standard_name="抹茶",
        aliases=[], term_type="VariantValue", product_line="MUJI",
        extra_properties={"numeric_value": 70},
    )

    term = await get_term(conn, tenant_id="muji", standard_name="抹茶")
    assert term.extra_properties == {"numeric_value": 70}


async def test_list_terms_paginates_with_limit_and_offset():
    """种 3 条术语（按 standard_name 排序为 A、B、C），limit=1 offset=1
    应该只拿到第 2 条（B）——验证分页哨兵参数按预期切片。"""
    conn = await _connect()
    for name in ("A", "B", "C"):
        await create_term(
            conn, tenant_id="default", standard_name=name, aliases=[],
            term_type="t", product_line="p",
        )

    page = await list_terms(conn, "default", limit=1, offset=1)

    assert [t.standard_name for t in page] == ["B"]


async def test_count_terms_returns_total_regardless_of_pagination():
    conn = await _connect()
    for name in ("A", "B", "C"):
        await create_term(
            conn, tenant_id="default", standard_name=name, aliases=[],
            term_type="t", product_line="p",
        )

    total = await count_terms(conn, "default")

    assert total == 3


async def test_list_terms_without_limit_offset_returns_full_unpaginated_list():
    """不传 limit/offset 时必须保持改造前的行为：返回该租户全部术语，
    这是既有调用方（agent 检索、摄取管线、eval runner、review_cli 等）
    赖以不变的默认行为——见 app/api/deps.py::get_terms 等处的调用方式。"""
    conn = await _connect()
    for name in ("A", "B", "C"):
        await create_term(
            conn, tenant_id="default", standard_name=name, aliases=[],
            term_type="t", product_line="p",
        )

    terms = await list_terms(conn, "default")

    assert [t.standard_name for t in terms] == ["A", "B", "C"]


async def test_upsert_term_with_node_key_grandfathers_removed_field_on_re_upsert():
    """字段被从 term_type 移除后，upsert 同一个 node_key 时旧值也要能豁免类型/
    未知字段校验——延续 update_term 已有的豁免原则。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import (
        ExtraFieldSpec, create_term_type, create_product_line, update_term_type,
    )
    await create_term_type(
        conn, tenant_id="muji", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await confirm_ontology(conn, "muji")
    await create_product_line(conn, value="MUJI")
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Variant:dim_007:00001", standard_name="抹茶",
        aliases=[], term_type="VariantValue", product_line="MUJI",
        extra_properties={"numeric_value": 70},
    )
    await checkout_draft(conn, "muji")
    await update_term_type(
        conn, tenant_id="muji", value="VariantValue", new_value="VariantValue",
        extra_fields=[],
    )
    await confirm_ontology(conn, "muji")

    # 不应该抛错
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Variant:dim_007:00001", standard_name="抹茶",
        aliases=[], term_type="VariantValue", product_line="MUJI",
        extra_properties={"numeric_value": 70},
    )


async def test_migrate_term_type_updates_matching_rows_and_returns_affected_count():
    conn = await _connect()
    for name in ("A", "B"):
        await create_term(
            conn, tenant_id="default", standard_name=name, aliases=[],
            term_type="t", product_line="p",
        )
    # 不应该被迁移到的另一个租户的同名旧类型行——验证按租户隔离
    await create_term_type(conn, tenant_id="other-tenant", value="t")
    await confirm_ontology(conn, "other-tenant")
    await create_term(
        conn, tenant_id="other-tenant", standard_name="C", aliases=[],
        term_type="t", product_line="p",
    )

    affected = await migrate_term_type(conn, "default", old_type="t", new_type="t2")

    assert affected == 2
    terms = await list_terms(conn, "default")
    assert {term.term_type for term in terms} == {"t2"}
    other_tenant_terms = await list_terms(conn, "other-tenant")
    assert other_tenant_terms[0].term_type == "t"


async def test_migrate_term_type_returns_zero_when_no_rows_match():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="A", aliases=[],
        term_type="t", product_line="p",
    )

    affected = await migrate_term_type(conn, "default", old_type="不存在的类型", new_type="t2")

    assert affected == 0
    terms = await list_terms(conn, "default")
    assert terms[0].term_type == "t"


async def test_create_term_defaults_source_to_manual():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="term-a", aliases=[],
        term_type="t", product_line="p",
    )
    term = await get_term(conn, "default", "term-a")
    assert term.source == "manual"


async def test_create_term_explicit_source():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="term-b", aliases=[],
        term_type="t", product_line="p", source="review",
    )
    term = await get_term(conn, "default", "term-b")
    assert term.source == "review"


async def test_upsert_term_with_node_key_defaults_source_to_etl():
    conn = await _connect()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="k1", standard_name="term-c", aliases=[],
        term_type="t", product_line="p",
    )
    term = await get_term(conn, "default", "term-c")
    assert term.source == "etl"


async def test_update_term_does_not_change_source():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="term-d", aliases=[],
        term_type="t", product_line="p", source="etl",
    )
    await update_term(
        conn, tenant_id="default", standard_name="term-d", new_standard_name="term-d-renamed",
        aliases=["alias"], term_type="t", product_line="p",
    )
    term = await get_term(conn, "default", "term-d-renamed")
    assert term.source == "etl"


async def test_list_terms_filters_by_source():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="m1", aliases=[],
        term_type="t", product_line="p", source="manual",
    )
    await create_term(
        conn, tenant_id="default", standard_name="e1", aliases=[],
        term_type="t", product_line="p", source="etl",
    )
    manual_only = await list_terms(conn, "default", source="manual")
    assert [t.standard_name for t in manual_only] == ["m1"]
