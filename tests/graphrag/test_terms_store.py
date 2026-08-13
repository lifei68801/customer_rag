import aiosqlite
import pytest

from app.graphrag.ontology import Term
from app.graphrag.terms_store import (
    TermNameConflictError,
    TermNotFoundError,
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
