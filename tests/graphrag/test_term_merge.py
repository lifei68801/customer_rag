from __future__ import annotations

import pytest

from app.graphrag.ontology import Term
from app.graphrag.term_edits_store import FIELD_CREATED, FIELD_DELETED, FIELD_EXTRA_PROPERTIES
from app.graphrag.term_merge import apply_edits


def _term(node_key: str, standard_name: str, **kwargs) -> Term:
    return Term(
        tenant_id="t1",
        node_key=node_key,
        standard_name=standard_name,
        aliases=kwargs.get("aliases", []),
        term_type=kwargs.get("term_type", "产品"),
        extra_properties=kwargs.get("extra_properties", {}),
        source=kwargs.get("source", "etl"),
    )


def test_no_edits_returns_terms_unchanged():
    terms = [_term("产品:A", "甲"), _term("产品:B", "乙")]

    assert apply_edits(terms, {}, tenant_id="t1") == terms


def test_field_edit_wins_for_that_field_only():
    """这是本设计的核心保证：人工改过的字段永远优先，未编辑的字段正常
    接受管道更新。整行覆盖会让 ETL 对未编辑字段的更新一并失效。"""
    terms = [_term("产品:A", "管道产出的名字", extra_properties={"revenue": 100})]
    edits = {"产品:A": {"standard_name": "人工改过的名字"}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert merged[0].standard_name == "人工改过的名字"
    assert merged[0].extra_properties == {"revenue": 100}


def test_extra_property_edit_is_field_level_within_extra_properties():
    """extra_properties.<name> 只覆盖那一个属性，同一个字典里其余属性
    仍然跟随管道。"""
    terms = [_term("产品:A", "甲", extra_properties={"revenue": 100, "cost": 60})]
    edits = {"产品:A": {"extra_properties.revenue": 999}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert merged[0].extra_properties == {"revenue": 999, "cost": 60}


def test_aliases_and_term_type_edits_apply():
    terms = [_term("产品:A", "甲", aliases=["旧别名"], term_type="产品")]
    edits = {"产品:A": {"aliases": ["新别名一", "新别名二"], "term_type": "类目"}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert merged[0].aliases == ["新别名一", "新别名二"]
    assert merged[0].term_type == "类目"


def test_deleted_edit_excludes_the_term_entirely():
    """人工删除不可被 ETL 恢复——terms 表里的行还在（ETL 还在维护它），
    但对所有读路径不可见。"""
    terms = [_term("产品:A", "甲"), _term("产品:B", "乙")]
    edits = {"产品:A": {FIELD_DELETED: None}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert [t.node_key for t in merged] == ["产品:B"]


def test_deleted_wins_even_when_other_field_edits_exist():
    terms = [_term("产品:A", "甲")]
    edits = {"产品:A": {FIELD_DELETED: None, "standard_name": "改过的名字"}}

    assert apply_edits(terms, edits, tenant_id="t1") == []


def test_created_edit_synthesizes_a_term_that_terms_table_lacks():
    """审核员批准一条关系时可能需要当场创建一个尚不存在的端点实体。
    这条路径是抽取管道能闭环的必要条件。"""
    edits = {
        "产品:NEW": {
            FIELD_CREATED: {
                "standard_name": "人工新建",
                "term_type": "产品",
                "aliases": ["别名"],
                "extra_properties": {"note": "x"},
            }
        }
    }

    merged = apply_edits([], edits, tenant_id="t1")

    assert len(merged) == 1
    assert merged[0].node_key == "产品:NEW"
    assert merged[0].standard_name == "人工新建"
    assert merged[0].aliases == ["别名"]
    assert merged[0].extra_properties == {"note": "x"}


def test_created_then_etl_produces_the_same_node_key(
):
    """**这条没有外部先例，是本设计自己的判断。**

    __created__ 的语义是"这个实体在数据源里不存在，我先建一个"。一旦
    数据源真的产出了它，那个前提就不再成立——数据源是更权威的来源，
    它的行接管该实体的存在性。但当初手工填的那些字段值仍然是人的判断，
    应当继续按字段级编辑优先。

    所以：__created__ 覆盖过的字段保持人工值，未覆盖的字段取 ETL 值。
    """
    terms = [
        _term(
            "产品:NEW", "ETL 产出的名字",
            aliases=["ETL 别名"],
            extra_properties={"revenue": 500, "cost": 300},
        )
    ]
    edits = {
        "产品:NEW": {
            FIELD_CREATED: {
                "standard_name": "人工新建",
                "term_type": "产品",
                "extra_properties": {"revenue": 999},
            }
        }
    }

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert len(merged) == 1
    # __created__ 覆盖过的字段：保持人工值。
    assert merged[0].standard_name == "人工新建"
    assert merged[0].extra_properties["revenue"] == 999
    # __created__ 没覆盖的字段：取 ETL 值。
    assert merged[0].aliases == ["ETL 别名"]
    assert merged[0].extra_properties["cost"] == 300


def test_orphan_field_edits_without_created_are_ignored():
    """编辑挂在一个 terms 表里不存在、也没有 __created__ 的 node_key 上
    ——不凭空造实体。这种孤儿编辑通常来自实体被 ETL 的 sweep 清理掉之后
    （见源端删除传播那份设计：sweep 只删 terms 行、不删 term_edits 行，
    源里若再出现同 node_key，编辑重新生效）。"""
    edits = {"产品:GONE": {"standard_name": "改过的名字"}}

    assert apply_edits([], edits, tenant_id="t1") == []


def test_edits_for_other_tenants_node_keys_do_not_leak():
    """apply_edits 拿到的 terms 和 edits 应当已经是同一个租户的。这条用例
    钉的是函数不会因为 edits 里有多余的 key 而凭空产出 Term。"""
    terms = [_term("产品:A", "甲")]
    edits = {"产品:A": {"standard_name": "改过"}, "产品:别的租户的": {"standard_name": "x"}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert [t.node_key for t in merged] == ["产品:A"]


def test_extra_properties_whole_dict_edit_replaces_the_base_and_clears_the_rest():
    """FIELD_EXTRA_PROPERTIES 是"人显式提交了完整属性集合"的整字典编辑，
    整体替换 term 原有的 extra_properties——这是管理后台 PUT 传 {} 能
    真正清空全部属性值的唯一途径（单键编辑没有"删掉某个键"的语义）。"""
    terms = [_term("产品:A", "甲", extra_properties={"revenue": 100, "cost": 60})]
    edits = {"产品:A": {FIELD_EXTRA_PROPERTIES: {}}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert merged[0].extra_properties == {}


def test_extra_properties_whole_dict_edit_is_the_base_for_single_key_edits():
    """整字典编辑和带点号的单键编辑共存时：整字典编辑是基底（整体替换
    管道原值），单键编辑仍然叠加在这个基底之上——不会因为两种粒度并存
    互相覆盖丢失。"""
    terms = [_term("产品:A", "甲", extra_properties={"revenue": 100, "cost": 60})]
    edits = {
        "产品:A": {
            FIELD_EXTRA_PROPERTIES: {"revenue": 1, "note": "接管的字典"},
            "extra_properties.note": "单键编辑覆盖了整字典里的这个键",
        }
    }

    merged = apply_edits(terms, edits, tenant_id="t1")

    # 整字典编辑接管了整个字段，管道的 cost 不再出现。
    assert merged[0].extra_properties == {
        "revenue": 1,
        "note": "单键编辑覆盖了整字典里的这个键",
    }


def test_extra_properties_without_whole_dict_edit_falls_back_to_single_key_merge():
    """没有整字典编辑时行为不变：单键编辑只覆盖那一个键，其余属性仍然
    跟随管道——回归保护，防止整字典编辑的引入意外改变了这条既有路径。"""
    terms = [_term("产品:A", "甲", extra_properties={"revenue": 100, "cost": 60})]
    edits = {"产品:A": {"extra_properties.revenue": 999}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert merged[0].extra_properties == {"revenue": 999, "cost": 60}
