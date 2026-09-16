from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_lifecycle import confirm_ontology, ensure_ontology_schema, replace_draft
from app.graphrag.ontology_skill_export import NothingToExportError, export_skill_yaml
from app.graphrag.ontology_skills import load_skill

pytestmark = pytest.mark.anyio

_MAPPING = """\
tenant_id: t1
entities:
  - term_type: SKU
    source_file: sku.xls
    standard_name_column: 商品名称
    node_key_parts:
      - column: JAN
    field_mappings:
      color: 现地语色
  - term_type: Store
    source_file: store.csv
    standard_name_column: 门店名
    node_key_parts:
      - column: STORE_CD
relations:
  - relation_type: SOLD_AT
    source_file: sales.csv
    subject_term_type: SKU
    object_term_type: Store
"""


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    return conn


async def _confirmed_ontology(conn) -> None:
    await replace_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
                "standard_name_value_type": "string",
            },
            {"value": "Store", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[
            {"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售", "description": ""}
        ],
        constraints=[
            {"subject_term_type": "SKU", "relation_type": "SOLD_AT", "object_term_type": "Store"}
        ],
        etl_mapping={"config_yaml": _MAPPING, "source_file_name": "sku.xls"},
        actor="alice",
    )
    await confirm_ontology(conn, "t1", actor="alice")


async def test_export_round_trips_through_load_skill(tmp_path):
    """导出的产物必须是 load_skill 收得下的——这是"导出→人工审阅→提交进
    app/ontology_skills/"这条路（spec 决策 6）成立的前提。"""
    conn = await _conn()
    await _confirmed_ontology(conn)
    text = await export_skill_yaml(
        conn, "t1", skill_name="consumer_retail_t1", display_name="消费品零售（t1 导出）",
        today="2026-09-16",
    )
    path = tmp_path / "skill.yaml"
    path.write_text(text, encoding="utf-8")
    skill = load_skill(path)
    assert skill.name == "consumer_retail_t1"
    assert skill.display_name == "消费品零售（t1 导出）"
    assert {t.value for t in skill.term_types} == {"SKU", "Store"}
    assert [r.relation_type for r in skill.relation_types] == ["SOLD_AT"]
    assert skill.constraints[0].subject == "SKU"
    assert skill.constraints[0].object == "Store"
    # 来源写在 description 里：这份产物会被人拿去改，得知道它是从哪个租户
    # 哪一天导出来的（spec 行为规格 §6）
    assert "t1" in skill.description
    assert "2026-09-16" in skill.description
    # questions 是 v2 的东西，导出时给空
    assert skill.questions == []


async def test_aliases_come_from_the_columns_the_mapping_actually_used(tmp_path):
    conn = await _conn()
    await _confirmed_ontology(conn)
    text = await export_skill_yaml(
        conn, "t1", skill_name="x", display_name="X", today="2026-09-16"
    )
    path = tmp_path / "skill.yaml"
    path.write_text(text, encoding="utf-8")
    skill = load_skill(path)
    by_value = {t.value: t for t in skill.term_types}
    assert by_value["SKU"].key_aliases == ["JAN"]
    assert by_value["SKU"].field_aliases["color"] == ["现地语色"]
    assert by_value["Store"].key_aliases == ["STORE_CD"]


async def test_export_survives_a_syntactically_broken_confirmed_mapping(tmp_path):
    """config_yaml 是否合法 YAML 在写入侧（set_draft_etl_mapping）不做校验，
    截断/手改出错都能落库；导出必须扛住这种坏数据，产出别名为空但仍可
    装回的 skill，而不是把 yaml.YAMLError 冒泡给调用方。"""
    conn = await _conn()
    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[],
        constraints=[],
        etl_mapping={"config_yaml": "entities: [\n", "source_file_name": "broken.yaml"},
        actor="alice",
    )
    await confirm_ontology(conn, "t1", actor="alice")
    text = await export_skill_yaml(
        conn, "t1", skill_name="x", display_name="X", today="2026-09-16"
    )
    path = tmp_path / "skill.yaml"
    path.write_text(text, encoding="utf-8")
    skill = load_skill(path)
    assert skill.term_types[0].key_aliases == []


async def test_field_aliases_drop_fields_no_longer_declared_on_the_term_type(tmp_path):
    """本体改了 extra_fields 但 ETL 映射没跟着改，是 replace_draft 的
    etl_mapping 可选参数所允许的可达漂移状态。产物里的 field_aliases 如果
    照抄映射，会指向一个本体已不再声明的字段名，load_skill._parse_term_type
    校验 field_aliases 只能引用已声明字段时会拒绝装载——导出出来的东西必须
    装得回去，所以要按这次导出的 extra_fields 过滤掉漂移的字段。"""
    conn = await _conn()
    await replace_draft(
        conn,
        "t1",
        term_types=[
            # 注意：没有声明 color 字段，但下面的映射仍然把 color 挂在 SKU 上。
            {"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"},
            {"value": "Store", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[
            {"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售", "description": ""}
        ],
        constraints=[
            {"subject_term_type": "SKU", "relation_type": "SOLD_AT", "object_term_type": "Store"}
        ],
        etl_mapping={"config_yaml": _MAPPING, "source_file_name": "sku.xls"},
        actor="alice",
    )
    await confirm_ontology(conn, "t1", actor="alice")
    text = await export_skill_yaml(
        conn, "t1", skill_name="x", display_name="X", today="2026-09-16"
    )
    path = tmp_path / "skill.yaml"
    path.write_text(text, encoding="utf-8")
    skill = load_skill(path)
    sku = next(t for t in skill.term_types if t.value == "SKU")
    assert "color" not in sku.field_aliases


async def test_export_without_mapping_still_produces_a_loadable_skill(tmp_path):
    """本体确认了、映射还没配，导出仍然要能用——别名为空而已，人工补。"""
    conn = await _conn()
    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[],
        constraints=[],
        actor="alice",
    )
    await confirm_ontology(conn, "t1", actor="alice")
    text = await export_skill_yaml(
        conn, "t1", skill_name="x", display_name="X", today="2026-09-16"
    )
    path = tmp_path / "skill.yaml"
    path.write_text(text, encoding="utf-8")
    skill = load_skill(path)
    assert skill.term_types[0].key_aliases == []


async def test_export_without_confirmed_ontology_raises():
    """没有已确认本体时导出的是一份空骨架——load_skill 会拒（term_types 非空
    是硬要求），与其产出一个下载下来才发现用不了的文件，不如当场说清楚。"""
    conn = await _conn()
    with pytest.raises(NothingToExportError):
        await export_skill_yaml(conn, "t1", skill_name="x", display_name="X", today="2026-09-16")


async def test_export_rejects_invalid_skill_name():
    conn = await _conn()
    await _confirmed_ontology(conn)
    with pytest.raises(ValueError):
        await export_skill_yaml(
            conn, "t1", skill_name="Not A Name", display_name="X", today="2026-09-16"
        )
