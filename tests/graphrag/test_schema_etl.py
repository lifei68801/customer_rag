from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.etl_stable_code_registry import allocate_stable_code, ensure_stable_code_registry_schema
from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
from app.graphrag.ontology_constraints import add_allowed_combination
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.ontology_relations import create_relation_type
from app.graphrag.schema_etl import SchemaETLNotConfirmedError, run_schema_etl
from app.graphrag.schema_etl_config import (
    AllocatedCodeNodeKeyPart,
    ColumnNodeKeyPart,
    EntityMapping,
    RelationMapping,
    SchemaETLConfig,
)
from app.graphrag.terms_store import ensure_terms_schema, get_term

pytestmark = pytest.mark.anyio


class FakeGraphClient:
    def __init__(self) -> None:
        self.synced: list[str] = []
        self.merged: list[tuple[str, str, str]] = []

    async def sync_term(self, term) -> None:
        self.synced.append(term.node_key)

    async def merge_relation(
        self, *, subject_standard_name, object_standard_name, relation_type,
        source, tenant_id, provenance, recorded_at,
    ) -> None:
        self.merged.append((subject_standard_name, object_standard_name, relation_type))


async def _confirmed_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_stable_code_registry_schema(conn)
    await create_term_type(
        conn, tenant_id="muji", value="Product",
        extra_fields=[ExtraFieldSpec(name="md_no", value_type="string")],
    )
    await create_term_type(conn, tenant_id="muji", value="SKU")
    await create_term_type(conn, tenant_id="muji", value="VariantValue")
    await checkout_draft(conn, "muji")
    await create_relation_type(
        conn, "muji", relation_type="HAS_SKU", example_phrase="Product HAS_SKU SKU",
    )
    await create_relation_type(
        conn, "muji", relation_type="HAS_VARIANT_VALUE",
        example_phrase="VariantValue HAS_VARIANT_VALUE VariantValue",
    )
    await add_allowed_combination(
        conn, "muji", subject_term_type="Product", relation_type="HAS_SKU", object_term_type="SKU",
    )
    await add_allowed_combination(
        conn, "muji", subject_term_type="VariantValue", relation_type="HAS_VARIANT_VALUE",
        object_term_type="VariantValue",
    )
    await confirm_ontology(conn, "muji")
    return conn


async def test_run_schema_etl_raises_when_schema_not_confirmed():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_stable_code_registry_schema(conn)
    config = SchemaETLConfig(tenant_id="muji", entities=[], relations=[])

    with pytest.raises(SchemaETLNotConfirmedError):
        await run_schema_etl(conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=Path("."))


async def test_run_schema_etl_writes_entities_and_relations(tmp_path):
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n", encoding="utf-8"
    )
    (tmp_path / "skus.csv").write_text(
        "jan,product_group_id\n4901234567890,1001\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
            EntityMapping(
                term_type="SKU", source_file="skus.csv",
                standard_name_column="jan",
                node_key_parts=[ColumnNodeKeyPart(column="jan")],
                field_mappings={},
            ),
        ],
        relations=[
            RelationMapping(
                relation_type="HAS_SKU", source_file="skus.csv",
                subject_term_type="Product", object_term_type="SKU",
            ),
        ],
    )
    graph_client = FakeGraphClient()

    report = await run_schema_etl(conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path)

    assert report.entities_written == 2
    assert report.entities_skipped == 0
    assert report.relations_written == 1
    product = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert product.node_key == "Product:1001"
    assert product.extra_properties == {"md_no": "A123"}
    assert "Product:1001" in graph_client.synced
    assert "SKU:4901234567890" in graph_client.synced
    assert ("Product:1001", "SKU:4901234567890", "HAS_SKU") in graph_client.merged


async def test_run_schema_etl_skips_bad_row_and_reports_it(tmp_path):
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n"
        "1001,圆角收纳盒,A123\n"
        ",没有ID的商品,B456\n",  # 第二行缺 product_group_id
        encoding="utf-8",
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 1
    assert report.entities_skipped == 1
    assert len(report.skipped_rows) == 1
    assert "products.csv" in report.skipped_rows[0].source_file


async def test_run_schema_etl_rerun_is_idempotent(tmp_path):
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    await run_schema_etl(conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path)
    await run_schema_etl(conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path)

    from app.graphrag.terms_store import list_terms
    all_terms = await list_terms(conn, tenant_id="muji")
    assert len(all_terms) == 1


async def test_run_schema_etl_skips_bad_row_in_the_middle_and_still_processes_the_rest(tmp_path):
    """坏行出现在文件中间时，它后面的行也必须继续处理——不能因为一行脏数据
    就提前结束整个文件的遍历（设计文档第 6.4 节）。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n"
        "1001,圆角收纳盒,A123\n"
        ",没有ID的商品,B456\n"  # 中间这一行缺 product_group_id
        "1003,亚麻抱枕套,C789\n",
        encoding="utf-8",
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )
    graph_client = FakeGraphClient()

    report = await run_schema_etl(
        conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
    )

    assert report.entities_written == 2
    assert report.entities_skipped == 1
    assert graph_client.synced == ["Product:1001", "Product:1003"]
    assert (await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")) is not None
    assert (await get_term(conn, tenant_id="muji", standard_name="亚麻抱枕套")) is not None
    assert report.skipped_rows[0].row_number == 3


async def test_run_schema_etl_unconfirmed_relation_type_skips_only_that_mapping(tmp_path):
    """relation_type 不在已确认 schema 里，只跳过这一个 mapping、记进
    skipped_mappings，其余实体照常写入，不抛异常中断整次运行。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n", encoding="utf-8"
    )
    (tmp_path / "skus.csv").write_text(
        "jan,product_group_id\n4901234567890,1001\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
            EntityMapping(
                term_type="SKU", source_file="skus.csv",
                standard_name_column="jan",
                node_key_parts=[ColumnNodeKeyPart(column="jan")],
                field_mappings={},
            ),
        ],
        relations=[
            RelationMapping(
                relation_type="NOT_REGISTERED", source_file="skus.csv",
                subject_term_type="Product", object_term_type="SKU",
            ),
        ],
    )
    graph_client = FakeGraphClient()

    report = await run_schema_etl(
        conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
    )

    assert report.entities_written == 2
    assert report.relations_written == 0
    assert graph_client.merged == []
    assert len(report.skipped_mappings) == 1
    assert report.skipped_mappings[0].label == "NOT_REGISTERED"


async def test_run_schema_etl_relation_type_confirmed_but_combination_not_allowed_skips_only_that_mapping(
    tmp_path,
):
    """relation_type 本身已确认，但 (subject_term_type, relation_type,
    object_term_type) 这个组合不在已确认的允许列表里——只跳过这一个
    mapping、记进 skipped_mappings，其余实体照常写入，不抛异常中断整次
    运行。HAS_SKU 在 _confirmed_conn() 里只允许 (Product, HAS_SKU, SKU)
    这一个方向，这里故意把主体/客体类型颠倒过来触发校验。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n", encoding="utf-8"
    )
    (tmp_path / "skus.csv").write_text(
        "jan,product_group_id\n4901234567890,1001\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
            EntityMapping(
                term_type="SKU", source_file="skus.csv",
                standard_name_column="jan",
                node_key_parts=[ColumnNodeKeyPart(column="jan")],
                field_mappings={},
            ),
        ],
        relations=[
            RelationMapping(
                relation_type="HAS_SKU", source_file="skus.csv",
                subject_term_type="SKU", object_term_type="Product",
            ),
        ],
    )
    graph_client = FakeGraphClient()

    report = await run_schema_etl(
        conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
    )

    assert report.entities_written == 2
    assert report.relations_written == 0
    assert graph_client.merged == []
    assert len(report.skipped_mappings) == 1
    assert report.skipped_mappings[0].label == "HAS_SKU"
    assert "允许列表" in report.skipped_mappings[0].reason


async def test_run_schema_etl_unregistered_term_type_skips_only_that_mapping(tmp_path):
    """term_type 没在该租户注册过时同理：跳过这一个实体 mapping，
    其它实体和关系照常处理。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n", encoding="utf-8"
    )
    (tmp_path / "skus.csv").write_text(
        "jan,product_group_id\n4901234567890,1001\n", encoding="utf-8"
    )
    (tmp_path / "unknown.csv").write_text("code,name\nX1,未知类型\n", encoding="utf-8")
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="NotRegistered", source_file="unknown.csv",
                standard_name_column="name",
                node_key_parts=[ColumnNodeKeyPart(column="code")],
                field_mappings={},
            ),
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
            EntityMapping(
                term_type="SKU", source_file="skus.csv",
                standard_name_column="jan",
                node_key_parts=[ColumnNodeKeyPart(column="jan")],
                field_mappings={},
            ),
        ],
        relations=[
            RelationMapping(
                relation_type="HAS_SKU", source_file="skus.csv",
                subject_term_type="Product", object_term_type="SKU",
            ),
        ],
    )
    graph_client = FakeGraphClient()

    report = await run_schema_etl(
        conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
    )

    assert report.entities_written == 2
    assert report.relations_written == 1
    assert len(report.skipped_mappings) == 1
    assert report.skipped_mappings[0].label == "NotRegistered"
    assert report.skipped_mappings[0].source_file == "unknown.csv"


async def test_run_schema_etl_relation_endpoint_never_written_is_skipped_not_ghost_merged(tmp_path):
    """关系文件引用了一个实体文件里从来没出现过的原始值时，必须跳过这一行——
    绝不能顺手给它分配一个新的稳定码、MERGE 出一个没有对应 Term 记录的幽灵节点。"""
    conn = await _confirmed_conn()
    (tmp_path / "variant_values.csv").write_text(
        "dim_code,raw_value\ndim_007,抹茶\n", encoding="utf-8"
    )
    (tmp_path / "variant_links.csv").write_text(
        "dim_code,raw_value\ndim_007,从没出现过的值\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="VariantValue", source_file="variant_values.csv",
                standard_name_column="raw_value",
                node_key_parts=[
                    AllocatedCodeNodeKeyPart(scope_columns=["dim_code"], raw_value_column="raw_value")
                ],
                field_mappings={},
            ),
        ],
        relations=[
            RelationMapping(
                relation_type="HAS_VARIANT_VALUE", source_file="variant_links.csv",
                subject_term_type="VariantValue", object_term_type="VariantValue",
            ),
        ],
    )
    graph_client = FakeGraphClient()

    report = await run_schema_etl(
        conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
    )

    assert report.entities_written == 1
    assert report.relations_written == 0
    assert report.relations_skipped == 1
    assert graph_client.merged == []
    # 关系路径没有消耗计数位：scope 下只有实体路径分配过的那一个码
    next_code = await allocate_stable_code(
        conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="从没出现过的值"
    )
    assert next_code == "00002"


async def test_run_schema_etl_column_key_endpoint_never_written_is_skipped_not_ghost_merged(
    tmp_path,
):
    """普通列节点键也要有和稳定码同样的守卫：关系文件引用了一个实体文件里
    从来没写成功过的值时，必须跳过这一行，不能 MERGE 出一个只有 node_key、
    没有 type/standard_name 的幽灵节点。

    这是真实发生过的事故：demo 租户的 `类目:Coffee` 和 `销量:1000` 两个实体
    因为当时的唯一索引冲突写入失败，关系写入这一趟却照写不误，在图里留下两个
    裸 :Term 节点挂着 16 条边。稳定码路径早就有守卫（见上一个测试），
    ColumnNodeKeyPart 路径没有。
    """
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text("name,md_no\nP1,MD1\n", encoding="utf-8")
    (tmp_path / "skus.csv").write_text("sku_code\nS1\n", encoding="utf-8")
    (tmp_path / "links.csv").write_text("name,sku_code\nP1,S_NEVER_WRITTEN\n", encoding="utf-8")
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv", standard_name_column="name",
                node_key_parts=[ColumnNodeKeyPart(column="name")], field_mappings={},
            ),
            EntityMapping(
                term_type="SKU", source_file="skus.csv", standard_name_column="sku_code",
                node_key_parts=[ColumnNodeKeyPart(column="sku_code")], field_mappings={},
            ),
        ],
        relations=[
            RelationMapping(
                relation_type="HAS_SKU", source_file="links.csv",
                subject_term_type="Product", object_term_type="SKU",
            ),
        ],
    )
    graph_client = FakeGraphClient()

    report = await run_schema_etl(
        conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
    )

    assert report.entities_written == 2
    assert report.relations_written == 0
    assert report.relations_skipped == 1
    assert graph_client.merged == []


async def test_run_schema_etl_reports_per_type_counts(tmp_path):
    """设计文档第 6.4 节要求汇总报告能按 term_type/relation_type 分别给出
    写入行数，不只是整次运行的总量。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n", encoding="utf-8"
    )
    (tmp_path / "skus.csv").write_text(
        "jan,product_group_id\n4901234567890,1001\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
            EntityMapping(
                term_type="SKU", source_file="skus.csv",
                standard_name_column="jan",
                node_key_parts=[ColumnNodeKeyPart(column="jan")],
                field_mappings={},
            ),
        ],
        relations=[
            RelationMapping(
                relation_type="HAS_SKU", source_file="skus.csv",
                subject_term_type="Product", object_term_type="SKU",
            ),
        ],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.written_by_type == {"Product": 1, "SKU": 1, "HAS_SKU": 1}
    assert report.skipped_by_type == {}


async def test_run_schema_etl_reads_gbk_encoded_csv(tmp_path):
    """国内 Excel 导出 CSV 常见默认编码是 GBK，不是 UTF-8——见
    docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md
    决策 6。这里直接写 GBK 编码的字节，不依赖任何自动转码工具，验证读取器
    自己能探测出编码并正确解码出中文列名/值。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_bytes(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n".encode("gbk")
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 1
    assert report.entities_skipped == 0
    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term is not None
    assert term.node_key == "Product:1001"


async def test_run_schema_etl_reads_tsv_source_file(tmp_path):
    """TSV 只是分隔符从逗号换成制表符，验证扩展名 .tsv 能被正确识别并用
    制表符分隔解析——见决策 1。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.tsv").write_text(
        "product_group_id\tproduct_group_name\tmd_no\n1001\t圆角收纳盒\tA123\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.tsv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 1
    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term is not None
    assert term.node_key == "Product:1001"


def _write_xlsx_fixture(path, *, header: list[str], rows: list[list[object]]) -> None:
    """用 openpyxl 生成一个最小的 xlsx 测试夹具文件——openpyxl 既能读也能
    写，不需要额外引入别的库。"""
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(header)
    for row in rows:
        worksheet.append(row)
    workbook.save(str(path))


def _write_xls_fixture(path, *, header: list[str], rows: list[list[object]]) -> None:
    """用 xlwt 生成一个最小的 xls（旧版二进制 Excel）测试夹具文件——xlrd
    只能读 xls 不能写，xlwt 只能写 xls 不能写 xlsx，两个库分工明确，这里
    只用来造测试数据，生产代码不导入 xlwt。"""
    import xlwt

    workbook = xlwt.Workbook()
    worksheet = workbook.add_sheet("Sheet1")
    for col, name in enumerate(header):
        worksheet.write(0, col, name)
    for row_idx, row in enumerate(rows, start=1):
        for col, value in enumerate(row):
            worksheet.write(row_idx, col, value)
    workbook.save(str(path))


async def test_run_schema_etl_reads_xlsx_source_file(tmp_path):
    conn = await _confirmed_conn()
    _write_xlsx_fixture(
        tmp_path / "products.xlsx",
        header=["product_group_id", "product_group_name", "md_no"],
        rows=[[1001, "圆角收纳盒", "A123"]],
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.xlsx",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 1
    assert report.entities_skipped == 0
    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term is not None
    assert term.node_key == "Product:1001"
    # xlsx 单元格里 1001 是原生 int，node_key 必须是 "Product:1001" 而不是
    # "Product:1001.0"——验证 convert_excel_cell_to_string 真的被用在了
    # 读取路径上，不是只在 Task 2 的单元测试里孤立存在。
    # 注意：get_term(conn, tenant_id, standard_name) 的真实签名只接受
    # standard_name，不接受 node_key 参数——按 standard_name 查询，
    # 再断言返回对象的 .node_key 字段。


async def test_run_schema_etl_reads_xls_source_file(tmp_path):
    conn = await _confirmed_conn()
    _write_xls_fixture(
        tmp_path / "products.xls",
        header=["product_group_id", "product_group_name", "md_no"],
        rows=[[1001, "圆角收纳盒", "A123"]],
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.xls",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 1
    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term is not None
    assert term.node_key == "Product:1001"


async def test_run_schema_etl_xlsx_empty_sheet_writes_nothing(tmp_path):
    """只有表头没有数据行的 xlsx，不应该报错，也不应该写入任何实体——跟
    CSV 场景下"只有表头"的行为一致。"""
    conn = await _confirmed_conn()
    _write_xlsx_fixture(
        tmp_path / "products.xlsx",
        header=["product_group_id", "product_group_name", "md_no"],
        rows=[],
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.xlsx",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 0
    assert report.entities_skipped == 0


async def test_run_schema_etl_xlsx_phantom_trailing_row_is_skipped_not_counted(tmp_path):
    """openpyxl 的 read_only 迭代会按工作表已用范围补齐行数，哪怕某一行
    早就被清空也会产出全空的幽灵行（常见于手工编辑过的 Excel 导出文件）。
    这条用例专门构造一个"先写值、再清空"的行，验证它被安静跳过、不计入
    entities_skipped，而不是被当成脏数据报出来。"""
    conn = await _confirmed_conn()

    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["product_group_id", "product_group_name", "md_no"])
    worksheet.append([1001, "圆角收纳盒", "A123"])
    # 故意先写一个值再清空，让 openpyxl 的已用范围延伸到这一行——这正是
    # 幽灵行产生的真实场景，不是凭空构造的极端情况。
    worksheet.append(["temp", "temp", "temp"])
    worksheet["A3"] = None
    worksheet["B3"] = None
    worksheet["C3"] = None
    workbook.save(str(tmp_path / "products.xlsx"))

    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.xlsx",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 1
    assert report.entities_skipped == 0
    assert report.skipped_rows == []


async def test_run_schema_etl_reads_utf8_bom_encoded_csv(tmp_path):
    """Excel 的"CSV UTF-8"导出格式会在文件开头写一个 BOM——见
    docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md
    决策 6 的补充说明。这里直接写带 BOM 的字节，验证读取器能正确剥掉
    BOM、不会让它粘在表头第一列名字前面导致列名对不上。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_bytes(
        "﻿product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n".encode("utf-8")
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 1
    assert report.entities_skipped == 0
    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term is not None
    assert term.node_key == "Product:1001"
