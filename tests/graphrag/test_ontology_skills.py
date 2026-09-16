from __future__ import annotations

from pathlib import Path

import pytest

from app.graphrag.ontology_skills import (
    DuplicateSkillNameError,
    SkillFormatError,
    SkillRegistry,
    UnknownSkillError,
    discover_skills,
    load_skill,
    normalize_alias,
)

BUILTIN_DIR = Path(__file__).resolve().parents[2] / "app" / "ontology_skills"

_MINIMAL = """\
name: {name}
version: "1"
display_name: 测试领域
description: 单元测试用
term_types:
  - value: 商品
    key_aliases: [sku, jan]
    extra_fields:
      - {{ name: color, value_type: string, display_name: 颜色 }}
    field_aliases:
      color: [color, 颜色]
  - value: 门店
    key_aliases: [store]
relation_types:
  - relation_type: SOLD_AT
    example_phrase: 某商品在某门店有售
constraints:
  - [商品, SOLD_AT, 门店]
questions:
  - 某个商品在哪些门店有售？
"""


def _write_skill(skills_dir: Path, dir_name: str, text: str) -> Path:
    skill_dir = skills_dir / dir_name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "skill.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("JAN", "jan"),
        ("sku_code", "skucode"),
        ("Item CD", "itemcd"),
        ("retail-price", "retailprice"),
        ("商品编码", "商品编码"),
    ],
)
def test_normalize_alias_folds_case_and_separators(raw, expected):
    assert normalize_alias(raw) == expected


def test_load_skill_reads_every_declared_section(tmp_path):
    path = _write_skill(tmp_path, "demo", _MINIMAL.format(name="demo"))
    skill = load_skill(path)
    assert skill.name == "demo"
    assert skill.version == "1"
    assert [t.value for t in skill.term_types] == ["商品", "门店"]
    sku = skill.term_types[0]
    # display_name 缺省回退到 value——界面上永远有名字可显示
    assert sku.display_name == "商品"
    assert sku.standard_name_value_type == "string"
    assert sku.key_aliases == ["sku", "jan"]
    assert sku.extra_fields[0].name == "color"
    assert sku.extra_fields[0].display_name == "颜色"
    assert sku.field_aliases == {"color": ["color", "颜色"]}
    assert skill.relation_types[0].relation_type == "SOLD_AT"
    assert skill.constraints[0].subject == "商品"
    assert skill.constraints[0].relation == "SOLD_AT"
    assert skill.constraints[0].object == "门店"
    assert skill.questions == ["某个商品在哪些门店有售？"]
    assert skill.match_hint == ""


def test_to_dict_round_trips_the_fields_the_frontend_reads(tmp_path):
    path = _write_skill(tmp_path, "demo", _MINIMAL.format(name="demo"))
    payload = load_skill(path).to_dict()
    assert payload["name"] == "demo"
    assert payload["term_types"][0]["key_aliases"] == ["sku", "jan"]
    assert payload["term_types"][0]["extra_fields"][0] == {
        "name": "color",
        "value_type": "string",
        "display_name": "颜色",
    }
    assert payload["constraints"][0] == {"subject": "商品", "relation": "SOLD_AT", "object": "门店"}


@pytest.mark.parametrize(
    "mutate,message_fragment",
    [
        (lambda t: t.replace("name: demo", "name: Demo Skill"), "Demo Skill"),
        (lambda t: t.replace("relation_type: SOLD_AT", "relation_type: sold_at"), "sold_at"),
        (lambda t: t.replace("value_type: string", "value_type: blob"), "blob"),
        (lambda t: t.replace("name: color, value_type", "name: 颜 色, value_type"), "颜 色"),
        (lambda t: t.replace("[商品, SOLD_AT, 门店]", "[商品, SOLD_AT, 仓库]"), "仓库"),
        (lambda t: t.replace("[商品, SOLD_AT, 门店]", "[商品, STOCKED_AT, 门店]"), "STOCKED_AT"),
        (lambda t: t.replace("      color: [color, 颜色]", "      size: [size]"), "size"),
        (
            lambda t: t.replace("  - value: 门店\n    key_aliases: [store]\n", "  - value: 商品\n"),
            "商品",
        ),
        (lambda t: t.replace("term_types:", "term_typez:"), "term_types"),
    ],
)
def test_load_skill_rejects_malformed_skill(tmp_path, mutate, message_fragment):
    path = _write_skill(tmp_path, "demo", mutate(_MINIMAL.format(name="demo")))
    with pytest.raises(SkillFormatError) as exc_info:
        load_skill(path)
    assert message_fragment in str(exc_info.value)


def test_load_skill_rejects_invalid_yaml(tmp_path):
    path = _write_skill(tmp_path, "demo", "name: demo\nterm_types: [unclosed")
    with pytest.raises(SkillFormatError):
        load_skill(path)


def test_discover_skills_requires_directory_name_to_match_skill_name(tmp_path):
    _write_skill(tmp_path, "not_demo", _MINIMAL.format(name="demo"))
    with pytest.raises(SkillFormatError) as exc_info:
        discover_skills(tmp_path)
    assert "not_demo" in str(exc_info.value)


def test_registry_rejects_duplicate_names(tmp_path):
    # 目录名校验先于重名校验（目录名必须等于 name），所以扫描路径上撞不出
    # 重名——直接对注册表注册两次同一个 skill 来验证这条防线。
    path = _write_skill(tmp_path, "demo", _MINIMAL.format(name="demo"))
    skill = load_skill(path)
    registry = SkillRegistry()
    registry.register(skill)
    with pytest.raises(DuplicateSkillNameError):
        registry.register(skill)


def test_registry_get_unknown_name_raises(tmp_path):
    registry = discover_skills(tmp_path)
    with pytest.raises(UnknownSkillError):
        registry.get("nope")


def test_discover_skills_sorts_by_name(tmp_path):
    _write_skill(tmp_path, "zeta", _MINIMAL.format(name="zeta"))
    _write_skill(tmp_path, "alpha", _MINIMAL.format(name="alpha"))
    assert [s.name for s in discover_skills(tmp_path).all()] == ["alpha", "zeta"]


def test_builtin_skills_all_load():
    """内置目录里每一个 skill 都必须能加载。

    这是"格式错误不静默跳过"那条约束在测试里的形态：谁改坏了
    consumer_retail/skill.yaml，这条先红，而不是等到服务起不来。
    """
    registry = discover_skills(BUILTIN_DIR)
    assert "consumer_retail" in [s.name for s in registry.all()]
    retail = registry.get("consumer_retail")
    sku = next(t for t in retail.term_types if t.value == "SKU")
    assert "jan" in [normalize_alias(a) for a in sku.key_aliases]
