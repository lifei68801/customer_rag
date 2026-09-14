import aiosqlite
import pytest

from app.graphrag.ontology_etl_mapping import (
    ensure_etl_mapping_schema,
    get_etl_mapping,
    remember_mapping_used_for_import,
    set_draft_etl_mapping,
)
from app.graphrag.ontology_lifecycle import (
    checkout_draft,
    confirm_ontology,
    ensure_ontology_schema,
    replace_draft,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    return conn


async def _seed_ontology_draft(conn) -> None:
    """给一份最小的本体草稿——确认动作需要它才会真正执行。"""
    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "客户", "extra_fields": []}],
        relation_types=[],
        constraints=[],
    actor="alice")


async def test_draft_mapping_survives_confirm():
    conn = await _conn()
    await _seed_ontology_draft(conn)
    await set_draft_etl_mapping(
        conn,
        "t1",
        config_yaml="entities: []",
        source_file_name="orders.csv",
        created_at="2026-09-03T00:00:00",
    )
    await confirm_ontology(conn, "t1", actor="alice")
    confirmed = await get_etl_mapping(conn, "t1", status="confirmed")
    assert confirmed is not None
    assert confirmed.config_yaml == "entities: []"
    assert confirmed.source_file_name == "orders.csv"
    # 草稿被原地提升，不再是 draft。
    assert await get_etl_mapping(conn, "t1", status="draft") is None


async def test_mapping_alone_does_not_trigger_confirm():
    """只有映射、没有本体草稿时，确认必须早退。

    confirm_ontology 的早退判据（has_draft_in_any_table）刻意**不含**映射表：
    一份没有本体草稿的映射不构成"有内容要确认"，把它算进去会让确认误以为
    有东西要提升，从而删掉已确认的本体。
    """
    conn = await _conn()
    await set_draft_etl_mapping(
        conn,
        "t1",
        config_yaml="entities: []",
        source_file_name="orders.csv",
        created_at="2026-09-03T00:00:00",
    )
    await confirm_ontology(conn, "t1", actor="alice")
    assert await get_etl_mapping(conn, "t1", status="confirmed") is None
    assert await get_etl_mapping(conn, "t1", status="draft") is not None


async def test_checkout_copies_confirmed_mapping_to_draft():
    """检出要把映射一起复制。

    不复制的话：用户确认后再去本体结构页改两笔（那会触发 checkout_draft），
    映射就只剩 confirmed 那份；等他再确认一次，confirm 会先删掉 confirmed
    再提升 draft——而 draft 里没有映射行，映射凭空消失。
    """
    conn = await _conn()
    await _seed_ontology_draft(conn)
    await set_draft_etl_mapping(
        conn,
        "t1",
        config_yaml="entities: []",
        source_file_name="orders.csv",
        created_at="2026-09-03T00:00:00",
    )
    await confirm_ontology(conn, "t1", actor="alice")
    await checkout_draft(conn, "t1")
    draft = await get_etl_mapping(conn, "t1", status="draft")
    assert draft is not None
    assert draft.source_file_name == "orders.csv"


# ── 导入时用过的映射记成默认 ─────────────────────────────────────────────


async def _set_mapping(conn, status: str, yaml_text: str) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO ontology_etl_mapping "
        "(tenant_id, status, config_yaml, source_file_name, created_at) "
        "VALUES ('t1', ?, ?, 'old.xlsx', '2026-09-01T00:00:00')",
        (status, yaml_text),
    )
    await conn.commit()


async def test_the_mapping_an_import_used_becomes_the_default():
    """表格导入页读的是 confirmed 那份。不写回去的话，用户改过的映射只作用于
    这一次，下次进来看到的"沿用上次配好的映射"是他改之前的那一版。"""
    conn = await _conn()
    await ensure_etl_mapping_schema(conn)
    await _set_mapping(conn, "confirmed", "old: 1")
    await _set_mapping(conn, "draft", "old: 1")

    followed = await remember_mapping_used_for_import(
        conn, "t1", config_yaml="new: 1", source_file_name="sales.xlsx",
        created_at="2026-09-14T00:00:00",
    )

    confirmed = await get_etl_mapping(conn, "t1", status="confirmed")
    assert confirmed is not None
    assert confirmed.config_yaml == "new: 1"
    assert confirmed.source_file_name == "sales.xlsx"
    # 草稿跟旧的 confirmed 一致 → 没有人在改它，跟着走。
    assert followed is True
    draft = await get_etl_mapping(conn, "t1", status="draft")
    assert draft is not None and draft.config_yaml == "new: 1"
    await conn.close()


async def test_a_draft_someone_is_editing_is_not_overwritten():
    """草稿跟 confirmed 不一样 = 有人正在改这个本体的映射（引导建模整份写
    草稿）。覆盖它就是把别人没提交的工作删了。"""
    conn = await _conn()
    await ensure_etl_mapping_schema(conn)
    await _set_mapping(conn, "confirmed", "old: 1")
    await _set_mapping(conn, "draft", "someone is editing: 1")

    followed = await remember_mapping_used_for_import(
        conn, "t1", config_yaml="new: 1", source_file_name="sales.xlsx",
        created_at="2026-09-14T00:00:00",
    )

    assert followed is False
    draft = await get_etl_mapping(conn, "t1", status="draft")
    assert draft is not None and draft.config_yaml == "someone is editing: 1"
    confirmed = await get_etl_mapping(conn, "t1", status="confirmed")
    assert confirmed is not None and confirmed.config_yaml == "new: 1"
    await conn.close()


async def test_works_when_the_tenant_has_no_mapping_yet():
    """第一次用构建器配好映射就直接跑的租户：两份都还不存在。"""
    conn = await _conn()
    await ensure_etl_mapping_schema(conn)

    followed = await remember_mapping_used_for_import(
        conn, "t1", config_yaml="first: 1", source_file_name="sales.xlsx",
        created_at="2026-09-14T00:00:00",
    )

    assert followed is True
    for status in ("confirmed", "draft"):
        mapping = await get_etl_mapping(conn, "t1", status=status)
        assert mapping is not None and mapping.config_yaml == "first: 1"
    await conn.close()


async def test_another_tenant_keeps_its_own_mapping():
    conn = await _conn()
    await ensure_etl_mapping_schema(conn)
    await conn.execute(
        "INSERT INTO ontology_etl_mapping (tenant_id, status, config_yaml, source_file_name, created_at) "
        "VALUES ('t2', 'confirmed', 'other: 1', 'o.xlsx', '2026-09-01T00:00:00')"
    )
    await conn.commit()

    await remember_mapping_used_for_import(
        conn, "t1", config_yaml="new: 1", source_file_name="sales.xlsx",
        created_at="2026-09-14T00:00:00",
    )

    other = await get_etl_mapping(conn, "t2", status="confirmed")
    assert other is not None and other.config_yaml == "other: 1"
    await conn.close()
