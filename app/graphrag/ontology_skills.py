"""领域建模 skill：一份声明式 YAML，描述某个领域的候选本体骨架（实体类型、
关系类型、约束）和从企业列名认出这些概念的匹配线索（别名）。

只内置、跟代码发版。放在 app/ontology_skills/<name>/skill.yaml，进程内首次
访问时扫描一次。格式错误直接抛异常，而不是跳过那一个 skill——照搬
app/agent/tool_registry.py 对 manifest 的态度：一个静默消失的 skill 会让用户
在工作台起步页看不到它，却没有任何地方告诉他为什么。

term_types / relation_types / constraints 三段的字段与 replace_draft 的 payload
一一对应，多出来的只有匹配线索（key_aliases / field_aliases）和两个 v2 预留
字段（questions / match_hint）。这个对应关系是导出（ontology_skill_export.py）
能做成纯机械操作的前提。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.graphrag.ontology_categories import EXTRA_FIELD_NAME_PATTERN
from app.graphrag.value_types import EXTRA_FIELD_VALUE_TYPES, STANDARD_NAME_VALUE_TYPES


class SkillFormatError(Exception):
    """skill.yaml 不合法：缺字段、类型不对、约束引用了没声明的类型等。"""


class DuplicateSkillNameError(Exception):
    """两个 skill 声明了同一个 name。"""


class UnknownSkillError(Exception):
    """注册表里没有这个名字的 skill。"""


_SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")
# 与 ontology_lifecycle._validate_draft_relation_type 用的是同一条规则：关系
# 类型名会被拼进 Cypher，只允许大写字母开头的 [A-Z0-9_]。那边的正则是模块
# 私有名，这里如实复制一份而不是跨模块 import——代价是两处规则要同步，这跟
# ontology_lifecycle.py 顶部那段注释记录的取舍一致。
_RELATION_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")
_ALIAS_SEPARATORS = re.compile(r"[\s_\-]+")


def normalize_alias(text: str) -> str:
    """别名/列名归一化：去掉空白、下划线、连字符，再 casefold。

    对齐规则要求归一化后**精确相等**，所以这里只做无损的大小写与分隔符折叠，
    不做同义词、不做前缀匹配——猜错列会把错误数据写进图谱，比让用户手动指
    一下列贵得多。前端 modelingWorkbench/aliases.ts 有同一条规则的 TS 版本，
    两边任何一边改动都必须同步，否则前端认为对上的列后端算出来是没对上。
    """
    return _ALIAS_SEPARATORS.sub("", text).casefold()


@dataclass(frozen=True)
class SkillExtraField:
    name: str
    value_type: str
    display_name: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "value_type": self.value_type, "display_name": self.display_name}


@dataclass(frozen=True)
class SkillTermType:
    value: str
    display_name: str
    standard_name_value_type: str
    extra_fields: list[SkillExtraField]
    key_aliases: list[str]
    field_aliases: dict[str, list[str]]

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "display_name": self.display_name,
            "standard_name_value_type": self.standard_name_value_type,
            "extra_fields": [f.to_dict() for f in self.extra_fields],
            "key_aliases": list(self.key_aliases),
            "field_aliases": {k: list(v) for k, v in self.field_aliases.items()},
        }


@dataclass(frozen=True)
class SkillRelationType:
    relation_type: str
    example_phrase: str
    description: str

    def to_dict(self) -> dict:
        return {
            "relation_type": self.relation_type,
            "example_phrase": self.example_phrase,
            "description": self.description,
        }


@dataclass(frozen=True)
class SkillConstraint:
    subject: str
    relation: str
    object: str

    def to_dict(self) -> dict:
        return {"subject": self.subject, "relation": self.relation, "object": self.object}


@dataclass(frozen=True)
class OntologySkill:
    name: str
    version: str
    display_name: str
    description: str
    term_types: list[SkillTermType]
    relation_types: list[SkillRelationType]
    constraints: list[SkillConstraint]
    questions: list[str] = field(default_factory=list)
    match_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "term_types": [t.to_dict() for t in self.term_types],
            "relation_types": [r.to_dict() for r in self.relation_types],
            "constraints": [c.to_dict() for c in self.constraints],
            "questions": list(self.questions),
            "match_hint": self.match_hint,
        }


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, OntologySkill] = {}

    def register(self, skill: OntologySkill) -> None:
        if skill.name in self._skills:
            raise DuplicateSkillNameError(f"skill 重名: {skill.name!r}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> OntologySkill:
        try:
            return self._skills[name]
        except KeyError:
            raise UnknownSkillError(f"没有名为 {name!r} 的 skill") from None

    def all(self) -> list[OntologySkill]:
        return [self._skills[name] for name in sorted(self._skills)]


def _require_str(raw: dict, key: str, *, where: str, default: str | None = None) -> str:
    if key not in raw:
        if default is not None:
            return default
        raise SkillFormatError(f"{where} 缺少必填字段 {key}")
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SkillFormatError(f"{where} 的 {key} 要是字符串，收到: {value!r}")
    return str(value)


def _as_str_list(value: object, *, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(v, (str, int)) and not isinstance(v, bool) for v in value
    ):
        raise SkillFormatError(f"{where} 要是字符串列表，收到: {value!r}")
    return [str(v) for v in value]


def _parse_extra_field(raw: object, *, where: str) -> SkillExtraField:
    if not isinstance(raw, dict):
        raise SkillFormatError(f"{where} 的 extra_fields 每一项要是映射，收到: {raw!r}")
    name = _require_str(raw, "name", where=f"{where} 的字段")
    if not EXTRA_FIELD_NAME_PATTERN.match(name):
        raise SkillFormatError(
            f"{where} 的字段名 {name!r} 不合法，必须满足 ^[a-zA-Z_][a-zA-Z0-9_]{{0,63}}$"
            f"（后续要作为 Neo4j 索引属性名/结构化查询字段名使用）"
        )
    value_type = _require_str(raw, "value_type", where=f"{where} 的字段 {name}")
    if value_type not in EXTRA_FIELD_VALUE_TYPES:
        raise SkillFormatError(
            f"{where} 的字段 {name} 的 value_type {value_type!r} 不合法，"
            f"可选: {sorted(EXTRA_FIELD_VALUE_TYPES)}"
        )
    return SkillExtraField(
        name=name,
        value_type=value_type,
        display_name=_require_str(raw, "display_name", where=where, default=""),
    )


def _parse_term_type(raw: object, *, skill_name: str) -> SkillTermType:
    if not isinstance(raw, dict):
        raise SkillFormatError(f"skill {skill_name} 的 term_types 每一项要是映射，收到: {raw!r}")
    value = _require_str(raw, "value", where=f"skill {skill_name} 的实体类型")
    if not value.strip():
        raise SkillFormatError(f"skill {skill_name} 有实体类型的 value 为空")
    where = f"skill {skill_name} 的实体类型 {value}"
    standard_name_value_type = _require_str(
        raw, "standard_name_value_type", where=where, default="string"
    )
    if standard_name_value_type not in STANDARD_NAME_VALUE_TYPES:
        raise SkillFormatError(
            f"{where} 的 standard_name_value_type {standard_name_value_type!r} 不合法，"
            f"可选: {sorted(STANDARD_NAME_VALUE_TYPES)}"
        )
    extra_fields_raw = raw.get("extra_fields") or []
    if not isinstance(extra_fields_raw, list):
        raise SkillFormatError(f"{where} 的 extra_fields 要是列表")
    extra_fields = [_parse_extra_field(item, where=where) for item in extra_fields_raw]
    declared_field_names = [f.name for f in extra_fields]
    if len(set(declared_field_names)) != len(declared_field_names):
        raise SkillFormatError(f"{where} 的 extra_fields 有重名字段")
    field_aliases_raw = raw.get("field_aliases") or {}
    if not isinstance(field_aliases_raw, dict):
        raise SkillFormatError(f"{where} 的 field_aliases 要是映射")
    field_aliases: dict[str, list[str]] = {}
    for field_name, aliases in field_aliases_raw.items():
        if field_name not in declared_field_names:
            # 别名指向没声明的字段，对齐时会给一个不存在的字段填列，最终
            # 落进 ETL 映射的 field_mappings 里；那时 ETL 会往节点上写一个
            # 本体没声明的属性。写 skill 时就该发现，不该留到跑批。
            raise SkillFormatError(f"{where} 的 field_aliases 引用了未声明的字段 {field_name!r}")
        field_aliases[str(field_name)] = _as_str_list(
            aliases, where=f"{where} 的 field_aliases.{field_name}"
        )
    return SkillTermType(
        value=value,
        display_name=_require_str(raw, "display_name", where=where, default=value),
        standard_name_value_type=standard_name_value_type,
        extra_fields=extra_fields,
        key_aliases=_as_str_list(raw.get("key_aliases"), where=f"{where} 的 key_aliases"),
        field_aliases=field_aliases,
    )


def _parse_relation_type(raw: object, *, skill_name: str) -> SkillRelationType:
    if not isinstance(raw, dict):
        raise SkillFormatError(
            f"skill {skill_name} 的 relation_types 每一项要是映射，收到: {raw!r}"
        )
    relation_type = _require_str(raw, "relation_type", where=f"skill {skill_name} 的关系类型")
    if not _RELATION_TYPE_PATTERN.match(relation_type):
        raise SkillFormatError(
            f"skill {skill_name} 的关系类型 {relation_type!r} 不合法，"
            f"必须满足 ^[A-Z][A-Z0-9_]{{0,63}}$"
        )
    where = f"skill {skill_name} 的关系类型 {relation_type}"
    return SkillRelationType(
        relation_type=relation_type,
        example_phrase=_require_str(raw, "example_phrase", where=where, default=""),
        description=_require_str(raw, "description", where=where, default=""),
    )


def _parse_constraint(
    raw: object, *, skill_name: str, term_values: set[str], relation_names: set[str]
) -> SkillConstraint:
    if not isinstance(raw, list) or len(raw) != 3 or not all(isinstance(x, str) for x in raw):
        raise SkillFormatError(
            f"skill {skill_name} 的 constraints 每一项要是 [主语, 关系, 宾语] 三元组，收到: {raw!r}"
        )
    subject, relation, obj = raw
    for name, pool, label in (
        (subject, term_values, "实体类型"),
        (obj, term_values, "实体类型"),
        (relation, relation_names, "关系类型"),
    ):
        if name not in pool:
            raise SkillFormatError(
                f"skill {skill_name} 的约束 {raw!r} 引用了未声明的{label} {name!r}"
            )
    return SkillConstraint(subject=subject, relation=relation, object=obj)


def load_skill(path: Path) -> OntologySkill:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SkillFormatError(f"{path} 不是合法的 YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SkillFormatError(f"{path} 顶层要是映射")
    name = _require_str(raw, "name", where=str(path))
    if not _SKILL_NAME_PATTERN.match(name):
        raise SkillFormatError(
            f"{path} 的 name {name!r} 不合法，必须满足 ^[a-z][a-z0-9_]{{0,63}}$"
            f"（要当目录名和 URL 路径参数用）"
        )
    display_name = _require_str(raw, "display_name", where=f"skill {name}")
    description = _require_str(raw, "description", where=f"skill {name}", default="")
    version = _require_str(raw, "version", where=f"skill {name}")

    term_types_raw = raw.get("term_types")
    if not isinstance(term_types_raw, list) or not term_types_raw:
        raise SkillFormatError(f"skill {name} 的 term_types 要是非空列表")
    term_types = [_parse_term_type(item, skill_name=name) for item in term_types_raw]
    term_values = [t.value for t in term_types]
    if len(set(term_values)) != len(term_values):
        dup = next(v for v in term_values if term_values.count(v) > 1)
        raise SkillFormatError(f"skill {name} 有重复的实体类型 {dup!r}")

    relation_types_raw = raw.get("relation_types") or []
    if not isinstance(relation_types_raw, list):
        raise SkillFormatError(f"skill {name} 的 relation_types 要是列表")
    relation_types = [_parse_relation_type(item, skill_name=name) for item in relation_types_raw]
    relation_names = [r.relation_type for r in relation_types]
    if len(set(relation_names)) != len(relation_names):
        dup = next(v for v in relation_names if relation_names.count(v) > 1)
        raise SkillFormatError(f"skill {name} 有重复的关系类型 {dup!r}")

    constraints_raw = raw.get("constraints") or []
    if not isinstance(constraints_raw, list):
        raise SkillFormatError(f"skill {name} 的 constraints 要是列表")
    constraints = [
        _parse_constraint(
            item,
            skill_name=name,
            term_values=set(term_values),
            relation_names=set(relation_names),
        )
        for item in constraints_raw
    ]

    return OntologySkill(
        name=name,
        version=version,
        display_name=display_name,
        description=description,
        term_types=term_types,
        relation_types=relation_types,
        constraints=constraints,
        questions=_as_str_list(raw.get("questions"), where=f"skill {name} 的 questions"),
        match_hint=_require_str(raw, "match_hint", where=f"skill {name}", default=""),
    )


def discover_skills(skills_dir: Path) -> SkillRegistry:
    """扫描 skills_dir/*/skill.yaml，注册进一张新的 SkillRegistry。只在进程
    启动后首次访问时调用一次（调用方负责单例缓存，本函数不缓存）。

    目录名必须等于 skill 的 name：URL 里传的是 name，排错时要能从 name 直接
    找到文件；两者不一致的话 get("consumer_retail") 对应的文件可能叫别的名字。
    """
    registry = SkillRegistry()
    for skill_path in sorted(skills_dir.glob("*/skill.yaml")):
        skill = load_skill(skill_path)
        if skill_path.parent.name != skill.name:
            raise SkillFormatError(
                f"{skill_path} 的目录名 {skill_path.parent.name!r} 与 name {skill.name!r} 不一致"
            )
        registry.register(skill)
    return registry
