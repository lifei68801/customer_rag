from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_etl_mapping import ensure_etl_mapping_schema
from app.graphrag.ontology_grounding import derive_grounding

pytestmark = pytest.mark.anyio

_YAML = """\
tenant_id: t1
entities:
  - term_type: SKU
    source_file: sku.xls
    standard_name_column: name
    node_key_parts:
      - column: jan
    field_mappings:
      color: 颜色
  - term_type: Store
    source_file: store.csv
    standard_name_column: store_name
    node_key_parts:
      - column: store_cd
relations:
  - relation_type: SOLD_AT
    source_file: sales.csv
    subject_term_type: SKU
    object_term_type: Store
"""


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_etl_mapping_schema(conn)
    return conn


async def _put(conn, tenant_id: str, status: str, yaml_text: str, file_name: str = "sku.xls") -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO ontology_etl_mapping "
        "(tenant_id, status, config_yaml, source_file_name, created_at) VALUES (?, ?, ?, ?, ?)",
        (tenant_id, status, yaml_text, file_name, "2026-09-16T10:00:00"),
    )
    await conn.commit()


async def test_no_mapping_means_nothing_is_grounded():
    conn = await _conn()
    grounding = await derive_grounding(conn, "t1")
    assert grounding.status is None
    assert grounding.grounded_term_types == []
    assert grounding.grounded_relation_types == []
    assert grounding.parse_error is None


async def test_derives_from_confirmed_when_no_draft():
    conn = await _conn()
    await _put(conn, "t1", "confirmed", _YAML)
    grounding = await derive_grounding(conn, "t1")
    assert grounding.status == "confirmed"
    assert grounding.grounded_term_types == ["SKU", "Store"]
    assert grounding.grounded_relation_types == ["SOLD_AT"]
    assert grounding.source_files == ["sales.csv", "sku.xls", "store.csv"]


async def test_draft_wins_over_confirmed():
    """草稿优先：用户刚在工作台应用了一版映射，落地状态必须立刻反映那一版，
    而不是上一次确认时的样子——否则他会看到自己刚接上的表仍然显示未落地。"""
    conn = await _conn()
    await _put(conn, "t1", "confirmed", _YAML)
    await _put(
        conn,
        "t1",
        "draft",
        "tenant_id: t1\nentities:\n"
        "  - term_type: Category\n"
        "    source_file: cat.csv\n"
        "    standard_name_column: name\n"
        "    node_key_parts:\n      - column: cat_cd\n"
        "relations: []\n",
    )
    grounding = await derive_grounding(conn, "t1")
    assert grounding.status == "draft"
    assert grounding.grounded_term_types == ["Category"]


async def test_other_tenants_mapping_does_not_leak():
    conn = await _conn()
    await _put(conn, "other", "confirmed", _YAML)
    grounding = await derive_grounding(conn, "t1")
    assert grounding.grounded_term_types == []


async def test_broken_yaml_reports_the_reason_instead_of_raising():
    """存下来的 YAML 坏了不该让工作台打不开：返回空落地 + 一句原因，跟
    admin_ontology_routes 里 summarize 失败时的处理口径一致。"""
    conn = await _conn()
    await _put(conn, "t1", "draft", "entities: [\n")
    grounding = await derive_grounding(conn, "t1")
    assert grounding.grounded_term_types == []
    assert grounding.parse_error is not None
    assert grounding.status == "draft"


async def test_duplicates_are_collapsed_and_sorted():
    conn = await _conn()
    await _put(
        conn,
        "t1",
        "draft",
        "tenant_id: t1\nentities:\n"
        "  - term_type: SKU\n    source_file: a.csv\n    standard_name_column: n\n"
        "    node_key_parts:\n      - column: jan\n"
        "  - term_type: SKU\n    source_file: b.csv\n    standard_name_column: n\n"
        "    node_key_parts:\n      - column: jan\n"
        "relations: []\n",
    )
    grounding = await derive_grounding(conn, "t1")
    assert grounding.grounded_term_types == ["SKU"]
    assert grounding.source_files == ["a.csv", "b.csv"]


async def test_to_dict_shape():
    conn = await _conn()
    await _put(conn, "t1", "confirmed", _YAML)
    payload = (await derive_grounding(conn, "t1")).to_dict()
    assert payload == {
        "status": "confirmed",
        "grounded_term_types": ["SKU", "Store"],
        "grounded_relation_types": ["SOLD_AT"],
        "source_files": ["sales.csv", "sku.xls", "store.csv"],
        "parse_error": None,
    }
