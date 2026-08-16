from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import AllocatedCodeNodeKeyPart, ColumnNodeKeyPart
from app.graphrag.schema_etl_row_processing import (
    RowProcessingError,
    compute_node_key,
    convert_field_value,
)

pytestmark = pytest.mark.anyio


async def test_compute_node_key_with_direct_column_only():
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)

    node_key = await compute_node_key(
        conn, tenant_id="muji", term_type="Product",
        node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
        row={"product_group_id": "1001", "product_group_name": "圆角收纳盒"},
    )

    assert node_key == "Product:1001"


async def test_compute_node_key_with_allocated_code_part():
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)

    node_key = await compute_node_key(
        conn, tenant_id="muji", term_type="VariantValue",
        node_key_parts=[
            ColumnNodeKeyPart(column="dim_code"),
            AllocatedCodeNodeKeyPart(scope_columns=["dim_code"], raw_value_column="raw_value"),
        ],
        row={"dim_code": "dim_007", "raw_value": "抹茶"},
    )

    assert node_key == "VariantValue:dim_007:00001"


async def test_compute_node_key_reuses_allocated_code_across_calls():
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)
    parts = [
        ColumnNodeKeyPart(column="dim_code"),
        AllocatedCodeNodeKeyPart(scope_columns=["dim_code"], raw_value_column="raw_value"),
    ]

    first = await compute_node_key(
        conn, tenant_id="muji", term_type="VariantValue", node_key_parts=parts,
        row={"dim_code": "dim_007", "raw_value": "抹茶"},
    )
    second = await compute_node_key(
        conn, tenant_id="muji", term_type="VariantValue", node_key_parts=parts,
        row={"dim_code": "dim_007", "raw_value": "抹茶"},
    )

    assert first == second == "VariantValue:dim_007:00001"


async def test_compute_node_key_raises_when_column_missing_from_row():
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)

    with pytest.raises(RowProcessingError):
        await compute_node_key(
            conn, tenant_id="muji", term_type="Product",
            node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
            row={"other_column": "x"},
        )


async def test_compute_node_key_raises_when_column_present_but_empty():
    """CSV 里一个空单元格解析出来是存在的空字符串，不是"键不存在"——
    必须单独检查空值，不能只判断列名在不在 row 里。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)

    with pytest.raises(RowProcessingError):
        await compute_node_key(
            conn, tenant_id="muji", term_type="Product",
            node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
            row={"product_group_id": ""},
        )


def test_convert_field_value_number():
    specs = {"numeric_value": ExtraFieldSpec(name="numeric_value", value_type="number")}
    assert convert_field_value(extra_field_specs=specs, field_name="numeric_value", raw_value="750") == 750.0


def test_convert_field_value_integer():
    specs = {"sku_count": ExtraFieldSpec(name="sku_count", value_type="integer")}
    assert convert_field_value(extra_field_specs=specs, field_name="sku_count", raw_value="12") == 12


def test_convert_field_value_string():
    specs = {"md_no": ExtraFieldSpec(name="md_no", value_type="string")}
    assert convert_field_value(extra_field_specs=specs, field_name="md_no", raw_value="A123") == "A123"


def test_convert_field_value_number_array_splits_on_semicolon():
    specs = {"dims": ExtraFieldSpec(name="dims", value_type="number[]")}
    result = convert_field_value(extra_field_specs=specs, field_name="dims", raw_value="20.5;10.0")
    assert result == [20.5, 10.0]


def test_convert_field_value_raises_when_field_not_declared():
    specs: dict = {}
    with pytest.raises(RowProcessingError):
        convert_field_value(extra_field_specs=specs, field_name="unknown_field", raw_value="x")


def test_convert_field_value_raises_on_non_numeric_string_for_number_type():
    specs = {"numeric_value": ExtraFieldSpec(name="numeric_value", value_type="number")}
    with pytest.raises(RowProcessingError):
        convert_field_value(extra_field_specs=specs, field_name="numeric_value", raw_value="不是数字")
