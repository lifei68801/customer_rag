from __future__ import annotations

import csv
import io
from pathlib import Path

import aiosqlite
import pytest
import yaml

from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory, create_term_type, list_term_types
from app.graphrag.ontology_constraints import AllowedCombination, add_allowed_combination, list_allowed_combinations
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.ontology_relations import create_relation_type
from app.graphrag.schema_etl import run_schema_etl
from app.graphrag.schema_etl_config import load_schema_etl_config
from app.graphrag.schema_etl_sample import (
    EmptySchemaError,
    SampleFile,
    generate_schema_etl_sample_files,
)
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema

pytestmark = pytest.mark.anyio


def _csv_rows(content: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content)))


def test_generate_raises_when_no_confirmed_term_types():
    with pytest.raises(EmptySchemaError):
        generate_schema_etl_sample_files(tenant_id="demo", term_types=[], allowed_combinations=[])


def test_entity_csv_includes_node_key_name_and_field_columns_with_two_example_rows():
    term_types = [
        TermTypeCategory(
            value="商品",
            extra_fields=[
                ExtraFieldSpec(name="价格", value_type="number"),
                ExtraFieldSpec(name="型号", value_type="string"),
            ],
        ),
    ]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=[]
    )

    entity_file = next(f for f in files if f.filename == "商品.csv")
    rows = _csv_rows(entity_file.content)
    assert len(rows) == 2
    assert rows[0] == {
        "商品编号": "商品001",
        "商品名称": "示例商品1",
        "价格列": "1.5",
        "型号列": "示例文本1",
    }
    assert rows[1] == {
        "商品编号": "商品002",
        "商品名称": "示例商品2",
        "价格列": "2.5",
        "型号列": "示例文本2",
    }


def test_entity_csv_with_no_extra_fields_only_has_node_key_and_name_columns():
    term_types = [TermTypeCategory(value="品类", extra_fields=[])]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=[]
    )

    entity_file = next(f for f in files if f.filename == "品类.csv")
    rows = _csv_rows(entity_file.content)
    assert rows == [
        {"品类编号": "品类001", "品类名称": "示例品类1"},
        {"品类编号": "品类002", "品类名称": "示例品类2"},
    ]


def test_integer_and_number_array_value_types_generate_expected_example_values():
    term_types = [
        TermTypeCategory(
            value="库存",
            extra_fields=[
                ExtraFieldSpec(name="数量", value_type="integer"),
                ExtraFieldSpec(name="坐标", value_type="number[]"),
            ],
        ),
    ]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=[]
    )

    rows = _csv_rows(next(f for f in files if f.filename == "库存.csv").content)
    assert rows[0]["数量列"] == "1"
    assert rows[1]["数量列"] == "2"
    assert rows[0]["坐标列"] == "1.5;2.5"
    assert rows[1]["坐标列"] == "3.5;4.5"


def test_relation_csv_includes_subject_and_object_node_key_columns():
    term_types = [
        TermTypeCategory(value="商品", extra_fields=[]),
        TermTypeCategory(value="品类", extra_fields=[]),
    ]
    combos = [AllowedCombination(subject_term_type="商品", relation_type="BELONG_TO", object_term_type="品类")]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=combos
    )

    relation_file = next(f for f in files if f.filename == "商品_BELONG_TO_品类.csv")
    rows = _csv_rows(relation_file.content)
    assert rows == [
        {"商品编号": "商品001", "品类编号": "品类001"},
        {"商品编号": "商品002", "品类编号": "品类002"},
    ]


def test_relation_csv_for_self_relation_reuses_one_shared_column():
    term_types = [TermTypeCategory(value="品类", extra_fields=[])]
    combos = [AllowedCombination(subject_term_type="品类", relation_type="RELATED_TO", object_term_type="品类")]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=combos
    )

    relation_file = next(f for f in files if f.filename == "品类_RELATED_TO_品类.csv")
    rows = _csv_rows(relation_file.content)
    # 主体/客体是同一个 term_type 时，node_key 列名相同，配置格式本身没法用
    # 一行 CSV 表达"品类 A 关联到不同的品类 B"——这不是本生成器的缺陷，是
    # schema_etl_row_processing.py::compute_node_key 对主体/客体各自独立取
    # 同名列的既有行为（两次取值天然相同）。示例如实反映这个真实限制。
    assert rows == [{"品类编号": "品类001"}, {"品类编号": "品类002"}]


def test_config_yaml_round_trips_through_load_schema_etl_config(tmp_path):
    term_types = [
        TermTypeCategory(
            value="商品",
            extra_fields=[ExtraFieldSpec(name="价格", value_type="number")],
        ),
        TermTypeCategory(value="品类", extra_fields=[]),
    ]
    combos = [AllowedCombination(subject_term_type="商品", relation_type="BELONG_TO", object_term_type="品类")]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=combos
    )
    config_file = next(f for f in files if f.filename == "config.yaml")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_file.content, encoding="utf-8")

    config = load_schema_etl_config(config_path)

    assert config.tenant_id == "demo"
    assert [e.term_type for e in config.entities] == ["商品", "品类"]
    product_mapping = config.entities[0]
    assert product_mapping.source_file == "商品.csv"
    assert product_mapping.standard_name_parts == ["商品名称"]
    assert product_mapping.field_mappings == {"价格": "价格列"}
    assert len(config.relations) == 1
    assert config.relations[0].source_file == "商品_BELONG_TO_品类.csv"


def test_config_yaml_has_empty_relations_list_when_no_combinations_confirmed():
    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=[TermTypeCategory(value="品类", extra_fields=[])],
        allowed_combinations=[],
    )
    config_file = next(f for f in files if f.filename == "config.yaml")
    parsed = yaml.safe_load(config_file.content)
    assert parsed["relations"] == []


def test_unsafe_term_type_characters_are_sanitized_out_of_the_filename():
    term_types = [TermTypeCategory(value="a/../b", extra_fields=[])]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=[]
    )

    filenames = [f.filename for f in files]
    assert "config.yaml" in filenames
    entity_filenames = [f for f in filenames if f != "config.yaml"]
    assert len(entity_filenames) == 1
    assert "/" not in entity_filenames[0]
    assert ".." not in entity_filenames[0]


class _FakeGraphClient:
    async def sync_term(self, term) -> None:
        pass

    async def merge_relation(self, **kwargs) -> None:
        pass

    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None:
        pass

    async def count_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> tuple[int, int]:
        # 样例配置不关心安全阀，(0, 0) 表示"没有陈旧边"，不会触发。
        return (0, 0)

    async def delete_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> int:
        # 这份假客户端只验证样例配置能跑通 run_schema_etl，不关心扫除计数。
        return 0


async def test_generated_sample_files_run_successfully_through_run_schema_etl(tmp_path):
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_stable_code_registry_schema(conn)
    await create_term_type(
        conn, tenant_id="demo", value="Product",
        extra_fields=[ExtraFieldSpec(name="price", value_type="number")],
    )
    await create_term_type(conn, tenant_id="demo", value="Category")
    await checkout_draft(conn, "demo")
    await create_relation_type(
        conn, "demo", relation_type="BELONG_TO", example_phrase="Product BELONG_TO Category",
    )
    await add_allowed_combination(
        conn, "demo", subject_term_type="Product", relation_type="BELONG_TO", object_term_type="Category",
    )
    await confirm_ontology(conn, "demo")

    term_types = await list_term_types(conn, "demo", status="confirmed")
    combos = await list_allowed_combinations(conn, "demo", status="confirmed")
    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=combos
    )
    for f in files:
        (tmp_path / f.filename).write_text(f.content, encoding="utf-8")
    config = load_schema_etl_config(tmp_path / "config.yaml")

    report = await run_schema_etl(
        conn=conn, graph_client=_FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 4  # 2 rows Product + 2 rows Category
    assert report.entities_skipped == 0
    assert report.relations_written == 2
    assert report.relations_skipped == 0
