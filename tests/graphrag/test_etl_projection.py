from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.etl_projection import (
    ProjectedRelationRow,
    ProjectedRow,
    RowFailure,
    format_duplicate_key_error,
    project_entity_rows,
    project_relation_rows,
    scan_entity_node_keys,
)
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import (
    AllocatedCodeNodeKeyPart,
    ColumnNodeKeyPart,
    EntityMapping,
    RelationMapping,
)
from app.graphrag.source_parse_options import SourceParseOptions

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


async def test_scan_reports_no_conflicts_when_keys_are_unique(tmp_path: Path):
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
        "张三,200,上海",
    ])
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn, tenant_id="t1", mapping=_mapping(),
        extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
        data_dir=tmp_path,
    )

    assert result.duplicate_keys == {}
    assert result.scanned_rows == 2


async def test_scan_collects_value_conflicts_with_source_row_numbers(tmp_path: Path):
    """同一个 node_key 在两行里算出了**不同的值**（city 分别是北京和深圳）
    ——这才是要拦的：写入时 ON CONFLICT DO UPDATE 会静默取最后一行。

    行号从 2 起算（第 1 行是表头），是消息里唯一能让人定位问题的东西。"""
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
        "李四,300,广州",
        "张三,100,深圳",
    ])
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn, tenant_id="t1", mapping=_mapping(),
        extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
        data_dir=tmp_path,
    )

    assert result.duplicate_keys == {"客户:张三:100": [2, 4]}
    assert result.scanned_rows == 3


async def test_scan_ignores_row_level_failures(tmp_path: Path):
    """缺列的脏行在第一遍里既不算冲突、也不该让扫描崩掉——行级问题由
    第二遍统一记录成 RowFailure，第一遍只关心值冲突。"""
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
        ",200,上海",
    ])
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn, tenant_id="t1", mapping=_mapping(),
        extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
        data_dir=tmp_path,
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


async def test_project_relation_rows_computes_both_endpoint_keys(tmp_path: Path):
    _write_csv(tmp_path / "orders.csv", [
        "order_id,name,zip",
        "O1,张三,100",
    ])
    conn = await _conn()
    subject = EntityMapping(
        term_type="订单", source_file="orders.csv",
        standard_name_parts=["order_id"],
        node_key_parts=[ColumnNodeKeyPart(column="order_id")],
        field_mappings={},
    )
    obj = _mapping()
    relation = RelationMapping(
        relation_type="ORDER_BY", source_file="orders.csv",
        subject_term_type="订单", object_term_type="客户",
    )

    rows = [
        r async for r in project_relation_rows(
            conn, tenant_id="t1", mapping=relation,
            subject_entity=subject, object_entity=obj, data_dir=tmp_path,
        )
    ]

    assert rows == [
        ProjectedRelationRow(row_number=2, subject_node_key="订单:O1", object_node_key="客户:张三:100")
    ]


async def test_project_relation_rows_never_allocates_new_stable_codes(tmp_path: Path):
    """关系路径必须 allow_allocation=False：这一行引用的实体值如果从没真正
    写入过，不该在关系这一步凭空产生一个新的稳定码、MERGE 出没有对应
    Term 记录的幽灵节点。未命中已有分配 → RowFailure，不是新分配。"""
    _write_csv(tmp_path / "orders.csv", [
        "order_id,name,zip,color",
        "O1,张三,100,红",
    ])
    conn = await _conn()
    subject = EntityMapping(
        term_type="订单", source_file="orders.csv",
        standard_name_parts=["order_id"],
        node_key_parts=[ColumnNodeKeyPart(column="order_id")],
        field_mappings={},
    )
    obj = EntityMapping(
        term_type="颜色", source_file="orders.csv",
        standard_name_parts=["color"],
        node_key_parts=[AllocatedCodeNodeKeyPart(scope_columns=[], raw_value_column="color")],
        field_mappings={},
    )
    relation = RelationMapping(
        relation_type="HAS_COLOR", source_file="orders.csv",
        subject_term_type="订单", object_term_type="颜色",
    )

    rows = [
        r async for r in project_relation_rows(
            conn, tenant_id="t1", mapping=relation,
            subject_entity=subject, object_entity=obj, data_dir=tmp_path,
        )
    ]

    assert len(rows) == 1
    assert isinstance(rows[0], RowFailure)
    assert "稳定码尚未分配" in rows[0].reason


def test_format_duplicate_key_error_does_not_point_at_a_nonexistent_run_report():
    """这次失败发生在 ETLRunReport 创建之前，根本不会有运行报告——旧措辞
    "完整清单见运行报告" 指向一个不存在的东西。超过展示上限时，消息必须
    准确说明"只展示了前 N 条、总共多少处"，不再提"运行报告"，且样例仍然
    只列 _MAX_DUPLICATE_SAMPLES（20）条。"""
    duplicates = {str(i): [2, 3] for i in range(25)}

    message = format_duplicate_key_error({"客户": duplicates})

    assert "运行报告" not in message
    assert "共 25 处" in message
    assert "仅展示前 20 处" in message
    sample_lines = [line for line in message.splitlines() if "← 源文件第" in line]
    assert len(sample_lines) == 20


async def test_scan_does_not_flag_a_repeated_entity_whose_values_agree(tmp_path: Path):
    """**反范式宽表里维度实体天然重复，这是良性的。**

    demo 的 soft_drink_sales.xlsx 有 10000 行，但只有 10 个产品、3 家公司、
    4 个类目——每个产品自然出现在约 1000 行里。node_key 要唯一标识的是
    实体，不是行；多行映射到同一实体正是宽表的定义，
    upsert_term_with_node_key 的 ON CONFLICT DO UPDATE 一直在正确处理它。

    这条用例钉住的是：只要那些行算出的值一致，就不该报错。此前的实现把
    "重复"本身当成配置错误，会让 demo 的 ETL 整个跑不起来。
    """
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
        "李四,300,广州",
        "张三,100,北京",
        "张三,100,北京",
    ])
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn, tenant_id="t1", mapping=_mapping(),
        extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
        data_dir=tmp_path,
    )

    assert result.duplicate_keys == {}
    assert result.scanned_rows == 4
    # 重复的行仍然算进 node_keys（sweep 要用它），只是不报冲突。
    assert result.node_keys == {"客户:张三:100", "客户:李四:300"}


async def test_project_entity_rows_reads_the_source_with_its_parse_options(tmp_path: Path):
    """选项只存不使用的话，界面上一切正常，跑批仍然读第一行表头——这正是
    这个功能要修的 bug 换了个地方复发。"""
    _write_csv(
        tmp_path / "customers.csv",
        [
            "本表仅供内部使用,,",
            "name,zip,city",
            "张三,100,北京",
        ],
    )
    conn = await _conn()

    rows = [
        row
        async for row in project_entity_rows(
            conn,
            tenant_id="demo",
            mapping=_mapping(),
            # city 需要在 extra_field_specs 里声明，否则 convert_field_value
            # 会因为"字段没有在 schema 里声明"而报 RowProcessingError——
            # _mapping() 的 field_mappings 里 city 映到 city，这一行的 city
            # 不是空值，会真的走到这条转换。
            extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
            data_dir=tmp_path,
            parse_options=SourceParseOptions(header_row=2),
        )
    ]

    # node_key 的实际拼接分隔符是英文冒号（见 compute_node_key），不是竖线
    # ——已用 test_scan_does_not_flag_a_repeated_entity_whose_values_agree
    # 里 "客户:张三:100" 这条既有断言核实过。
    assert [r.node_key for r in rows] == ["客户:张三:100"]


async def test_project_entity_rows_numbers_rows_from_the_real_first_data_row(tmp_path: Path):
    """报错信息里的"第 N 行"要跟用户在 Excel 里看到的行号对上。表头在第 2
    行时，第一条数据是第 3 行，不是第 2 行。"""
    _write_csv(
        tmp_path / "customers.csv",
        [
            "本表仅供内部使用,,",
            "name,zip,city",
            "张三,,北京",
        ],
    )
    conn = await _conn()

    results = [
        row
        async for row in project_entity_rows(
            conn,
            tenant_id="demo",
            mapping=_mapping(),
            extra_field_specs={},
            data_dir=tmp_path,
            parse_options=SourceParseOptions(header_row=2),
        )
    ]

    failures = [r for r in results if isinstance(r, RowFailure)]
    assert [f.row_number for f in failures] == [3]


async def test_scan_entity_node_keys_reads_the_source_with_its_parse_options(tmp_path: Path):
    """两遍扫描必须用同一份选项。第一遍按第 1 行读、第二遍按第 2 行读的话，
    预检查到的键跟真正写入的键不是同一批——预检就等于没做。"""
    _write_csv(
        tmp_path / "customers.csv",
        [
            "本表仅供内部使用,,",
            "name,zip,city",
            "张三,100,北京",
            "张三,100,上海",
        ],
    )
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn,
        tenant_id="demo",
        mapping=_mapping(),
        # 同上：city 需要声明在 extra_field_specs 里，否则值转换会先因为
        # "字段未声明"报错，冲突永远走不到值指纹比对那一步。
        extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
        data_dir=tmp_path,
        parse_options=SourceParseOptions(header_row=2),
    )

    # 同一个 node_key 两行、city 不同 —— 这是值冲突，必须被检出来。
    # KeyScanResult 没有 conflicts 字段（字段是 duplicate_keys/scanned_rows/
    # node_keys，见 etl_projection.py），按实际字段名断言。
    assert result.duplicate_keys
