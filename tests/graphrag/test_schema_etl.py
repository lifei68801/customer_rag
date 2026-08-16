from __future__ import annotations

from datetime import datetime
from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.ontology_categories import ExtraFieldSpec, create_product_line, create_term_type
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.ontology_relations import create_relation_type
from app.graphrag.schema_etl import SchemaETLNotConfirmedError, run_schema_etl
from app.graphrag.schema_etl_config import ColumnNodeKeyPart, EntityMapping, RelationMapping, SchemaETLConfig
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


async def _confirmed_conn(tmp_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_stable_code_registry_schema(conn)
    await create_term_type(
        conn, tenant_id="muji", value="Product",
        extra_fields=[ExtraFieldSpec(name="md_no", value_type="string")],
    )
    await create_term_type(conn, tenant_id="muji", value="SKU")
    await create_product_line(conn, value="MUJI")
    await checkout_draft(conn, "muji")
    await create_relation_type(
        conn, "muji", relation_type="HAS_SKU", example_phrase="Product HAS_SKU SKU",
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
    conn = await _confirmed_conn(tmp_path)
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
                term_type="Product", source_file="products.csv", product_line="MUJI",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
            EntityMapping(
                term_type="SKU", source_file="skus.csv", product_line="MUJI",
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
    conn = await _confirmed_conn(tmp_path)
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
                term_type="Product", source_file="products.csv", product_line="MUJI",
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
    conn = await _confirmed_conn(tmp_path)
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv", product_line="MUJI",
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
