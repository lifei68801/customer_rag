from __future__ import annotations

from pathlib import Path

import pytest

from app.graphrag.schema_etl_config import (
    AllocatedCodeNodeKeyPart,
    ColumnNodeKeyPart,
    EntityMapping,
    InvalidSchemaETLConfigError,
    RelationMapping,
    load_schema_etl_config,
)


def test_load_schema_etl_config_parses_entities_and_relations(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
tenant_id: muji

entities:
  - term_type: Product
    source_file: products.csv
    standard_name_column: product_group_name
    node_key_parts:
      - column: product_group_id
    field_mappings:
      md_no: md_no

  - term_type: VariantValue
    source_file: variant_values.csv
    standard_name_column: label_cn
    node_key_parts:
      - column: dim_code
      - allocated_code:
          scope_columns: [dim_code]
          raw_value_column: raw_value
    field_mappings:
      numeric_value: numeric_value

relations:
  - relation_type: HAS_SKU
    source_file: skus.csv
    subject_term_type: Product
    object_term_type: SKU
""",
        encoding="utf-8",
    )

    config = load_schema_etl_config(config_path)

    assert config.tenant_id == "muji"
    assert len(config.entities) == 2
    product = config.entities[0]
    assert product.term_type == "Product"
    assert product.source_file == "products.csv"
    assert product.standard_name_parts == ["product_group_name"]
    assert product.node_key_parts == [ColumnNodeKeyPart(column="product_group_id")]
    assert product.field_mappings == {"md_no": "md_no"}

    variant = config.entities[1]
    assert variant.node_key_parts == [
        ColumnNodeKeyPart(column="dim_code"),
        AllocatedCodeNodeKeyPart(scope_columns=["dim_code"], raw_value_column="raw_value"),
    ]

    assert len(config.relations) == 1
    relation = config.relations[0]
    assert relation == RelationMapping(
        relation_type="HAS_SKU", source_file="skus.csv",
        subject_term_type="Product", object_term_type="SKU",
    )


def test_load_schema_etl_config_rejects_missing_tenant_id(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("entities: []\nrelations: []\n", encoding="utf-8")

    with pytest.raises(InvalidSchemaETLConfigError):
        load_schema_etl_config(config_path)


def test_load_schema_etl_config_rejects_entity_with_no_node_key_parts(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
tenant_id: muji
entities:
  - term_type: Product
    source_file: products.csv
    standard_name_column: name
    node_key_parts: []
    field_mappings: {}
relations: []
""",
        encoding="utf-8",
    )

    with pytest.raises(InvalidSchemaETLConfigError):
        load_schema_etl_config(config_path)


def test_load_schema_etl_config_defaults_entities_and_relations_to_empty_list(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("tenant_id: muji\n", encoding="utf-8")

    config = load_schema_etl_config(config_path)

    assert config.entities == []
    assert config.relations == []


def test_load_schema_etl_config_rejects_entity_missing_required_field(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
tenant_id: muji
entities:
  - term_type: Product
    standard_name_column: name
    node_key_parts:
      - column: id
    field_mappings: {}
relations: []
""",
        encoding="utf-8",
    )

    with pytest.raises(InvalidSchemaETLConfigError):
        load_schema_etl_config(config_path)


def test_load_schema_etl_config_rejects_relation_missing_required_field(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
tenant_id: muji
entities: []
relations:
  - relation_type: HAS_SKU
    source_file: skus.csv
    subject_term_type: Product
""",
        encoding="utf-8",
    )

    with pytest.raises(InvalidSchemaETLConfigError):
        load_schema_etl_config(config_path)


def test_load_schema_etl_config_rejects_allocated_code_missing_scope_columns(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
tenant_id: muji
entities:
  - term_type: VariantValue
    source_file: variant_values.csv
    standard_name_column: label_cn
    node_key_parts:
      - allocated_code:
          raw_value_column: raw_value
    field_mappings: {}
relations: []
""",
        encoding="utf-8",
    )

    with pytest.raises(InvalidSchemaETLConfigError):
        load_schema_etl_config(config_path)


def test_standard_name_column_is_normalized_into_parts(tmp_path):
    """单列写法必须继续可用——已有租户配置一行都不改。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        "tenant_id: t1\n"
        "entities:\n"
        "  - term_type: 产品\n"
        "    source_file: a.csv\n"
        "    standard_name_column: Product\n"
        "    node_key_parts:\n"
        "      - column: Product\n",
        encoding="utf-8",
    )

    config = load_schema_etl_config(path)

    assert config.entities[0].standard_name_parts == ["Product"]


def test_standard_name_parts_accepts_multiple_columns(tmp_path):
    """多列写法把判别列拼进展示名，让同名不同实体在界面上可区分。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        "tenant_id: t1\n"
        "entities:\n"
        "  - term_type: 用户名\n"
        "    source_file: a.csv\n"
        "    standard_name_parts: [Customer Name, Customer Zip Code]\n"
        "    node_key_parts:\n"
        "      - column: Customer Name\n"
        "      - column: Customer Zip Code\n",
        encoding="utf-8",
    )

    config = load_schema_etl_config(path)

    assert config.entities[0].standard_name_parts == ["Customer Name", "Customer Zip Code"]


def test_entity_mapping_requires_one_of_the_two_forms(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "tenant_id: t1\n"
        "entities:\n"
        "  - term_type: 产品\n"
        "    source_file: a.csv\n"
        "    node_key_parts:\n"
        "      - column: Product\n",
        encoding="utf-8",
    )

    with pytest.raises(InvalidSchemaETLConfigError):
        load_schema_etl_config(path)


def test_load_schema_etl_config_rejects_allocated_code_missing_raw_value_column(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
tenant_id: muji
entities:
  - term_type: VariantValue
    source_file: variant_values.csv
    standard_name_column: label_cn
    node_key_parts:
      - allocated_code:
          scope_columns: [dim_code]
    field_mappings: {}
relations: []
""",
        encoding="utf-8",
    )

    with pytest.raises(InvalidSchemaETLConfigError):
        load_schema_etl_config(config_path)
