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


async def test_ensure_terms_schema_without_seed_path_creates_empty_table():
    conn = await _connect()

    assert await list_terms(conn) == []


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
    seeded = await list_terms(conn)
    assert [t.standard_name for t in seeded] == ["种子术语"]

    # 再次调用（模拟第二次进程启动）：表已存在，即使 YAML 内容变了也不
    # 重新导入——只在首次建表时导入一次
    yaml_path.write_text(
        "terms:\n  - standard_name: 另一个术语\n    aliases: []\n"
        "    term_type: t\n    product_line: p\n",
        encoding="utf-8",
    )
    await ensure_terms_schema(conn, seed_yaml_path=yaml_path)
    after_second_call = await list_terms(conn)
    assert [t.standard_name for t in after_second_call] == ["种子术语"]


async def test_ensure_terms_schema_skips_seeding_when_yaml_path_missing(tmp_path):
    conn = await aiosqlite.connect(":memory:")
    missing_path = tmp_path / "does-not-exist.yaml"

    await ensure_terms_schema(conn, seed_yaml_path=missing_path)

    assert await list_terms(conn) == []


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
    term = await get_term(conn, "历史术语")
    assert term.extra_properties == {}


async def test_create_term_then_list_returns_it():
    conn = await _connect()

    await create_term(
        conn, standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code", product_line="核心平台",
    )

    terms = await list_terms(conn)
    assert terms == [
        Term(
            standard_name="错误码E502", aliases=["网关超时"],
            term_type="error_code", product_line="核心平台",
        )
    ]


async def test_create_term_rejects_duplicate_standard_name():
    conn = await _connect()
    await create_term(
        conn, standard_name="错误码E502", aliases=[],
        term_type="error_code", product_line="核心平台",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, standard_name="错误码E502", aliases=[],
            term_type="other", product_line="other",
        )


async def test_create_term_rejects_alias_that_collides_with_another_terms_standard_name():
    conn = await _connect()
    await create_term(
        conn, standard_name="登录模块", aliases=[],
        term_type="module", product_line="核心平台",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, standard_name="错误码E502", aliases=["登录模块"],
            term_type="error_code", product_line="核心平台",
        )


async def test_create_term_rejects_alias_that_collides_with_another_terms_alias():
    conn = await _connect()
    await create_term(
        conn, standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code", product_line="核心平台",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, standard_name="登录模块", aliases=["网关超时"],
            term_type="module", product_line="核心平台",
        )


async def test_get_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await get_term(conn, "不存在的术语")


async def test_update_term_without_rename_changes_fields_in_place():
    conn = await _connect()
    await create_term(
        conn, standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code", product_line="核心平台",
    )

    await update_term(
        conn, standard_name="错误码E502", new_standard_name="错误码E502",
        aliases=["网关超时", "502错误"], term_type="error_code", product_line="新产品线",
    )

    term = await get_term(conn, "错误码E502")
    assert term.aliases == ["网关超时", "502错误"]
    assert term.product_line == "新产品线"


async def test_update_term_with_rename_moves_to_new_standard_name():
    conn = await _connect()
    await create_term(
        conn, standard_name="旧名字", aliases=[],
        term_type="t", product_line="p",
    )

    await update_term(
        conn, standard_name="旧名字", new_standard_name="新名字",
        aliases=[], term_type="t", product_line="p",
    )

    with pytest.raises(TermNotFoundError):
        await get_term(conn, "旧名字")
    renamed = await get_term(conn, "新名字")
    assert renamed.standard_name == "新名字"


async def test_update_term_rejects_rename_into_an_existing_name():
    conn = await _connect()
    await create_term(conn, standard_name="A", aliases=[], term_type="t", product_line="p")
    await create_term(conn, standard_name="B", aliases=[], term_type="t", product_line="p")

    with pytest.raises(TermNameConflictError):
        await update_term(
            conn, standard_name="A", new_standard_name="B",
            aliases=[], term_type="t", product_line="p",
        )


async def test_update_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await update_term(
            conn, standard_name="不存在", new_standard_name="不存在",
            aliases=[], term_type="t", product_line="p",
        )


async def test_delete_term_removes_it():
    conn = await _connect()
    await create_term(conn, standard_name="待删除", aliases=[], term_type="t", product_line="p")

    await delete_term(conn, "待删除")

    assert await list_terms(conn) == []


async def test_delete_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await delete_term(conn, "不存在")


async def test_create_term_persists_extra_properties():
    conn = await _connect()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级"])
    await create_product_line(conn, value="示例产品线")

    await create_term(
        conn,
        standard_name="错误码E502",
        aliases=[],
        term_type="错误码",
        product_line="示例产品线",
        extra_properties={"严重等级": "高"},
    )

    term = await get_term(conn, "错误码E502")
    assert term.extra_properties == {"严重等级": "高"}


async def test_create_term_rejects_unknown_term_type():
    conn = await _connect()
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, standard_name="x", aliases=[], term_type="没有这个分类",
            product_line="示例产品线",
        )


async def test_create_term_rejects_unknown_product_line():
    conn = await _connect()
    await create_term_type(conn, value="错误码")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, standard_name="x", aliases=[], term_type="错误码",
            product_line="没有这个产品线",
        )


async def test_create_term_rejects_extra_property_not_declared_on_term_type():
    conn = await _connect()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级"])
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, standard_name="x", aliases=[], term_type="错误码",
            product_line="示例产品线", extra_properties={"没声明过的字段": "值"},
        )


async def test_removing_extra_field_from_term_type_preserves_existing_term_value():
    conn = await _connect()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级", "影响范围"])
    await create_product_line(conn, value="示例产品线")
    await create_term(
        conn, standard_name="错误码E502", aliases=[], term_type="错误码",
        product_line="示例产品线", extra_properties={"严重等级": "高", "影响范围": "全站不可用"},
    )

    await update_term_type(conn, value="错误码", new_value="错误码", extra_fields=["严重等级"])

    term = await get_term(conn, "错误码E502")
    assert term.extra_properties == {"严重等级": "高", "影响范围": "全站不可用"}
