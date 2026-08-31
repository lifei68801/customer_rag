import aiosqlite
import pytest

from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import (
    create_term_type,
    list_term_types,
    update_term_type,
)
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag import terms_store
from app.graphrag.term_edits_store import (
    FIELD_CREATED,
    FIELD_DELETED,
    ensure_term_edits_schema,
    upsert_term_edit,
)
from app.graphrag.terms_store import (
    InvalidExtraPropertyTypeError,
    TermNameConflictError,
    TermNotFoundError,
    UnknownCategoryError,
    count_terms,
    create_term,
    delete_term,
    delete_terms_by_node_keys,
    ensure_terms_schema,
    get_term,
    get_term_by_node_key,
    get_term_merged_by_node_key,
    is_tombstoned,
    list_etl_node_keys_by_term_type,
    list_terms,
    list_terms_merged,
    merge_terms,
    migrate_term_type,
    update_term,
    upsert_term_with_node_key,
)


def test_term_dataclass_has_tenant_id_and_node_key():
    term = Term(
        tenant_id="t1", node_key="k1", standard_name="错误码E502",
        aliases=[], term_type="error_code",
    )
    assert term.tenant_id == "t1"
    assert term.node_key == "k1"


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    # confirm_ontology/checkout_draft 需要 tenant_relation_types/
    # term_type_relation_allowlist 等表存在——ensure_terms_schema 只建
    # ontology_term_types 一张分类表，这里补齐
    # 完整的本体生命周期表结构（幂等，与 ensure_categories_schema 不冲突）。
    await ensure_ontology_schema(conn)
    # round-1 计划已写的既有测试直接用这些字面量当 term_type，
    # 早于分类枚举表存在——这里补齐分类，保持既有测试的字面量不变
    # （新增测试自己会为各自用到的分类调用 create_term_type，
    # 不依赖这份预置，两者字面量不重叠）。
    await create_term_type(conn, tenant_id="default", value="error_code")
    await create_term_type(conn, tenant_id="default", value="module")
    await create_term_type(conn, tenant_id="default", value="other")
    await create_term_type(conn, tenant_id="default", value="t")
    # 真实术语只认已确认的实体类型（见 validate_term_categories），这里创建完就
    # 立刻确认，让共享 fixture 产出的类型对 create_term/update_term 可用。
    await confirm_ontology(conn, "default")
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


async def test_ensure_terms_schema_drops_legacy_product_line_column():
    """模拟一个已经是 tenant_id 新结构、但还带着 product_line 列的老库（
    本次改造前的真实状态），验证 ensure_terms_schema 会把这一列原地删掉，
    且不影响其余数据。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        """
        CREATE TABLE terms (
            tenant_id TEXT NOT NULL, node_key TEXT NOT NULL,
            standard_name TEXT NOT NULL, aliases TEXT NOT NULL,
            term_type TEXT NOT NULL, product_line TEXT NOT NULL,
            extra_properties TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (tenant_id, node_key)
        );
        """
    )
    await conn.execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
        "product_line, extra_properties) VALUES "
        "('t1', 'k1', 'n1', '[]', 'tt', 'pl', '{\"severity\": \"high\"}')"
    )
    await conn.commit()

    await ensure_terms_schema(conn)

    cursor = await conn.execute("PRAGMA table_info(terms)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert "product_line" not in columns
    term = await get_term(conn, "t1", "n1")
    assert term.standard_name == "n1"
    assert term.term_type == "tt"
    assert term.extra_properties == {"severity": "high"}


async def test_ensure_terms_schema_migration_is_idempotent():
    """重复调用 ensure_terms_schema 不应该报错、不应该重复迁移导致数据翻倍。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await _setup_default_categories(conn)
    await create_term(
        conn, tenant_id="default", standard_name="A", aliases=[],
        term_type="t",
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
        term_type="t",
    )
    await create_term(
        conn, tenant_id="tenant_b", standard_name="错误码E502", aliases=[],
        term_type="t",
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
        term_type="t",
    )
    original = await get_term(conn, tenant_id="t1", standard_name="错误码E502")

    await update_term(
        conn, tenant_id="t1", node_key=original.node_key,
        new_standard_name="错误码E502v2", aliases=[], term_type="t",
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
        term_type="t",
    )

    # 不应该抛 TermNameConflictError
    await create_term(
        conn, tenant_id="tenant_b", standard_name="登录模块", aliases=["认证模块"],
        term_type="t",
    )


async def test_delete_term_scoped_to_tenant():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await _setup_default_categories(conn)
    # Register categories for t1 tenant
    await create_term_type(conn, tenant_id="t1", value="t")
    await confirm_ontology(conn, "t1")
    await create_term(
        conn, tenant_id="t1", standard_name="待删除", aliases=[], term_type="t",
    )
    to_delete = await get_term(conn, tenant_id="t1", standard_name="待删除")

    await delete_term(conn, "t1", to_delete.node_key)

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
        "    term_type: type1\n",
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
        "    term_type: t\n",
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


async def test_ensure_terms_schema_bridges_historical_term_type_into_categories():
    """向后兼容桥接的回归测试：老版本上线时 term_type 还是自由文本，
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
        "term_type TEXT NOT NULL"
        ");"
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, aliases, term_type) "
        "VALUES (?, ?, ?)",
        ("历史术语", "[]", "error_code"),
    )
    await conn.commit()

    await ensure_terms_schema(conn)

    term_type_values = {
        t.value for t in await list_term_types(conn, tenant_id="default", status="confirmed")
    }
    assert "error_code" in term_type_values
    # 历史行本身也要能正常读出来——extra_properties 是后补的列，历史行没有
    # 写过这个值，读出来应该是默认的空字典，而不是报错或缺列。
    term = await get_term(conn, tenant_id="default", standard_name="历史术语")
    assert term.extra_properties == {}


async def test_create_term_then_list_returns_it():
    conn = await _connect()

    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code",
    )

    terms = await list_terms(conn, tenant_id="default")
    assert len(terms) == 1
    assert terms[0].tenant_id == "default"
    assert terms[0].standard_name == "错误码E502"
    assert terms[0].aliases == ["网关超时"]
    assert terms[0].term_type == "error_code"


async def test_create_term_rejects_duplicate_standard_name():
    """2026-08-22 起，standard_name 唯一性收窄到"同一 term_type 内"——
    这里两次 create_term 都用同一个 term_type，验证同类型内仍然拒绝重复。
    跨类型重名允许共存，见
    test_create_term_allows_same_standard_name_across_different_types。"""
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=[],
        term_type="error_code",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, tenant_id="default", standard_name="错误码E502", aliases=[],
            term_type="error_code",
        )


async def test_create_term_rejects_alias_that_collides_with_another_terms_standard_name():
    """同类型内 alias 与另一术语的 standard_name 冲突仍然被拒绝——两次
    create_term 用同一个 term_type（module），见上面那条测试的说明。"""
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="登录模块", aliases=[],
        term_type="module",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, tenant_id="default", standard_name="错误码E502", aliases=["登录模块"],
            term_type="module",
        )


async def test_create_term_rejects_alias_that_collides_with_another_terms_alias():
    """同类型内 alias 与另一术语的 alias 冲突仍然被拒绝——两次 create_term
    用同一个 term_type（error_code），见上面那条测试的说明。"""
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, tenant_id="default", standard_name="登录模块", aliases=["网关超时"],
            term_type="error_code",
        )


async def test_get_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await get_term(conn, tenant_id="default", standard_name="不存在的术语")


async def test_update_term_without_rename_changes_fields_in_place():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code",
    )
    existing = await get_term(conn, tenant_id="default", standard_name="错误码E502")

    await update_term(
        conn, tenant_id="default", node_key=existing.node_key, new_standard_name="错误码E502",
        aliases=["网关超时", "502错误"], term_type="error_code",
    )

    term = await get_term(conn, tenant_id="default", standard_name="错误码E502")
    assert term.aliases == ["网关超时", "502错误"]


async def test_update_term_with_rename_moves_to_new_standard_name():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="旧名字", aliases=[],
        term_type="t",
    )
    existing = await get_term(conn, tenant_id="default", standard_name="旧名字")

    await update_term(
        conn, tenant_id="default", node_key=existing.node_key, new_standard_name="新名字",
        aliases=[], term_type="t",
    )

    with pytest.raises(TermNotFoundError):
        await get_term(conn, tenant_id="default", standard_name="旧名字")
    renamed = await get_term(conn, tenant_id="default", standard_name="新名字")
    assert renamed.standard_name == "新名字"


async def test_update_term_rejects_rename_into_an_existing_name():
    conn = await _connect()
    await create_term(conn, tenant_id="default", standard_name="A", aliases=[], term_type="t")
    await create_term(conn, tenant_id="default", standard_name="B", aliases=[], term_type="t")
    term_a = await get_term(conn, tenant_id="default", standard_name="A")

    with pytest.raises(TermNameConflictError):
        await update_term(
            conn, tenant_id="default", node_key=term_a.node_key, new_standard_name="B",
            aliases=[], term_type="t",
        )


async def test_update_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await update_term(
            conn, tenant_id="default", node_key="不存在", new_standard_name="不存在",
            aliases=[], term_type="t",
        )


async def test_delete_term_removes_it():
    conn = await _connect()
    await create_term(conn, tenant_id="default", standard_name="待删除", aliases=[], term_type="t")
    to_delete = await get_term(conn, tenant_id="default", standard_name="待删除")

    await delete_term(conn, "default", to_delete.node_key)

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

    await create_term(
        conn,
        tenant_id="default",
        standard_name="错误码E502",
        aliases=[],
        term_type="错误码",
        extra_properties={"severity_level": "高"},
    )

    term = await get_term(conn, tenant_id="default", standard_name="错误码E502")
    assert term.extra_properties == {"severity_level": "高"}


async def test_create_term_rejects_unknown_term_type():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="default", standard_name="x", aliases=[], term_type="没有这个分类",
        )


async def test_create_term_rejects_extra_property_not_declared_on_term_type():
    from app.graphrag.ontology_categories import ExtraFieldSpec
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="错误码", extra_fields=[ExtraFieldSpec(name="severity_level", value_type="string")])
    await confirm_ontology(conn, "default")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="default", standard_name="x", aliases=[], term_type="错误码",
            extra_properties={"没声明过的字段": "值"},
        )


async def test_removing_extra_field_from_term_type_preserves_existing_term_value():
    from app.graphrag.ontology_categories import ExtraFieldSpec
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="错误码", extra_fields=[ExtraFieldSpec(name="severity_level", value_type="string"), ExtraFieldSpec(name="impact_scope", value_type="string")])
    await confirm_ontology(conn, "default")
    await create_term(
        conn, tenant_id="default", standard_name="错误码E502", aliases=[], term_type="错误码",
        extra_properties={"severity_level": "高", "impact_scope": "全站不可用"},
    )

    # update_term_type 只操作草稿行，确认之后草稿已清空，需要先检出一份新草稿
    await checkout_draft(conn, "default")
    await update_term_type(conn, tenant_id="default", value="错误码", new_value="错误码", extra_fields=[ExtraFieldSpec(name="severity_level", value_type="string")])

    term = await get_term(conn, tenant_id="default", standard_name="错误码E502")
    assert term.extra_properties == {"severity_level": "高", "impact_scope": "全站不可用"}


async def test_update_term_resubmitting_undeclared_but_already_stored_key_succeeds():
    """回归测试：字段从 term_type 里被去掉之后，重新保存这条术语（哪怕值原样
    不改）不能因为这个字段"未声明"而被拒绝或静默丢弃——见
    validate_term_categories 的 existing_extra_property_keys 参数说明。"""
    from app.graphrag.ontology_categories import ExtraFieldSpec
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="房型", extra_fields=[ExtraFieldSpec(name="area", value_type="string")])
    await confirm_ontology(conn, "default")
    await create_term(
        conn, tenant_id="default", standard_name="大床房", aliases=[], term_type="房型",
        extra_properties={"area": "30"},
    )
    existing = await get_term(conn, tenant_id="default", standard_name="大床房")

    # 业务把"area"从房型的声明字段里移除——update_term_type 只操作草稿行，
    # 确认之后草稿已清空，需要先检出一份新草稿；改完再确认一次，让
    # 下面 update_term 的 validate_term_categories（查已确认声明）真正看到
    # "area 已不再声明"这个状态，否则测的就不是这里的豁免逻辑了。
    await checkout_draft(conn, "default")
    await update_term_type(conn, tenant_id="default", value="房型", new_value="房型", extra_fields=[])
    await confirm_ontology(conn, "default")

    # 重新保存这条术语，提交里仍然带着这个已经被去掉声明的字段——不应该报错
    await update_term(
        conn, tenant_id="default", node_key=existing.node_key, new_standard_name="大床房",
        aliases=["豪华大床房"], term_type="房型",
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
    await create_term(
        conn, tenant_id="default", standard_name="大床房", aliases=[], term_type="房型",
        extra_properties={},
    )
    existing = await get_term(conn, tenant_id="default", standard_name="大床房")

    with pytest.raises(UnknownCategoryError):
        await update_term(
            conn, tenant_id="default", node_key=existing.node_key, new_standard_name="大床房",
            aliases=[], term_type="房型",
            extra_properties={"从未出现过的字段": "值"},
        )


async def test_validate_term_categories_rejects_term_type_from_another_tenant():
    """term_type 校验闭环之后必须按租户过滤——tenant_a 注册的分类，
    tenant_b 提交同名 term_type 应该被拒绝（对 tenant_b 而言这是未知分类）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await create_term_type(conn, tenant_id="tenant_a", value="错误码")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="tenant_b", standard_name="X", aliases=[],
            term_type="错误码",
        )


async def test_create_term_with_typed_extra_properties():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[
            ExtraFieldSpec(name="numeric_value", value_type="number"),
            ExtraFieldSpec(name="dims", value_type="number[]"),
        ],
    )
    await confirm_ontology(conn, "t1")

    await create_term(
        conn, tenant_id="t1", standard_name="容量750ml", aliases=[],
        term_type="VariantValue",
        extra_properties={"numeric_value": 750, "dims": [20.5, 10.0]},
    )

    term = await get_term(conn, tenant_id="t1", standard_name="容量750ml")
    assert term.extra_properties == {"numeric_value": 750, "dims": [20.5, 10.0]}


async def test_create_term_rejects_extra_property_with_wrong_type():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await confirm_ontology(conn, "t1")

    with pytest.raises(InvalidExtraPropertyTypeError):
        await create_term(
            conn, tenant_id="t1", standard_name="容量750ml", aliases=[],
            term_type="VariantValue",
            extra_properties={"numeric_value": "不是数字"},
        )


async def test_create_term_rejects_bool_as_number():
    """bool 是 int 的子类，必须显式排除——见 Global Constraints。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await confirm_ontology(conn, "t1")

    with pytest.raises(InvalidExtraPropertyTypeError):
        await create_term(
            conn, tenant_id="t1", standard_name="X", aliases=[],
            term_type="VariantValue",
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
        ExtraFieldSpec, create_term_type, update_term_type,
    )
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await confirm_ontology(conn, "t1")
    await create_term(
        conn, tenant_id="t1", standard_name="X", aliases=[],
        term_type="VariantValue",
        extra_properties={"numeric_value": 750},
    )
    existing = await get_term(conn, tenant_id="t1", standard_name="X")
    await checkout_draft(conn, "t1")
    await update_term_type(
        conn, tenant_id="t1", value="VariantValue", new_value="VariantValue",
        extra_fields=[],
    )
    await confirm_ontology(conn, "t1")

    # 不应该抛 InvalidExtraPropertyTypeError 或 UnknownCategoryError
    await update_term(
        conn, tenant_id="t1", node_key=existing.node_key, new_standard_name="X",
        aliases=[], term_type="VariantValue",
        extra_properties={"numeric_value": 750},
    )


async def test_upsert_term_with_node_key_creates_new_row():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import create_term_type
    await create_term_type(conn, tenant_id="muji", value="Product")
    await confirm_ontology(conn, "muji")

    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒",
        aliases=[], term_type="Product",
    )

    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term.node_key == "Product:1001"


async def test_upsert_term_with_node_key_updates_existing_row_by_node_key():
    """再次 upsert 同一个 node_key、standard_name 变了——更新而不是报冲突，
    这是 upsert 和 create_term 的本质区别（见 terms_store.py 里的说明）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import create_term_type
    await create_term_type(conn, tenant_id="muji", value="Product")
    await confirm_ontology(conn, "muji")
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒",
        aliases=[], term_type="Product",
    )

    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒(新装)",
        aliases=[], term_type="Product",
    )

    all_terms = await list_terms(conn, tenant_id="muji")
    assert len(all_terms) == 1
    assert all_terms[0].standard_name == "圆角收纳盒(新装)"
    assert all_terms[0].node_key == "Product:1001"


async def test_upsert_term_with_node_key_allows_duplicate_standard_name_different_node_key():
    """2026-08-30 起 standard_name 的唯一索引降级为普通索引，唯一性下沉到
    node_key——本用例原先断言第二次 upsert 会因为 standard_name 撞车抛
    TermNameConflictError，那正是本任务要拿掉的行为（复合 node_key 场景下，
    两个真实存在、同名的不同实体必须都能落库，见
    docs/superpowers/specs/2026-08-30-name-uniqueness-to-node-key-design.md）。
    改成断言两条 node_key 不同的记录都成功写入。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import create_term_type
    await create_term_type(conn, tenant_id="muji", value="Product")
    await confirm_ontology(conn, "muji")
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒",
        aliases=[], term_type="Product",
    )

    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1002", standard_name="圆角收纳盒",
        aliases=[], term_type="Product",
    )

    all_terms = await list_terms(conn, tenant_id="muji")
    same_name = [t for t in all_terms if t.standard_name == "圆角收纳盒"]
    assert {t.node_key for t in same_name} == {"Product:1001", "Product:1002"}


async def test_upsert_term_with_node_key_typed_extra_properties():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    await create_term_type(
        conn, tenant_id="muji", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await confirm_ontology(conn, "muji")

    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Variant:dim_007:00001", standard_name="抹茶",
        aliases=[], term_type="VariantValue",
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
            term_type="t",
        )

    page = await list_terms(conn, "default", limit=1, offset=1)

    assert [t.standard_name for t in page] == ["B"]


async def test_count_terms_returns_total_regardless_of_pagination():
    conn = await _connect()
    for name in ("A", "B", "C"):
        await create_term(
            conn, tenant_id="default", standard_name=name, aliases=[],
            term_type="t",
        )

    total = await count_terms(conn, "default")

    assert total == 3


async def test_list_terms_without_limit_offset_returns_full_unpaginated_list():
    """不传 limit/offset 时必须保持改造前的行为：返回该租户全部术语，
    这是既有调用方（agent 检索、摄取管线、eval runner、review_cli 等）
    赖以不变的默认行为——见 app/api/agent_routes.py 等处直接调用本函数
    的调用方式。"""
    conn = await _connect()
    for name in ("A", "B", "C"):
        await create_term(
            conn, tenant_id="default", standard_name=name, aliases=[],
            term_type="t",
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
        ExtraFieldSpec, create_term_type, update_term_type,
    )
    await create_term_type(
        conn, tenant_id="muji", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await confirm_ontology(conn, "muji")
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Variant:dim_007:00001", standard_name="抹茶",
        aliases=[], term_type="VariantValue",
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
        aliases=[], term_type="VariantValue",
        extra_properties={"numeric_value": 70},
    )


async def test_migrate_term_type_updates_matching_rows_and_returns_affected_count():
    conn = await _connect()
    for name in ("A", "B"):
        await create_term(
            conn, tenant_id="default", standard_name=name, aliases=[],
            term_type="t",
        )
    # 不应该被迁移到的另一个租户的同名旧类型行——验证按租户隔离
    await create_term_type(conn, tenant_id="other-tenant", value="t")
    await confirm_ontology(conn, "other-tenant")
    await create_term(
        conn, tenant_id="other-tenant", standard_name="C", aliases=[],
        term_type="t",
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
        term_type="t",
    )

    affected = await migrate_term_type(conn, "default", old_type="不存在的类型", new_type="t2")

    assert affected == 0
    terms = await list_terms(conn, "default")
    assert terms[0].term_type == "t"


async def test_create_term_defaults_source_to_manual():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="term-a", aliases=[],
        term_type="t",
    )
    term = await get_term(conn, "default", "term-a")
    assert term.source == "manual"


async def test_create_term_explicit_source():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="term-b", aliases=[],
        term_type="t", source="review",
    )
    term = await get_term(conn, "default", "term-b")
    assert term.source == "review"


async def test_upsert_term_with_node_key_defaults_source_to_etl():
    conn = await _connect()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="k1", standard_name="term-c", aliases=[],
        term_type="t",
    )
    term = await get_term(conn, "default", "term-c")
    assert term.source == "etl"


async def test_update_term_does_not_change_source():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="term-d", aliases=[],
        term_type="t", source="etl",
    )
    existing = await get_term(conn, "default", "term-d")
    await update_term(
        conn, tenant_id="default", node_key=existing.node_key, new_standard_name="term-d-renamed",
        aliases=["alias"], term_type="t",
    )
    term = await get_term(conn, "default", "term-d-renamed")
    assert term.source == "etl"


async def test_list_terms_filters_by_source():
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="m1", aliases=[],
        term_type="t", source="manual",
    )
    await create_term(
        conn, tenant_id="default", standard_name="e1", aliases=[],
        term_type="t", source="etl",
    )
    manual_only = await list_terms(conn, "default", source="manual")
    assert [t.standard_name for t in manual_only] == ["m1"]


async def test_migrate_standard_name_index_to_type_scoped_widens_old_two_column_index():
    """模拟一个还是老的两列索引（tenant_id, standard_name）的库，跑
    ensure_terms_schema 迁移之后，索引应该变成三列（tenant_id, term_type,
    standard_name），且旧数据不丢、也不需要重新迁移旧数据本身（索引变化
    不影响已有行的可读性，只影响新写入/新冲突判定的范围）。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        """
        CREATE TABLE terms (
            tenant_id         TEXT NOT NULL,
            node_key          TEXT NOT NULL,
            standard_name     TEXT NOT NULL,
            aliases           TEXT NOT NULL,
            term_type         TEXT NOT NULL,
            extra_properties  TEXT NOT NULL DEFAULT '{}',
            source            TEXT NOT NULL DEFAULT 'unknown',
            PRIMARY KEY (tenant_id, node_key)
        );
        CREATE UNIQUE INDEX idx_terms_tenant_standard_name
            ON terms(tenant_id, standard_name);
        """
    )
    await conn.execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
        "extra_properties, source) VALUES ('t1', '产品:Coffee', 'Coffee', '[]', '产品', '{}', 'etl')"
    )
    await conn.commit()

    await ensure_terms_schema(conn)

    cursor = await conn.execute("PRAGMA index_info('idx_terms_tenant_standard_name')")
    columns = await cursor.fetchall()
    assert [c[2] for c in columns] == ["tenant_id", "term_type", "standard_name"]
    terms = await list_terms(conn, tenant_id="t1")
    assert len(terms) == 1
    assert terms[0].standard_name == "Coffee"


async def test_migrate_standard_name_index_is_idempotent_when_already_type_scoped():
    """已经是三列索引的库，迁移函数直接跳过，不报错、不重建索引。"""
    conn = await _connect()
    await ensure_terms_schema(conn)  # 第一次调用已经建成三列索引

    await ensure_terms_schema(conn)  # 第二次调用应该是无操作的幂等跳过

    cursor = await conn.execute("PRAGMA index_info('idx_terms_tenant_standard_name')")
    columns = await cursor.fetchall()
    assert [c[2] for c in columns] == ["tenant_id", "term_type", "standard_name"]


async def _connect_t1_with_product_category_types() -> aiosqlite.Connection:
    """给"跨类型重名"测试组专用的建库辅助：注册 tenant_id='t1' 下的
    "产品"/"类目" 两个分类并确认，供 create_term/update_term 的分类校验
    通过（这两个分类字面量是这批测试专用的，不与 _connect() 预置的
    error_code/module/other/t 重叠）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await create_term_type(conn, tenant_id="t1", value="产品")
    await create_term_type(conn, tenant_id="t1", value="类目")
    await confirm_ontology(conn, "t1")
    return conn


async def test_create_term_allows_same_standard_name_across_different_types():
    conn = await _connect_t1_with_product_category_types()
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="产品")

    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="类目")

    terms = await list_terms(conn, tenant_id="t1")
    assert {t.term_type for t in terms} == {"产品", "类目"}
    assert all(t.standard_name == "Coffee" for t in terms)


async def test_create_term_rejects_same_standard_name_within_same_type():
    conn = await _connect_t1_with_product_category_types()
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="产品")

    with pytest.raises(TermNameConflictError):
        await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="产品")


async def test_create_term_rejects_alias_matching_another_term_of_same_type():
    conn = await _connect_t1_with_product_category_types()
    await create_term(conn, tenant_id="t1", standard_name="拿铁", aliases=["Coffee"], term_type="产品")

    with pytest.raises(TermNameConflictError):
        await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="产品")


async def test_create_term_allows_alias_matching_another_term_of_different_type():
    conn = await _connect_t1_with_product_category_types()
    await create_term(conn, tenant_id="t1", standard_name="拿铁", aliases=["Coffee"], term_type="产品")

    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="类目")

    terms = await list_terms(conn, tenant_id="t1")
    assert len(terms) == 2


async def test_create_term_generates_type_prefixed_node_key():
    conn = await _connect_t1_with_product_category_types()
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="产品")

    term = await get_term(conn, "t1", "Coffee", "产品")
    assert term.node_key == "产品:Coffee"


async def test_get_term_disambiguates_by_term_type():
    conn = await _connect_t1_with_product_category_types()
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="产品")
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="类目")

    product = await get_term(conn, "t1", "Coffee", "产品")
    category = await get_term(conn, "t1", "Coffee", "类目")

    assert product.node_key == "产品:Coffee"
    assert category.node_key == "类目:Coffee"


async def test_delete_term_removes_only_the_matching_type_not_the_other():
    """回归测试：这是本任务里最关键的一条——delete_term 底层 DELETE 语句
    以前只按 standard_name 过滤，一旦允许跨类型重名会连另一个同名但不同
    类型的术语一起删掉。"""
    conn = await _connect_t1_with_product_category_types()
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="产品")
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="类目")
    product = await get_term(conn, "t1", "Coffee", "产品")

    await delete_term(conn, "t1", product.node_key)

    remaining = await list_terms(conn, tenant_id="t1")
    assert len(remaining) == 1
    assert remaining[0].term_type == "类目"


async def test_update_term_rename_does_not_conflict_with_other_type_same_name():
    conn = await _connect_t1_with_product_category_types()
    await create_term(conn, tenant_id="t1", standard_name="拿铁", aliases=[], term_type="产品")
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="类目")
    latte = await get_term(conn, "t1", "拿铁", "产品")

    await update_term(
        conn, tenant_id="t1", node_key=latte.node_key, new_standard_name="Coffee",
        aliases=[], term_type="产品",
    )

    product = await get_term(conn, "t1", "Coffee", "产品")
    category = await get_term(conn, "t1", "Coffee", "类目")
    # node_key 创建时固定，改名不变（ADR-0003）——创建时 term_type="产品"、
    # standard_name="拿铁"，所以 node_key 是 "产品:拿铁"（Step 10 的
    # f"{term_type}:{standard_name}" 前缀规则），不随后续改名变化。
    assert product.node_key == "产品:拿铁"
    assert category.standard_name == "Coffee"


async def test_update_term_rename_still_rejects_conflict_within_same_type():
    conn = await _connect_t1_with_product_category_types()
    await create_term(conn, tenant_id="t1", standard_name="拿铁", aliases=[], term_type="产品")
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="产品")
    latte = await get_term(conn, "t1", "拿铁", "产品")

    with pytest.raises(TermNameConflictError):
        await update_term(
            conn, tenant_id="t1", node_key=latte.node_key, new_standard_name="Coffee",
            aliases=[], term_type="产品",
        )


async def test_update_term_rename_and_retype_in_one_call_still_detects_conflict_with_unrelated_term():
    """2026-08-23 C1 回归测试：编辑动作在同一次调用里既改名又改类型时（管理
    后台编辑表单里 term_type 是一个下拉选择框，这是一次正常操作），
    _check_name_conflict 以前按"旧名字"排除自己——但 standard_name 现在不再
    租户内全局唯一，如果目标类型下恰好已经存在一个同名（等于被编辑术语的
    旧名字）但完全无关的术语，按名字排除会连它一起误伤，冲突检查被静默
    跳过。

    产品"Coffee"（别名"拿铁"）被编辑：改名"Coffee"->"拿铁"、改类型
    产品->类目，同时把旧名字"Coffee"留作新别名——这会让它在目标类型"类目"
    下产生一个别名"Coffee"，跟已经存在、完全无关的类目"Coffee"撞名，
    必须被拒绝，而不是静默写成两条都在"类目"类型下、一个标准名一个别名
    都叫"Coffee"的重复状态。
    """
    conn = await _connect_t1_with_product_category_types()
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=["拿铁"], term_type="产品")
    await create_term(conn, tenant_id="t1", standard_name="Coffee", aliases=[], term_type="类目")
    product_coffee = await get_term(conn, "t1", "Coffee", "产品")

    with pytest.raises(TermNameConflictError):
        await update_term(
            conn, tenant_id="t1", node_key=product_coffee.node_key, new_standard_name="拿铁",
            aliases=["Coffee"], term_type="类目",
        )

    # 冲突检查必须发生在写入之前——两条记录都应该保持编辑前的原状。
    product = await get_term(conn, "t1", "Coffee", "产品")
    category = await get_term(conn, "t1", "Coffee", "类目")
    assert product.aliases == ["拿铁"]
    assert category.term_type == "类目"


# merge_terms：把"合并两条术语"这个领域操作从 app/graphrag/duplicate_
# review_queue.py 收进 Term 仓储自己——那边只负责查行+调用+标记 approved，
# 墓碑命名/别名并集/失败补偿这些安全语义都在这里，用真实的 update_term()
# 测（不再需要一个 fake terms_module 去避免建 terms 表）。


async def test_merge_terms_tombstones_merged_and_appends_aliases_onto_keeper():
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="公司")
    await confirm_ontology(conn, "default")
    await create_term(
        conn, tenant_id="default", standard_name="Coca-Cola", aliases=["coke"], term_type="公司",
    )
    await create_term(
        conn, tenant_id="default", standard_name="可口可乐", aliases=["可乐公司"], term_type="公司",
    )

    await merge_terms(
        conn, tenant_id="default",
        keep_node_key="公司:Coca-Cola", merged_node_key="公司:可口可乐",
    )

    keeper = await get_term(conn, "default", "Coca-Cola", "公司")
    assert set(keeper.aliases) == {"coke", "可口可乐", "可乐公司"}

    all_terms = await list_terms(conn, tenant_id="default")
    merged_row = next(t for t in all_terms if t.node_key == "公司:可口可乐")
    assert is_tombstoned(merged_row)
    assert merged_row.standard_name == "[已合并] 公司:可口可乐"
    assert merged_row.aliases == []


async def test_merge_terms_raises_term_not_found_for_unknown_node_key():
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="公司")
    await confirm_ontology(conn, "default")
    await create_term(conn, tenant_id="default", standard_name="Coca-Cola", aliases=[], term_type="公司")

    with pytest.raises(TermNotFoundError):
        await merge_terms(
            conn, tenant_id="default",
            keep_node_key="公司:Coca-Cola", merged_node_key="公司:不存在",
        )


async def _insert_term_row_bypassing_conflict_check(
    conn: aiosqlite.Connection, *, tenant_id: str, node_key: str,
    standard_name: str, aliases: list[str], term_type: str,
) -> None:
    """直接往 terms 表插行，绕开 create_term() 的 _check_name_conflict——
    用来构造"两条术语已经共享同一个别名"这种正常创建路径本来无法到达的
    状态（真实场景类似 ETL 写入路径绕开了通常的冲突检查），验证 merge_terms
    在这种状态下遇到真实冲突时的处理是否正确。跟
    tests/api/test_admin_duplicate_review_routes.py::_seed_terms 是同一个
    思路。"""
    import json as _json
    await conn.execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
        "extra_properties, source) VALUES (?, ?, ?, ?, ?, '{}', 'manual')",
        (tenant_id, node_key, standard_name, _json.dumps(aliases, ensure_ascii=False), term_type),
    )
    await conn.commit()


async def test_merge_terms_restores_merged_term_when_append_step_fails(monkeypatch):
    """追加别名这一步（Step 2）失败时，merged 那条应该被恢复成合并前的
    原状，不是停留在墓碑状态。用 monkeypatch 只在第二次 update_term 调用
    （追加那一步）注入一次性失败，第三次调用（补偿恢复）用真实的
    update_term 正常执行——验证补偿本身在"没有更深层冲突"的普通失败场景
    下确实能成功恢复。（"一个真实的同名冲突，恰好连补偿恢复也会撞上同一个
    冲突"这种更刁钻的场景，见下面 test_merge_terms_logs_and_reraises_
    original_error_when_compensation_also_fails——那个测试顺带也验证了
    TermNameConflictError 真的会从一次真实冲突里传播出来，这里不重复
    构造一次。）"""
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="公司")
    await confirm_ontology(conn, "default")
    await create_term(
        conn, tenant_id="default", standard_name="Coca-Cola", aliases=[], term_type="公司",
    )
    await create_term(
        conn, tenant_id="default", standard_name="可口可乐股份", aliases=["可乐"], term_type="公司",
    )

    real_update_term = terms_store.update_term
    call_count = {"n": 0}

    async def _fail_on_second_call(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated transient failure on append step")
        return await real_update_term(*args, **kwargs)

    monkeypatch.setattr(terms_store, "update_term", _fail_on_second_call)

    with pytest.raises(RuntimeError, match="simulated transient failure"):
        await merge_terms(
            conn, tenant_id="default",
            keep_node_key="公司:Coca-Cola", merged_node_key="公司:可口可乐股份",
        )

    assert call_count["n"] == 3
    # merged 那条应该被恢复成合并前的原状，不是停留在墓碑状态。
    merged_row = await get_term(conn, "default", "可口可乐股份", "公司")
    assert not is_tombstoned(merged_row)
    assert merged_row.aliases == ["可乐"]
    # keeper 完全没有被追加成功（Step 2 一开始就失败了）。
    keeper = await get_term(conn, "default", "Coca-Cola", "公司")
    assert keeper.aliases == []


async def test_merge_terms_logs_and_reraises_original_error_when_compensation_also_fails(
    monkeypatch, caplog,
):
    """恢复 merged 那条（补偿写入）本身也可能失败——比如恢复目标名字这期间
    被并发写入抢占。用 monkeypatch 让 update_term 在第三次调用（补偿那次）
    时额外抛出，验证：(a) 调用方看到的是触发整条回滚路径的原始异常，不是
    补偿失败的异常；(b) 有一条 ERROR 日志留下人工核对所需的全部线索。"""
    conn = await _connect()
    await create_term_type(conn, tenant_id="default", value="公司")
    await confirm_ontology(conn, "default")
    await create_term(
        conn, tenant_id="default", standard_name="Coca-Cola", aliases=[], term_type="公司",
    )
    await _insert_term_row_bypassing_conflict_check(
        conn, tenant_id="default", node_key="公司:可口可乐股份",
        standard_name="可口可乐股份", aliases=["可乐"], term_type="公司",
    )
    await _insert_term_row_bypassing_conflict_check(
        conn, tenant_id="default", node_key="公司:某第三方公司",
        standard_name="某第三方公司", aliases=["可乐"], term_type="公司",
    )

    real_update_term = terms_store.update_term
    call_count = {"n": 0}

    async def _flaky_update_term(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated concurrent write stole the name back")
        return await real_update_term(*args, **kwargs)

    monkeypatch.setattr(terms_store, "update_term", _flaky_update_term)

    with caplog.at_level("ERROR", logger="app.graphrag.terms_store"):
        with pytest.raises(TermNameConflictError):
            await merge_terms(
                conn, tenant_id="default",
                keep_node_key="公司:Coca-Cola", merged_node_key="公司:可口可乐股份",
            )

    assert call_count["n"] == 3
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    message = error_records[0].getMessage()
    assert "default" in message
    assert "可口可乐股份" in message
    assert "可乐" in message
    assert "simulated concurrent write stole the name back" in message


async def test_same_standard_name_allowed_when_node_key_differs():
    """同 term_type 下同名不同 node_key 必须都能写进去。

    这是 2026-08-30 那次事故的直接复现：用户名的复合节点键
    (姓名+邮编) 让两个 William Jackson 有了不同的 node_key，但
    standard_name 都是 "William Jackson"，第二条被唯一索引拒掉，
    10000 行只落库 9335 条，665 笔订单因此失去客户边。
    """
    conn = await _connect()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:William Jackson:72848",
        standard_name="William Jackson", aliases=[], term_type="t",
        extra_properties={},
    )
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:William Jackson:68046",
        standard_name="William Jackson", aliases=[], term_type="t",
        extra_properties={},
    )

    terms = await list_terms(conn, "default")
    same_name = [t for t in terms if t.standard_name == "William Jackson"]
    assert {t.node_key for t in same_name} == {
        "t:William Jackson:72848", "t:William Jackson:68046",
    }


async def test_manual_create_still_rejects_a_duplicate_name():
    """人工录入路径仍然拦重名——数据库不再兜底之后这层策略检查更重要。

    人手工敲进一个已存在的名字，绝大多数是笔误而不是"我确实要建一个
    同名的不同实体"。
    """
    conn = await _connect()
    await create_term(
        conn, tenant_id="default", standard_name="重复的名字", aliases=[], term_type="t",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, tenant_id="default", standard_name="重复的名字", aliases=[], term_type="t",
        )


async def test_get_term_by_node_key_picks_the_right_one_among_same_names():
    """同名多条时，按 node_key 能精确定位——按名字则不能。"""
    conn = await _connect()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:张三:100", standard_name="张三",
        aliases=[], term_type="t", extra_properties={},
    )
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:张三:200", standard_name="张三",
        aliases=[], term_type="t", extra_properties={},
    )

    term = await get_term_by_node_key(conn, "default", "t:张三:200")

    assert term.node_key == "t:张三:200"


async def test_get_term_by_node_key_raises_when_absent():
    conn = await _connect()
    with pytest.raises(TermNotFoundError):
        await get_term_by_node_key(conn, "default", "t:不存在:000")


async def test_update_term_by_node_key_only_touches_the_matching_row_among_same_names():
    """回归测试（2026-08-30 终审缺陷）：同一 tenant + 同一 term_type 下存在两条
    同名术语（node_key 不同）时，update_term 必须精确按 node_key 定位，只改
    命中的那一条，另一条必须纹丝未动。旧实现按 standard_name 用 fetchone()
    把记录查回来，命中哪条由 SQLite 内部行序决定，管理员编辑 A 有可能改到
    B——见 get_term 的同名参数说明和 get_term_by_node_key 的引入动机。"""
    conn = await _connect()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:张三:100", standard_name="张三",
        aliases=["老张"], term_type="t", extra_properties={},
    )
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:张三:200", standard_name="张三",
        aliases=["小张"], term_type="t", extra_properties={},
    )

    await update_term(
        conn, tenant_id="default", node_key="t:张三:100",
        new_standard_name="张三改名了", aliases=["老张"], term_type="t",
    )

    updated = await get_term_by_node_key(conn, "default", "t:张三:100")
    untouched = await get_term_by_node_key(conn, "default", "t:张三:200")
    assert updated.standard_name == "张三改名了"
    assert updated.aliases == ["老张"]
    assert untouched.standard_name == "张三"
    assert untouched.aliases == ["小张"]


async def test_delete_term_by_node_key_only_deletes_the_matching_row_among_same_names():
    """回归测试（2026-08-30 终审缺陷）：同名多条时，delete_term 必须精确按
    node_key 定位删除，不能误删另一条同名记录。"""
    conn = await _connect()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:李四:100", standard_name="李四",
        aliases=[], term_type="t", extra_properties={},
    )
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:李四:200", standard_name="李四",
        aliases=[], term_type="t", extra_properties={},
    )

    await delete_term(conn, "default", "t:李四:100")

    with pytest.raises(TermNotFoundError):
        await get_term_by_node_key(conn, "default", "t:李四:100")
    remaining = await get_term_by_node_key(conn, "default", "t:李四:200")
    assert remaining.standard_name == "李四"


async def test_list_etl_node_keys_by_term_type_excludes_manual_and_review_rows():
    """sweep 只能扫 ETL 自己写进来的行。审核界面创建的（source='review'）和
    管理后台手工录入的（source='manual'）从来就不来自这个数据源，"源里没有"
    对它们不成立，扫掉它们是数据丢失。"""
    conn = await _connect()
    # _connect() 只预置了 tenant "default" 下的几个分类字面量；这里用的
    # tenant "t1" + term_type "产品" 是新组合，validate_term_categories 要求
    # 分类必须已注册并确认，先补齐。
    await create_term_type(conn, tenant_id="t1", value="产品")
    await confirm_ontology(conn, "t1")
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="产品:A", standard_name="A",
        aliases=[], term_type="产品", extra_properties={}, source="etl",
    )
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="产品:B", standard_name="B",
        aliases=[], term_type="产品", extra_properties={}, source="review",
    )
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="产品:C", standard_name="C",
        aliases=[], term_type="产品", extra_properties={}, source="manual",
    )

    keys = await list_etl_node_keys_by_term_type(conn, "t1", "产品")

    assert keys == {"产品:A"}


async def test_list_etl_node_keys_by_term_type_is_scoped_to_tenant_and_type():
    conn = await _connect()
    await create_term_type(conn, tenant_id="t1", value="产品")
    await create_term_type(conn, tenant_id="t1", value="类目")
    await confirm_ontology(conn, "t1")
    await create_term_type(conn, tenant_id="t2", value="产品")
    await confirm_ontology(conn, "t2")
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="产品:A", standard_name="A",
        aliases=[], term_type="产品", extra_properties={}, source="etl",
    )
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="类目:X", standard_name="X",
        aliases=[], term_type="类目", extra_properties={}, source="etl",
    )
    await upsert_term_with_node_key(
        conn, tenant_id="t2", node_key="产品:A", standard_name="A",
        aliases=[], term_type="产品", extra_properties={}, source="etl",
    )

    assert await list_etl_node_keys_by_term_type(conn, "t1", "产品") == {"产品:A"}


async def test_delete_terms_by_node_keys_removes_only_the_named_rows():
    conn = await _connect()
    await create_term_type(conn, tenant_id="t1", value="产品")
    await confirm_ontology(conn, "t1")
    for key in ("产品:A", "产品:B", "产品:C"):
        await upsert_term_with_node_key(
            conn, tenant_id="t1", node_key=key, standard_name=key,
            aliases=[], term_type="产品", extra_properties={}, source="etl",
        )

    removed = await delete_terms_by_node_keys(conn, "t1", {"产品:A", "产品:C"})

    assert removed == 2
    assert await list_etl_node_keys_by_term_type(conn, "t1", "产品") == {"产品:B"}


async def test_delete_terms_by_node_keys_on_empty_set_is_a_noop():
    """空集合必须是干净的空操作——绝不能退化成"没有 WHERE 条件"把整张表删了。
    这是本函数最危险的失败形态。"""
    conn = await _connect()
    await create_term_type(conn, tenant_id="t1", value="产品")
    await confirm_ontology(conn, "t1")
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="产品:A", standard_name="A",
        aliases=[], term_type="产品", extra_properties={}, source="etl",
    )

    removed = await delete_terms_by_node_keys(conn, "t1", set())

    assert removed == 0
    assert await list_etl_node_keys_by_term_type(conn, "t1", "产品") == {"产品:A"}


# ---------------------------------------------------------------------------
# list_terms_merged / get_term_merged_by_node_key —— 合并视图两个查询入口的
# 集成测试（真实 aiosqlite 连接）。term_merge.apply_edits 本身的合并语义已经
# 在 tests/graphrag/test_term_merge.py 用纯函数单测穷举过，这里只验证两个
# 包装函数确实把 terms 表和 term_edits 表接起来了——它们是"所有读路径都该
# 走这个"的入口，后续任务全部建在它们之上，不能只靠间接调用验证。
# ---------------------------------------------------------------------------


async def _connect_with_edits() -> aiosqlite.Connection:
    conn = await _connect()
    await ensure_term_edits_schema(conn)
    return conn


async def test_list_terms_merged_applies_field_edits_over_pipeline_output():
    """改过的字段取人工值，其余字段仍取管道值。"""
    conn = await _connect_with_edits()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:A", standard_name="管道产出的名字",
        aliases=[], term_type="t", extra_properties={}, source="etl",
    )
    await upsert_term_edit(
        conn, tenant_id="default", node_key="t:A", field="standard_name",
        value="人工改过的名字", edited_by="alice",
    )

    merged = await list_terms_merged(conn, "default")

    assert [t.standard_name for t in merged] == ["人工改过的名字"]
    assert merged[0].node_key == "t:A"


async def test_list_terms_merged_excludes_entities_marked_deleted():
    conn = await _connect_with_edits()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:A", standard_name="A",
        aliases=[], term_type="t", extra_properties={}, source="etl",
    )
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:B", standard_name="B",
        aliases=[], term_type="t", extra_properties={}, source="etl",
    )
    await upsert_term_edit(
        conn, tenant_id="default", node_key="t:A", field=FIELD_DELETED,
        value=None, edited_by="alice",
    )

    merged = await list_terms_merged(conn, "default")

    assert [t.node_key for t in merged] == ["t:B"]


async def test_list_terms_merged_includes_pure_edit_layer_created_entities():
    """terms 表里没有对应行，纯粹由 __created__ 编辑合成的实体也要出现在
    列表里——这是抽取管道审核流程"当场建端点实体"能闭环的前提。"""
    conn = await _connect_with_edits()
    await upsert_term_edit(
        conn, tenant_id="default", node_key="t:NEW", field=FIELD_CREATED,
        value={"standard_name": "人工新建", "term_type": "t", "aliases": [], "extra_properties": {}},
        edited_by="alice",
    )

    merged = await list_terms_merged(conn, "default")

    assert [t.node_key for t in merged] == ["t:NEW"]
    assert merged[0].standard_name == "人工新建"
    assert merged[0].source == "review"


async def test_get_term_merged_by_node_key_raises_for_deleted_entity():
    """对读路径而言，被 __deleted__ 标记过的实体就是不存在，跟 terms 表里
    根本没有这一行不该有可观测的区别。"""
    conn = await _connect_with_edits()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:A", standard_name="A",
        aliases=[], term_type="t", extra_properties={}, source="etl",
    )
    await upsert_term_edit(
        conn, tenant_id="default", node_key="t:A", field=FIELD_DELETED,
        value=None, edited_by="alice",
    )

    with pytest.raises(TermNotFoundError):
        await get_term_merged_by_node_key(conn, "default", "t:A")


async def test_get_term_merged_by_node_key_returns_synthesized_created_entity():
    """terms 表无行、只有 __created__ 编辑时，按 node_key 单条查询也要能
    拿到合成结果——不能只有 list_terms_merged 认得这种实体。"""
    conn = await _connect_with_edits()
    await upsert_term_edit(
        conn, tenant_id="default", node_key="t:NEW", field=FIELD_CREATED,
        value={"standard_name": "人工新建", "term_type": "t", "aliases": [], "extra_properties": {}},
        edited_by="alice",
    )

    term = await get_term_merged_by_node_key(conn, "default", "t:NEW")

    assert term.node_key == "t:NEW"
    assert term.standard_name == "人工新建"
    assert term.source == "review"


async def test_list_terms_merged_source_filter_excludes_edit_layer_created_entities():
    """source 过滤要对合并结果整体生效：apply_edits 追加的纯编辑层创建
    实体固定 source="review"，传 source="etl" 时不该把它们也带出来——
    这是评审发现的缺口，管理后台"来源筛选"依赖这条行为。"""
    conn = await _connect_with_edits()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:A", standard_name="A",
        aliases=[], term_type="t", extra_properties={}, source="etl",
    )
    await upsert_term_edit(
        conn, tenant_id="default", node_key="t:NEW", field=FIELD_CREATED,
        value={"standard_name": "人工新建", "term_type": "t", "aliases": [], "extra_properties": {}},
        edited_by="alice",
    )

    etl_only = await list_terms_merged(conn, "default", source="etl")

    assert [t.node_key for t in etl_only] == ["t:A"]


async def test_list_terms_merged_without_source_filter_includes_edit_layer_created_entities():
    """source=None（默认）的行为必须完全不变：绝大多数调用方走这条路径，
    修 source 过滤的 bug 不能连带把默认场景也过滤掉。"""
    conn = await _connect_with_edits()
    await upsert_term_with_node_key(
        conn, tenant_id="default", node_key="t:A", standard_name="A",
        aliases=[], term_type="t", extra_properties={}, source="etl",
    )
    await upsert_term_edit(
        conn, tenant_id="default", node_key="t:NEW", field=FIELD_CREATED,
        value={"standard_name": "人工新建", "term_type": "t", "aliases": [], "extra_properties": {}},
        edited_by="alice",
    )

    merged = await list_terms_merged(conn, "default")

    assert {t.node_key for t in merged} == {"t:A", "t:NEW"}
