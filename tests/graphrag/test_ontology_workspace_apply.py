from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_lifecycle import ensure_ontology_schema, replace_draft
from app.graphrag.ontology_workspace_apply import diff_against_draft

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    return conn


async def _seed_draft(conn) -> None:
    await replace_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
                "standard_name_value_type": "string",
            },
            {"value": "手工加的", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[{"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售"}],
        constraints=[],
        actor="alice",
    )


async def test_empty_draft_means_everything_is_added():
    conn = await _conn()
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[{"relation_type": "SOLD_AT"}],
        constraints=[{"subject_term_type": "SKU", "relation_type": "SOLD_AT", "object_term_type": "SKU"}],
    )
    assert diff.added_term_types == ["SKU"]
    assert diff.added_relation_types == ["SOLD_AT"]
    assert diff.added_constraints == ["SKU -SOLD_AT-> SKU"]
    assert diff.removed_term_types == []


async def test_elements_only_in_draft_are_reported_as_removed():
    """整份替换会把它们删掉，而它们多半是用户在「本体结构」页手工加的
    （spec 决策 10）。不单独列出来的话，用户点"应用"时不知道自己在删东西。"""
    conn = await _conn()
    await _seed_draft(conn)
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
                "standard_name_value_type": "string",
            }
        ],
        relation_types=[{"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售"}],
        constraints=[],
    )
    assert diff.removed_term_types == ["手工加的"]
    assert diff.added_term_types == []
    assert diff.changed_term_types == []
    assert diff.removed_relation_types == []


async def test_changed_term_type_is_reported_with_what_changed():
    conn = await _conn()
    await _seed_draft(conn)
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [
                    {"name": "color", "value_type": "string", "label": "颜色"},
                    {"name": "size", "value_type": "string", "label": "尺码"},
                ],
                "standard_name_value_type": "string",
            },
            {"value": "手工加的", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[{"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售"}],
        constraints=[],
    )
    assert diff.added_term_types == []
    assert diff.removed_term_types == []
    assert len(diff.changed_term_types) == 1
    # 只说"SKU 变了"用户没法判断要不要点——必须说出变的是哪个字段
    assert "SKU" in diff.changed_term_types[0]
    assert "size" in diff.changed_term_types[0]


async def test_identical_submission_produces_an_empty_diff():
    conn = await _conn()
    await _seed_draft(conn)
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
                "standard_name_value_type": "string",
            },
            {"value": "手工加的", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[{"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售"}],
        constraints=[],
    )
    assert diff.to_dict() == {
        "added_term_types": [],
        "removed_term_types": [],
        "changed_term_types": [],
        "added_relation_types": [],
        "removed_relation_types": [],
        "added_constraints": [],
        "removed_constraints": [],
    }


async def test_extra_field_order_does_not_count_as_a_change():
    """字段顺序在本体表里没有语义（extra_fields 是一个 JSON 列表，读的地方
    都按 name 取）。顺序当成变更的话，每次应用都会报一堆假变更。"""
    conn = await _conn()
    await replace_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [
                    {"name": "color", "value_type": "string", "label": "颜色"},
                    {"name": "size", "value_type": "string", "label": "尺码"},
                ],
                "standard_name_value_type": "string",
            }
        ],
        relation_types=[],
        constraints=[],
        actor="alice",
    )
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [
                    {"name": "size", "value_type": "string", "label": "尺码"},
                    {"name": "color", "value_type": "string", "label": "颜色"},
                ],
                "standard_name_value_type": "string",
            }
        ],
        relation_types=[],
        constraints=[],
    )
    assert diff.changed_term_types == []


async def test_standard_name_value_type_change_is_detected():
    conn = await _conn()
    await _seed_draft(conn)
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
                "standard_name_value_type": "number",
            },
            {"value": "手工加的", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[{"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售"}],
        constraints=[],
    )
    assert len(diff.changed_term_types) == 1
    assert "number" in diff.changed_term_types[0]
