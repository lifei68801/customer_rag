"""端到端核心保证测试：人工编辑不被 ETL 重跑覆盖。

这些测试跨越编辑层、合并视图、ETL 写入和图谱同步四个模块，
验证本设计的主要保证：重跑 ETL 保留人工修正，但仍接收未编辑字段的更新。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
from app.graphrag.ontology_constraints import add_allowed_combination
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.ontology_relations import create_relation_type
from app.graphrag.schema_etl import run_schema_etl
from app.graphrag.schema_etl_config import (
    ColumnNodeKeyPart,
    EntityMapping,
    RelationMapping,
    SchemaETLConfig,
)
from app.graphrag.term_edits_store import (
    FIELD_DELETED,
    ensure_term_edits_schema,
    upsert_term_edit,
)
from app.graphrag.terms_store import (
    ensure_terms_schema,
    get_term_merged_by_node_key,
    list_term_edits,
    list_terms_merged,
)
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema

pytestmark = pytest.mark.anyio


class FakeGraphClient:
    """记录图谱同步调用的假客户端。"""

    def __init__(self) -> None:
        self.synced: list[str] = []
        self.synced_terms: list = []
        self.merged: list[tuple[str, str, str]] = []
        self.deleted_nodes: list[str] = []
        self.stale_sweeps: list[tuple[str, str]] = []

    async def sync_term(self, term) -> None:
        self.synced.append(term.node_key)
        self.synced_terms.append(term)

    async def merge_relation(
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
        provenance,
        recorded_at,
    ) -> None:
        self.merged.append((subject_standard_name, object_standard_name, relation_type))

    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None:
        self.deleted_nodes.append(node_key)

    async def delete_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> int:
        self.stale_sweeps.append((source, before_recorded_at))
        return 0


async def _confirmed_conn() -> aiosqlite.Connection:
    """建好各表、注册确认了 Product/SKU/VariantValue 三个 term_type 的连接。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_stable_code_registry_schema(conn)
    await create_term_type(
        conn,
        tenant_id="muji",
        value="Product",
        extra_fields=[ExtraFieldSpec(name="md_no", value_type="string")],
    )
    await create_term_type(conn, tenant_id="muji", value="SKU")
    await create_term_type(conn, tenant_id="muji", value="VariantValue")
    await checkout_draft(conn, "muji")
    await create_relation_type(
        conn,
        "muji",
        relation_type="HAS_SKU",
        example_phrase="Product HAS_SKU SKU",
    )
    await create_relation_type(
        conn,
        "muji",
        relation_type="HAS_VARIANT_VALUE",
        example_phrase="VariantValue HAS_VARIANT_VALUE VariantValue",
    )
    await add_allowed_combination(
        conn,
        "muji",
        subject_term_type="Product",
        relation_type="HAS_SKU",
        object_term_type="SKU",
    )
    await add_allowed_combination(
        conn,
        "muji",
        subject_term_type="VariantValue",
        relation_type="HAS_VARIANT_VALUE",
        object_term_type="VariantValue",
    )
    await confirm_ontology(conn, "muji")
    return conn


async def test_etl_rerun_keeps_the_manual_display_name_and_updates_other_fields(tmp_path):
    """本设计的核心保证。

    ETL 写入实体 → 人工改展示名 → 改源文件：展示名改成别的值，同时把那个属性字段也改成新值
    → 重跑 ETL → 断言展示名仍是人工值；那个属性字段取到了 ETL 的新值。

    第二半的断言至关重要——它证明合并是字段级的，没退化成整行覆盖。
    """
    conn = await _confirmed_conn()
    graph_client = FakeGraphClient()

    # 第一次 ETL：写入产品
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n",
        encoding="utf-8",
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product",
                source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )
    await run_schema_etl(
        conn=conn,
        graph_client=graph_client,
        config=config,
        data_dir=tmp_path,
    )

    # 人工改展示名
    await upsert_term_edit(
        conn,
        tenant_id="muji",
        node_key="Product:1001",
        field="standard_name",
        value="手动改的名字",
        edited_by="admin",
    )

    # 改源文件：展示名改成别的值，同时把 md_no 也改成新值
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,新的ETL名字,B456\n",
        encoding="utf-8",
    )

    # 重跑 ETL
    graph_client_2 = FakeGraphClient()
    await run_schema_etl(
        conn=conn,
        graph_client=graph_client_2,
        config=config,
        data_dir=tmp_path,
    )

    # 断言：展示名应该是人工值
    term = await get_term_merged_by_node_key(conn, "muji", "Product:1001")
    assert term.standard_name == "手动改的名字", "展示名应该保持人工改过的值"

    # 断言：md_no 应该是 ETL 的新值（字段级隔离）
    assert term.extra_properties.get("md_no") == "B456", "未编辑的属性字段应该取到 ETL 的新值"


async def test_manual_deletion_survives_an_etl_rerun(tmp_path):
    """人工删除 → 重跑 ETL → 断言该实体在合并视图和图谱里都不出现。

    今天的行为是它会复活（upsert 重新插入）。本设计应该防止这种复活。
    """
    conn = await _confirmed_conn()

    # 第一次 ETL：写入产品
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n",
        encoding="utf-8",
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product",
                source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )
    graph_client = FakeGraphClient()
    await run_schema_etl(
        conn=conn,
        graph_client=graph_client,
        config=config,
        data_dir=tmp_path,
    )

    # 人工标记删除
    await upsert_term_edit(
        conn,
        tenant_id="muji",
        node_key="Product:1001",
        field=FIELD_DELETED,
        value=None,
        edited_by="admin",
    )

    # 重跑 ETL
    graph_client_2 = FakeGraphClient()
    await run_schema_etl(
        conn=conn,
        graph_client=graph_client_2,
        config=config,
        data_dir=tmp_path,
    )

    # 断言：实体不应该在合并视图里出现
    terms = await list_terms_merged(conn, "muji")
    node_keys = [t.node_key for t in terms]
    assert "Product:1001" not in node_keys, "已删除的实体不应该在合并视图里出现"

    # 断言：图谱侧应该调用 delete_term_node，不应该调用 sync_term
    assert "Product:1001" in graph_client_2.deleted_nodes, "图谱侧应该删除该节点"
    assert "Product:1001" not in graph_client_2.synced, "图谱侧不应该同步已删除的节点"


async def test_editing_one_field_does_not_freeze_the_others(tmp_path):
    """字段级隔离：只编辑 standard_name，断言 extra_properties 仍随 ETL 更新。

    防止实现退化成整行覆盖。
    """
    conn = await _confirmed_conn()

    # 第一次 ETL：写入产品
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n",
        encoding="utf-8",
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product",
                source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )
    graph_client = FakeGraphClient()
    await run_schema_etl(
        conn=conn,
        graph_client=graph_client,
        config=config,
        data_dir=tmp_path,
    )

    # 只编辑 standard_name
    await upsert_term_edit(
        conn,
        tenant_id="muji",
        node_key="Product:1001",
        field="standard_name",
        value="手动改的名字",
        edited_by="admin",
    )

    # 改源文件：只改 md_no，standard_name 保持相同以隔离变量
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,B456\n",
        encoding="utf-8",
    )

    # 重跑 ETL
    graph_client_2 = FakeGraphClient()
    await run_schema_etl(
        conn=conn,
        graph_client=graph_client_2,
        config=config,
        data_dir=tmp_path,
    )

    # 断言：standard_name 仍是人工值
    term = await get_term_merged_by_node_key(conn, "muji", "Product:1001")
    assert term.standard_name == "手动改的名字"

    # 断言：md_no 更新为 ETL 的新值（字段级隔离）
    assert (
        term.extra_properties.get("md_no") == "B456"
    ), "只编辑了 standard_name，other fields 应该随 ETL 更新"


async def test_etl_path_never_writes_term_edits(tmp_path):
    """全局约束第一条的直接断言：跑一整轮 ETL（含实体写入、关系写入、sweep），
    断言 term_edits 表一行没多。
    """
    conn = await _confirmed_conn()

    # 查看初始行数（应该是 0）
    cursor = await conn.execute("SELECT COUNT(*) FROM term_edits")
    initial_count = (await cursor.fetchone())[0]
    assert initial_count == 0

    # 跑一整轮 ETL
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n1002,储物盒,B456\n",
        encoding="utf-8",
    )
    (tmp_path / "skus.csv").write_text(
        "jan,product_group_id\n4901234567890,1001\n4901234567891,1002\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product",
                source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
            EntityMapping(
                term_type="SKU",
                source_file="skus.csv",
                standard_name_parts=["jan"],
                node_key_parts=[ColumnNodeKeyPart(column="jan")],
                field_mappings={},
            ),
        ],
        relations=[
            RelationMapping(
                relation_type="HAS_SKU",
                source_file="skus.csv",
                subject_term_type="Product",
                object_term_type="SKU",
            ),
        ],
    )
    graph_client = FakeGraphClient()
    await run_schema_etl(
        conn=conn,
        graph_client=graph_client,
        config=config,
        data_dir=tmp_path,
        allow_large_sweep=True,
    )

    # 再查行数（应该还是 0）
    cursor = await conn.execute("SELECT COUNT(*) FROM term_edits")
    final_count = (await cursor.fetchone())[0]
    assert final_count == initial_count, "ETL 不应该写入 term_edits 表"
