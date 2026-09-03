import aiosqlite
import pytest

from app.graphrag.ontology_etl_mapping import (
    ensure_etl_mapping_schema,
    get_etl_mapping,
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
    )


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
    await confirm_ontology(conn, "t1")
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
    await confirm_ontology(conn, "t1")
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
    await confirm_ontology(conn, "t1")
    await checkout_draft(conn, "t1")
    draft = await get_etl_mapping(conn, "t1", status="draft")
    assert draft is not None
    assert draft.source_file_name == "orders.csv"
