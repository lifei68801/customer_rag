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
from app.graphrag.terms_store import (
    TermNameConflictError,
    TermNotFoundError,
    UnknownCategoryError,
    create_term,
    delete_term,
    ensure_terms_schema,
    get_term,
    list_terms,
    update_term,
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
    # round-1 计划已写的既有测试直接用这些字面量当 term_type/product_line，
    # 早于分类枚举表存在——这里补齐分类，保持既有测试的字面量不变
    # （新增测试自己会为各自用到的分类调用 create_term_type/create_product_line，
    # 不依赖这份预置，两者字面量不重叠）。
    await create_term_type(conn, value="error_code")
    await create_term_type(conn, value="module")
    await create_term_type(conn, value="other")
    await create_term_type(conn, value="t")
    await create_product_line(conn, value="核心平台")
    await create_product_line(conn, value="other")
    await create_product_line(conn, value="新产品线")
    await create_product_line(conn, value="p")
    return conn


async def _setup_default_categories(conn: aiosqlite.Connection) -> None:
    """Set up the standard categories for tests that use the new tenant-scoped functions."""
    await create_term_type(conn, value="error_code")
    await create_term_type(conn, value="module")
    await create_term_type(conn, value="other")
    await create_term_type(conn, value="t")
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

    term_type_values = {t.value for t in await list_term_types(conn)}
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
    conn = await _connect()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级"])
    await create_product_line(conn, value="示例产品线")

    await create_term(
        conn,
        tenant_id="default",
        standard_name="错误码E502",
        aliases=[],
        term_type="错误码",
        product_line="示例产品线",
        extra_properties={"严重等级": "高"},
    )

    term = await get_term(conn, tenant_id="default", standard_name="错误码E502")
    assert term.extra_properties == {"严重等级": "高"}


async def test_create_term_rejects_unknown_term_type():
    conn = await _connect()
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="default", standard_name="x", aliases=[], term_type="没有这个分类",
            product_line="示例产品线",
        )


async def test_create_term_rejects_unknown_product_line():
    conn = await _connect()
    await create_term_type(conn, value="错误码")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="default", standard_name="x", aliases=[], term_type="错误码",
            product_line="没有这个产品线",
        )


async def test_create_term_rejects_extra_property_not_declared_on_term_type():
    conn = await _connect()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级"])
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="default", standard_name="x", aliases=[], term_type="错误码",
            product_line="示例产品线", extra_properties={"没声明过的字段": "值"},
        )


async def test_removing_extra_field_from_term_type_preserves_existing_term_value():
    conn = await _connect()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级", "影响范围"])
    await create_product_line(conn, value="示例产品线")
    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=[], term_type="错误码",
        product_line="示例产品线", extra_properties={"严重等级": "高", "影响范围": "全站不可用"},
    )

    await update_term_type(conn, value="错误码", new_value="错误码", extra_fields=["严重等级"])

    term = await get_term(conn, tenant_id="default", standard_name="错误码E502")
    assert term.extra_properties == {"严重等级": "高", "影响范围": "全站不可用"}


async def test_update_term_resubmitting_undeclared_but_already_stored_key_succeeds():
    """回归测试：字段从 term_type 里被去掉之后，重新保存这条术语（哪怕值原样
    不改）不能因为这个字段"未声明"而被拒绝或静默丢弃——见
    _validate_categories 的 existing_extra_property_keys 参数说明。"""
    conn = await _connect()
    await create_term_type(conn, value="房型", extra_fields=["面积"])
    await create_product_line(conn, value="示例产品线")
    await create_term(
        conn, tenant_id="default", standard_name="大床房", aliases=[], term_type="房型",
        product_line="示例产品线", extra_properties={"面积": "30"},
    )

    # 业务把"面积"从房型的声明字段里移除
    await update_term_type(conn, value="房型", new_value="房型", extra_fields=[])

    # 重新保存这条术语，提交里仍然带着这个已经被去掉声明的字段——不应该报错
    await update_term(
        conn, tenant_id="default", standard_name="大床房", new_standard_name="大床房",
        aliases=["豪华大床房"], term_type="房型", product_line="示例产品线",
        extra_properties={"面积": "30"},
    )

    term = await get_term(conn, tenant_id="default", standard_name="大床房")
    assert term.extra_properties == {"面积": "30"}
    assert term.aliases == ["豪华大床房"]


async def test_update_term_rejects_genuinely_new_undeclared_key():
    """字段既不在 term_type 当前声明里，也从未在这条术语上出现过——不能因为
    "existing_extra_property_keys 放行"这条豁免被滥用成完全绕过校验。"""
    conn = await _connect()
    await create_term_type(conn, value="房型", extra_fields=[])
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
