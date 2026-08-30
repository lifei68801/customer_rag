from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.etl_projection import (
    ProjectedRow,
    RowFailure,
    project_entity_rows,
    scan_entity_node_keys,
)
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import ColumnNodeKeyPart, EntityMapping

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)
    return conn


def _write_csv(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mapping() -> EntityMapping:
    return EntityMapping(
        term_type="客户",
        source_file="customers.csv",
        standard_name_parts=["name", "zip"],
        node_key_parts=[ColumnNodeKeyPart(column="name"), ColumnNodeKeyPart(column="zip")],
        field_mappings={"city": "city"},
    )


async def test_scan_reports_no_duplicates_when_keys_are_unique(tmp_path: Path):
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
        "张三,200,上海",
    ])
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn, tenant_id="t1", mapping=_mapping(), data_dir=tmp_path
    )

    assert result.duplicate_keys == {}
    assert result.scanned_rows == 2


async def test_scan_collects_duplicate_keys_with_source_row_numbers(tmp_path: Path):
    """行号从 2 起算——第 1 行是表头。同一个 node_key 出现在哪几行，是
    DuplicateNodeKeyError 的消息里唯一能让人定位问题的东西。"""
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
        "李四,300,广州",
        "张三,100,深圳",
    ])
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn, tenant_id="t1", mapping=_mapping(), data_dir=tmp_path
    )

    assert result.duplicate_keys == {"客户:张三:100": [2, 4]}
    assert result.scanned_rows == 3


async def test_scan_ignores_row_level_failures(tmp_path: Path):
    """缺列的脏行在第一遍里既不算重复、也不该让扫描崩掉——行级问题由
    第二遍统一记录成 RowFailure，第一遍只关心键的重复。"""
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
        ",200,上海",
    ])
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn, tenant_id="t1", mapping=_mapping(), data_dir=tmp_path
    )

    assert result.duplicate_keys == {}
    assert result.scanned_rows == 2


async def test_project_materializes_key_display_name_and_properties(tmp_path: Path):
    """展示名用 " / " 连接，不用冒号——冒号是 node_key 的分隔符，展示名里
    再用一次会让两者在日志和界面上难以区分。"""
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
    ])
    conn = await _conn()

    rows = [
        r async for r in project_entity_rows(
            conn, tenant_id="t1", mapping=_mapping(),
            extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
            data_dir=tmp_path,
        )
    ]

    assert rows == [
        ProjectedRow(
            row_number=2,
            node_key="客户:张三:100",
            standard_name="张三 / 100",
            extra_properties={"city": "北京"},
        )
    ]


async def test_project_yields_row_failure_for_dirty_rows_and_keeps_going(tmp_path: Path):
    """行级脏数据不中断整批：产出一个 RowFailure，继续处理后面的行。"""
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        ",200,上海",
        "张三,100,北京",
    ])
    conn = await _conn()

    rows = [
        r async for r in project_entity_rows(
            conn, tenant_id="t1", mapping=_mapping(),
            extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
            data_dir=tmp_path,
        )
    ]

    assert len(rows) == 2
    assert isinstance(rows[0], RowFailure)
    assert rows[0].row_number == 2
    assert isinstance(rows[1], ProjectedRow)
    assert rows[1].node_key == "客户:张三:100"


async def test_project_reports_missing_standard_name_column_as_row_failure(tmp_path: Path):
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,,北京",
    ])
    conn = await _conn()

    rows = [
        r async for r in project_entity_rows(
            conn, tenant_id="t1", mapping=_mapping(),
            extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
            data_dir=tmp_path,
        )
    ]

    assert len(rows) == 1
    assert isinstance(rows[0], RowFailure)
