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


async def test_compute_node_key_without_allocation_reuses_existing_code():
    """关系写入路径（allow_allocation=False）解析端点时，命中实体路径已经
    分配好的稳定码就正常返回，不会再分配一个新的。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)
    parts = [
        ColumnNodeKeyPart(column="dim_code"),
        AllocatedCodeNodeKeyPart(scope_columns=["dim_code"], raw_value_column="raw_value"),
    ]
    row = {"dim_code": "dim_007", "raw_value": "抹茶"}
    allocated = await compute_node_key(
        conn, tenant_id="muji", term_type="VariantValue", node_key_parts=parts, row=row,
    )

    looked_up = await compute_node_key(
        conn, tenant_id="muji", term_type="VariantValue", node_key_parts=parts, row=row,
        allow_allocation=False,
    )

    assert looked_up == allocated == "VariantValue:dim_007:00001"
    # 只读解析没有消耗计数位：同 scope 下的下一个原始值仍然拿到 00002
    other = await compute_node_key(
        conn, tenant_id="muji", term_type="VariantValue", node_key_parts=parts,
        row={"dim_code": "dim_007", "raw_value": "草莓"},
    )
    assert other == "VariantValue:dim_007:00002"


async def test_compute_node_key_without_allocation_raises_when_code_never_allocated():
    """从没分配过稳定码的原始值，在关系写入路径上必须报错跳过这一行，
    绝不能悄悄分配一个新码、MERGE 出没有 Term 记录的幽灵节点。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)

    with pytest.raises(RowProcessingError):
        await compute_node_key(
            conn, tenant_id="muji", term_type="VariantValue",
            node_key_parts=[
                ColumnNodeKeyPart(column="dim_code"),
                AllocatedCodeNodeKeyPart(scope_columns=["dim_code"], raw_value_column="raw_value"),
            ],
            row={"dim_code": "dim_007", "raw_value": "从没出现过的值"},
            allow_allocation=False,
        )

    cursor = await conn.execute("SELECT COUNT(*) FROM etl_stable_code_registry")
    assert (await cursor.fetchone())[0] == 0


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


from datetime import date, datetime

from app.graphrag.schema_etl_row_processing import convert_excel_cell_to_string


def test_convert_excel_cell_to_string_int():
    assert convert_excel_cell_to_string(123) == "123"


def test_convert_excel_cell_to_string_float_integer_value_drops_trailing_zero():
    assert convert_excel_cell_to_string(123.0) == "123"


def test_convert_excel_cell_to_string_float_with_decimal_part():
    assert convert_excel_cell_to_string(123.45) == "123.45"


def test_convert_excel_cell_to_string_datetime_with_time_part():
    assert convert_excel_cell_to_string(datetime(2026, 8, 21, 14, 30, 0)) == "2026-08-21 14:30:00"


def test_convert_excel_cell_to_string_datetime_at_midnight_formats_as_date_only():
    assert convert_excel_cell_to_string(datetime(2026, 8, 21, 0, 0, 0)) == "2026-08-21"


def test_convert_excel_cell_to_string_date_object():
    assert convert_excel_cell_to_string(date(2026, 8, 21)) == "2026-08-21"


def test_convert_excel_cell_to_string_bool_true():
    assert convert_excel_cell_to_string(True) == "True"


def test_convert_excel_cell_to_string_bool_false():
    assert convert_excel_cell_to_string(False) == "False"


def test_convert_excel_cell_to_string_none_is_empty_string():
    assert convert_excel_cell_to_string(None) == ""


def test_convert_excel_cell_to_string_string_value_is_stripped():
    assert convert_excel_cell_to_string("  圆角收纳盒  ") == "圆角收纳盒"


def test_convert_excel_cell_to_string_bool_is_not_caught_by_int_branch():
    """回归测试：bool 是 int 的子类，如果 isinstance(value, int) 的判断排在
    isinstance(value, bool) 前面，True 会被 int 分支吞掉变成 "1" 而不是
    "True"——这条测试专门守住分支顺序，不依赖上面两条 bool 测试的巧合。"""
    result = convert_excel_cell_to_string(True)
    assert result == "True"
    assert result != "1"
