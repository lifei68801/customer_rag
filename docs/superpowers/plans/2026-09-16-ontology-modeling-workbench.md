# 本体建模工作台（v1）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用一个"内置 skill 起步 → 多表数据对齐 → 落地状态推导 → 带 diff 应用到草稿 → 导出为 skill"的建模工作台，替换今天的单表引导建模页。

**Architecture:** 后端新增四个纯函数模块（skill 注册表、工作区存储、落地推导、草稿 diff、skill 导出）和一个只做编排的路由文件，全部挂在 `tenant_scoped` 下；工作区状态对后端是一份不透明 JSON（只校验外形），所有"对齐/投影/合并"逻辑放在前端纯函数里，页面只负责把纯函数串起来。写草稿不新增端点，复用既有的 `POST /api/admin/ontology/{tenant_id}/draft/replace`。

**Tech Stack:** FastAPI + aiosqlite + PyYAML（后端）；React 18 + TypeScript + vitest + @testing-library/react（前端）；复用 `schemaEtlConfigBuilder/sourceParser.ts` 读表、`guidedOntology/columnStats.ts` + `columnRoles.ts` 算列角色、`buildConfigYaml.ts` 产 ETL YAML。

**Spec:** `docs/superpowers/specs/2026-09-16-ontology-modeling-workbench-design.md`（只做 v1 范围，见 spec 决策 11）

## Global Constraints

- **只做 v1**：阶段一的 skill 分支 + 阶段三的数据分支。LLM 冷启动、文档发现、问题清单、`llm`/`document`/`question` 三种 provenance 一律不做；state_json 里 `questions` 保持 `[]`，skill.yaml 里 `questions`/`match_hint` 允许存在但不读（spec 决策 11、v2 路线）。
- **落地状态纯推导**：`grounding` 每次从 `ontology_etl_mapping.config_yaml`（草稿优先，没有则已确认）现算，不存、不缓存，不在 state_json 里出现（spec 决策 7）。
- **未落地元素不阻塞、不删除、持续可见**（spec 决策 2）。
- **工作区不是草稿**：只有"应用"这个显式动作才写本体草稿表；应用前必须先展示 diff，删除项单独醒目列出（spec 决策 9、10）。
- **本体三张表一列不加**（spec 决策 9）。
- **skill 只内置**（`app/ontology_skills/<name>/skill.yaml`），格式错误启动失败、不静默跳过（照 `app/agent/tool_registry.py` 的态度）；导出只生成 YAML 下载，不自动入仓（spec 决策 5、6）。
- **命中规则确定性**：别名归一化（casefold、去空格/下划线/连字符）后精确匹配，命不中就命不中，不猜（spec 行为规格 §3）。
- **权限**：全部路由挂 `tenant_scoped`（`require_tenant_access`），不要求 admin 角色（与 `59bd79b` 对引导建模的处理一致）。
- **鉴权测试口径**：`tests/api/test_admin_route_shapes.py` 的 `_TENANT_SCOPED_PREFIXES` 已含 `/api/admin/ontology/{tenant_id}/`，新路由路径必须以它开头。
- **进程约束（沿用本仓库既有）**：`git add` 逐个文件点名，绝不 `-A`/`.`；不碰工作区里用户刻意保留的未提交改动（`docs/superpowers/plans/` 下的删除与新增、`docs/superpowers/specs/2026-08-27-query-matching-and-rewrite-redesign-design.md`）；不推送 origin；不改 `.env`；不动 `data/` 下真实库；注释里不写未经验证的因果。
- **测试命令**：后端 `PYTHONIOENCODING=utf-8 python -u -m pytest <path> -q`（Windows 下要这个环境变量，否则中文断言信息乱码/报错）；前端在 `frontend/` 下 `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run <path> --maxWorkers=2`，不要跑 `npx prettier --write`。
- **每个关键行为做变异测试**：把实现里关键一行改坏/注掉，对应测试必须变红，再改回来。任务里的"变异检查"步骤不可跳过。
- **注释密度**：跟周围代码一致——这个仓库的注释解释"为什么这样做、不这样做会怎样"，不复述代码在做什么。

## 与 spec 的差异（计划层裁定）

| # | spec 写法 | 计划做法 | 理由 |
|---|---|---|---|
| 1 | `GET /api/admin/ontology-skills`（非租户路径） | `GET /api/admin/ontology/{tenant_id}/modeling-workspace/skills` | 非租户路径要进 `_NON_TENANT_PREFIXES` 白名单并加 `require_admin_role` 例外说明；skill 列表只在工作台里用，且工作台本来就在租户上下文里，放进租户路径零成本地绕开这两条 |
| 2 | `POST .../modeling-workspace/discover`（后端对齐） | 不做后端端点；对齐是前端纯函数 `alignToSkeleton.ts` | 读表、算列角色已经全在浏览器里（`columnStats`/`columnRoles`），再把列名传回后端只是多一次往返；spec 也说结果要在前端跟审阅决定合并 |
| 3 | `POST .../modeling-workspace/apply`（调 `replace_draft`） | 不做；前端 `apply-preview` 之后直接调既有 `POST .../draft/replace` | `replace_draft` 已经有整份校验、变更日志、映射同提交；再包一层只是复制一遍它的错误映射 |
| 4 | state_json 里 term_type 无别名字段 | term_type 里存 `key_aliases` / `field_aliases`（从 skill 复制，manual 新增的初始为 `[value]`） | 对齐在前端做（差异 2），前端只有工作区没有 skill；且用户改名后按 value 回查 skill 会断 |
| 5 | state_json 无 `sources` | 加 `sources: [{file, sheet?, header_row?, first_data_row?}]` | 应用时 `buildConfigYaml` 要为每张表写 `sources:`，否则 ETL 用缺省表头行读 MUJI 那种表头在第 6 行的表会全错 |
| 6 | `display_name` | 只用于工作台界面；投影到草稿时 `value` 进 `ontology_term_types.value` | 本体表没有显示名列（决策 9 不加列）；`value` 本来就是自由文本的显示用标签 |

---

## File Structure

**后端（新建）**
- `app/ontology_skills/__init__.py` — 空包文件，让 skills 目录跟 `app/agent/tools/` 一样是个包
- `app/ontology_skills/consumer_retail/skill.yaml` — 第一个内置 skill（消费品零售，源自 MUJI）
- `app/graphrag/ontology_skills.py` — skill 数据类、YAML 加载与校验、目录扫描、注册表、别名归一化
- `app/graphrag/ontology_modeling_workspace.py` — `ontology_modeling_workspaces` 表、读/建/存（乐观锁）、从 skill 生成初始 state
- `app/graphrag/ontology_grounding.py` — 从 ETL 映射推导落地状态
- `app/graphrag/ontology_workspace_apply.py` — 提交的草稿 payload 与当前草稿的 diff
- `app/graphrag/ontology_skill_export.py` — 已确认本体 + 映射 → skill YAML
- `app/api/admin_modeling_workspace_routes.py` — 七个端点，只做参数校验与错误映射

**后端（修改）**
- `app/api/deps.py` — 加 `get_skill_registry()` 单例
- `app/graphrag/ontology_lifecycle.py:129-140` — `ensure_ontology_schema` 多建一张表
- `app/main.py:232-251` — `tenant_scoped.include_router(admin_modeling_workspace_router)`

**前端（新建，目录 `frontend/src/admin/modelingWorkbench/`）**
- `types.ts` — 工作区 state、skill、grounding、diff 的 TS 形状（与后端 JSON 一一对应）
- `workspaceApi.ts` — 七个端点的薄封装
- `aliases.ts` — `normalizeAlias`（与后端 `normalize_alias` 同规则）
- `alignToSkeleton.ts` — 数据发现：列 → 骨架对齐、未接住列、跨表关系提议、合并进 state
- `projectToDraft.ts` — state → `draft/replace` payload + ETL YAML
- `nextStep.ts` — "下一步建议"
- `skeletonEdits.ts` — 改名 / 加人工旁证 / 手工新增三个纯函数
- `ui.ts` — 共用样式常量（沿用原引导页那一组类名）
- `ModelingWorkbenchPage.tsx` — 页面骨架：加载工作区、四个面板切换、保存
- `panels/StartPanel.tsx` / `SkeletonPanel.tsx` / `DataPanel.tsx` / `UngroundedPanel.tsx` / `ApplyPanel.tsx`

**前端（修改/删除）**
- `frontend/src/App.tsx:20,70` — 路由 `ontology/guided` 改指向 `ModelingWorkbenchPage`
- `frontend/src/adminRoutes.ts:135` — 侧边栏标签 `引导建模` → `建模工作台`
- `frontend/src/admin/OntologySchemaPage.tsx:340` — 链接文案改成 `打开建模工作台`
- 删除 `frontend/src/admin/guidedOntology/GuidedOntologyPage.tsx`、`ProposalReview.tsx`、`guidedEntry.test.tsx`、`guidedPage.test.tsx`、`guidedSubmit.test.tsx`、`proposalReview.test.tsx`
- 保留 `guidedOntology/columnStats.ts`、`columnRoles.ts`、`draftProposal.ts`、`types.ts` 及其 `.test.ts`、`sourceParserPassthrough.test.ts`（纯逻辑，工作台继续用）

---

### Task 1: Skill 注册表 + 第一个内置 skill

**Files:**
- Create: `app/graphrag/ontology_skills.py`
- Create: `app/ontology_skills/__init__.py`（空文件）
- Create: `app/ontology_skills/consumer_retail/skill.yaml`
- Modify: `app/api/deps.py`（import 区、`__all__`、单例区、`get_tool_registry` 之后）
- Test: `tests/graphrag/test_ontology_skills.py`

**Interfaces:**
- Consumes: `app.graphrag.value_types.EXTRA_FIELD_VALUE_TYPES` / `STANDARD_NAME_VALUE_TYPES`（frozenset[str]）；`app.graphrag.ontology_categories.EXTRA_FIELD_NAME_PATTERN`（编译好的正则）
- Produces:
  - `normalize_alias(text: str) -> str`
  - 数据类 `SkillExtraField(name, value_type, display_name="")`、`SkillTermType(value, display_name, standard_name_value_type, extra_fields, key_aliases, field_aliases)`、`SkillRelationType(relation_type, example_phrase, description)`、`SkillConstraint(subject, relation, object)`、`OntologySkill(name, version, display_name, description, term_types, relation_types, constraints, questions, match_hint)`，全部带 `to_dict() -> dict`
  - `load_skill(path: Path) -> OntologySkill`（抛 `SkillFormatError`）
  - `discover_skills(skills_dir: Path) -> SkillRegistry`
  - `SkillRegistry.register/get/all`，异常 `SkillFormatError` / `DuplicateSkillNameError` / `UnknownSkillError`
  - `deps.get_skill_registry() -> SkillRegistry`（async 单例）

- [ ] **Step 1: 写失败的测试**

```python
# tests/graphrag/test_ontology_skills.py
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_skills.py -q`
Expected: collection error，`ModuleNotFoundError: No module named 'app.graphrag.ontology_skills'`

- [ ] **Step 3: 写 skill 模块**

```python
# app/graphrag/ontology_skills.py
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
```

- [ ] **Step 4: 写第一个内置 skill**

`app/ontology_skills/__init__.py` 写成空文件（`touch` 即可，不要 `New-Item -Force`）。

```yaml
# app/ontology_skills/consumer_retail/skill.yaml
name: consumer_retail
version: "1"
display_name: 消费品零售
description: 面向品牌方/零售商的 SKU 主数据、品类、门店骨架。源自 2026-09 MUJI 商品知识中台项目，别名按该项目实际列名整理。

term_types:
  - value: SKU
    display_name: 商品
    standard_name_value_type: string
    extra_fields:
      - { name: color, value_type: string, display_name: 颜色 }
      - { name: size, value_type: string, display_name: 尺码 }
      - { name: retail_price, value_type: number, display_name: 零售价 }
    # 数据发现用的匹配线索。别名归一化后与列名精确相等即对上；命不中就
    # 命不中，由用户在工作台手动指列（v2 才交给 match_hint 里的提示词）。
    key_aliases: [jan, jan_cd, sku, sku_code, sku_cd, item_cd, item_code, 商品编码, 品番]
    field_aliases:
      color: [color, colour, color_cd, 颜色, 現地語色]
      size: [size, size_cd, 尺码, サイズ]
      retail_price: [retail_price, price, sale_price, 零售价, 売価]
  - value: Category
    display_name: 品类
    key_aliases: [category, category_cd, cat_cd, class_cd, 品类, 大分类, 分类]
  - value: Store
    display_name: 门店
    key_aliases: [store, store_cd, store_code, shop, shop_cd, 门店, 店铺, 店舗]

relation_types:
  - relation_type: BELONGS_TO_CATEGORY
    example_phrase: 某商品属于某品类
    description: SKU 到其所属品类
  - relation_type: SOLD_AT
    example_phrase: 某商品在某门店有售
    description: SKU 到有售门店

constraints:
  - [SKU, BELONGS_TO_CATEGORY, Category]
  - [SKU, SOLD_AT, Store]

# v2 用；v1 加载但不读。
questions:
  - 哪个品类的商品数最多？
  - 某个 SKU 在哪些门店有售？
```

- [ ] **Step 5: 在 deps 里加单例**

`app/api/deps.py` 的 import 区（`from app.graphrag.ontology_store import open_ontology_store_conn` 附近）加：

```python
from app.graphrag.ontology_skills import SkillRegistry, discover_skills
```

`__all__` 里 `"get_review_conn",` 之前按字母序插入 `"get_skill_registry",`。

单例变量区（`_tool_registry_lock = asyncio.Lock()` 之后）加：

```python
_skill_registry_cache: SkillRegistry | None = None
_skill_registry_lock = asyncio.Lock()
```

`get_tool_registry` 函数之后加：

```python
async def get_skill_registry() -> SkillRegistry:
    """进程内单例：首次访问时扫描 app/ontology_skills/*/skill.yaml 构建一次，
    此后复用——跟 get_tool_registry 同一个双重检查锁定模式，也同样意味着
    新增/修改 skill 目录后运行中的进程不会感知到，要重启服务才生效。"""
    global _skill_registry_cache
    if _skill_registry_cache is None:
        async with _skill_registry_lock:
            if _skill_registry_cache is None:
                skills_dir = Path(__file__).resolve().parent.parent / "ontology_skills"
                _skill_registry_cache = discover_skills(skills_dir)
    return _skill_registry_cache
```

- [ ] **Step 6: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_skills.py -q`
Expected: 全部 PASS

- [ ] **Step 7: 变异检查**

1. 把 `_parse_constraint` 里的 `if name not in pool:` 改成 `if False:`：`test_load_skill_rejects_malformed_skill[mutate4-仓库]` 与 `[mutate5-STOCKED_AT]` 必须变红。改回。
2. 把 `normalize_alias` 里的 `.casefold()` 删掉：`test_normalize_alias_folds_case_and_separators[JAN-jan]` 必须变红。改回。
3. 把 `discover_skills` 里目录名一致性那个 `if` 整段删掉：`test_discover_skills_requires_directory_name_to_match_skill_name` 必须变红。改回。

- [ ] **Step 8: 提交**

```bash
git add app/graphrag/ontology_skills.py app/ontology_skills/__init__.py app/ontology_skills/consumer_retail/skill.yaml app/api/deps.py tests/graphrag/test_ontology_skills.py
git commit -m "feat(ontology): 领域建模 skill 注册表与第一个内置 skill

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 建模工作区存储

**Files:**
- Create: `app/graphrag/ontology_modeling_workspace.py`
- Modify: `app/graphrag/ontology_lifecycle.py:129-140`（`ensure_ontology_schema` 多建一张表）
- Test: `tests/graphrag/test_ontology_modeling_workspace.py`

**Interfaces:**
- Consumes: Task 1 的 `OntologySkill`（`initial_state_from_skill` 的入参）
- Produces:
  - `ensure_modeling_workspace_schema(conn) -> None`
  - `@dataclass(frozen=True) ModelingWorkspace(tenant_id, skill_name: str | None, skill_version: str | None, state: dict, updated_at: str, updated_by: str)`
  - `async get_workspace(conn, tenant_id) -> ModelingWorkspace | None`
  - `async create_workspace(conn, tenant_id, *, skill: OntologySkill | None, actor: str, now: str) -> ModelingWorkspace`（已存在时抛 `WorkspaceExistsError`）
  - `async save_workspace(conn, tenant_id, *, state: dict, expected_updated_at: str, actor: str, now: str) -> ModelingWorkspace`（不存在抛 `WorkspaceNotFoundError`，时间戳不匹配抛 `WorkspaceConflictError`）
  - `async delete_workspace(conn, tenant_id) -> None`
  - `initial_state_from_skill(skill: OntologySkill | None) -> dict`
  - `validate_state(state: object) -> dict`（抛 `InvalidWorkspaceStateError`）
  - 异常：`WorkspaceExistsError` / `WorkspaceNotFoundError` / `WorkspaceConflictError` / `InvalidWorkspaceStateError`

**state_json 的形状**（后端只校验外形，语义在前端）：

```jsonc
{
  "term_types": [{
    "value": "SKU", "display_name": "商品",
    "provenance": "skill",            // skill | data | manual
    "review": "pending",              // pending | accepted | rejected
    "standard_name_value_type": "string",
    "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
    "key_aliases": ["jan"], "field_aliases": {"color": ["颜色"]},
    "clues": [{"kind": "manual", "note": "数据下个月接", "by": "alice", "at": "..."}],
    "data_match": null                // 或 {source_file, key_columns, field_columns, matched_by}
  }],
  "relation_types": [{"relation_type": "SOLD_AT", "example_phrase": "", "description": "",
                      "provenance": "skill", "review": "pending", "clues": [], "data_match": null}],
  "constraints": [{"subject": "SKU", "relation": "SOLD_AT", "object": "Store",
                   "provenance": "skill", "review": "pending"}],
  "sources": [{"file": "CN_001.xls", "sheet": null, "header_row": 6, "first_data_row": 7}],
  "unmatched_columns": {"CN_001.xls": ["md_no", "brand_cd"]},
  "questions": []
}
```

- [ ] **Step 1: 写失败的测试**

```python
# tests/graphrag/test_ontology_modeling_workspace.py
from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.ontology_modeling_workspace import (
    InvalidWorkspaceStateError,
    WorkspaceConflictError,
    WorkspaceExistsError,
    WorkspaceNotFoundError,
    create_workspace,
    delete_workspace,
    ensure_modeling_workspace_schema,
    get_workspace,
    initial_state_from_skill,
    save_workspace,
    validate_state,
)
from app.graphrag.ontology_skills import discover_skills

pytestmark = pytest.mark.anyio

BUILTIN_DIR = Path(__file__).resolve().parents[2] / "app" / "ontology_skills"


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_modeling_workspace_schema(conn)
    return conn


def _retail():
    return discover_skills(BUILTIN_DIR).get("consumer_retail")


def test_initial_state_from_skill_copies_skeleton_as_pending():
    state = initial_state_from_skill(_retail())
    sku = next(t for t in state["term_types"] if t["value"] == "SKU")
    assert sku["provenance"] == "skill"
    assert sku["review"] == "pending"
    assert sku["display_name"] == "商品"
    # 别名跟着进工作区：对齐在前端做，前端只拿得到工作区，拿不到 skill
    assert "jan" in sku["key_aliases"]
    assert sku["field_aliases"]["color"]
    # extra_fields 用本体表的键名（name/value_type/label），应用到草稿时直接透传
    assert sku["extra_fields"][0]["label"] == "颜色"
    assert sku["clues"] == []
    assert sku["data_match"] is None
    assert {c["relation"] for c in state["constraints"]} == {"BELONGS_TO_CATEGORY", "SOLD_AT"}
    assert state["sources"] == []
    assert state["unmatched_columns"] == {}
    # v1 不做问题清单，但这个键要在，前端不必判 undefined
    assert state["questions"] == []


def test_initial_state_without_skill_is_empty_but_well_formed():
    state = initial_state_from_skill(None)
    assert state == {
        "term_types": [],
        "relation_types": [],
        "constraints": [],
        "sources": [],
        "unmatched_columns": {},
        "questions": [],
    }


@pytest.mark.parametrize(
    "bad,fragment",
    [
        ([], "映射"),
        ({"term_types": {}}, "term_types"),
        ({"term_types": [{"value": "SKU", "provenance": "llm", "review": "pending"}]}, "llm"),
        ({"term_types": [{"value": "SKU", "provenance": "skill", "review": "maybe"}]}, "maybe"),
        ({"term_types": [{"provenance": "skill", "review": "pending"}]}, "value"),
        ({"relation_types": [{"relation_type": "SOLD_AT", "provenance": "skill", "review": "x"}]}, "x"),
        ({"constraints": [{"subject": "SKU", "relation": "SOLD_AT"}]}, "object"),
        ({"sources": [{"sheet": 0}]}, "file"),
        ({"unmatched_columns": {"a.csv": "md_no"}}, "unmatched_columns"),
    ],
)
def test_validate_state_rejects_malformed_state(bad, fragment):
    with pytest.raises(InvalidWorkspaceStateError) as exc_info:
        validate_state(bad)
    assert fragment in str(exc_info.value)


def test_validate_state_fills_missing_top_level_keys():
    # 前端少传一个键不该 500：补齐成空值，语义等同"这一类什么都没有"
    state = validate_state({"term_types": []})
    assert state["relation_types"] == []
    assert state["questions"] == []


async def test_create_then_get_round_trips():
    conn = await _conn()
    created = await create_workspace(
        conn, "t1", skill=_retail(), actor="alice", now="2026-09-16T10:00:00"
    )
    assert created.skill_name == "consumer_retail"
    assert created.skill_version == "1"
    assert created.updated_by == "alice"
    loaded = await get_workspace(conn, "t1")
    assert loaded == created
    assert any(t["value"] == "SKU" for t in loaded.state["term_types"])


async def test_get_returns_none_when_absent():
    conn = await _conn()
    assert await get_workspace(conn, "t1") is None


async def test_create_twice_raises():
    conn = await _conn()
    await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-16T10:00:00")
    with pytest.raises(WorkspaceExistsError):
        await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-16T10:01:00")


async def test_save_requires_matching_updated_at():
    conn = await _conn()
    created = await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-16T10:00:00")
    saved = await save_workspace(
        conn,
        "t1",
        state={"term_types": [{"value": "SKU", "provenance": "manual", "review": "accepted"}]},
        expected_updated_at=created.updated_at,
        actor="bob",
        now="2026-09-16T10:05:00",
    )
    assert saved.updated_at == "2026-09-16T10:05:00"
    assert saved.updated_by == "bob"
    # 拿旧时间戳再写一次：两个人同时开着工作台，后写的人不该静默盖掉前一个人
    with pytest.raises(WorkspaceConflictError):
        await save_workspace(
            conn,
            "t1",
            state={"term_types": []},
            expected_updated_at=created.updated_at,
            actor="carol",
            now="2026-09-16T10:06:00",
        )
    # 冲突之后库里还是 bob 那一版，没有被改动
    current = await get_workspace(conn, "t1")
    assert current.updated_by == "bob"
    assert current.state["term_types"][0]["value"] == "SKU"


async def test_save_absent_workspace_raises():
    conn = await _conn()
    with pytest.raises(WorkspaceNotFoundError):
        await save_workspace(
            conn, "t1", state={}, expected_updated_at="whatever", actor="a", now="b"
        )


async def test_save_rejects_malformed_state_without_writing():
    conn = await _conn()
    created = await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-16T10:00:00")
    with pytest.raises(InvalidWorkspaceStateError):
        await save_workspace(
            conn,
            "t1",
            state={"term_types": [{"value": "SKU", "provenance": "llm", "review": "pending"}]},
            expected_updated_at=created.updated_at,
            actor="bob",
            now="2026-09-16T10:05:00",
        )
    assert (await get_workspace(conn, "t1")).updated_by == "alice"


async def test_delete_workspace_is_idempotent():
    conn = await _conn()
    await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-16T10:00:00")
    await delete_workspace(conn, "t1")
    assert await get_workspace(conn, "t1") is None
    await delete_workspace(conn, "t1")  # 再删一次不报错


async def test_ensure_ontology_schema_creates_the_workspace_table():
    """建表挂进统一入口。不挂的话，真实的 get_review_conn 开出来的连接上没有
    这张表，工作台第一次请求就是 no such table。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_modeling_workspaces'"
    )
    assert await cursor.fetchone() is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_modeling_workspace.py -q`
Expected: collection error，`ModuleNotFoundError: app.graphrag.ontology_modeling_workspace`

- [ ] **Step 3: 写存储模块**

```python
# app/graphrag/ontology_modeling_workspace.py
"""建模工作区：一个租户一份、长期存在的建模过程状态。

它**不是**本体草稿——骨架里的元素要用户显式"应用"才写进
ontology_term_types 等三张草稿表。两者分开的理由（spec 决策 9/10）：工作区
要留住"这个我拒过""这个还没数据"这类过程信息，而本体表是结果，多存一列
过程状态就会被 ETL、问答、结构页各自解释一遍。

叫"工作区"不叫"会话"：代码库里"会话"已经指登录会话（AdminSession）和前台
聊天会话两样东西，这份状态跟两者都无关——不随登录失效、不属于某个用户，
跟租户的本体同寿。

state_json 对本模块是**不透明**的：只校验外形（键在不在、枚举值合不合法、
列表还是映射），不校验语义（元素之间引不引用得上、别名对不对得上列）。
语义检查在两个更合适的地方各做一次：前端做，因为它拿得到用户正在看的界面；
replace_draft 做，因为那是真正会写进本体的那一刻，它已经有一整套引用检查。
在这里再做第三遍只会让"保存一下"这个动作变得可能失败。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

from app.graphrag.ontology_skills import OntologySkill

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_modeling_workspaces (
    tenant_id     TEXT NOT NULL PRIMARY KEY,
    skill_name    TEXT,
    skill_version TEXT,
    state_json    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    updated_by    TEXT NOT NULL
);
"""

#: 来源。v1 只会产生 skill / data / manual 三种；llm / document / question 是
#: v2 的三条来源，现在拒掉——放行的话前端会收到自己不认识的标，而"标是哪来的"
#: 没有任何地方说得清。
_PROVENANCES = frozenset({"skill", "data", "manual"})
_REVIEWS = frozenset({"pending", "accepted", "rejected"})
#: 顶层键 -> 它该是什么容器。值同时当"缺省值工厂"用（list() / dict()）。
_TOP_LEVEL_DEFAULTS: dict[str, type] = {
    "term_types": list,
    "relation_types": list,
    "constraints": list,
    "sources": list,
    "unmatched_columns": dict,
    "questions": list,
}


class WorkspaceExistsError(Exception):
    """这个租户已经有工作区了。"""


class WorkspaceNotFoundError(Exception):
    """这个租户还没有工作区。"""


class WorkspaceConflictError(Exception):
    """带来的 updated_at 不是库里那一版——期间有别人存过。"""


class InvalidWorkspaceStateError(Exception):
    """state_json 外形不合法。"""


@dataclass(frozen=True)
class ModelingWorkspace:
    tenant_id: str
    skill_name: str | None
    skill_version: str | None
    state: dict
    updated_at: str
    updated_by: str

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "state": self.state,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


async def ensure_modeling_workspace_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def initial_state_from_skill(skill: OntologySkill | None) -> dict:
    """把 skill 的骨架抄成工作区初始状态，每个元素 provenance=skill、review=pending。

    别名（key_aliases / field_aliases）一起抄进来，不是留在 skill 里按名字回查：
    数据对齐在前端做，前端只拿得到工作区；而且用户改过名之后，按 value 回查
    skill 会查不到，别名会静默消失，表现为"改了个名字，数据就对不上了"。
    """
    if skill is None:
        return {
            "term_types": [],
            "relation_types": [],
            "constraints": [],
            "sources": [],
            "unmatched_columns": {},
            "questions": [],
        }
    return {
        "term_types": [
            {
                "value": t.value,
                "display_name": t.display_name,
                "provenance": "skill",
                "review": "pending",
                "standard_name_value_type": t.standard_name_value_type,
                # 键名跟着本体表走（name/value_type/label），应用到草稿时原样
                # 透传给 replace_draft，不需要中间再翻译一次。
                "extra_fields": [
                    {"name": f.name, "value_type": f.value_type, "label": f.display_name}
                    for f in t.extra_fields
                ],
                "key_aliases": list(t.key_aliases),
                "field_aliases": {k: list(v) for k, v in t.field_aliases.items()},
                "clues": [],
                "data_match": None,
            }
            for t in skill.term_types
        ],
        "relation_types": [
            {
                "relation_type": r.relation_type,
                "example_phrase": r.example_phrase,
                "description": r.description,
                "provenance": "skill",
                "review": "pending",
                "clues": [],
                "data_match": None,
            }
            for r in skill.relation_types
        ],
        "constraints": [
            {
                "subject": c.subject,
                "relation": c.relation,
                "object": c.object,
                "provenance": "skill",
                "review": "pending",
            }
            for c in skill.constraints
        ],
        "sources": [],
        "unmatched_columns": {},
        "questions": [],
    }


def _require_keys(item: object, keys: tuple[str, ...], *, where: str) -> dict:
    if not isinstance(item, dict):
        raise InvalidWorkspaceStateError(f"{where} 的每一项要是映射，收到: {item!r}")
    for key in keys:
        if not isinstance(item.get(key), str) or not item[key]:
            raise InvalidWorkspaceStateError(f"{where} 缺少非空字符串字段 {key}: {item!r}")
    return item


def _check_review_and_provenance(item: dict, *, where: str) -> None:
    provenance = item.get("provenance")
    if provenance not in _PROVENANCES:
        raise InvalidWorkspaceStateError(
            f"{where} 的 provenance {provenance!r} 不合法，v1 只接受 {sorted(_PROVENANCES)}"
        )
    review = item.get("review")
    if review not in _REVIEWS:
        raise InvalidWorkspaceStateError(
            f"{where} 的 review {review!r} 不合法，只接受 {sorted(_REVIEWS)}"
        )


def validate_state(state: object) -> dict:
    """校验外形并补齐缺失的顶层键，返回可以直接 json.dumps 的 dict。

    缺键补齐而不是报错：前端少传一类（比如从来没做过数据发现，不传
    unmatched_columns）语义就是"这一类什么都没有"，让它 400 只会逼前端
    每次都构造完整对象。
    """
    if not isinstance(state, dict):
        raise InvalidWorkspaceStateError(f"工作区状态要是映射，收到: {type(state).__name__}")
    normalized: dict = {}
    for key, kind in _TOP_LEVEL_DEFAULTS.items():
        value = state.get(key, kind())
        if not isinstance(value, kind):
            raise InvalidWorkspaceStateError(
                f"工作区状态的 {key} 要是{'列表' if kind is list else '映射'}，收到: {value!r}"
            )
        normalized[key] = value

    for item in normalized["term_types"]:
        _require_keys(item, ("value",), where="term_types")
        _check_review_and_provenance(item, where=f"term_types[{item.get('value')!r}]")
    for item in normalized["relation_types"]:
        _require_keys(item, ("relation_type",), where="relation_types")
        _check_review_and_provenance(item, where=f"relation_types[{item.get('relation_type')!r}]")
    for item in normalized["constraints"]:
        _require_keys(item, ("subject", "relation", "object"), where="constraints")
        _check_review_and_provenance(item, where=f"constraints[{item!r}]")
    for item in normalized["sources"]:
        _require_keys(item, ("file",), where="sources")
    for file_name, columns in normalized["unmatched_columns"].items():
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise InvalidWorkspaceStateError(
                f"unmatched_columns[{file_name!r}] 要是字符串列表，收到: {columns!r}"
            )
    return normalized


def _row_to_workspace(tenant_id: str, row) -> ModelingWorkspace:
    return ModelingWorkspace(
        tenant_id=tenant_id,
        skill_name=row[0],
        skill_version=row[1],
        state=json.loads(row[2]),
        updated_at=row[3],
        updated_by=row[4],
    )


async def get_workspace(conn: aiosqlite.Connection, tenant_id: str) -> ModelingWorkspace | None:
    cursor = await conn.execute(
        "SELECT skill_name, skill_version, state_json, updated_at, updated_by "
        "FROM ontology_modeling_workspaces WHERE tenant_id = ?",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else _row_to_workspace(tenant_id, row)


async def create_workspace(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    skill: OntologySkill | None,
    actor: str,
    now: str,
) -> ModelingWorkspace:
    if await get_workspace(conn, tenant_id) is not None:
        raise WorkspaceExistsError(f"租户 {tenant_id} 已经有建模工作区了")
    state = initial_state_from_skill(skill)
    await conn.execute(
        "INSERT INTO ontology_modeling_workspaces "
        "(tenant_id, skill_name, skill_version, state_json, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            tenant_id,
            None if skill is None else skill.name,
            None if skill is None else skill.version,
            json.dumps(state, ensure_ascii=False),
            now,
            actor,
        ),
    )
    await conn.commit()
    return ModelingWorkspace(
        tenant_id=tenant_id,
        skill_name=None if skill is None else skill.name,
        skill_version=None if skill is None else skill.version,
        state=state,
        updated_at=now,
        updated_by=actor,
    )


async def save_workspace(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    state: dict,
    expected_updated_at: str,
    actor: str,
    now: str,
) -> ModelingWorkspace:
    """整份写回，乐观锁。

    带时间戳而不是无脑覆盖：工作台是长期存在的（spec 决策 10），同一个租户
    的两个人同时开着它很正常。无锁覆盖时后点保存的人会静默抹掉前一个人刚
    做完的一批审阅，而两边界面都显示"已保存"。
    """
    current = await get_workspace(conn, tenant_id)
    if current is None:
        raise WorkspaceNotFoundError(f"租户 {tenant_id} 还没有建模工作区")
    if current.updated_at != expected_updated_at:
        raise WorkspaceConflictError(
            f"工作区在 {current.updated_at} 被 {current.updated_by} 改过，"
            f"你手上这份是 {expected_updated_at} 的。刷新后重试。"
        )
    # 校验放在写之前：校验失败时库里还是上一版，不需要事务回滚（理由同
    # replace_draft 的 docstring：单例连接上不能用显式事务）。
    normalized = validate_state(state)
    await conn.execute(
        "UPDATE ontology_modeling_workspaces SET state_json = ?, updated_at = ?, updated_by = ? "
        "WHERE tenant_id = ?",
        (json.dumps(normalized, ensure_ascii=False), now, actor, tenant_id),
    )
    await conn.commit()
    return ModelingWorkspace(
        tenant_id=tenant_id,
        skill_name=current.skill_name,
        skill_version=current.skill_version,
        state=normalized,
        updated_at=now,
        updated_by=actor,
    )


async def delete_workspace(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """删掉工作区。幂等——"重新起步"按钮会先删再建，删一个不存在的不该报错。"""
    await conn.execute("DELETE FROM ontology_modeling_workspaces WHERE tenant_id = ?", (tenant_id,))
    await conn.commit()
```

- [ ] **Step 4: 挂进统一建表入口**

`app/graphrag/ontology_lifecycle.py` 的 import 区加：

```python
from app.graphrag.ontology_modeling_workspace import ensure_modeling_workspace_schema
```

`ensure_ontology_schema` 里 `await ensure_etl_mapping_schema(conn)` 之后加一行：

```python
    await ensure_modeling_workspace_schema(conn)
```

并把该函数 docstring 里"四张表一起建"改成"五张表一起建"，补一句：建模工作区
挂进来的理由是它跟本体同寿（一个租户一份，随本体一起存在），单独让调用方记
得建表必然会漏，漏了的表现是工作台首次请求 `no such table`。

- [ ] **Step 5: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_modeling_workspace.py -q`
Expected: 全部 PASS

- [ ] **Step 6: 变异检查**

1. 把 `save_workspace` 里 `if current.updated_at != expected_updated_at:` 改成 `if False:`：`test_save_requires_matching_updated_at` 必须变红。改回。
2. 把 `save_workspace` 里的 `validate_state(state)` 换成 `normalized = state`：`test_save_rejects_malformed_state_without_writing` 必须变红。改回。
3. 把 `ensure_ontology_schema` 里新加的那行注释掉：`test_ensure_ontology_schema_creates_the_workspace_table` 必须变红。改回。

- [ ] **Step 7: 跑一遍本体相关回归**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_lifecycle.py tests/graphrag/test_ontology_etl_mapping.py -q`
Expected: 全部 PASS（建表入口改动不该影响既有行为）

- [ ] **Step 8: 提交**

```bash
git add app/graphrag/ontology_modeling_workspace.py app/graphrag/ontology_lifecycle.py tests/graphrag/test_ontology_modeling_workspace.py
git commit -m "feat(ontology): 建模工作区存储，带乐观锁与状态外形校验

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 落地状态推导

**Files:**
- Create: `app/graphrag/ontology_grounding.py`
- Test: `tests/graphrag/test_ontology_grounding.py`

**Interfaces:**
- Consumes: `app.graphrag.ontology_etl_mapping.get_etl_mapping(conn, tenant_id, *, status) -> EtlMapping | None`（字段 `config_yaml` / `source_file_name` / `created_at`）；`app.graphrag.schema_etl_config.parse_schema_etl_config(text, *, origin) -> SchemaETLConfig`（`entities[*].term_type`、`relations[*].relation_type`）与 `InvalidSchemaETLConfigError`
- Produces:
  - `@dataclass(frozen=True) Grounding(status: str | None, grounded_term_types: list[str], grounded_relation_types: list[str], source_files: list[str], parse_error: str | None)`，带 `to_dict()`
  - `async derive_grounding(conn, tenant_id) -> Grounding`

- [ ] **Step 1: 写失败的测试**

```python
# tests/graphrag/test_ontology_grounding.py
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_etl_mapping import ensure_etl_mapping_schema
from app.graphrag.ontology_grounding import derive_grounding

pytestmark = pytest.mark.anyio

_YAML = """\
tenant_id: t1
entities:
  - term_type: SKU
    source_file: sku.xls
    standard_name_column: name
    node_key_parts:
      - column: jan
    field_mappings:
      color: 颜色
  - term_type: Store
    source_file: store.csv
    standard_name_column: store_name
    node_key_parts:
      - column: store_cd
relations:
  - relation_type: SOLD_AT
    source_file: sales.csv
    subject_term_type: SKU
    object_term_type: Store
"""


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_etl_mapping_schema(conn)
    return conn


async def _put(conn, tenant_id: str, status: str, yaml_text: str, file_name: str = "sku.xls") -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO ontology_etl_mapping "
        "(tenant_id, status, config_yaml, source_file_name, created_at) VALUES (?, ?, ?, ?, ?)",
        (tenant_id, status, yaml_text, file_name, "2026-09-16T10:00:00"),
    )
    await conn.commit()


async def test_no_mapping_means_nothing_is_grounded():
    conn = await _conn()
    grounding = await derive_grounding(conn, "t1")
    assert grounding.status is None
    assert grounding.grounded_term_types == []
    assert grounding.grounded_relation_types == []
    assert grounding.parse_error is None


async def test_derives_from_confirmed_when_no_draft():
    conn = await _conn()
    await _put(conn, "t1", "confirmed", _YAML)
    grounding = await derive_grounding(conn, "t1")
    assert grounding.status == "confirmed"
    assert grounding.grounded_term_types == ["SKU", "Store"]
    assert grounding.grounded_relation_types == ["SOLD_AT"]
    assert grounding.source_files == ["sales.csv", "sku.xls", "store.csv"]


async def test_draft_wins_over_confirmed():
    """草稿优先：用户刚在工作台应用了一版映射，落地状态必须立刻反映那一版，
    而不是上一次确认时的样子——否则他会看到自己刚接上的表仍然显示未落地。"""
    conn = await _conn()
    await _put(conn, "t1", "confirmed", _YAML)
    await _put(
        conn,
        "t1",
        "draft",
        "tenant_id: t1\nentities:\n"
        "  - term_type: Category\n"
        "    source_file: cat.csv\n"
        "    standard_name_column: name\n"
        "    node_key_parts:\n      - column: cat_cd\n"
        "relations: []\n",
    )
    grounding = await derive_grounding(conn, "t1")
    assert grounding.status == "draft"
    assert grounding.grounded_term_types == ["Category"]


async def test_other_tenants_mapping_does_not_leak():
    conn = await _conn()
    await _put(conn, "other", "confirmed", _YAML)
    grounding = await derive_grounding(conn, "t1")
    assert grounding.grounded_term_types == []


async def test_broken_yaml_reports_the_reason_instead_of_raising():
    """存下来的 YAML 坏了不该让工作台打不开：返回空落地 + 一句原因，跟
    admin_ontology_routes 里 summarize 失败时的处理口径一致。"""
    conn = await _conn()
    await _put(conn, "t1", "draft", "entities: [\n")
    grounding = await derive_grounding(conn, "t1")
    assert grounding.grounded_term_types == []
    assert grounding.parse_error is not None
    assert grounding.status == "draft"


async def test_duplicates_are_collapsed_and_sorted():
    conn = await _conn()
    await _put(
        conn,
        "t1",
        "draft",
        "tenant_id: t1\nentities:\n"
        "  - term_type: SKU\n    source_file: a.csv\n    standard_name_column: n\n"
        "    node_key_parts:\n      - column: jan\n"
        "  - term_type: SKU\n    source_file: b.csv\n    standard_name_column: n\n"
        "    node_key_parts:\n      - column: jan\n"
        "relations: []\n",
    )
    grounding = await derive_grounding(conn, "t1")
    assert grounding.grounded_term_types == ["SKU"]
    assert grounding.source_files == ["a.csv", "b.csv"]


async def test_to_dict_shape():
    conn = await _conn()
    await _put(conn, "t1", "confirmed", _YAML)
    payload = (await derive_grounding(conn, "t1")).to_dict()
    assert payload == {
        "status": "confirmed",
        "grounded_term_types": ["SKU", "Store"],
        "grounded_relation_types": ["SOLD_AT"],
        "source_files": ["sales.csv", "sku.xls", "store.csv"],
        "parse_error": None,
    }
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_grounding.py -q`
Expected: `ModuleNotFoundError: app.graphrag.ontology_grounding`

- [ ] **Step 3: 写推导模块**

```python
# app/graphrag/ontology_grounding.py
"""落地状态：某个实体类型/关系类型有没有真实数据支撑。

**纯推导，不存、不缓存**（spec 决策 7）：ETL 映射里有一条指向它就是落地，
否则未落地。存一份的话就有两个真相——用户在表格导入页改了映射，工作台里
那份标记不会跟着变，而没有任何东西告诉他两者已经不一致。

草稿映射优先于已确认映射：用户刚在工作台点了"应用"，写下去的是草稿映射；
这时若仍按已确认的算，他会看到自己刚接上的表显示"未落地"。
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from app.graphrag.ontology_etl_mapping import get_etl_mapping
from app.graphrag.schema_etl_config import (
    InvalidSchemaETLConfigError,
    parse_schema_etl_config,
)


@dataclass(frozen=True)
class Grounding:
    #: 这份落地状态是从哪个版本的映射算出来的：draft / confirmed / None（没有映射）
    status: str | None
    grounded_term_types: list[str]
    grounded_relation_types: list[str]
    #: 映射里出现过的表名，去重排序。界面上用来回答"这些结论是看哪几张表得出的"。
    source_files: list[str]
    #: 映射存在但解析失败时的原因。此时三个列表都是空的——**不是**"没有落地"，
    #: 而是"算不出来"，界面必须把这句话显示出来，否则用户会以为数据全掉了。
    parse_error: str | None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "grounded_term_types": self.grounded_term_types,
            "grounded_relation_types": self.grounded_relation_types,
            "source_files": self.source_files,
            "parse_error": self.parse_error,
        }


async def derive_grounding(conn: aiosqlite.Connection, tenant_id: str) -> Grounding:
    for status in ("draft", "confirmed"):
        mapping = await get_etl_mapping(conn, tenant_id, status=status)
        if mapping is None:
            continue
        try:
            config = parse_schema_etl_config(
                mapping.config_yaml, origin=f"{tenant_id} 的 {status} ETL 映射"
            )
        except InvalidSchemaETLConfigError as exc:
            return Grounding(
                status=status,
                grounded_term_types=[],
                grounded_relation_types=[],
                source_files=[],
                parse_error=str(exc),
            )
        return Grounding(
            status=status,
            grounded_term_types=sorted({e.term_type for e in config.entities}),
            grounded_relation_types=sorted({r.relation_type for r in config.relations}),
            source_files=sorted(
                {e.source_file for e in config.entities} | {r.source_file for r in config.relations}
            ),
            parse_error=None,
        )
    return Grounding(
        status=None,
        grounded_term_types=[],
        grounded_relation_types=[],
        source_files=[],
        parse_error=None,
    )
```

注意：`parse_schema_etl_config` 对语法错误的 YAML 抛的可能是 `yaml.YAMLError`
而不是 `InvalidSchemaETLConfigError`。跑 Step 4 时若 `test_broken_yaml_...`
以 `yaml.scanner.ScannerError` 失败，把 except 子句改成
`except (InvalidSchemaETLConfigError, yaml.YAMLError) as exc:` 并 `import yaml`
——**不要**改成裸 `except Exception`，那会把编程错误也吞掉。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_grounding.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 变异检查**

把 `for status in ("draft", "confirmed"):` 改成 `("confirmed", "draft")`：
`test_draft_wins_over_confirmed` 必须变红。改回。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/ontology_grounding.py tests/graphrag/test_ontology_grounding.py
git commit -m "feat(ontology): 从 ETL 映射推导落地状态

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 应用前的草稿 diff

**Files:**
- Create: `app/graphrag/ontology_workspace_apply.py`
- Test: `tests/graphrag/test_ontology_workspace_apply.py`

**Interfaces:**
- Consumes: `list_term_types(conn, tenant_id, *, status) -> list[TermTypeCategory(value, extra_fields: list[ExtraFieldSpec(name, value_type, label)], standard_name_value_type)]`；`list_relation_types(conn, tenant_id, *, status) -> list[RelationTypeDef(relation_type, example_phrase, description, allow_chain_query, source)]`；`list_allowed_combinations(conn, tenant_id, *, status) -> list[AllowedCombination(subject_term_type, relation_type, object_term_type)]`
- Produces:
  - `@dataclass(frozen=True) DraftDiff(added_term_types, removed_term_types, changed_term_types, added_relation_types, removed_relation_types, added_constraints, removed_constraints)`，全是 `list[str]`（约束用 `"主语 -关系-> 宾语"` 的可读串），带 `to_dict()`
  - `async diff_against_draft(conn, tenant_id, *, term_types: list[dict], relation_types: list[dict], constraints: list[dict]) -> DraftDiff`

- [ ] **Step 1: 写失败的测试**

```python
# tests/graphrag/test_ontology_workspace_apply.py
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_lifecycle import ensure_ontology_schema, replace_draft
from app.graphrag.ontology_workspace_apply import diff_against_draft

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    return conn


async def _seed_draft(conn) -> None:
    await replace_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
                "standard_name_value_type": "string",
            },
            {"value": "手工加的", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[{"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售"}],
        constraints=[],
        actor="alice",
    )


async def test_empty_draft_means_everything_is_added():
    conn = await _conn()
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[{"relation_type": "SOLD_AT"}],
        constraints=[{"subject_term_type": "SKU", "relation_type": "SOLD_AT", "object_term_type": "SKU"}],
    )
    assert diff.added_term_types == ["SKU"]
    assert diff.added_relation_types == ["SOLD_AT"]
    assert diff.added_constraints == ["SKU -SOLD_AT-> SKU"]
    assert diff.removed_term_types == []


async def test_elements_only_in_draft_are_reported_as_removed():
    """整份替换会把它们删掉，而它们多半是用户在「本体结构」页手工加的
    （spec 决策 10）。不单独列出来的话，用户点"应用"时不知道自己在删东西。"""
    conn = await _conn()
    await _seed_draft(conn)
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
                "standard_name_value_type": "string",
            }
        ],
        relation_types=[{"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售"}],
        constraints=[],
    )
    assert diff.removed_term_types == ["手工加的"]
    assert diff.added_term_types == []
    assert diff.changed_term_types == []
    assert diff.removed_relation_types == []


async def test_changed_term_type_is_reported_with_what_changed():
    conn = await _conn()
    await _seed_draft(conn)
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [
                    {"name": "color", "value_type": "string", "label": "颜色"},
                    {"name": "size", "value_type": "string", "label": "尺码"},
                ],
                "standard_name_value_type": "string",
            },
            {"value": "手工加的", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[{"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售"}],
        constraints=[],
    )
    assert diff.added_term_types == []
    assert diff.removed_term_types == []
    assert len(diff.changed_term_types) == 1
    # 只说"SKU 变了"用户没法判断要不要点——必须说出变的是哪个字段
    assert "SKU" in diff.changed_term_types[0]
    assert "size" in diff.changed_term_types[0]


async def test_identical_submission_produces_an_empty_diff():
    conn = await _conn()
    await _seed_draft(conn)
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
                "standard_name_value_type": "string",
            },
            {"value": "手工加的", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[{"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售"}],
        constraints=[],
    )
    assert diff.to_dict() == {
        "added_term_types": [],
        "removed_term_types": [],
        "changed_term_types": [],
        "added_relation_types": [],
        "removed_relation_types": [],
        "added_constraints": [],
        "removed_constraints": [],
    }


async def test_extra_field_order_does_not_count_as_a_change():
    """字段顺序在本体表里没有语义（extra_fields 是一个 JSON 列表，读的地方
    都按 name 取）。顺序当成变更的话，每次应用都会报一堆假变更。"""
    conn = await _conn()
    await replace_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [
                    {"name": "color", "value_type": "string", "label": "颜色"},
                    {"name": "size", "value_type": "string", "label": "尺码"},
                ],
                "standard_name_value_type": "string",
            }
        ],
        relation_types=[],
        constraints=[],
        actor="alice",
    )
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [
                    {"name": "size", "value_type": "string", "label": "尺码"},
                    {"name": "color", "value_type": "string", "label": "颜色"},
                ],
                "standard_name_value_type": "string",
            }
        ],
        relation_types=[],
        constraints=[],
    )
    assert diff.changed_term_types == []


async def test_standard_name_value_type_change_is_detected():
    conn = await _conn()
    await _seed_draft(conn)
    diff = await diff_against_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
                "standard_name_value_type": "number",
            },
            {"value": "手工加的", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[{"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售"}],
        constraints=[],
    )
    assert len(diff.changed_term_types) == 1
    assert "number" in diff.changed_term_types[0]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_workspace_apply.py -q`
Expected: `ModuleNotFoundError: app.graphrag.ontology_workspace_apply`

- [ ] **Step 3: 写 diff 模块**

```python
# app/graphrag/ontology_workspace_apply.py
"""把工作台将要提交的一份草案，跟当前草稿比一比。

存在的理由：replace_draft 是**整份替换**——工作台提交什么，草稿就变成什么。
草稿里可能有用户在「本体结构」页手工加的东西（spec 决策 10 明确要保留这条
路径），它们不在工作区里，会被这次替换删掉。不先告诉用户就是静默删数据。

只算差异、不做决定：这里不阻止任何一种差异，用户看过 diff 自己点。
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations
from app.graphrag.ontology_relations import list_relation_types


@dataclass(frozen=True)
class DraftDiff:
    added_term_types: list[str]
    removed_term_types: list[str]
    #: 已有但内容变了的实体类型，每条是一句人话，说清变的是什么。
    changed_term_types: list[str]
    added_relation_types: list[str]
    removed_relation_types: list[str]
    added_constraints: list[str]
    removed_constraints: list[str]

    def to_dict(self) -> dict:
        return {
            "added_term_types": self.added_term_types,
            "removed_term_types": self.removed_term_types,
            "changed_term_types": self.changed_term_types,
            "added_relation_types": self.added_relation_types,
            "removed_relation_types": self.removed_relation_types,
            "added_constraints": self.added_constraints,
            "removed_constraints": self.removed_constraints,
        }


def _field_signature(extra_fields: list) -> dict[str, tuple[str, str]]:
    """字段签名：name -> (value_type, label)。

    用映射而不是列表：顺序在本体表里没有语义（读的地方都按 name 取），
    按列表比的话，用户在工作台调一下字段顺序就会报一条假变更。
    """
    signature: dict[str, tuple[str, str]] = {}
    for item in extra_fields:
        if isinstance(item, dict):
            signature[item["name"]] = (item.get("value_type", ""), item.get("label", ""))
        else:  # ExtraFieldSpec
            signature[item.name] = (item.value_type, item.label)
    return signature


def _describe_field_changes(before: dict, after: dict) -> list[str]:
    changes: list[str] = []
    for name in sorted(set(after) - set(before)):
        changes.append(f"新增字段 {name}")
    for name in sorted(set(before) - set(after)):
        changes.append(f"删除字段 {name}")
    for name in sorted(set(before) & set(after)):
        if before[name] != after[name]:
            changes.append(f"字段 {name} 由 {before[name]} 改成 {after[name]}")
    return changes


def _constraint_label(subject: str, relation: str, obj: str) -> str:
    return f"{subject} -{relation}-> {obj}"


async def diff_against_draft(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    term_types: list[dict],
    relation_types: list[dict],
    constraints: list[dict],
) -> DraftDiff:
    current_terms = {t.value: t for t in await list_term_types(conn, tenant_id, status="draft")}
    current_relations = {
        r.relation_type for r in await list_relation_types(conn, tenant_id, status="draft")
    }
    current_constraints = {
        _constraint_label(c.subject_term_type, c.relation_type, c.object_term_type)
        for c in await list_allowed_combinations(conn, tenant_id, status="draft")
    }

    submitted_terms = {t["value"]: t for t in term_types}
    submitted_relations = {r["relation_type"] for r in relation_types}
    submitted_constraints = {
        _constraint_label(c["subject_term_type"], c["relation_type"], c["object_term_type"])
        for c in constraints
    }

    changed: list[str] = []
    for value in sorted(set(current_terms) & set(submitted_terms)):
        before = current_terms[value]
        after = submitted_terms[value]
        descriptions = _describe_field_changes(
            _field_signature(before.extra_fields), _field_signature(after.get("extra_fields", []))
        )
        after_value_type = after.get("standard_name_value_type", "string")
        if before.standard_name_value_type != after_value_type:
            descriptions.append(
                f"标准名类型由 {before.standard_name_value_type} 改成 {after_value_type}"
            )
        if descriptions:
            changed.append(f"{value}：" + "；".join(descriptions))

    return DraftDiff(
        added_term_types=sorted(set(submitted_terms) - set(current_terms)),
        removed_term_types=sorted(set(current_terms) - set(submitted_terms)),
        changed_term_types=changed,
        added_relation_types=sorted(submitted_relations - current_relations),
        removed_relation_types=sorted(current_relations - submitted_relations),
        added_constraints=sorted(submitted_constraints - current_constraints),
        removed_constraints=sorted(current_constraints - submitted_constraints),
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_workspace_apply.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 变异检查**

1. 把 `removed_term_types` 那一行改成 `removed_term_types=[]`：`test_elements_only_in_draft_are_reported_as_removed` 必须变红。改回。
2. 把 `_field_signature` 改成返回 `{i: item for i, item in enumerate(extra_fields)}` 之类的按序结构（或直接比列表）：`test_extra_field_order_does_not_count_as_a_change` 必须变红。改回。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/ontology_workspace_apply.py tests/graphrag/test_ontology_workspace_apply.py
git commit -m "feat(ontology): 应用到草稿前的差异计算，删除项单列

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 导出为领域模板（skill YAML）

**Files:**
- Create: `app/graphrag/ontology_skill_export.py`
- Test: `tests/graphrag/test_ontology_skill_export.py`

**Interfaces:**
- Consumes: Task 1 的 `load_skill` / `SkillFormatError`（往返测试用）；`list_term_types` / `list_relation_types` / `list_allowed_combinations`（`status="confirmed"`）；`get_etl_mapping(conn, tenant_id, status="confirmed")`；`parse_schema_etl_config`
- Produces: `async export_skill_yaml(conn, tenant_id, *, skill_name: str, display_name: str, today: str) -> str`；`class NothingToExportError(Exception)`

- [ ] **Step 1: 写失败的测试**

```python
# tests/graphrag/test_ontology_skill_export.py
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_lifecycle import confirm_ontology, ensure_ontology_schema, replace_draft
from app.graphrag.ontology_skill_export import NothingToExportError, export_skill_yaml
from app.graphrag.ontology_skills import load_skill

pytestmark = pytest.mark.anyio

_MAPPING = """\
tenant_id: t1
entities:
  - term_type: SKU
    source_file: sku.xls
    standard_name_column: 商品名称
    node_key_parts:
      - column: JAN
    field_mappings:
      color: 现地语色
  - term_type: Store
    source_file: store.csv
    standard_name_column: 门店名
    node_key_parts:
      - column: STORE_CD
relations:
  - relation_type: SOLD_AT
    source_file: sales.csv
    subject_term_type: SKU
    object_term_type: Store
"""


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    return conn


async def _confirmed_ontology(conn) -> None:
    await replace_draft(
        conn,
        "t1",
        term_types=[
            {
                "value": "SKU",
                "extra_fields": [{"name": "color", "value_type": "string", "label": "颜色"}],
                "standard_name_value_type": "string",
            },
            {"value": "Store", "extra_fields": [], "standard_name_value_type": "string"},
        ],
        relation_types=[
            {"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售", "description": ""}
        ],
        constraints=[
            {"subject_term_type": "SKU", "relation_type": "SOLD_AT", "object_term_type": "Store"}
        ],
        etl_mapping={"config_yaml": _MAPPING, "source_file_name": "sku.xls"},
        actor="alice",
    )
    await confirm_ontology(conn, "t1", actor="alice")


async def test_export_round_trips_through_load_skill(tmp_path):
    """导出的产物必须是 load_skill 收得下的——这是"导出→人工审阅→提交进
    app/ontology_skills/"这条路（spec 决策 6）成立的前提。"""
    conn = await _conn()
    await _confirmed_ontology(conn)
    text = await export_skill_yaml(
        conn, "t1", skill_name="consumer_retail_t1", display_name="消费品零售（t1 导出）",
        today="2026-09-16",
    )
    path = tmp_path / "skill.yaml"
    path.write_text(text, encoding="utf-8")
    skill = load_skill(path)
    assert skill.name == "consumer_retail_t1"
    assert skill.display_name == "消费品零售（t1 导出）"
    assert {t.value for t in skill.term_types} == {"SKU", "Store"}
    assert [r.relation_type for r in skill.relation_types] == ["SOLD_AT"]
    assert skill.constraints[0].subject == "SKU"
    assert skill.constraints[0].object == "Store"
    # 来源写在 description 里：这份产物会被人拿去改，得知道它是从哪个租户
    # 哪一天导出来的（spec 行为规格 §6）
    assert "t1" in skill.description
    assert "2026-09-16" in skill.description
    # questions 是 v2 的东西，导出时给空
    assert skill.questions == []


async def test_aliases_come_from_the_columns_the_mapping_actually_used():
    conn = await _conn()
    await _confirmed_ontology(conn)
    text = await export_skill_yaml(
        conn, "t1", skill_name="x", display_name="X", today="2026-09-16"
    )
    assert "JAN" in text
    assert "STORE_CD" in text
    assert "现地语色" in text


async def test_export_without_mapping_still_produces_a_loadable_skill(tmp_path):
    """本体确认了、映射还没配，导出仍然要能用——别名为空而已，人工补。"""
    conn = await _conn()
    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[],
        constraints=[],
        actor="alice",
    )
    await confirm_ontology(conn, "t1", actor="alice")
    text = await export_skill_yaml(
        conn, "t1", skill_name="x", display_name="X", today="2026-09-16"
    )
    path = tmp_path / "skill.yaml"
    path.write_text(text, encoding="utf-8")
    skill = load_skill(path)
    assert skill.term_types[0].key_aliases == []


async def test_export_without_confirmed_ontology_raises():
    """没有已确认本体时导出的是一份空骨架——load_skill 会拒（term_types 非空
    是硬要求），与其产出一个下载下来才发现用不了的文件，不如当场说清楚。"""
    conn = await _conn()
    with pytest.raises(NothingToExportError):
        await export_skill_yaml(conn, "t1", skill_name="x", display_name="X", today="2026-09-16")


async def test_export_rejects_invalid_skill_name():
    conn = await _conn()
    await _confirmed_ontology(conn)
    with pytest.raises(ValueError):
        await export_skill_yaml(
            conn, "t1", skill_name="Not A Name", display_name="X", today="2026-09-16"
        )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_skill_export.py -q`
Expected: `ModuleNotFoundError: app.graphrag.ontology_skill_export`

- [ ] **Step 3: 写导出模块**

```python
# app/graphrag/ontology_skill_export.py
"""把一个租户做完的本体导出成 skill YAML，供人工审阅、脱敏、补别名之后
提交进 app/ontology_skills/。

**不自动入仓**（spec 决策 6）：导出的产物带着这个租户的真实列名（JAN、
STORE_CD 这类还好，客户自定义的列名可能含业务机密），必须有人看过才能
进代码仓给所有租户用。

导出是纯机械操作，因为 skill 格式的 term_types/relation_types/constraints
三段跟本体表一一对应；唯一需要"翻译"的是别名——它在本体里不存在，用 ETL
映射里实际用到的列名当第一个（也是唯一一个）别名。这是一个真实的线索：
那一列就是这个客户对这个概念的叫法。
"""

from __future__ import annotations

import re

import aiosqlite
import yaml

from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations
from app.graphrag.ontology_etl_mapping import get_etl_mapping
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.schema_etl_config import (
    ColumnNodeKeyPart,
    InvalidSchemaETLConfigError,
    parse_schema_etl_config,
)

_SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")


class NothingToExportError(Exception):
    """这个租户还没有已确认的本体。"""


async def export_skill_yaml(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    skill_name: str,
    display_name: str,
    today: str,
) -> str:
    if not _SKILL_NAME_PATTERN.match(skill_name):
        raise ValueError(
            f"skill 名 {skill_name!r} 不合法，必须满足 ^[a-z][a-z0-9_]{{0,63}}$（要当目录名用）"
        )
    term_types = await list_term_types(conn, tenant_id, status="confirmed")
    if not term_types:
        raise NothingToExportError(
            f"租户 {tenant_id} 还没有已确认的本体，导出的会是一份空骨架，装不回去。"
        )
    relation_types = await list_relation_types(conn, tenant_id, status="confirmed")
    combinations = await list_allowed_combinations(conn, tenant_id, status="confirmed")

    key_aliases: dict[str, list[str]] = {}
    field_aliases: dict[str, dict[str, list[str]]] = {}
    mapping = await get_etl_mapping(conn, tenant_id, status="confirmed")
    if mapping is not None:
        try:
            config = parse_schema_etl_config(
                mapping.config_yaml, origin=f"{tenant_id} 的已确认 ETL 映射"
            )
        except InvalidSchemaETLConfigError:
            # 映射坏了不该挡住导出：本体本身是完整的，别名空着让人工补，
            # 比让"导出"这个只读动作报错更有用。
            config = None
        if config is not None:
            for entity in config.entities:
                columns = [
                    part.column
                    for part in entity.node_key_parts
                    if isinstance(part, ColumnNodeKeyPart)
                ]
                if columns:
                    key_aliases.setdefault(entity.term_type, []).extend(columns)
                for field_name, column in entity.field_mappings.items():
                    field_aliases.setdefault(entity.term_type, {}).setdefault(
                        field_name, []
                    ).append(column)

    document = {
        "name": skill_name,
        "version": "1",
        "display_name": display_name,
        "description": (
            f"从租户 {tenant_id} 的已确认本体导出于 {today}。"
            f"别名取自该租户 ETL 映射里实际用到的列名，提交进代码仓前请人工审阅、"
            f"脱敏，并补上其它常见叫法。"
        ),
        "term_types": [
            {
                "value": t.value,
                "display_name": t.value,
                "standard_name_value_type": t.standard_name_value_type,
                "extra_fields": [
                    {"name": f.name, "value_type": f.value_type, "display_name": f.display_name}
                    for f in t.extra_fields
                ],
                "key_aliases": key_aliases.get(t.value, []),
                "field_aliases": field_aliases.get(t.value, {}),
            }
            for t in term_types
        ],
        "relation_types": [
            {
                "relation_type": r.relation_type,
                "example_phrase": r.example_phrase,
                "description": r.description,
            }
            for r in relation_types
        ],
        "constraints": [
            [c.subject_term_type, c.relation_type, c.object_term_type] for c in combinations
        ],
        # v2 才读；导出时留空，由人工按这个领域的真实业务问题填。
        "questions": [],
    }
    # sort_keys=False 保留上面这个顺序：产物是给人读、给人改的，name 在最前面
    # 比按字母序排完 constraints 排在 description 前面更好读。
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_skill_export.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 变异检查**

把 `yaml.safe_dump(...)` 的 `allow_unicode=True` 去掉：`test_aliases_come_from_the_columns_the_mapping_actually_used`（断言 `"现地语色" in text`）必须变红。改回。再把 `key_aliases.setdefault(...)` 那几行注释掉：同一条测试的 `"JAN" in text` 必须变红。改回。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/ontology_skill_export.py tests/graphrag/test_ontology_skill_export.py
git commit -m "feat(ontology): 把已确认本体导出成领域模板 YAML

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 工作台后端路由

**Files:**
- Create: `app/api/admin_modeling_workspace_routes.py`
- Modify: `app/main.py`（import 区 + `tenant_scoped.include_router(...)`，见 `app/main.py:232-251`）
- Test: `tests/api/test_admin_modeling_workspace_routes.py`

**Interfaces:**
- Consumes: Task 1 `deps.get_skill_registry` / `UnknownSkillError`；Task 2 全部；Task 3 `derive_grounding`；Task 4 `diff_against_draft`；Task 5 `export_skill_yaml` / `NothingToExportError`；既有 `deps.get_review_conn`、`deps.require_admin_session`、`app.api.tenant_guard.require_active_tenant_or_404`
- Produces: `router`（`APIRouter(prefix="/api/admin/ontology", dependencies=[Depends(deps.require_admin_session)])`），端点：

| 方法 | 路径（prefix 之后） | 说明 |
|---|---|---|
| GET | `/{tenant_id}/modeling-workspace/skills` | 内置 skill 列表（`to_dict()` 全量，前端起步页要展示实体/关系数） |
| GET | `/{tenant_id}/modeling-workspace` | 读工作区；没有时 `{"workspace": null}`（**不是 404**，见下） |
| POST | `/{tenant_id}/modeling-workspace` | 建工作区，body `{"skill_name": str | null}` |
| PUT | `/{tenant_id}/modeling-workspace` | 整份写回，body `{"state": {...}, "updated_at": str}`，冲突 409 |
| DELETE | `/{tenant_id}/modeling-workspace` | 删工作区（重新起步） |
| GET | `/{tenant_id}/modeling-workspace/grounding` | 落地状态 |
| POST | `/{tenant_id}/modeling-workspace/apply-preview` | diff，body 同 `draft/replace` 的三段 |
| POST | `/{tenant_id}/modeling-workspace/export-skill` | 导出 YAML 文本，body `{"skill_name", "display_name"}` |

- [ ] **Step 1: 写失败的测试**

```python
# tests/api/test_admin_modeling_workspace_routes.py
from __future__ import annotations

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSession
from app.graphrag.ontology_lifecycle import confirm_ontology, ensure_ontology_schema, replace_draft
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.main import app

pytestmark = pytest.mark.anyio

AUTH = {"Authorization": "Bearer x"}


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    await create_tenants_table(conn)
    for tid in ("t1", "t2"):
        await create_tenant(conn, tenant_id=tid, name=tid)
    return conn


@pytest.fixture
def conn_holder() -> dict:
    return {}


@pytest.fixture
def client(conn_holder):
    async def _get_conn():
        if "conn" not in conn_holder:
            conn_holder["conn"] = await _review_conn()
        return conn_holder["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    # 用 admin 身份，跟 tests/api/test_admin_ontology_routes.py 的 _fake_admin_session
    # 一致：这些用例关心的是路由逻辑，不是权限。member 走 require_tenant_access 会去读
    # user_tenants 表（这个手工建表的测试连接里没有），而且访问未授权租户返回的是 403，
    # 会把 test_unknown_tenant_is_404 这条的语义搅乱。
    # 「工作台对 member 开放」这条性质不靠这里保证：新路由挂在 tenant_scoped 下、
    # 没有 require_admin_role，而 tenant_scoped 里的路由按定义就不要求 admin 角色；
    # 界面一侧由 workbenchPage.test.tsx 的「member 也能用」那条盯着。
    app.dependency_overrides[deps.require_admin_session] = lambda: AdminSession(
        username="alice", role="admin", tenant_id=None, expires_at=1e18
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_lists_builtin_skills(client):
    resp = client.get("/api/admin/ontology/t1/modeling-workspace/skills", headers=AUTH)
    assert resp.status_code == 200
    skills = resp.json()["skills"]
    retail = next(s for s in skills if s["name"] == "consumer_retail")
    assert retail["display_name"] == "消费品零售"
    assert any(t["value"] == "SKU" for t in retail["term_types"])


def test_get_absent_workspace_returns_null_not_404(client):
    """404 会被 adminFetch 的调用方当成"这个租户不存在"；"还没建工作区"是
    工作台的正常首屏状态，不是错误。"""
    resp = client.get("/api/admin/ontology/t1/modeling-workspace", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"workspace": None}


def test_create_read_save_round_trip(client):
    created = client.post(
        "/api/admin/ontology/t1/modeling-workspace",
        json={"skill_name": "consumer_retail"},
        headers=AUTH,
    )
    assert created.status_code == 200
    workspace = created.json()["workspace"]
    assert workspace["skill_name"] == "consumer_retail"
    assert workspace["updated_by"] == "alice"
    assert any(t["value"] == "SKU" for t in workspace["state"]["term_types"])

    state = workspace["state"]
    state["term_types"][0]["review"] = "accepted"
    saved = client.put(
        "/api/admin/ontology/t1/modeling-workspace",
        json={"state": state, "updated_at": workspace["updated_at"]},
        headers=AUTH,
    )
    assert saved.status_code == 200
    assert saved.json()["workspace"]["state"]["term_types"][0]["review"] == "accepted"

    again = client.get("/api/admin/ontology/t1/modeling-workspace", headers=AUTH)
    assert again.json()["workspace"]["state"]["term_types"][0]["review"] == "accepted"


def test_create_blank_workspace(client):
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["workspace"]["skill_name"] is None
    assert resp.json()["workspace"]["state"]["term_types"] == []


def test_create_with_unknown_skill_is_400(client):
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": "nope"}, headers=AUTH
    )
    assert resp.status_code == 400
    assert "nope" in resp.json()["detail"]


def test_create_twice_is_409(client):
    client.post("/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH)
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH
    )
    assert resp.status_code == 409


def test_save_with_stale_updated_at_is_409(client):
    created = client.post(
        "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH
    ).json()["workspace"]
    client.put(
        "/api/admin/ontology/t1/modeling-workspace",
        json={"state": created["state"], "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    resp = client.put(
        "/api/admin/ontology/t1/modeling-workspace",
        json={"state": created["state"], "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 409


def test_save_absent_workspace_is_404(client):
    resp = client.put(
        "/api/admin/ontology/t1/modeling-workspace",
        json={"state": {"term_types": []}, "updated_at": "whatever"},
        headers=AUTH,
    )
    assert resp.status_code == 404


def test_save_malformed_state_is_400(client):
    created = client.post(
        "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH
    ).json()["workspace"]
    resp = client.put(
        "/api/admin/ontology/t1/modeling-workspace",
        json={
            "state": {"term_types": [{"value": "SKU", "provenance": "llm", "review": "pending"}]},
            "updated_at": created["updated_at"],
        },
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert "llm" in resp.json()["detail"]


def test_delete_then_recreate(client):
    client.post("/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH)
    assert client.delete("/api/admin/ontology/t1/modeling-workspace", headers=AUTH).status_code == 200
    assert client.get("/api/admin/ontology/t1/modeling-workspace", headers=AUTH).json()["workspace"] is None
    assert (
        client.post(
            "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH
        ).status_code
        == 200
    )


async def test_grounding_reflects_the_mapping(client, conn_holder):
    client.get("/api/admin/ontology/t1/modeling-workspace", headers=AUTH)  # 建连接
    conn = conn_holder["conn"]
    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[],
        constraints=[],
        etl_mapping={
            "config_yaml": (
                "tenant_id: t1\nentities:\n  - term_type: SKU\n    source_file: sku.xls\n"
                "    standard_name_column: n\n    node_key_parts:\n      - column: jan\n"
                "relations: []\n"
            ),
            "source_file_name": "sku.xls",
        },
        actor="alice",
    )
    resp = client.get("/api/admin/ontology/t1/modeling-workspace/grounding", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["grounded_term_types"] == ["SKU"]
    assert resp.json()["status"] == "draft"


def test_apply_preview_returns_a_diff_without_writing(client):
    payload = {
        "term_types": [{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        "relation_types": [],
        "constraints": [],
    }
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace/apply-preview", json=payload, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["added_term_types"] == ["SKU"]
    # 预览不写库：再查一次草稿，SKU 不该在里面
    listed = client.get("/api/admin/ontology/t1/term-types?status=draft", headers=AUTH)
    assert all(t["value"] != "SKU" for t in listed.json()["term_types"])


async def test_export_skill_returns_yaml(client, conn_holder):
    client.get("/api/admin/ontology/t1/modeling-workspace", headers=AUTH)
    conn = conn_holder["conn"]
    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[],
        constraints=[],
        actor="alice",
    )
    await confirm_ontology(conn, "t1", actor="alice")
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace/export-skill",
        json={"skill_name": "t1_domain", "display_name": "T1 领域"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert "name: t1_domain" in resp.json()["yaml"]


def test_export_without_confirmed_ontology_is_400(client):
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace/export-skill",
        json={"skill_name": "t1_domain", "display_name": "T1 领域"},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_unknown_tenant_is_404(client):
    resp = client.get("/api/admin/ontology/nope/modeling-workspace", headers=AUTH)
    assert resp.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/api/test_admin_modeling_workspace_routes.py -q`
Expected: 全部 404（路由还没挂）

- [ ] **Step 3: 写路由**

```python
# app/api/admin_modeling_workspace_routes.py
"""建模工作台的后端接口。

只做编排：参数校验、调 app/graphrag/ 下的纯逻辑、把领域异常翻译成 HTTP 状态。
对齐、投影这些"想法"全在前端（modelingWorkbench/alignToSkeleton.ts、
projectToDraft.ts）——读表和列统计已经在浏览器里做完了，把列名传回后端再对
一次只是多一趟往返。

写草稿没有自己的端点：前端拿 apply-preview 的 diff 给用户看过之后，直接调
既有的 POST /api/admin/ontology/{tenant_id}/draft/replace。那条端点已经有
整份校验、变更日志和映射同提交，再包一层只会把它的错误映射复制一遍。

prefix 用 /api/admin/ontology 而不是新起一个：
tests/api/test_admin_route_shapes.py 的 _TENANT_SCOPED_PREFIXES 里已经有
"/api/admin/ontology/{tenant_id}/"，沿用它就不必再往那份白名单里加条目。
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.ontology_grounding import derive_grounding
from app.graphrag.ontology_modeling_workspace import (
    InvalidWorkspaceStateError,
    WorkspaceConflictError,
    WorkspaceExistsError,
    WorkspaceNotFoundError,
    create_workspace,
    delete_workspace,
    get_workspace,
    save_workspace,
)
from app.graphrag.ontology_skill_export import NothingToExportError, export_skill_yaml
from app.graphrag.ontology_skills import SkillRegistry, UnknownSkillError
from app.graphrag.ontology_workspace_apply import diff_against_draft

router = APIRouter(
    prefix="/api/admin/ontology", dependencies=[Depends(deps.require_admin_session)]
)


class CreateWorkspaceRequest(BaseModel):
    #: 用哪个内置 skill 起步；None = 空白起步
    skill_name: str | None = None


class SaveWorkspaceRequest(BaseModel):
    state: dict
    #: 乐观锁：手上这份工作区的 updated_at。对不上说明期间有人存过。
    updated_at: str


class ApplyPreviewRequest(BaseModel):
    #: 三段的形状跟 /draft/replace 的 payload 一致——预览的就是那次提交。
    term_types: list[dict] = []
    relation_types: list[dict] = []
    constraints: list[dict] = []


class ExportSkillRequest(BaseModel):
    skill_name: str
    display_name: str


@router.get("/{tenant_id}/modeling-workspace/skills")
async def list_modeling_skills(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    registry: SkillRegistry = Depends(deps.get_skill_registry),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    return {"skills": [skill.to_dict() for skill in registry.all()]}


@router.get("/{tenant_id}/modeling-workspace")
async def read_modeling_workspace(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    """没有工作区时返回 {"workspace": null} 而不是 404。

    404 在前端是"这个租户不存在"的信号（require_active_tenant_or_404 用的就是
    它）；"还没建工作区"是工作台的正常首屏状态，用同一个状态码会让起步页跟
    错误页混在一起。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    workspace = await get_workspace(review_conn, tenant_id)
    return {"workspace": None if workspace is None else workspace.to_dict()}


@router.post("/{tenant_id}/modeling-workspace")
async def create_modeling_workspace(
    tenant_id: str,
    payload: CreateWorkspaceRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
    registry: SkillRegistry = Depends(deps.get_skill_registry),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    skill = None
    if payload.skill_name is not None:
        try:
            skill = registry.get(payload.skill_name)
        except UnknownSkillError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    try:
        workspace = await create_workspace(
            review_conn,
            tenant_id,
            skill=skill,
            actor=session.username,
            now=datetime.now().isoformat(),
        )
    except WorkspaceExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"workspace": workspace.to_dict()}


@router.put("/{tenant_id}/modeling-workspace")
async def save_modeling_workspace(
    tenant_id: str,
    payload: SaveWorkspaceRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        workspace = await save_workspace(
            review_conn,
            tenant_id,
            state=payload.state,
            expected_updated_at=payload.updated_at,
            actor=session.username,
            now=datetime.now().isoformat(),
        )
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except InvalidWorkspaceStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"workspace": workspace.to_dict()}


@router.delete("/{tenant_id}/modeling-workspace")
async def delete_modeling_workspace(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    """重新起步。删的只是过程状态——已经应用进草稿的本体不受影响。"""
    await require_active_tenant_or_404(review_conn, tenant_id)
    await delete_workspace(review_conn, tenant_id)
    return {"deleted": True}


@router.get("/{tenant_id}/modeling-workspace/grounding")
async def read_modeling_grounding(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    return (await derive_grounding(review_conn, tenant_id)).to_dict()


@router.post("/{tenant_id}/modeling-workspace/apply-preview")
async def preview_modeling_apply(
    tenant_id: str,
    payload: ApplyPreviewRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    """只算差异，不写库。写库是前端下一步调 /draft/replace 的事。"""
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        diff = await diff_against_draft(
            review_conn,
            tenant_id,
            term_types=payload.term_types,
            relation_types=payload.relation_types,
            constraints=payload.constraints,
        )
    except KeyError as exc:
        # 提交里少了 value / relation_type / subject_term_type 这类必需键。
        # 让它变成裸 500 的话，界面上只会显示"服务器错误"。
        raise HTTPException(status_code=400, detail=f"提交里缺少必需字段 {exc.args[0]!r}")
    return diff.to_dict()


@router.post("/{tenant_id}/modeling-workspace/export-skill")
async def export_modeling_skill(
    tenant_id: str,
    payload: ExportSkillRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    """导出 YAML 文本。用 POST 而不是 GET：要带 skill_name/display_name 两个
    参数，且产物是给人下载的一次性文件，不该被缓存。"""
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        text = await export_skill_yaml(
            review_conn,
            tenant_id,
            skill_name=payload.skill_name,
            display_name=payload.display_name,
            today=datetime.now().date().isoformat(),
        )
    except (NothingToExportError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"yaml": text}
```

- [ ] **Step 4: 挂进 main.py**

`app/main.py` 的 router import 区（`admin_ontology_routes` 那行附近）加：

```python
from app.api.admin_modeling_workspace_routes import router as admin_modeling_workspace_router
```

`tenant_scoped.include_router(admin_ontology_router)` 之后加：

```python
tenant_scoped.include_router(admin_modeling_workspace_router)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/api/test_admin_modeling_workspace_routes.py -q`
Expected: 全部 PASS

- [ ] **Step 6: 跑路由形状测试**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/api/test_admin_route_shapes.py -q`
Expected: 全部 PASS（新路由都在 `/api/admin/ontology/{tenant_id}/` 下，已被 `_TENANT_SCOPED_PREFIXES` 覆盖，且挂在 `tenant_scoped` 里因而自带 `require_tenant_access` 和 CSRF）。若 `test_every_admin_write_route_checks_csrf` 变红，说明 router 挂错了父节点——挂到 `tenant_scoped` 上，不要挂到 `app` 或 `admin_scoped` 上。

- [ ] **Step 7: 变异检查**

1. 把 `read_modeling_workspace` 里的 `return {"workspace": None if ...}` 改成没有工作区时 `raise HTTPException(404)`：`test_get_absent_workspace_returns_null_not_404` 必须变红。改回。
2. 把 `save_modeling_workspace` 里 `except WorkspaceConflictError` 改成 `status_code=200`（或删掉这个 except 让它 500）：`test_save_with_stale_updated_at_is_409` 必须变红。改回。
3. 把 `main.py` 里新加的 `include_router` 注释掉：本文件所有测试变红。改回。

- [ ] **Step 8: 提交**

```bash
git add app/api/admin_modeling_workspace_routes.py app/main.py tests/api/test_admin_modeling_workspace_routes.py
git commit -m "feat(admin): 建模工作台后端路由

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 前端类型、API 封装与别名归一化

**Files:**
- Create: `frontend/src/admin/modelingWorkbench/types.ts`
- Create: `frontend/src/admin/modelingWorkbench/aliases.ts`
- Create: `frontend/src/admin/modelingWorkbench/workspaceApi.ts`
- Test: `frontend/src/admin/modelingWorkbench/aliases.test.ts`
- Test: `frontend/src/admin/modelingWorkbench/workspaceApi.test.ts`

**Interfaces:**
- Consumes: `../adminApi` 的 `adminFetch(path, sessionToken, options?) -> Promise<Response>` 与 `extractErrorDetail(body, fallback) -> string`；`../schemaEtlConfigBuilder/sourceParser` 的 `SourceParseOptions`
- Produces（后面每个前端任务都按这些名字来）：
  - 类型 `Provenance`、`ReviewState`、`WorkspaceExtraField`、`DataMatch`、`Clue`、`WorkspaceTermType`、`WorkspaceRelationType`、`WorkspaceConstraint`、`WorkspaceSource`、`WorkspaceState`、`ModelingWorkspace`、`SkillSummary`、`Grounding`、`DraftDiff`
  - `normalizeAlias(text: string): string`
  - `fetchSkills(tenantId, token)`、`fetchWorkspace(tenantId, token)`、`createWorkspace(tenantId, token, skillName)`、`saveWorkspace(tenantId, token, state, updatedAt)`、`deleteWorkspace(tenantId, token)`、`fetchGrounding(tenantId, token)`、`previewApply(tenantId, token, payload)`、`exportSkill(tenantId, token, skillName, displayName)`
  - `class WorkspaceConflictError extends Error`（`saveWorkspace` 收到 409 时抛）

- [ ] **Step 1: 写失败的测试**

```ts
// frontend/src/admin/modelingWorkbench/aliases.test.ts
import { describe, expect, it } from 'vitest'
import { normalizeAlias } from './aliases'

/**
 * 归一化规则必须跟后端 app/graphrag/ontology_skills.py::normalize_alias 完全
 * 一致：两边算出来的结果不同，就会出现"前端说这列对上了、后端导出的别名
 * 却对不上"这种谁都解释不了的不一致。这几条用例跟后端那份是同一批输入。
 */
describe('normalizeAlias', () => {
  it.each([
    ['JAN', 'jan'],
    ['sku_code', 'skucode'],
    ['Item CD', 'itemcd'],
    ['retail-price', 'retailprice'],
    ['商品编码', '商品编码'],
    ['  JAN  ', 'jan'],
  ])('%s -> %s', (raw, expected) => {
    expect(normalizeAlias(raw)).toBe(expected)
  })
})
```

```ts
// frontend/src/admin/modelingWorkbench/workspaceApi.test.ts
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  WorkspaceConflictError,
  createWorkspace,
  exportSkill,
  fetchSkills,
  fetchWorkspace,
  previewApply,
  saveWorkspace,
} from './workspaceApi'
import type { WorkspaceState } from './types'

const EMPTY_STATE: WorkspaceState = {
  term_types: [],
  relation_types: [],
  constraints: [],
  sources: [],
  unmatched_columns: {},
  questions: [],
}

let calls: { url: string; init: RequestInit | undefined }[] = []

function stubFetch(responder: (url: string) => Response) {
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init })
      return Promise.resolve(responder(String(input)))
    }),
  )
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => {
  vi.unstubAllGlobals()
})

describe('workspaceApi', () => {
  it('租户 id 进路径时做过转义', async () => {
    stubFetch(() => json({ skills: [] }))
    await fetchSkills('a/b', 'tok')
    expect(calls[0].url).toContain('/api/admin/ontology/a%2Fb/modeling-workspace/skills')
  })

  it('没有工作区时返回 null，不抛', async () => {
    stubFetch(() => json({ workspace: null }))
    expect(await fetchWorkspace('t1', 'tok')).toBeNull()
  })

  it('建工作区把 skill_name 放进 body', async () => {
    stubFetch(() =>
      json({
        workspace: {
          tenant_id: 't1',
          skill_name: 'consumer_retail',
          skill_version: '1',
          state: EMPTY_STATE,
          updated_at: 'now',
          updated_by: 'alice',
        },
      }),
    )
    const workspace = await createWorkspace('t1', 'tok', 'consumer_retail')
    expect(workspace.skill_name).toBe('consumer_retail')
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ skill_name: 'consumer_retail' })
  })

  it('保存带上 updated_at 做乐观锁', async () => {
    stubFetch(() =>
      json({
        workspace: {
          tenant_id: 't1',
          skill_name: null,
          skill_version: null,
          state: EMPTY_STATE,
          updated_at: 'later',
          updated_by: 'alice',
        },
      }),
    )
    await saveWorkspace('t1', 'tok', EMPTY_STATE, 'earlier')
    expect(JSON.parse(String(calls[0].init?.body)).updated_at).toBe('earlier')
  })

  it('保存撞上 409 时抛 WorkspaceConflictError，带服务端那句话', async () => {
    // 普通 Error 的话，页面没法把"别人改过，请刷新"跟"网络挂了"分开——
    // 前者要提示刷新，后者要提示重试
    stubFetch(() => json({ detail: '工作区在 later 被 bob 改过' }, 409))
    await expect(saveWorkspace('t1', 'tok', EMPTY_STATE, 'earlier')).rejects.toBeInstanceOf(
      WorkspaceConflictError,
    )
    await expect(saveWorkspace('t1', 'tok', EMPTY_STATE, 'earlier')).rejects.toThrow('bob')
  })

  it('其它错误码抛普通 Error，带 detail', async () => {
    stubFetch(() => json({ detail: 'provenance llm 不合法' }, 400))
    await expect(saveWorkspace('t1', 'tok', EMPTY_STATE, 'x')).rejects.toThrow('llm')
  })

  it('apply-preview 把三段原样发出去', async () => {
    stubFetch(() =>
      json({
        added_term_types: ['SKU'],
        removed_term_types: [],
        changed_term_types: [],
        added_relation_types: [],
        removed_relation_types: [],
        added_constraints: [],
        removed_constraints: [],
      }),
    )
    const diff = await previewApply('t1', 'tok', {
      term_types: [{ value: 'SKU', extra_fields: [], standard_name_value_type: 'string' }],
      relation_types: [],
      constraints: [],
    })
    expect(diff.added_term_types).toEqual(['SKU'])
    expect(JSON.parse(String(calls[0].init?.body)).term_types[0].value).toBe('SKU')
  })

  it('导出返回 YAML 文本', async () => {
    stubFetch(() => json({ yaml: 'name: t1_domain\n' }))
    expect(await exportSkill('t1', 'tok', 't1_domain', 'T1 领域')).toContain('name: t1_domain')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run（在 `frontend/` 下）：`NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench --maxWorkers=2`
Expected: 找不到模块 `./aliases` / `./workspaceApi`

- [ ] **Step 3: 写类型**

```ts
// frontend/src/admin/modelingWorkbench/types.ts
import type { SourceParseOptions } from '../schemaEtlConfigBuilder/sourceParser'

/** 这个元素是谁提出来的。v1 只会出现这三种（llm / document / question 是 v2）。 */
export type Provenance = 'skill' | 'data' | 'manual'

export type ReviewState = 'pending' | 'accepted' | 'rejected'

export interface WorkspaceExtraField {
  /** 内部名。应用到草稿时原样进 ontology_term_types.extra_fields。 */
  name: string
  value_type: string
  /** 显示名。可能是空串（后端 ExtraFieldSpec.label 允许为空）。 */
  label?: string
}

/** 数据发现时对上了哪张表的哪些列。只是历史记录——落地状态由 ETL 映射说了算。 */
export interface DataMatch {
  source_file: string
  key_columns: string[]
  field_columns: Record<string, string>
  /** 怎么对上的：`alias:<别名>` | `column_role` | `manual`。界面要说出依据。 */
  matched_by: string
}

/** 旁证。不改变落地状态，只用于排序和解释。v1 只有 manual 一种。 */
export interface Clue {
  kind: 'manual'
  note: string
  by: string
  at: string
}

export interface WorkspaceTermType {
  value: string
  display_name: string
  provenance: Provenance
  review: ReviewState
  standard_name_value_type: string
  extra_fields: WorkspaceExtraField[]
  key_aliases: string[]
  field_aliases: Record<string, string[]>
  clues: Clue[]
  data_match: DataMatch | null
}

export interface WorkspaceRelationType {
  relation_type: string
  example_phrase: string
  description: string
  provenance: Provenance
  review: ReviewState
  clues: Clue[]
  data_match: DataMatch | null
}

export interface WorkspaceConstraint {
  subject: string
  relation: string
  object: string
  provenance: Provenance
  review: ReviewState
}

/** 一张表怎么读。跟 SourceParseOptions 同义，只是键名按后端 YAML 的写法。 */
export interface WorkspaceSource {
  file: string
  sheet?: string | number | null
  header_row?: number
  first_data_row?: number
}

export interface WorkspaceState {
  term_types: WorkspaceTermType[]
  relation_types: WorkspaceRelationType[]
  constraints: WorkspaceConstraint[]
  sources: WorkspaceSource[]
  /** 表名 -> 数据里有、骨架没接住的列。用户可以一键提升成新实体类型。 */
  unmatched_columns: Record<string, string[]>
  /** v2 的问题清单。v1 恒为空数组。 */
  questions: string[]
}

export interface ModelingWorkspace {
  tenant_id: string
  skill_name: string | null
  skill_version: string | null
  state: WorkspaceState
  updated_at: string
  updated_by: string
}

export interface SkillSummary {
  name: string
  version: string
  display_name: string
  description: string
  term_types: {
    value: string
    display_name: string
    standard_name_value_type: string
    extra_fields: { name: string; value_type: string; display_name: string }[]
    key_aliases: string[]
    field_aliases: Record<string, string[]>
  }[]
  relation_types: { relation_type: string; example_phrase: string; description: string }[]
  constraints: { subject: string; relation: string; object: string }[]
  questions: string[]
  match_hint: string
}

export interface Grounding {
  status: 'draft' | 'confirmed' | null
  grounded_term_types: string[]
  grounded_relation_types: string[]
  source_files: string[]
  /** 映射存在但解析失败时的原因。非空时三个列表是"算不出来"，不是"没有"。 */
  parse_error: string | null
}

export interface DraftDiff {
  added_term_types: string[]
  removed_term_types: string[]
  changed_term_types: string[]
  added_relation_types: string[]
  removed_relation_types: string[]
  added_constraints: string[]
  removed_constraints: string[]
}

/** `/draft/replace` 与 `apply-preview` 共用的提交形状。 */
export interface DraftPayload {
  term_types: { value: string; extra_fields: WorkspaceExtraField[]; standard_name_value_type: string }[]
  relation_types: {
    relation_type: string
    example_phrase: string
    description: string
    allow_chain_query: boolean
  }[]
  constraints: { subject_term_type: string; relation_type: string; object_term_type: string }[]
}

/** state 里的 sources 条目翻成读表用的解析选项。 */
export function parseOptionsOf(source: WorkspaceSource | undefined): SourceParseOptions {
  if (!source) return {}
  const options: SourceParseOptions = {}
  if (source.sheet !== undefined && source.sheet !== null) options.sheet = source.sheet
  if (source.header_row !== undefined) options.headerRow = source.header_row
  if (source.first_data_row !== undefined) options.firstDataRow = source.first_data_row
  return options
}
```

- [ ] **Step 4: 写别名归一化**

```ts
// frontend/src/admin/modelingWorkbench/aliases.ts
/**
 * 别名/列名归一化：去掉空白、下划线、连字符，再转小写。
 *
 * 必须跟后端 app/graphrag/ontology_skills.py::normalize_alias 逐字对应。
 * 两边不一致的后果不是报错，而是"前端说这列对上了、后端导出的别名对不上"
 * 这种谁也解释不了的错位，所以 aliases.test.ts 用的是跟后端那份同一批输入。
 *
 * 只做无损折叠，不做同义词、不做前缀匹配：猜错列会把错误数据写进图谱，
 * 比让用户手动指一次列贵得多。
 */
export function normalizeAlias(text: string): string {
  return text.replace(/[\s_-]+/g, '').toLowerCase()
}
```

- [ ] **Step 5: 写 API 封装**

```ts
// frontend/src/admin/modelingWorkbench/workspaceApi.ts
import { adminFetch, extractErrorDetail } from '../adminApi'
import type {
  DraftDiff,
  DraftPayload,
  Grounding,
  ModelingWorkspace,
  SkillSummary,
  WorkspaceState,
} from './types'

/**
 * 保存时撞上"期间有别人存过"。单独一个类型，因为页面对它的处置跟别的错误
 * 不一样：这个要提示刷新后重做，别的要提示重试。
 */
export class WorkspaceConflictError extends Error {}

function base(tenantId: string): string {
  return `/api/admin/ontology/${encodeURIComponent(tenantId)}/modeling-workspace`
}

async function readOrThrow(response: Response, fallback: string): Promise<unknown> {
  if (response.ok) return response.json()
  const body = await response.json().catch(() => ({}))
  const detail = extractErrorDetail(body, fallback)
  if (response.status === 409) throw new WorkspaceConflictError(detail)
  throw new Error(detail)
}

export async function fetchSkills(tenantId: string, token: string): Promise<SkillSummary[]> {
  const response = await adminFetch(`${base(tenantId)}/skills`, token)
  const body = (await readOrThrow(response, '读取领域模板失败')) as { skills: SkillSummary[] }
  return body.skills
}

export async function fetchWorkspace(
  tenantId: string,
  token: string,
): Promise<ModelingWorkspace | null> {
  const response = await adminFetch(base(tenantId), token)
  const body = (await readOrThrow(response, '读取建模工作区失败')) as {
    workspace: ModelingWorkspace | null
  }
  return body.workspace
}

export async function createWorkspace(
  tenantId: string,
  token: string,
  skillName: string | null,
): Promise<ModelingWorkspace> {
  const response = await adminFetch(base(tenantId), token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName }),
  })
  const body = (await readOrThrow(response, '创建建模工作区失败')) as {
    workspace: ModelingWorkspace
  }
  return body.workspace
}

export async function saveWorkspace(
  tenantId: string,
  token: string,
  state: WorkspaceState,
  updatedAt: string,
): Promise<ModelingWorkspace> {
  const response = await adminFetch(base(tenantId), token, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state, updated_at: updatedAt }),
  })
  const body = (await readOrThrow(response, '保存建模工作区失败')) as {
    workspace: ModelingWorkspace
  }
  return body.workspace
}

export async function deleteWorkspace(tenantId: string, token: string): Promise<void> {
  const response = await adminFetch(base(tenantId), token, { method: 'DELETE' })
  await readOrThrow(response, '删除建模工作区失败')
}

export async function fetchGrounding(tenantId: string, token: string): Promise<Grounding> {
  const response = await adminFetch(`${base(tenantId)}/grounding`, token)
  return (await readOrThrow(response, '读取落地状态失败')) as Grounding
}

export async function previewApply(
  tenantId: string,
  token: string,
  payload: DraftPayload,
): Promise<DraftDiff> {
  const response = await adminFetch(`${base(tenantId)}/apply-preview`, token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return (await readOrThrow(response, '计算差异失败')) as DraftDiff
}

export async function exportSkill(
  tenantId: string,
  token: string,
  skillName: string,
  displayName: string,
): Promise<string> {
  const response = await adminFetch(`${base(tenantId)}/export-skill`, token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName, display_name: displayName }),
  })
  const body = (await readOrThrow(response, '导出领域模板失败')) as { yaml: string }
  return body.yaml
}
```

- [ ] **Step 6: 跑测试确认通过**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench --maxWorkers=2`
Expected: 全部 PASS

- [ ] **Step 7: 变异检查**

1. 把 `readOrThrow` 里 `if (response.status === 409)` 改成 `if (false)`：`保存撞上 409 时抛 WorkspaceConflictError` 必须变红。改回。
2. 把 `base()` 里的 `encodeURIComponent` 去掉：`租户 id 进路径时做过转义` 必须变红。改回。

- [ ] **Step 8: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/types.ts frontend/src/admin/modelingWorkbench/aliases.ts frontend/src/admin/modelingWorkbench/workspaceApi.ts frontend/src/admin/modelingWorkbench/aliases.test.ts frontend/src/admin/modelingWorkbench/workspaceApi.test.ts
git commit -m "feat(admin): 建模工作台前端类型与接口封装

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: 数据发现——把表的列对齐到骨架

**Files:**
- Create: `frontend/src/admin/modelingWorkbench/alignToSkeleton.ts`
- Test: `frontend/src/admin/modelingWorkbench/alignToSkeleton.test.ts`

**Interfaces:**
- Consumes: Task 7 的 `normalizeAlias` 与全部类型；`../guidedOntology/types` 的 `RoledColumn`（`{ stats: ColumnStats; role: ColumnRole; reason: string }`，`ColumnRole = 'identifier' | 'measure' | 'freetext' | 'date' | 'dimension'`）
- Produces:
  - `interface ScannedTable { file: string; roled: RoledColumn[] }`
  - `interface TableAlignment { file: string; matches: { termValue: string; keyColumns: string[]; fieldColumns: Record<string, string>; matchedBy: string }[]; unmatchedColumns: string[] }`
  - `alignTable(table: ScannedTable, termTypes: WorkspaceTermType[]): TableAlignment`
  - `proposeCrossTableRelations(alignments: TableAlignment[], tables: ScannedTable[], state: WorkspaceState): WorkspaceRelationType[]` 及配套 `proposeCrossTableConstraints(...): WorkspaceConstraint[]`
  - `mergeAlignments(state: WorkspaceState, alignments: TableAlignment[], tables: ScannedTable[]): WorkspaceState`（纯函数，不改入参）

**对齐规则**（spec 行为规格 §3，逐条实现）：
1. 只考虑 `review !== 'rejected'` 的实体类型。
2. `key_aliases` 归一化后与列名归一化精确相等 → `keyColumns`，`matchedBy = "alias:<原别名>"`。
3. 其余列用 `field_aliases` 撞 → `fieldColumns`（字段内部名 → 列名）。
4. 没被任何实体接住、且列角色是 `identifier` 或 `dimension` 的列 → `unmatchedColumns`。
5. 跨表关系：同一实体在两张表各命中一列，一张表里该列角色是 `identifier`、另一张是 `dimension` → 提议一条关系，主语 = dimension 那张表所属实体，宾语 = identifier 那张表所属实体；关系名取骨架里主宾匹配的约束，没有则 `RELATES_TO`。不做值级 join。

- [ ] **Step 1: 写失败的测试**

```ts
// frontend/src/admin/modelingWorkbench/alignToSkeleton.test.ts
import { describe, expect, it } from 'vitest'
import { alignTable, mergeAlignments, proposeCrossTableRelations } from './alignToSkeleton'
import type { ScannedTable } from './alignToSkeleton'
import type { WorkspaceState, WorkspaceTermType } from './types'
import type { ColumnRole, RoledColumn } from '../guidedOntology/types'

function column(name: string, role: ColumnRole): RoledColumn {
  return {
    stats: {
      name,
      nonEmptyCount: 100,
      distinctCount: role === 'identifier' ? 100 : 5,
      distinctCapped: false,
      samples: [],
      inferredType: 'string',
      isWholeNumber: false,
    },
    role,
    reason: '测试构造',
  }
}

function term(value: string, keyAliases: string[], fieldAliases: Record<string, string[]> = {}): WorkspaceTermType {
  return {
    value,
    display_name: value,
    provenance: 'skill',
    review: 'pending',
    standard_name_value_type: 'string',
    extra_fields: Object.keys(fieldAliases).map((name) => ({
      name,
      value_type: 'string',
      label: name,
    })),
    key_aliases: keyAliases,
    field_aliases: fieldAliases,
    clues: [],
    data_match: null,
  }
}

const SKU = term('SKU', ['jan', 'sku_code'], { color: ['现地语色', 'color'] })
const STORE = term('Store', ['store_cd'])

const emptyState = (termTypes: WorkspaceTermType[]): WorkspaceState => ({
  term_types: termTypes,
  relation_types: [],
  constraints: [],
  sources: [],
  unmatched_columns: {},
  questions: [],
})

describe('alignTable', () => {
  it('别名归一化后精确命中列名，记下是哪个别名对上的', () => {
    const table: ScannedTable = {
      file: 'sku.xls',
      roled: [column('JAN', 'identifier'), column('现地语色', 'dimension')],
    }
    const alignment = alignTable(table, [SKU, STORE])
    expect(alignment.matches).toHaveLength(1)
    expect(alignment.matches[0].termValue).toBe('SKU')
    expect(alignment.matches[0].keyColumns).toEqual(['JAN'])
    // 依据要能显示出来：用户看到"按别名 jan 对上"才判断得了对不对
    expect(alignment.matches[0].matchedBy).toBe('alias:jan')
    expect(alignment.matches[0].fieldColumns).toEqual({ color: '现地语色' })
    expect(alignment.unmatchedColumns).toEqual([])
  })

  it('命不中就是命不中，不做前缀或包含匹配', () => {
    const table: ScannedTable = { file: 'x.csv', roled: [column('jan_code_v2', 'identifier')] }
    expect(alignTable(table, [SKU]).matches).toEqual([])
    expect(alignTable(table, [SKU]).unmatchedColumns).toEqual(['jan_code_v2'])
  })

  it('已拒绝的实体类型不参与对齐', () => {
    const rejected = { ...SKU, review: 'rejected' as const }
    const table: ScannedTable = { file: 'sku.xls', roled: [column('JAN', 'identifier')] }
    expect(alignTable(table, [rejected]).matches).toEqual([])
  })

  it('没接住的列只收 identifier 和 dimension', () => {
    const table: ScannedTable = {
      file: 'x.csv',
      roled: [
        column('md_no', 'identifier'),
        column('brand', 'dimension'),
        column('revenue', 'measure'),
        column('备注', 'freetext'),
        column('创建日期', 'date'),
      ],
    }
    // 度量/自由文本/日期提升成实体类型没有意义：会给每个金额建一个节点
    expect(alignTable(table, [SKU]).unmatchedColumns).toEqual(['md_no', 'brand'])
  })

  it('已被某个实体当属性用掉的列不算没接住', () => {
    const table: ScannedTable = {
      file: 'sku.xls',
      roled: [column('JAN', 'identifier'), column('现地语色', 'dimension')],
    }
    expect(alignTable(table, [SKU]).unmatchedColumns).toEqual([])
  })
})

describe('proposeCrossTableRelations', () => {
  const skuTable: ScannedTable = { file: 'sku.xls', roled: [column('JAN', 'identifier')] }
  const salesTable: ScannedTable = {
    file: 'sales.csv',
    roled: [column('JAN', 'dimension'), column('STORE_CD', 'identifier')],
  }

  it('同一实体在一张表是标识、在另一张是维度时提一条关系', () => {
    const tables = [skuTable, salesTable]
    const alignments = tables.map((t) => alignTable(t, [SKU, STORE]))
    const relations = proposeCrossTableRelations(alignments, tables, emptyState([SKU, STORE]))
    expect(relations).toHaveLength(1)
    // 主语是 dimension 那张表所属的实体（sales 表的标识是 Store），宾语是
    // identifier 那张表的实体（SKU）
    expect(relations[0].relation_type).toBe('RELATES_TO')
    expect(relations[0].provenance).toBe('data')
    expect(relations[0].review).toBe('pending')
  })

  it('骨架里已有匹配主宾的约束时，用那条约束的关系名', () => {
    const state = emptyState([SKU, STORE])
    state.constraints = [
      { subject: 'Store', relation: 'SOLD_AT', object: 'SKU', provenance: 'skill', review: 'pending' },
    ]
    const tables = [skuTable, salesTable]
    const alignments = tables.map((t) => alignTable(t, [SKU, STORE]))
    expect(proposeCrossTableRelations(alignments, tables, state)[0].relation_type).toBe('SOLD_AT')
  })

  it('两张表里角色相同时不提关系（没有方向依据）', () => {
    const a: ScannedTable = { file: 'a.csv', roled: [column('JAN', 'identifier')] }
    const b: ScannedTable = { file: 'b.csv', roled: [column('JAN', 'identifier')] }
    const tables = [a, b]
    const alignments = tables.map((t) => alignTable(t, [SKU]))
    expect(proposeCrossTableRelations(alignments, tables, emptyState([SKU]))).toEqual([])
  })
})

describe('mergeAlignments', () => {
  const table: ScannedTable = {
    file: 'sku.xls',
    roled: [column('JAN', 'identifier'), column('现地语色', 'dimension'), column('md_no', 'identifier')],
  }

  it('写入 data_match 与未接住列，不动用户的审阅决定和改名', () => {
    const state = emptyState([{ ...SKU, review: 'accepted', display_name: '商品（改过名）' }])
    const merged = mergeAlignments(state, [alignTable(table, state.term_types)], [table])
    const sku = merged.term_types[0]
    expect(sku.review).toBe('accepted')
    expect(sku.display_name).toBe('商品（改过名）')
    expect(sku.data_match).toEqual({
      source_file: 'sku.xls',
      key_columns: ['JAN'],
      field_columns: { color: '现地语色' },
      matched_by: 'alias:jan',
    })
    expect(merged.unmatched_columns).toEqual({ 'sku.xls': ['md_no'] })
  })

  it('不修改传进来的 state（页面靠引用变化判断要不要重渲染）', () => {
    const state = emptyState([SKU])
    const before = JSON.stringify(state)
    mergeAlignments(state, [alignTable(table, state.term_types)], [table])
    expect(JSON.stringify(state)).toBe(before)
  })

  it('同一张表重新扫描时覆盖上一次的结果，不追加', () => {
    const state = emptyState([SKU])
    const once = mergeAlignments(state, [alignTable(table, state.term_types)], [table])
    const twice = mergeAlignments(once, [alignTable(table, once.term_types)], [table])
    expect(twice.unmatched_columns['sku.xls']).toEqual(['md_no'])
    expect(twice.term_types[0].data_match?.key_columns).toEqual(['JAN'])
  })

  it('提议的关系进 state，重复提议不会叠加', () => {
    const skuTable: ScannedTable = { file: 'sku.xls', roled: [column('JAN', 'identifier')] }
    const salesTable: ScannedTable = {
      file: 'sales.csv',
      roled: [column('JAN', 'dimension'), column('STORE_CD', 'identifier')],
    }
    const tables = [skuTable, salesTable]
    const state = emptyState([SKU, STORE])
    const alignments = tables.map((t) => alignTable(t, state.term_types))
    const once = mergeAlignments(state, alignments, tables)
    expect(once.relation_types).toHaveLength(1)
    const twice = mergeAlignments(once, alignments, tables)
    expect(twice.relation_types).toHaveLength(1)
    expect(twice.constraints).toHaveLength(1)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench/alignToSkeleton.test.ts --maxWorkers=2`
Expected: 找不到模块 `./alignToSkeleton`

- [ ] **Step 3: 实现**

```ts
// frontend/src/admin/modelingWorkbench/alignToSkeleton.ts
import type { RoledColumn } from '../guidedOntology/types'
import { normalizeAlias } from './aliases'
import type {
  WorkspaceConstraint,
  WorkspaceRelationType,
  WorkspaceState,
  WorkspaceTermType,
} from './types'

export interface ScannedTable {
  file: string
  roled: RoledColumn[]
}

export interface TableMatch {
  termValue: string
  keyColumns: string[]
  fieldColumns: Record<string, string>
  /** `alias:<原别名>`。界面要显示依据——用户看到"按别名 jan 对上"才判断得了对不对。 */
  matchedBy: string
}

export interface TableAlignment {
  file: string
  matches: TableMatch[]
  unmatchedColumns: string[]
}

/** 能被提升成实体类型的列角色。度量/自由文本/日期提上来会给每个金额建一个节点。 */
const PROMOTABLE_ROLES = new Set(['identifier', 'dimension'])

/**
 * 把一张表的列对齐到骨架。
 *
 * 命中规则是确定性的：别名归一化后与列名精确相等才算命中，命不中就命不中。
 * 不做前缀/包含/编辑距离——猜错一列会把错误数据写进图谱，而这一步失败的代价
 * 只是用户手动指一下列（spec 行为规格 §3）。
 */
export function alignTable(table: ScannedTable, termTypes: WorkspaceTermType[]): TableAlignment {
  const columns = table.roled.map((c) => c.stats.name)
  const roleOf = new Map(table.roled.map((c) => [c.stats.name, c.role]))
  const used = new Set<string>()
  const matches: TableMatch[] = []

  for (const term of termTypes) {
    if (term.review === 'rejected') continue
    let keyColumn: string | null = null
    let matchedBy = ''
    for (const alias of term.key_aliases) {
      const hit = columns.find((name) => normalizeAlias(name) === normalizeAlias(alias))
      if (hit !== undefined) {
        keyColumn = hit
        matchedBy = `alias:${alias}`
        break
      }
    }
    if (keyColumn === null) continue
    const fieldColumns: Record<string, string> = {}
    for (const [fieldName, aliases] of Object.entries(term.field_aliases)) {
      for (const alias of aliases) {
        const hit = columns.find(
          (name) => name !== keyColumn && normalizeAlias(name) === normalizeAlias(alias),
        )
        if (hit !== undefined) {
          fieldColumns[fieldName] = hit
          break
        }
      }
    }
    used.add(keyColumn)
    for (const column of Object.values(fieldColumns)) used.add(column)
    matches.push({ termValue: term.value, keyColumns: [keyColumn], fieldColumns, matchedBy })
  }

  return {
    file: table.file,
    matches,
    unmatchedColumns: columns.filter(
      (name) => !used.has(name) && PROMOTABLE_ROLES.has(roleOf.get(name) ?? ''),
    ),
  }
}

interface RoleSighting {
  file: string
  role: string
  /** 这张表里这一列所属的实体（也就是这张表的"主语候选"）。 */
  ownerTermValue: string
}

/**
 * 表之间的关系提议（spec 决策 13）。
 *
 * 判据只有一条：同一个实体类型的键列，在 A 表里是 identifier（每行一个，
 * 说明这张表讲的就是它）、在 B 表里是 dimension（重复出现，说明 B 表的行
 * 指向它）。那就提一条 B 的实体 → A 的实体的关系。
 *
 * **不做值级 join 分析**：那要把两张表的列值都读进来比对，代价是又一遍全表
 * 扫描，而结论并不更可靠——两列值域重合不代表有业务关系。
 */
export function proposeCrossTableRelations(
  alignments: TableAlignment[],
  tables: ScannedTable[],
  state: WorkspaceState,
): WorkspaceRelationType[] {
  return proposeCrossTable(alignments, tables, state).relations
}

export function proposeCrossTableConstraints(
  alignments: TableAlignment[],
  tables: ScannedTable[],
  state: WorkspaceState,
): WorkspaceConstraint[] {
  return proposeCrossTable(alignments, tables, state).constraints
}

function proposeCrossTable(
  alignments: TableAlignment[],
  tables: ScannedTable[],
  state: WorkspaceState,
): { relations: WorkspaceRelationType[]; constraints: WorkspaceConstraint[] } {
  const roleByFile = new Map(
    tables.map((t) => [t.file, new Map(t.roled.map((c) => [c.stats.name, String(c.role)]))]),
  )
  // 每张表的"主语"：这张表里角色是 identifier 的那个命中实体。
  const ownerOf = new Map<string, string>()
  for (const alignment of alignments) {
    const owner = alignment.matches.find(
      (m) => roleByFile.get(alignment.file)?.get(m.keyColumns[0]) === 'identifier',
    )
    if (owner) ownerOf.set(alignment.file, owner.termValue)
  }

  const sightings = new Map<string, RoleSighting[]>()
  for (const alignment of alignments) {
    for (const match of alignment.matches) {
      const role = roleByFile.get(alignment.file)?.get(match.keyColumns[0]) ?? ''
      const owner = ownerOf.get(alignment.file)
      if (!owner) continue
      const list = sightings.get(match.termValue) ?? []
      list.push({ file: alignment.file, role, ownerTermValue: owner })
      sightings.set(match.termValue, list)
    }
  }

  const relations: WorkspaceRelationType[] = []
  const constraints: WorkspaceConstraint[] = []
  const seen = new Set<string>()
  for (const [termValue, list] of sightings) {
    const identifierSide = list.find((s) => s.role === 'identifier')
    if (!identifierSide) continue
    for (const dimensionSide of list) {
      if (dimensionSide.role !== 'dimension') continue
      const subject = dimensionSide.ownerTermValue
      const object = identifierSide.ownerTermValue
      if (subject === object) continue
      const known = state.constraints.find((c) => c.subject === subject && c.object === object)
      const relationType = known?.relation ?? 'RELATES_TO'
      const key = `${subject}|${relationType}|${object}`
      if (seen.has(key)) continue
      seen.add(key)
      relations.push({
        relation_type: relationType,
        example_phrase: `${subject} 关联 ${object}`,
        description: `由 ${dimensionSide.file} 与 ${identifierSide.file} 里同名的 ${termValue} 列推出`,
        provenance: 'data',
        review: 'pending',
        clues: [],
        data_match: null,
      })
      constraints.push({
        subject,
        relation: relationType,
        object,
        provenance: 'data',
        review: 'pending',
      })
    }
  }
  return { relations, constraints }
}

/**
 * 把对齐结果合并进工作区状态。**纯函数**：返回新对象，不改入参。
 *
 * 合并而不是覆盖，是因为用户可能已经改过名、做过审阅决定（spec 里 discover
 * 不写库的理由就是这个）。这里只动 data_match 和 unmatched_columns 两处，
 * review / display_name / extra_fields 一律照抄。
 */
export function mergeAlignments(
  state: WorkspaceState,
  alignments: TableAlignment[],
  tables: ScannedTable[],
): WorkspaceState {
  const matchOf = new Map<string, { file: string; match: TableMatch }>()
  for (const alignment of alignments) {
    for (const match of alignment.matches) {
      // 一个实体在多张表里命中时保留第一张：ETL 映射里一个实体一条 entities
      // 条目，多张表要用户自己选，工作台在「数据」面板列出全部命中供改选。
      if (!matchOf.has(match.termValue)) matchOf.set(match.termValue, { file: alignment.file, match })
    }
  }

  const termTypes = state.term_types.map((term) => {
    const found = matchOf.get(term.value)
    if (!found) return term
    return {
      ...term,
      data_match: {
        source_file: found.file,
        key_columns: [...found.match.keyColumns],
        field_columns: { ...found.match.fieldColumns },
        matched_by: found.match.matchedBy,
      },
    }
  })

  const unmatched = { ...state.unmatched_columns }
  for (const alignment of alignments) {
    // 覆盖而不是并集：重新扫描同一张表就是要拿这一次的结论，并集会把用户
    // 上一次已经提升掉的列又列出来。
    unmatched[alignment.file] = [...alignment.unmatchedColumns]
  }

  const { relations, constraints } = proposeCrossTable(alignments, tables, state)
  const existingRelations = new Set(state.relation_types.map((r) => r.relation_type))
  const existingConstraints = new Set(
    state.constraints.map((c) => `${c.subject}|${c.relation}|${c.object}`),
  )

  const sources = [...state.sources]
  for (const table of tables) {
    if (!sources.some((s) => s.file === table.file)) sources.push({ file: table.file })
  }

  return {
    ...state,
    term_types: termTypes,
    relation_types: [
      ...state.relation_types,
      ...relations.filter((r) => !existingRelations.has(r.relation_type)),
    ],
    constraints: [
      ...state.constraints,
      ...constraints.filter(
        (c) => !existingConstraints.has(`${c.subject}|${c.relation}|${c.object}`),
      ),
    ],
    sources,
    unmatched_columns: unmatched,
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench/alignToSkeleton.test.ts --maxWorkers=2`
Expected: 全部 PASS

- [ ] **Step 5: 变异检查**

1. 把 `alignTable` 里的 `normalizeAlias(name) === normalizeAlias(alias)` 改成 `normalizeAlias(name).includes(normalizeAlias(alias))`：`命不中就是命不中` 必须变红。改回。
2. 把 `mergeAlignments` 里 `...term,` 展开去掉、直接构造新对象：`不动用户的审阅决定和改名` 必须变红。改回。
3. 把 `if (term.review === 'rejected') continue` 删掉：`已拒绝的实体类型不参与对齐` 必须变红。改回。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/alignToSkeleton.ts frontend/src/admin/modelingWorkbench/alignToSkeleton.test.ts
git commit -m "feat(admin): 数据发现——多表列名按别名对齐到骨架

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: 投影成草稿 payload 与 ETL 映射

**Files:**
- Create: `frontend/src/admin/modelingWorkbench/projectToDraft.ts`
- Create: `frontend/src/admin/modelingWorkbench/nextStep.ts`
- Test: `frontend/src/admin/modelingWorkbench/projectToDraft.test.ts`
- Test: `frontend/src/admin/modelingWorkbench/nextStep.test.ts`

**Interfaces:**
- Consumes: Task 7 的类型与 `parseOptionsOf`；`../schemaEtlConfigBuilder/buildConfigYaml` 的 `buildConfigYaml({tenantId, entities, relations, files})`；`../schemaEtlConfigBuilder/types` 的 `AddedFile` / `BuilderEntity` / `BuilderRelation`
- Produces:
  - `projectToDraftPayload(state: WorkspaceState): DraftPayload`
  - `projectToEtlYaml(state: WorkspaceState, tenantId: string): { yaml: string; fileName: string } | null`
  - `nextStepHint(workspace: ModelingWorkspace | null, diff: DraftDiff | null): string`

- [ ] **Step 1: 写失败的测试**

```ts
// frontend/src/admin/modelingWorkbench/projectToDraft.test.ts
import { describe, expect, it } from 'vitest'
import { projectToDraftPayload, projectToEtlYaml } from './projectToDraft'
import type { WorkspaceState, WorkspaceTermType } from './types'

function term(overrides: Partial<WorkspaceTermType> & { value: string }): WorkspaceTermType {
  return {
    display_name: overrides.value,
    provenance: 'skill',
    review: 'accepted',
    standard_name_value_type: 'string',
    extra_fields: [],
    key_aliases: [],
    field_aliases: {},
    clues: [],
    data_match: null,
    ...overrides,
  }
}

const SKU = term({
  value: 'SKU',
  extra_fields: [{ name: 'color', value_type: 'string', label: '颜色' }],
  data_match: {
    source_file: 'sku.xls',
    key_columns: ['JAN'],
    field_columns: { color: '现地语色' },
    matched_by: 'alias:jan',
  },
})

const state = (overrides: Partial<WorkspaceState> = {}): WorkspaceState => ({
  term_types: [SKU],
  relation_types: [
    {
      relation_type: 'SOLD_AT',
      example_phrase: '某商品在某门店有售',
      description: '',
      provenance: 'skill',
      review: 'accepted',
      clues: [],
      data_match: null,
    },
  ],
  constraints: [
    { subject: 'SKU', relation: 'SOLD_AT', object: 'SKU', provenance: 'skill', review: 'accepted' },
  ],
  sources: [{ file: 'sku.xls', header_row: 6, first_data_row: 7 }],
  unmatched_columns: {},
  questions: [],
  ...overrides,
})

describe('projectToDraftPayload', () => {
  it('只投影 accepted 的元素', () => {
    const payload = projectToDraftPayload(
      state({
        term_types: [SKU, term({ value: '拒过的', review: 'rejected' }), term({ value: '没审的', review: 'pending' })],
      }),
    )
    expect(payload.term_types.map((t) => t.value)).toEqual(['SKU'])
  })

  it('extra_fields 原样带过去（键名就是本体表的键名）', () => {
    expect(projectToDraftPayload(state()).term_types[0].extra_fields).toEqual([
      { name: 'color', value_type: 'string', label: '颜色' },
    ])
  })

  it('约束引用的类型没被 accepted 时整条丢掉', () => {
    // 留着的话 replace_draft 会抛 UnknownCategoryError，整次应用失败，而用户
    // 看到的只是一条"引用了未声明的实体类型"——他并不知道是哪次拒绝造成的
    const payload = projectToDraftPayload(
      state({
        constraints: [
          { subject: 'SKU', relation: 'SOLD_AT', object: '没审的', provenance: 'skill', review: 'accepted' },
        ],
      }),
    )
    expect(payload.constraints).toEqual([])
  })

  it('关系类型没被 accepted 时，引用它的约束也丢掉', () => {
    const payload = projectToDraftPayload(
      state({
        relation_types: [
          {
            relation_type: 'SOLD_AT',
            example_phrase: '',
            description: '',
            provenance: 'skill',
            review: 'pending',
            clues: [],
            data_match: null,
          },
        ],
      }),
    )
    expect(payload.relation_types).toEqual([])
    expect(payload.constraints).toEqual([])
  })
})

describe('projectToEtlYaml', () => {
  it('有 data_match 的实体进 entities，键列进 node_key_parts', () => {
    const built = projectToEtlYaml(state(), 't1')
    expect(built).not.toBeNull()
    expect(built!.yaml).toContain('term_type: "SKU"')
    expect(built!.yaml).toContain('source_file: "sku.xls"')
    expect(built!.yaml).toContain('- column: "JAN"')
    expect(built!.yaml).toContain('"color": "现地语色"')
    expect(built!.fileName).toBe('sku.xls')
  })

  it('解析选项写进 sources，否则 ETL 会用第 1 行当表头', () => {
    // MUJI 那张表表头在第 6 行。漏了 sources，后端读出来的列名全是空的，
    // 而且一条错误都不报——这正是 2026-09-15 那个分支解决的问题
    const built = projectToEtlYaml(state(), 't1')
    expect(built!.yaml).toContain('sources:')
    expect(built!.yaml).toContain('header_row: 6')
    expect(built!.yaml).toContain('first_data_row: 7')
  })

  it('没有任何 data_match 时返回 null（这次应用不带映射）', () => {
    const built = projectToEtlYaml(
      state({ term_types: [term({ value: 'SKU' })] }),
      't1',
    )
    expect(built).toBeNull()
  })

  it('未 accepted 的实体不进映射', () => {
    const built = projectToEtlYaml(
      state({ term_types: [{ ...SKU, review: 'pending' }] }),
      't1',
    )
    expect(built).toBeNull()
  })
})
```

```ts
// frontend/src/admin/modelingWorkbench/nextStep.test.ts
import { describe, expect, it } from 'vitest'
import { nextStepHint } from './nextStep'
import type { ModelingWorkspace, WorkspaceState } from './types'

const wrap = (state: Partial<WorkspaceState>): ModelingWorkspace => ({
  tenant_id: 't1',
  skill_name: 'consumer_retail',
  skill_version: '1',
  state: {
    term_types: [],
    relation_types: [],
    constraints: [],
    sources: [],
    unmatched_columns: {},
    questions: [],
    ...state,
  },
  updated_at: 'now',
  updated_by: 'alice',
})

const aTerm = (review: 'pending' | 'accepted' | 'rejected', dataMatch = false) => ({
  value: 'SKU',
  display_name: 'SKU',
  provenance: 'skill' as const,
  review,
  standard_name_value_type: 'string',
  extra_fields: [],
  key_aliases: [],
  field_aliases: {},
  clues: [],
  data_match: dataMatch
    ? { source_file: 'a.xls', key_columns: ['JAN'], field_columns: {}, matched_by: 'alias:jan' }
    : null,
})

describe('nextStepHint', () => {
  it('没有工作区时先建一个', () => {
    expect(nextStepHint(null, null)).toContain('选一个领域模板')
  })

  it('有未审的骨架元素时先审骨架', () => {
    expect(nextStepHint(wrap({ term_types: [aTerm('pending')] }), null)).toContain('审阅骨架')
  })

  it('骨架审完但没接数据时提示传表', () => {
    expect(nextStepHint(wrap({ term_types: [aTerm('accepted')] }), null)).toContain('上传数据表')
  })

  it('有 accepted 且接上数据时提示应用', () => {
    expect(nextStepHint(wrap({ term_types: [aTerm('accepted', true)] }), null)).toContain('应用到草稿')
  })

  it('diff 为空时说明已经应用过了', () => {
    const emptyDiff = {
      added_term_types: [],
      removed_term_types: [],
      changed_term_types: [],
      added_relation_types: [],
      removed_relation_types: [],
      added_constraints: [],
      removed_constraints: [],
    }
    expect(nextStepHint(wrap({ term_types: [aTerm('accepted', true)] }), emptyDiff)).toContain(
      '已经和草稿一致',
    )
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench --maxWorkers=2`
Expected: 找不到 `./projectToDraft` / `./nextStep`

- [ ] **Step 3: 实现投影**

```ts
// frontend/src/admin/modelingWorkbench/projectToDraft.ts
import { buildConfigYaml } from '../schemaEtlConfigBuilder/buildConfigYaml'
import type { AddedFile, BuilderEntity, BuilderRelation } from '../schemaEtlConfigBuilder/types'
import { parseOptionsOf } from './types'
import type { DraftPayload, WorkspaceState } from './types'

/**
 * 工作区 → `/draft/replace` 的 payload。
 *
 * 只取 review === 'accepted' 的元素：pending 是"还没看"，rejected 是"看过不要"，
 * 两者都不该进本体。拒掉的元素**留在工作区里**（spec 决策 2），这里只是不投影。
 *
 * 约束做引用过滤而不是原样带过去：replace_draft 会对引用未声明类型的约束抛
 * UnknownCategoryError，整次应用失败，而用户看到的只是一句"引用了未声明的
 * 实体类型"——他并不知道是自己哪一次拒绝造成的。
 */
export function projectToDraftPayload(state: WorkspaceState): DraftPayload {
  const terms = state.term_types.filter((t) => t.review === 'accepted')
  const relations = state.relation_types.filter((r) => r.review === 'accepted')
  const termValues = new Set(terms.map((t) => t.value))
  const relationNames = new Set(relations.map((r) => r.relation_type))
  return {
    term_types: terms.map((t) => ({
      value: t.value,
      extra_fields: t.extra_fields.map((f) => ({ ...f })),
      standard_name_value_type: t.standard_name_value_type,
    })),
    relation_types: relations.map((r) => ({
      relation_type: r.relation_type,
      example_phrase: r.example_phrase,
      description: r.description,
      allow_chain_query: true,
    })),
    constraints: state.constraints
      .filter(
        (c) =>
          c.review === 'accepted' &&
          termValues.has(c.subject) &&
          termValues.has(c.object) &&
          relationNames.has(c.relation),
      )
      .map((c) => ({
        subject_term_type: c.subject,
        relation_type: c.relation,
        object_term_type: c.object,
      })),
  }
}

/**
 * 工作区 → ETL 映射 YAML。没有任何实体接上了数据时返回 null——那时这次应用
 * 不带映射，replace_draft 会保留已有的那份（见它的 docstring）。
 *
 * 复用 buildConfigYaml 而不是自己拼 YAML：它已经处理了引号转义、sources 段、
 * allocated_code 这些细节，而且表格导入页用的就是它，两条路径产出的 YAML
 * 形状一致，后端只需要认一种。
 *
 * 它要一个 AddedFile（含真正的 File 对象）才能拿到文件名。工作台这里只有
 * 文件名——用户可能是上一次会话传的表，File 对象早没了。所以构造一个空的
 * `new File([], name)`：buildConfigYaml 只读 `file.name`，不读内容。
 */
export function projectToEtlYaml(
  state: WorkspaceState,
  tenantId: string,
): { yaml: string; fileName: string } | null {
  const matched = state.term_types.filter((t) => t.review === 'accepted' && t.data_match !== null)
  if (matched.length === 0) return null

  const fileNames = [...new Set(matched.map((t) => t.data_match!.source_file))]
  const files: AddedFile[] = fileNames.map((name) => ({
    id: name,
    file: new File([], name),
    columns: [],
    parseOptions: parseOptionsOf(state.sources.find((s) => s.file === name)),
  }))

  const entities: BuilderEntity[] = matched.map((term) => {
    const match = term.data_match!
    return {
      id: term.value,
      termType: term.value,
      fileId: match.source_file,
      // 标准名暂用键列：工作台没有"显示名取哪一列"的判断依据，而键列一定
      // 存在。用户可以在表格导入页改。
      standardNameColumn: match.key_columns[0],
      nodeKeyParts: match.key_columns.map((column) => ({ kind: 'column' as const, column })),
      fieldMappings: { ...match.field_columns },
    }
  })

  const relations: BuilderRelation[] = state.constraints
    .filter(
      (c) =>
        c.review === 'accepted' &&
        matched.some((t) => t.value === c.subject) &&
        matched.some((t) => t.value === c.object),
    )
    .map((c) => ({
      id: `${c.subject}-${c.relation}-${c.object}`,
      // 关系从主语所在的那张表出：那张表的每一行都指向一个宾语。
      fileId: matched.find((t) => t.value === c.subject)!.data_match!.source_file,
      subjectTermType: c.subject,
      relationType: c.relation,
      objectTermType: c.object,
    }))

  return {
    yaml: buildConfigYaml({ tenantId, entities, relations, files }),
    fileName: fileNames[0],
  }
}
```

- [ ] **Step 4: 实现下一步建议**

```ts
// frontend/src/admin/modelingWorkbench/nextStep.ts
import type { DraftDiff, ModelingWorkspace } from './types'

/**
 * 工作台顶部那一行"下一步建议"。
 *
 * 工作台是工作台不是向导（spec 决策 12）——四个面板随时都能点。但"随时都能点"
 * 对第一次来的人等于没有入口，所以用一句话指出当下最该做的事，同时不挡任何
 * 别的操作。
 */
export function nextStepHint(
  workspace: ModelingWorkspace | null,
  diff: DraftDiff | null,
): string {
  if (workspace === null) return '先选一个领域模板起步，或者空白起步。'
  const terms = workspace.state.term_types
  const relations = workspace.state.relation_types
  if (terms.length === 0 && relations.length === 0) {
    return '骨架还是空的——在「骨架」面板手工新增，或者传一张表让数据告诉你有什么。'
  }
  if (terms.some((t) => t.review === 'pending') || relations.some((r) => r.review === 'pending')) {
    return '先去「骨架」面板审阅骨架：每一条接受还是拒绝。拒掉的会留在工作区里，随时能翻回来。'
  }
  const accepted = terms.filter((t) => t.review === 'accepted')
  if (accepted.length === 0) return '骨架里一条都没接受，先去「骨架」面板至少接受一个实体类型。'
  if (accepted.every((t) => t.data_match === null)) {
    return '骨架审完了，去「数据」面板上传数据表，看看哪些概念真有数据。'
  }
  if (diff !== null && Object.values(diff).every((items) => items.length === 0)) {
    return '工作区已经和草稿一致，没有要应用的改动。'
  }
  return '有接受且接上数据的元素了，去「应用」面板看差异，确认后写入草稿。'
}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench --maxWorkers=2`
Expected: 全部 PASS

- [ ] **Step 6: 变异检查**

1. 把 `projectToEtlYaml` 里 `parseOptions: parseOptionsOf(...)` 改成 `parseOptions: {}`：`解析选项写进 sources` 必须变红。改回。
2. 把 `projectToDraftPayload` 的约束过滤里 `termValues.has(c.object) &&` 删掉：`约束引用的类型没被 accepted 时整条丢掉` 必须变红。改回。

- [ ] **Step 7: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/projectToDraft.ts frontend/src/admin/modelingWorkbench/nextStep.ts frontend/src/admin/modelingWorkbench/projectToDraft.test.ts frontend/src/admin/modelingWorkbench/nextStep.test.ts
git commit -m "feat(admin): 工作区投影成本体草稿与 ETL 映射

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: 工作台页面与四个面板

**Files:**
- Create: `frontend/src/admin/modelingWorkbench/ui.ts`（共用样式常量）
- Create: `frontend/src/admin/modelingWorkbench/panels/StartPanel.tsx`
- Create: `frontend/src/admin/modelingWorkbench/panels/SkeletonPanel.tsx`
- Create: `frontend/src/admin/modelingWorkbench/panels/DataPanel.tsx`
- Create: `frontend/src/admin/modelingWorkbench/panels/UngroundedPanel.tsx`
- Create: `frontend/src/admin/modelingWorkbench/panels/ApplyPanel.tsx`
- Create: `frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx`
- Test: `frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx`

**Interfaces:**
- Consumes: Task 7 的全部导出；Task 8 的 `alignTable` / `mergeAlignments` / `ScannedTable`；Task 9 的 `projectToDraftPayload` / `projectToEtlYaml` / `nextStepHint`；既有 `../guidedOntology/columnStats` 的 `scanTableFile`、`../guidedOntology/columnRoles` 的 `assignRoles`、`../schemaEtlConfigBuilder/sourceParser` 的 `readSourceHeader` / `listSheetNames`、`../schemaEtlConfigBuilder/parseSettingsDraft` 的 `draftFromOptions` / `optionsFromDraft`、`../adminApi` 的 `adminFetch` / `extractErrorDetail`、`../useAdminAuth`、`../TenantContext`、`../ConfirmContext`、`../ToastContext`、`../../adminRoutes` 的 `PAGE_TITLES`
- Produces: `export function ModelingWorkbenchPage()`（Task 11 的路由指向它）

- [ ] **Step 1: 写失败的测试**

```tsx
// frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../../App'
import { SkinProvider } from '../SkinContext'
import { ConfirmProvider } from '../ConfirmContext'
import { ToastProvider } from '../ToastContext'
import { ADMIN_ROUTES } from '../../adminRoutes'
import { resetAdminSession } from '../useAdminAuth'

/**
 * 工作台页面的行为测试。跟 guidedPage.test.tsx 同一套搭台方式（whoami 打桩、
 * 整个 App 挂在 MemoryRouter 里走真实路由），因为要验证的正是"这条路由现在
 * 渲染的是工作台"。
 */
let signedInRole: 'admin' | 'member' | null = null
let workspace: unknown = null
const saved: unknown[] = []

const SKILLS = [
  {
    name: 'consumer_retail',
    version: '1',
    display_name: '消费品零售',
    description: '面向品牌方/零售商的骨架',
    term_types: [
      {
        value: 'SKU',
        display_name: '商品',
        standard_name_value_type: 'string',
        extra_fields: [{ name: 'color', value_type: 'string', display_name: '颜色' }],
        key_aliases: ['jan'],
        field_aliases: { color: ['现地语色'] },
      },
    ],
    relation_types: [{ relation_type: 'SOLD_AT', example_phrase: '', description: '' }],
    constraints: [],
    questions: [],
    match_hint: '',
  },
]

function workspaceWith(termReview: 'pending' | 'accepted') {
  return {
    tenant_id: 'demo',
    skill_name: 'consumer_retail',
    skill_version: '1',
    updated_at: '2026-09-16T10:00:00',
    updated_by: 'alice',
    state: {
      term_types: [
        {
          value: 'SKU',
          display_name: '商品',
          provenance: 'skill',
          review: termReview,
          standard_name_value_type: 'string',
          extra_fields: [],
          key_aliases: ['jan'],
          field_aliases: {},
          clues: [],
          data_match: null,
        },
      ],
      relation_types: [],
      constraints: [],
      sources: [],
      unmatched_columns: {},
      questions: [],
    },
  }
}

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }))

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/auth/whoami')) {
        if (signedInRole === null) return json({ detail: '未登录' }, 401)
        return json({
          username: 'alice',
          role: signedInRole,
          tenant_id: signedInRole === 'admin' ? null : 'demo',
          current_tenant_id: 'demo',
        })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      if (url.includes('/modeling-workspace/skills')) return json({ skills: SKILLS })
      if (url.includes('/modeling-workspace/grounding')) {
        return json({
          status: null,
          grounded_term_types: [],
          grounded_relation_types: [],
          source_files: [],
          parse_error: null,
        })
      }
      if (url.includes('/modeling-workspace/apply-preview')) {
        return json({
          added_term_types: ['SKU'],
          removed_term_types: ['手工加的'],
          changed_term_types: [],
          added_relation_types: [],
          removed_relation_types: [],
          added_constraints: [],
          removed_constraints: [],
        })
      }
      if (url.includes('/modeling-workspace')) {
        if (method === 'POST') {
          workspace = workspaceWith('pending')
          return json({ workspace })
        }
        if (method === 'PUT') {
          const body = JSON.parse(String(init?.body))
          saved.push(body)
          workspace = { ...workspaceWith('pending'), state: body.state, updated_at: 'later' }
          return json({ workspace })
        }
        return json({ workspace })
      }
      if (url.includes('/draft/replace')) return json({ replaced: true })
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  signedInRole = null
  workspace = null
  saved.length = 0
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  stubApi()
})

function renderWorkbench() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.guidedOntology]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

describe('建模工作台', () => {
  it('没有工作区时列出内置领域模板', async () => {
    signedInRole = 'member'
    renderWorkbench()
    expect(await screen.findByText('消费品零售')).toBeInTheDocument()
    expect(screen.getByText(/1 个实体类型/)).toBeInTheDocument()
    // 下一步建议：第一次来的人要知道从哪开始
    expect(screen.getByText(/先选一个领域模板起步/)).toBeInTheDocument()
  })

  it('member 也能用——工作台不是管理员专属', async () => {
    signedInRole = 'member'
    renderWorkbench()
    expect(await screen.findByText('消费品零售')).toBeInTheDocument()
    expect(screen.queryByText(/只有管理员/)).not.toBeInTheDocument()
  })

  it('选模板起步后进入骨架面板，元素标着来源和待审', async () => {
    signedInRole = 'member'
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /用这个模板起步/ }))
    expect(await screen.findByText('SKU')).toBeInTheDocument()
    expect(screen.getByText('来自模板')).toBeInTheDocument()
    expect(screen.getByText(/审阅骨架/)).toBeInTheDocument()
  })

  it('接受一个实体类型会把整份工作区存回去', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('pending')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: '接受 SKU' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as { state: { term_types: { review: string }[] }; updated_at: string }
    expect(body.state.term_types[0].review).toBe('accepted')
    // 乐观锁：带上手上这份的时间戳
    expect(body.updated_at).toBe('2026-09-16T10:00:00')
  })

  it('拒绝的元素留在界面上，不消失', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('pending')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: '拒绝 SKU' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(screen.getByText('SKU')).toBeInTheDocument()
  })

  it('未落地清单把没有数据支撑的元素聚到一起', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /未落地/ }))
    expect(await screen.findByText(/SKU/)).toBeInTheDocument()
    expect(screen.getByText(/还没有数据支撑/)).toBeInTheDocument()
  })

  it('应用面板先显示差异，删除项单独醒目列出', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    await userEvent.click(await screen.findByRole('button', { name: /看看会改什么/ }))
    expect(await screen.findByText(/会新增：SKU/)).toBeInTheDocument()
    expect(screen.getByText(/会删掉：手工加的/)).toBeInTheDocument()
    // 没看过 diff 之前不给写入，避免静默删掉用户在本体结构页手工加的东西
    expect(screen.getByRole('button', { name: /写入草稿/ })).toBeEnabled()
  })

  it('写入草稿走既有的 draft/replace', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    await userEvent.click(await screen.findByRole('button', { name: /看看会改什么/ }))
    await userEvent.click(await screen.findByRole('button', { name: /写入草稿/ }))
    await waitFor(() => {
      const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
      expect(calls.some(([url]) => String(url).includes('/draft/replace'))).toBe(true)
    })
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench/workbenchPage.test.tsx --maxWorkers=2`
Expected: 失败——路由还渲染着旧的引导页，找不到"消费品零售"

- [ ] **Step 3: 写共用样式常量**

```ts
// frontend/src/admin/modelingWorkbench/ui.ts
// 跟 GuidedOntologyPage 用的是同一组类名——工作台替换的是那一页，样式不该
// 在替换过程中发生无关的变化。
export const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export const primaryButtonClass = `min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

export const secondaryButtonClass = `inline-flex min-h-[44px] cursor-pointer items-center rounded-control border border-subtle bg-paper px-4 py-2 text-sm font-bold text-ink transition hover:bg-interactive-hover active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

export const panelClass = 'rounded-panel border border-subtle bg-paper p-4'

export const tagClass = 'rounded-control border border-subtle px-2 py-0.5 text-xs text-ink-soft'
```

- [ ] **Step 4: 写起步面板**

```tsx
// frontend/src/admin/modelingWorkbench/panels/StartPanel.tsx
import type { SkillSummary } from '../types'
import { panelClass, primaryButtonClass, secondaryButtonClass } from '../ui'

/**
 * 还没有工作区时的首屏：选一个内置领域模板，或者空白起步。
 *
 * 每个模板要报出"有多少实体类型/关系类型"——只给名字和一句描述的话，用户
 * 没有任何依据在两个模板之间选。
 */
export function StartPanel(props: {
  skills: SkillSummary[]
  busy: boolean
  onStart: (skillName: string | null) => void
}) {
  return (
    <div className="flex flex-col gap-4">
      {props.skills.length === 0 && (
        <p className="text-sm text-ink-soft">还没有内置领域模板，可以先空白起步。</p>
      )}
      {props.skills.map((skill) => (
        <div key={skill.name} className={`${panelClass} flex flex-col gap-2`}>
          <div className="flex items-baseline gap-2">
            <h2 className="font-mono text-base font-semibold text-ink">{skill.display_name}</h2>
            <span className="text-xs text-ink-soft">v{skill.version}</span>
          </div>
          <p className="text-sm text-ink-soft">{skill.description}</p>
          <p className="text-sm text-ink-soft">
            {`包含 ${skill.term_types.length} 个实体类型、${skill.relation_types.length} 个关系类型、${skill.constraints.length} 条约束`}
          </p>
          <button
            type="button"
            className={primaryButtonClass}
            disabled={props.busy}
            onClick={() => props.onStart(skill.name)}
          >
            用这个模板起步
          </button>
        </div>
      ))}
      <div className={`${panelClass} flex flex-col gap-2`}>
        <h2 className="font-mono text-base font-semibold text-ink">空白起步</h2>
        <p className="text-sm text-ink-soft">
          没有合适的模板时从零开始：先传数据表，让列名告诉你这里有哪些概念。
        </p>
        <button
          type="button"
          className={secondaryButtonClass}
          disabled={props.busy}
          onClick={() => props.onStart(null)}
        >
          空白起步
        </button>
      </div>
    </div>
  )
}
```

- [ ] **Step 5: 写骨架面板**

```tsx
// frontend/src/admin/modelingWorkbench/panels/SkeletonPanel.tsx
import type { Grounding, WorkspaceState } from '../types'
import { panelClass, secondaryButtonClass, tagClass } from '../ui'

const PROVENANCE_LABEL: Record<string, string> = {
  skill: '来自模板',
  data: '来自数据',
  manual: '手工新增',
}

const REVIEW_LABEL: Record<string, string> = {
  pending: '待审',
  accepted: '已接受',
  rejected: '已拒绝',
}

/**
 * 骨架审阅：每个元素一行，标出来源、落地与否、审阅状态。
 *
 * 拒绝的元素折叠在下面而不是消失（spec 行为规格 §2）：用户回头要能看到
 * "这个我拒过"，否则同一个概念会被反复提议、反复拒绝。
 */
export function SkeletonPanel(props: {
  state: WorkspaceState
  grounding: Grounding | null
  onReview: (kind: 'term' | 'relation', key: string, review: 'accepted' | 'rejected') => void
}) {
  const groundedTerms = new Set(props.grounding?.grounded_term_types ?? [])
  const groundedRelations = new Set(props.grounding?.grounded_relation_types ?? [])
  const terms = props.state.term_types
  const relations = props.state.relation_types

  return (
    <div className="flex flex-col gap-4">
      <section className={`${panelClass} flex flex-col gap-3`}>
        <h2 className="font-mono text-base font-semibold text-ink">实体类型</h2>
        {terms.length === 0 && <p className="text-sm text-ink-soft">还没有实体类型。</p>}
        {terms.map((term) => (
          <div key={term.value} className="flex flex-wrap items-center gap-2 border-b border-subtle pb-2">
            <span className="font-mono text-sm font-semibold text-ink">{term.value}</span>
            <span className="text-sm text-ink-soft">{term.display_name}</span>
            <span className={tagClass}>{PROVENANCE_LABEL[term.provenance]}</span>
            <span className={tagClass}>{REVIEW_LABEL[term.review]}</span>
            <span className={tagClass}>{groundedTerms.has(term.value) ? '已落地' : '未落地'}</span>
            {term.data_match && (
              <span className={tagClass}>
                {`${term.data_match.source_file} · ${term.data_match.key_columns.join('/')} · ${term.data_match.matched_by}`}
              </span>
            )}
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={term.review === 'accepted'}
              onClick={() => props.onReview('term', term.value, 'accepted')}
            >
              {`接受 ${term.value}`}
            </button>
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={term.review === 'rejected'}
              onClick={() => props.onReview('term', term.value, 'rejected')}
            >
              {`拒绝 ${term.value}`}
            </button>
          </div>
        ))}
      </section>

      <section className={`${panelClass} flex flex-col gap-3`}>
        <h2 className="font-mono text-base font-semibold text-ink">关系类型</h2>
        {relations.length === 0 && <p className="text-sm text-ink-soft">还没有关系类型。</p>}
        {relations.map((relation) => (
          <div
            key={relation.relation_type}
            className="flex flex-wrap items-center gap-2 border-b border-subtle pb-2"
          >
            <span className="font-mono text-sm font-semibold text-ink">{relation.relation_type}</span>
            <span className="text-sm text-ink-soft">{relation.example_phrase}</span>
            <span className={tagClass}>{PROVENANCE_LABEL[relation.provenance]}</span>
            <span className={tagClass}>{REVIEW_LABEL[relation.review]}</span>
            <span className={tagClass}>
              {groundedRelations.has(relation.relation_type) ? '已落地' : '未落地'}
            </span>
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={relation.review === 'accepted'}
              onClick={() => props.onReview('relation', relation.relation_type, 'accepted')}
            >
              {`接受 ${relation.relation_type}`}
            </button>
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={relation.review === 'rejected'}
              onClick={() => props.onReview('relation', relation.relation_type, 'rejected')}
            >
              {`拒绝 ${relation.relation_type}`}
            </button>
          </div>
        ))}
      </section>
    </div>
  )
}
```

- [ ] **Step 6: 写数据面板**

```tsx
// frontend/src/admin/modelingWorkbench/panels/DataPanel.tsx
import { useState, type ChangeEvent } from 'react'
import { assignRoles } from '../../guidedOntology/columnRoles'
import { scanTableFile } from '../../guidedOntology/columnStats'
import { draftFromOptions, optionsFromDraft, type ParseDraft } from '../../schemaEtlConfigBuilder/parseSettingsDraft'
import { listSheetNames } from '../../schemaEtlConfigBuilder/sourceParser'
import { alignTable, mergeAlignments, type ScannedTable } from '../alignToSkeleton'
import { parseOptionsOf } from '../types'
import type { WorkspaceState } from '../types'
import { panelClass, primaryButtonClass, secondaryButtonClass, tagClass } from '../ui'

/**
 * 数据发现：传表 → 扫列 → 按别名对齐 → 把结果合并进工作区。
 *
 * 解析选项（工作表、表头行）必须在这里就能填：MUJI 那张表表头在第 6 行，
 * 按缺省第 1 行读出来的列名全是空的，对齐一条都命不中，而界面上看不出原因。
 */
export function DataPanel(props: {
  state: WorkspaceState
  busy: boolean
  onMerged: (next: WorkspaceState) => void
  onPromote: (file: string, column: string) => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [sheetNames, setSheetNames] = useState<string[]>([])
  const [draft, setDraft] = useState<ParseDraft>(draftFromOptions({}))
  const [error, setError] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)

  const handleFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const picked = event.target.files?.[0]
    if (!picked) return
    setError(null)
    setFile(picked)
    setDraft(draftFromOptions(parseOptionsOf(props.state.sources.find((s) => s.file === picked.name))))
    try {
      setSheetNames(await listSheetNames(picked))
    } catch {
      // CSV 没有工作表概念，listSheetNames 抛错是正常的——不显示工作表选择即可
      setSheetNames([])
    }
  }

  const handleScan = async () => {
    if (!file) return
    const parsed = optionsFromDraft(draft)
    if (!parsed.ok) {
      setError(parsed.message)
      return
    }
    setScanning(true)
    setError(null)
    try {
      const stats = await scanTableFile(file, parsed.options)
      const table: ScannedTable = { file: file.name, roled: assignRoles(stats) }
      const withSource: WorkspaceState = {
        ...props.state,
        sources: [
          ...props.state.sources.filter((s) => s.file !== file.name),
          {
            file: file.name,
            sheet: parsed.options.sheet ?? null,
            header_row: parsed.options.headerRow,
            first_data_row: parsed.options.firstDataRow,
          },
        ],
      }
      props.onMerged(
        mergeAlignments(withSource, [alignTable(table, withSource.term_types)], [table]),
      )
    } catch (err) {
      // 扫描失败必须说清原因（比如 xlsx 超过体积上限），不能静静停住
      setError(err instanceof Error ? err.message : '扫描失败')
    } finally {
      setScanning(false)
    }
  }

  const matched = props.state.term_types.filter((t) => t.data_match !== null)
  const unmatchedEntries = Object.entries(props.state.unmatched_columns)

  return (
    <div className="flex flex-col gap-4">
      <section className={`${panelClass} flex flex-col gap-3`}>
        <h2 className="font-mono text-base font-semibold text-ink">上传数据表</h2>
        <input
          type="file"
          accept=".csv,.tsv,.txt,.xlsx,.xls"
          aria-label="选择数据表"
          onChange={handleFile}
          className="text-sm text-ink"
        />
        {file && (
          <div className="flex flex-wrap items-end gap-3">
            {sheetNames.length > 0 && (
              <label className="flex flex-col gap-1 text-sm text-ink">
                工作表
                <select
                  className="rounded-control border border-subtle bg-paper px-2 py-1"
                  value={String(draft.sheet ?? '')}
                  onChange={(e) =>
                    setDraft({ ...draft, sheet: e.target.value === '' ? undefined : e.target.value })
                  }
                >
                  <option value="">第一个</option>
                  {sheetNames.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="flex flex-col gap-1 text-sm text-ink">
              表头行
              <input
                className="w-24 rounded-control border border-subtle bg-paper px-2 py-1"
                value={draft.headerRow}
                onChange={(e) => setDraft({ ...draft, headerRow: e.target.value })}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm text-ink">
              首数据行
              <input
                className="w-24 rounded-control border border-subtle bg-paper px-2 py-1"
                placeholder="紧跟表头"
                value={draft.firstDataRow}
                onChange={(e) => setDraft({ ...draft, firstDataRow: e.target.value })}
              />
            </label>
            <button
              type="button"
              className={primaryButtonClass}
              disabled={scanning || props.busy}
              onClick={handleScan}
            >
              {scanning ? '扫描中…' : '扫描并对齐'}
            </button>
          </div>
        )}
        {error && (
          <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
            {error}
          </p>
        )}
      </section>

      <section className={`${panelClass} flex flex-col gap-2`}>
        <h2 className="font-mono text-base font-semibold text-ink">对上的概念</h2>
        {matched.length === 0 && <p className="text-sm text-ink-soft">还没有概念对上数据。</p>}
        {matched.map((term) => (
          <p key={term.value} className="text-sm text-ink">
            <span className="font-mono font-semibold">{term.value}</span>
            <span className={`ml-2 ${tagClass}`}>
              {`${term.data_match!.source_file} · 键列 ${term.data_match!.key_columns.join('/')} · ${term.data_match!.matched_by}`}
            </span>
          </p>
        ))}
      </section>

      <section className={`${panelClass} flex flex-col gap-2`}>
        <h2 className="font-mono text-base font-semibold text-ink">数据里还有这些，骨架里没有</h2>
        {unmatchedEntries.length === 0 && <p className="text-sm text-ink-soft">没有剩下的列。</p>}
        {unmatchedEntries.map(([fileName, columns]) => (
          <div key={fileName} className="flex flex-col gap-1">
            <p className="text-sm font-bold text-ink">{fileName}</p>
            <div className="flex flex-wrap gap-2">
              {columns.map((column) => (
                <button
                  key={column}
                  type="button"
                  className={secondaryButtonClass}
                  disabled={props.busy}
                  onClick={() => props.onPromote(fileName, column)}
                >
                  {`把 ${column} 提升为实体类型`}
                </button>
              ))}
            </div>
          </div>
        ))}
      </section>
    </div>
  )
}
```

- [ ] **Step 7: 写未落地清单与应用面板**

```tsx
// frontend/src/admin/modelingWorkbench/panels/UngroundedPanel.tsx
import type { Grounding, WorkspaceState } from '../types'
import { panelClass, tagClass } from '../ui'

/**
 * 未落地清单：接受了、但 ETL 映射里没有任何一条指向它的元素。
 *
 * 这就是"下一批该接什么数据"的待办（spec 行为规格 §4）。不阻塞任何操作——
 * 未落地是一个合法的中间状态，企业的数据本来就是分批接进来的。
 */
export function UngroundedPanel(props: { state: WorkspaceState; grounding: Grounding | null }) {
  const groundedTerms = new Set(props.grounding?.grounded_term_types ?? [])
  const groundedRelations = new Set(props.grounding?.grounded_relation_types ?? [])
  const terms = props.state.term_types.filter(
    (t) => t.review !== 'rejected' && !groundedTerms.has(t.value),
  )
  const relations = props.state.relation_types.filter(
    (r) => r.review !== 'rejected' && !groundedRelations.has(r.relation_type),
  )

  return (
    <section className={`${panelClass} flex flex-col gap-3`}>
      <h2 className="font-mono text-base font-semibold text-ink">未落地清单</h2>
      {props.grounding?.parse_error && (
        <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          {`存下来的 ETL 映射解析失败，落地状态算不出来：${props.grounding.parse_error}`}
        </p>
      )}
      <p className="text-sm text-ink-soft">
        下面这些还没有数据支撑——ETL 映射里没有任何一条指向它们。这不是错误，是下一批该接的数据。
      </p>
      {terms.length === 0 && relations.length === 0 && (
        <p className="text-sm text-ink-soft">全都有数据支撑了。</p>
      )}
      {terms.map((term) => (
        <p key={term.value} className="text-sm text-ink">
          <span className="font-mono font-semibold">{term.value}</span>
          <span className={`ml-2 ${tagClass}`}>实体类型</span>
          {term.clues.map((clue, index) => (
            <span key={index} className={`ml-2 ${tagClass}`}>
              {`旁证：${clue.note}`}
            </span>
          ))}
        </p>
      ))}
      {relations.map((relation) => (
        <p key={relation.relation_type} className="text-sm text-ink">
          <span className="font-mono font-semibold">{relation.relation_type}</span>
          <span className={`ml-2 ${tagClass}`}>关系类型</span>
        </p>
      ))}
    </section>
  )
}
```

```tsx
// frontend/src/admin/modelingWorkbench/panels/ApplyPanel.tsx
import type { DraftDiff } from '../types'
import { panelClass, primaryButtonClass, secondaryButtonClass } from '../ui'

/**
 * 应用：先看 diff，再写草稿。
 *
 * 删除项单独醒目列出（spec 决策 10）：replace_draft 是整份替换，草稿里用户
 * 在「本体结构」页手工加的东西会被这次替换删掉，不说出来就是静默删数据。
 */
export function ApplyPanel(props: {
  diff: DraftDiff | null
  busy: boolean
  onPreview: () => void
  onApply: () => void
  onExport: () => void
}) {
  return (
    <section className={`${panelClass} flex flex-col gap-3`}>
      <h2 className="font-mono text-base font-semibold text-ink">应用到本体草稿</h2>
      <div className="flex flex-wrap gap-2">
        <button type="button" className={secondaryButtonClass} disabled={props.busy} onClick={props.onPreview}>
          看看会改什么
        </button>
        <button type="button" className={primaryButtonClass} disabled={props.busy} onClick={props.onApply}>
          写入草稿
        </button>
        <button type="button" className={secondaryButtonClass} disabled={props.busy} onClick={props.onExport}>
          导出为领域模板
        </button>
      </div>
      {props.diff === null ? (
        <p className="text-sm text-ink-soft">
          先点「看看会改什么」：整份替换会把草稿换成工作区里已接受的那些，草稿里别处加的东西会被删掉。
        </p>
      ) : (
        <div className="flex flex-col gap-1 text-sm text-ink">
          {props.diff.removed_term_types.length > 0 && (
            <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
              {`会删掉：${props.diff.removed_term_types.join('、')}（多半是你在「本体结构」页手工加的）`}
            </p>
          )}
          {props.diff.removed_relation_types.length > 0 && (
            <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
              {`会删掉关系类型：${props.diff.removed_relation_types.join('、')}`}
            </p>
          )}
          {props.diff.added_term_types.length > 0 && (
            <p>{`会新增：${props.diff.added_term_types.join('、')}`}</p>
          )}
          {props.diff.added_relation_types.length > 0 && (
            <p>{`会新增关系类型：${props.diff.added_relation_types.join('、')}`}</p>
          )}
          {props.diff.changed_term_types.map((line) => (
            <p key={line}>{`会改：${line}`}</p>
          ))}
          {props.diff.added_constraints.length > 0 && (
            <p>{`会新增约束：${props.diff.added_constraints.join('、')}`}</p>
          )}
          {props.diff.removed_constraints.length > 0 && (
            <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
              {`会删掉约束：${props.diff.removed_constraints.join('、')}`}
            </p>
          )}
          {Object.values(props.diff).every((items) => items.length === 0) && (
            <p className="text-ink-soft">没有差异，草稿已经是这个样子。</p>
          )}
        </div>
      )}
    </section>
  )
}
```

- [ ] **Step 8: 写页面**

```tsx
// frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx
import { useCallback, useEffect, useState } from 'react'
import { PAGE_TITLES } from '../../adminRoutes'
import { adminFetch, extractErrorDetail } from '../adminApi'
import { useAdminAuth } from '../useAdminAuth'
import { useAdminTenant } from '../TenantContext'
import { useConfirm } from '../ConfirmContext'
import { useToast } from '../ToastContext'
import { nextStepHint } from './nextStep'
import { projectToDraftPayload, projectToEtlYaml } from './projectToDraft'
import {
  WorkspaceConflictError,
  createWorkspace,
  exportSkill,
  fetchGrounding,
  fetchSkills,
  fetchWorkspace,
  previewApply,
  saveWorkspace,
} from './workspaceApi'
import type { DraftDiff, Grounding, ModelingWorkspace, SkillSummary, WorkspaceState } from './types'
import { ApplyPanel } from './panels/ApplyPanel'
import { DataPanel } from './panels/DataPanel'
import { SkeletonPanel } from './panels/SkeletonPanel'
import { StartPanel } from './panels/StartPanel'
import { UngroundedPanel } from './panels/UngroundedPanel'
import { panelClass, secondaryButtonClass } from './ui'

type Tab = 'skeleton' | 'data' | 'ungrounded' | 'apply'

const TAB_LABELS: { id: Tab; label: string }[] = [
  { id: 'skeleton', label: '骨架' },
  { id: 'data', label: '数据' },
  { id: 'ungrounded', label: '未落地' },
  { id: 'apply', label: '应用' },
]

/**
 * 建模工作台。替换原来的「引导建模」页，路由不变。
 *
 * 是工作台不是向导（spec 决策 12）：四个面板随时可切，因为真实的建模不是
 * 一条直线——用户会在"传了一张表、发现骨架少一个概念、回去加一条、再传下
 * 一张表"之间来回走。顶部一行"下一步建议"负责回答"现在最该做什么"。
 *
 * 每次改动立刻整份存回后端（PUT 带 updated_at 乐观锁），不做本地草稿：
 * 工作区是长期存在的，用户关掉页面一周后回来必须看到自己上次做到哪。
 */
export function ModelingWorkbenchPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const confirm = useConfirm()
  const showToast = useToast()
  const [workspace, setWorkspace] = useState<ModelingWorkspace | null>(null)
  const [skills, setSkills] = useState<SkillSummary[]>([])
  const [grounding, setGrounding] = useState<Grounding | null>(null)
  const [diff, setDiff] = useState<DraftDiff | null>(null)
  const [tab, setTab] = useState<Tab>('skeleton')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)

  const reportError = useCallback((err: unknown, fallback: string) => {
    if (err instanceof WorkspaceConflictError) {
      // 冲突要提示刷新，不是重试——再点一次仍然是旧时间戳
      setError(`${err.message}`)
      return
    }
    setError(err instanceof Error ? err.message : fallback)
  }, [])

  useEffect(() => {
    if (!sessionToken) return
    let cancelled = false
    ;(async () => {
      try {
        const [loadedWorkspace, loadedSkills, loadedGrounding] = await Promise.all([
          fetchWorkspace(tenantId, sessionToken),
          fetchSkills(tenantId, sessionToken),
          fetchGrounding(tenantId, sessionToken),
        ])
        if (cancelled) return
        setWorkspace(loadedWorkspace)
        setSkills(loadedSkills)
        setGrounding(loadedGrounding)
      } catch (err) {
        if (!cancelled) reportError(err, '加载建模工作区失败')
      } finally {
        if (!cancelled) setLoaded(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionToken, tenantId, reportError])

  const persist = async (next: WorkspaceState) => {
    if (!sessionToken || !workspace) return
    setBusy(true)
    setError(null)
    try {
      setWorkspace(await saveWorkspace(tenantId, sessionToken, next, workspace.updated_at))
      // 应用之前的任何改动都会让上一次算的 diff 过时；留着它会让用户照着
      // 一份旧差异点「写入草稿」。
      setDiff(null)
    } catch (err) {
      reportError(err, '保存建模工作区失败')
    } finally {
      setBusy(false)
    }
  }

  const handleStart = async (skillName: string | null) => {
    if (!sessionToken) return
    setBusy(true)
    setError(null)
    try {
      setWorkspace(await createWorkspace(tenantId, sessionToken, skillName))
      setTab('skeleton')
    } catch (err) {
      reportError(err, '创建建模工作区失败')
    } finally {
      setBusy(false)
    }
  }

  const handleReview = (
    kind: 'term' | 'relation',
    key: string,
    review: 'accepted' | 'rejected',
  ) => {
    if (!workspace) return
    const state = workspace.state
    void persist(
      kind === 'term'
        ? {
            ...state,
            term_types: state.term_types.map((t) => (t.value === key ? { ...t, review } : t)),
          }
        : {
            ...state,
            relation_types: state.relation_types.map((r) =>
              r.relation_type === key ? { ...r, review } : r,
            ),
          },
    )
  }

  const handlePromote = (file: string, column: string) => {
    if (!workspace) return
    const state = workspace.state
    if (state.term_types.some((t) => t.value === column)) return
    void persist({
      ...state,
      term_types: [
        ...state.term_types,
        {
          value: column,
          display_name: column,
          provenance: 'data',
          review: 'pending',
          standard_name_value_type: 'string',
          extra_fields: [],
          // 这一列自己就是它的别名——下次扫同一张表还能对上
          key_aliases: [column],
          field_aliases: {},
          clues: [],
          data_match: {
            source_file: file,
            key_columns: [column],
            field_columns: {},
            matched_by: 'manual',
          },
        },
      ],
      unmatched_columns: {
        ...state.unmatched_columns,
        [file]: (state.unmatched_columns[file] ?? []).filter((name) => name !== column),
      },
    })
  }

  const handlePreview = async () => {
    if (!sessionToken || !workspace) return
    setBusy(true)
    setError(null)
    try {
      setDiff(await previewApply(tenantId, sessionToken, projectToDraftPayload(workspace.state)))
    } catch (err) {
      reportError(err, '计算差异失败')
    } finally {
      setBusy(false)
    }
  }

  const handleApply = async () => {
    if (!sessionToken || !workspace) return
    const payload = projectToDraftPayload(workspace.state)
    if (payload.term_types.length === 0) {
      setError('工作区里一个已接受的实体类型都没有，写入草稿没有意义。先去「骨架」面板接受几条。')
      return
    }
    if (
      diff !== null &&
      diff.removed_term_types.length + diff.removed_relation_types.length > 0 &&
      !(await confirm({
        message: `写入后这些会从草稿里消失：${[...diff.removed_term_types, ...diff.removed_relation_types].join('、')}。`,
        confirmLabel: '继续写入',
      }))
    ) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      const mapping = projectToEtlYaml(workspace.state, tenantId)
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/draft/replace`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ...payload,
            etl_mapping: mapping
              ? { config_yaml: mapping.yaml, source_file_name: mapping.fileName }
              : null,
          }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '写入草稿失败'))
      }
      showToast('已写入本体草稿')
      // 刻意不调 /confirm：确认是不可逆的，工作台不替用户做这个决定
      setGrounding(await fetchGrounding(tenantId, sessionToken))
      setDiff(null)
    } catch (err) {
      reportError(err, '写入草稿失败')
    } finally {
      setBusy(false)
    }
  }

  const handleExport = async () => {
    if (!sessionToken) return
    setBusy(true)
    setError(null)
    try {
      const text = await exportSkill(
        tenantId,
        sessionToken,
        `${tenantId.toLowerCase().replace(/[^a-z0-9_]/g, '_')}_domain`,
        `${tenantId} 导出的领域模板`,
      )
      const url = URL.createObjectURL(new Blob([text], { type: 'text/yaml;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `${tenantId}-skill.yaml`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      showToast('已导出领域模板，人工审阅后才能提交进代码仓')
    } catch (err) {
      reportError(err, '导出领域模板失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.guidedOntology}</h1>
        <p className="text-sm text-ink-soft">{nextStepHint(workspace, diff)}</p>
      </div>

      {error && (
        <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          {error}
        </p>
      )}

      {!loaded && <p className="text-sm text-ink-soft">加载中…</p>}

      {loaded && workspace === null && (
        <StartPanel skills={skills} busy={busy} onStart={handleStart} />
      )}

      {loaded && workspace !== null && (
        <>
          <div className={`${panelClass} flex flex-wrap gap-2`}>
            {TAB_LABELS.map(({ id, label }) => (
              <button
                key={id}
                type="button"
                aria-pressed={tab === id}
                className={secondaryButtonClass}
                onClick={() => setTab(id)}
              >
                {label}
              </button>
            ))}
          </div>
          {tab === 'skeleton' && (
            <SkeletonPanel state={workspace.state} grounding={grounding} onReview={handleReview} />
          )}
          {tab === 'data' && (
            <DataPanel
              state={workspace.state}
              busy={busy}
              onMerged={(next) => void persist(next)}
              onPromote={handlePromote}
            />
          )}
          {tab === 'ungrounded' && (
            <UngroundedPanel state={workspace.state} grounding={grounding} />
          )}
          {tab === 'apply' && (
            <ApplyPanel
              diff={diff}
              busy={busy}
              onPreview={handlePreview}
              onApply={handleApply}
              onExport={handleExport}
            />
          )}
        </>
      )}
    </div>
  )
}
```

- [ ] **Step 9: 临时把路由指向工作台以跑测试**

改 `frontend/src/App.tsx`：第 20 行的 import 换成
`import { ModelingWorkbenchPage } from './admin/modelingWorkbench/ModelingWorkbenchPage'`，
第 70 行的 element 换成 `<ModelingWorkbenchPage />`。（Task 11 会把旧文件删掉，
这里先换指向，本任务的页面测试才跑得起来。）

- [ ] **Step 10: 跑测试确认通过**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench --maxWorkers=2`
Expected: 全部 PASS。错误提示用的是本项目既有的那组类名
（`rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink`，
见原 GuidedOntologyPage 的 page-error 块），不要新造颜色 token。

- [ ] **Step 11: 变异检查**

1. 把 `persist` 里的 `setDiff(null)` 删掉：没有测试会红——补一条测试：先 preview
   拿到 diff，再点「拒绝 SKU」，断言 diff 区域回到「先点『看看会改什么』」那句话。
   补完再做这次变异，确认它变红。改回。
2. 把 `saveWorkspace(..., workspace.updated_at)` 改成传 `'x'`：`接受一个实体类型会把整份工作区存回去` 里
   `expect(body.updated_at).toBe('2026-09-16T10:00:00')` 必须变红。改回。
3. 把 `ApplyPanel` 里 `removed_term_types` 那段整块删掉：`应用面板先显示差异，删除项单独醒目列出` 必须变红。改回。

- [ ] **Step 12: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/ui.ts frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx frontend/src/admin/modelingWorkbench/panels/StartPanel.tsx frontend/src/admin/modelingWorkbench/panels/SkeletonPanel.tsx frontend/src/admin/modelingWorkbench/panels/DataPanel.tsx frontend/src/admin/modelingWorkbench/panels/UngroundedPanel.tsx frontend/src/admin/modelingWorkbench/panels/ApplyPanel.tsx frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx frontend/src/App.tsx
git commit -m "feat(admin): 建模工作台页面与四个面板

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: 下线引导建模页，改名与全量回归

**Files:**
- Delete: `frontend/src/admin/guidedOntology/GuidedOntologyPage.tsx`、`ProposalReview.tsx`、`guidedEntry.test.tsx`、`guidedPage.test.tsx`、`guidedSubmit.test.tsx`、`proposalReview.test.tsx`
- Keep（不动）: `guidedOntology/columnStats.ts`、`columnRoles.ts`、`draftProposal.ts`、`types.ts` 及其 `.test.ts`、`sourceParserPassthrough.test.ts`
- Modify: `frontend/src/adminRoutes.ts:135`（侧边栏标签）
- Modify: `frontend/src/admin/OntologySchemaPage.tsx:329-340`（入口链接文案）
- Test: `frontend/src/adminRoutes.test.ts`（既有，跑通即可）

**Interfaces:**
- Consumes: Task 10 的 `ModelingWorkbenchPage`（App.tsx 在 Task 10 里已经改指向）
- Produces: 无新接口。路由键 `guidedOntology` 与路径 `/admin/ontology/guided` **不变**——改路径会让所有收藏了这个地址的人 404，而重定向表里已经有一条 `/admin/model/guided` 指向它，再加一层没有收益。

- [ ] **Step 1: 确认待删文件没有别处引用**

Run（在仓库根）：`grep -rn "GuidedOntologyPage\|ProposalReview" frontend/src --include=*.ts --include=*.tsx`
Expected: 只剩下待删的那几个文件自己（App.tsx 已在 Task 10 改过）。如果还有别处
引用，先处理引用再删。

- [ ] **Step 2: 删文件**

```bash
git rm frontend/src/admin/guidedOntology/GuidedOntologyPage.tsx \
       frontend/src/admin/guidedOntology/ProposalReview.tsx \
       frontend/src/admin/guidedOntology/guidedEntry.test.tsx \
       frontend/src/admin/guidedOntology/guidedPage.test.tsx \
       frontend/src/admin/guidedOntology/guidedSubmit.test.tsx \
       frontend/src/admin/guidedOntology/proposalReview.test.tsx
```

`draftProposal.ts` 保留：它的 `suggestRelationName` / `sanitizeFieldName` 是纯逻辑，
将来"空白起步 + 只传一张表"的退化路径要用（spec 前端一节明确说保留）。
`columnStats.ts` / `columnRoles.ts` 是工作台数据面板正在用的，更不能删。

- [ ] **Step 3: 改侧边栏标签**

`frontend/src/adminRoutes.ts` 第 135 行：

```ts
      { path: ADMIN_ROUTES.guidedOntology, label: '建模工作台', icon: Wand2 },
```

（路由键与路径不变，只改显示文案。`PAGE_TITLES` 从 `ALL_NAV_ITEMS` 取标题，
所以页面 h1 会自动跟着变成"建模工作台"。）

- [ ] **Step 4: 改本体结构页的入口文案**

`frontend/src/admin/OntologySchemaPage.tsx` 第 340 行附近，把
`从表格开始引导建模` 改成 `打开建模工作台`。

- [ ] **Step 5: 跑前端全量测试**

Run（在 `frontend/` 下）：`NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2`
Expected: 全部 PASS。可能变红的三处及处置：
- `adminRoutes.test.ts` 若断言过 `'引导建模'` 这个文案，改成 `'建模工作台'`。
- `schemaEtlPage.test.tsx:660` 只是注释里提到引导建模，不用改。
- 任何还 import 已删文件的测试——说明 Step 1 漏了，回去处理。

- [ ] **Step 6: 跑后端全量测试**

Run（在仓库根，后台跑并轮询日志）：
`PYTHONIOENCODING=utf-8 python -u -m pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add frontend/src/adminRoutes.ts frontend/src/admin/OntologySchemaPage.tsx
git commit -m "refactor(admin): 下线单表引导建模页，入口改为建模工作台

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

（`git rm` 的删除已经在暂存区里，会随这次提交一起进去；仍然逐个文件点名，
不要用 `git add -A`。）

- [ ] **Step 8: 人工验收（需要用户在场）**

起服务：`scripts/start-backend.ps1` / `scripts/start-frontend.ps1`。
用 member 账号登录 → 侧边栏「建模工作台」→ 选「消费品零售」起步 → 骨架面板
接受 SKU/Category/Store → 数据面板传 MUJI 的 `.xls`（表头行 6、首数据行 7）→
看 SKU 是否按 `alias:jan` 对上、未接住的列是否列了出来 → 未落地面板 → 应用面板
点「看看会改什么」确认 diff → 点「写入草稿」→ 去「本体结构」页确认草稿里有这几个
实体类型，去「表格导入」页确认映射带着 `sources:`（表头行 6）。
**不要点「开始导入」**（那会往 `demo` 租户的真实图谱里写数据）。

---

### Task 12: 骨架面板补齐改名、人工旁证、手工新增

Task 10 的 `SkeletonPanel` 只做了接受/拒绝。spec 行为规格 §2 列的操作是
**接受 / 拒绝 / 改名 / 加人工旁证 / 手工新增**，这个任务把后三样补上。
单列一个任务而不是塞进 Task 10：这三样各自有自己的状态变换和测试，混在
一次评审里会让"页面能跑起来"和"审阅动作齐全"两件事分不开。

**Files:**
- Create: `frontend/src/admin/modelingWorkbench/skeletonEdits.ts`（三个纯函数）
- Modify: `frontend/src/admin/modelingWorkbench/panels/SkeletonPanel.tsx`（加三组控件）
- Modify: `frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx`（接上三个回调）
- Test: `frontend/src/admin/modelingWorkbench/skeletonEdits.test.ts`
- Test: `frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx`（追加三条）

**Interfaces:**
- Consumes: Task 7 的类型
- Produces:
  - `renameTermType(state: WorkspaceState, from: string, to: string): WorkspaceState`
  - `addManualClue(state: WorkspaceState, termValue: string, note: string, by: string, at: string): WorkspaceState`
  - `addManualTermType(state: WorkspaceState, value: string): WorkspaceState`
  - `SkeletonPanel` 新增 props：`onRename(from: string, to: string)`、`onAddClue(termValue: string, note: string)`、`onAddTerm(value: string)`

- [ ] **Step 1: 写失败的测试**

```ts
// frontend/src/admin/modelingWorkbench/skeletonEdits.test.ts
import { describe, expect, it } from 'vitest'
import { addManualClue, addManualTermType, renameTermType } from './skeletonEdits'
import type { WorkspaceState, WorkspaceTermType } from './types'

const term = (value: string): WorkspaceTermType => ({
  value,
  display_name: value,
  provenance: 'skill',
  review: 'accepted',
  standard_name_value_type: 'string',
  extra_fields: [],
  key_aliases: ['jan'],
  field_aliases: {},
  clues: [],
  data_match: null,
})

const state = (): WorkspaceState => ({
  term_types: [term('SKU')],
  relation_types: [],
  constraints: [
    { subject: 'SKU', relation: 'SOLD_AT', object: 'Store', provenance: 'skill', review: 'pending' },
  ],
  sources: [],
  unmatched_columns: {},
  questions: [],
})

describe('renameTermType', () => {
  it('改名时把约束里的引用一起改掉', () => {
    // 不一起改的话，投影时约束引用的类型不存在，整条会被悄悄丢掉
    const next = renameTermType(state(), 'SKU', '商品')
    expect(next.term_types[0].value).toBe('商品')
    expect(next.constraints[0].subject).toBe('商品')
  })

  it('别名保留：改的是名字，不是"这个概念怎么从列名认出来"', () => {
    expect(renameTermType(state(), 'SKU', '商品').term_types[0].key_aliases).toEqual(['jan'])
  })

  it('改成已存在的名字时原样返回（调用方负责提示）', () => {
    const before = state()
    before.term_types.push(term('Store'))
    expect(renameTermType(before, 'SKU', 'Store')).toBe(before)
  })

  it('空名字不接受', () => {
    const before = state()
    expect(renameTermType(before, 'SKU', '   ')).toBe(before)
  })

  it('不改入参', () => {
    const before = state()
    const snapshot = JSON.stringify(before)
    renameTermType(before, 'SKU', '商品')
    expect(JSON.stringify(before)).toBe(snapshot)
  })
})

describe('addManualClue', () => {
  it('旁证追加到对应实体上，带上是谁什么时候加的', () => {
    const next = addManualClue(state(), 'SKU', '数据下个月接', 'alice', '2026-09-16T10:00:00')
    expect(next.term_types[0].clues).toEqual([
      { kind: 'manual', note: '数据下个月接', by: 'alice', at: '2026-09-16T10:00:00' },
    ])
  })

  it('空旁证不加', () => {
    const before = state()
    expect(addManualClue(before, 'SKU', '  ', 'alice', 'now')).toBe(before)
  })
})

describe('addManualTermType', () => {
  it('手工新增的元素来源是 manual、状态直接是 accepted', () => {
    // 用户自己敲进去的，不需要再自己审一遍
    const next = addManualTermType(state(), '促销活动')
    const added = next.term_types.find((t) => t.value === '促销活动')!
    expect(added.provenance).toBe('manual')
    expect(added.review).toBe('accepted')
    expect(added.key_aliases).toEqual(['促销活动'])
  })

  it('重名时原样返回', () => {
    const before = state()
    expect(addManualTermType(before, 'SKU')).toBe(before)
  })
})
```

追加到 `workbenchPage.test.tsx` 的 `describe('建模工作台')` 里：

```tsx
  it('改名会连同约束里的引用一起存回去', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('pending')
    renderWorkbench()
    const input = await screen.findByLabelText('SKU 的新名字')
    await userEvent.clear(input)
    await userEvent.type(input, '商品')
    await userEvent.click(screen.getByRole('button', { name: '改名 SKU' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as { state: { term_types: { value: string }[] } }
    expect(body.state.term_types[0].value).toBe('商品')
  })

  it('能给未落地的元素加一条人工旁证', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.type(await screen.findByLabelText('给 SKU 加旁证'), '数据下个月接')
    await userEvent.click(screen.getByRole('button', { name: '加旁证 SKU' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as { state: { term_types: { clues: { note: string }[] }[] } }
    expect(body.state.term_types[0].clues[0].note).toBe('数据下个月接')
  })

  it('能手工新增一个实体类型', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.type(await screen.findByLabelText('新实体类型名'), '促销活动')
    await userEvent.click(screen.getByRole('button', { name: '新增实体类型' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as { state: { term_types: { value: string; provenance: string }[] } }
    expect(body.state.term_types.some((t) => t.value === '促销活动' && t.provenance === 'manual')).toBe(
      true,
    )
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench --maxWorkers=2`
Expected: 找不到 `./skeletonEdits`；页面那三条找不到对应控件

- [ ] **Step 3: 写纯函数**

```ts
// frontend/src/admin/modelingWorkbench/skeletonEdits.ts
import type { WorkspaceState, WorkspaceTermType } from './types'

/**
 * 骨架审阅里的三个编辑动作。全是纯函数：输入非法（空名、重名）时**原样返回
 * 同一个对象引用**，调用方据此判断"什么都没变"并给出提示——抛异常的话每个
 * 调用点都要包 try，而这几种情况都不是异常，是用户打错字。
 */

function replaceTerm(
  state: WorkspaceState,
  value: string,
  update: (term: WorkspaceTermType) => WorkspaceTermType,
): WorkspaceState {
  return {
    ...state,
    term_types: state.term_types.map((t) => (t.value === value ? update(t) : t)),
  }
}

/**
 * 改名。约束里对它的引用一起改——不改的话投影时 `projectToDraftPayload` 的
 * 引用过滤会把那几条约束静默丢掉，用户只会发现"改了个名字，关系没了"。
 *
 * 别名不动：改的是这个概念叫什么，不是"怎么从列名认出它"。
 */
export function renameTermType(state: WorkspaceState, from: string, to: string): WorkspaceState {
  const trimmed = to.trim()
  if (trimmed === '' || trimmed === from) return state
  if (state.term_types.some((t) => t.value === trimmed)) return state
  if (!state.term_types.some((t) => t.value === from)) return state
  return {
    ...replaceTerm(state, from, (term) => ({ ...term, value: trimmed, display_name: trimmed })),
    constraints: state.constraints.map((c) => ({
      ...c,
      subject: c.subject === from ? trimmed : c.subject,
      object: c.object === from ? trimmed : c.object,
    })),
  }
}

/**
 * 加一条人工旁证（"我们有这个数据，下个月接"）。
 *
 * 旁证**不改变落地状态**（spec 决策 7：落地纯粹由 ETL 映射推导）。它只是让
 * 未落地清单上的这一条带着解释——否则三个月后没人记得为什么它还在清单上。
 */
export function addManualClue(
  state: WorkspaceState,
  termValue: string,
  note: string,
  by: string,
  at: string,
): WorkspaceState {
  const trimmed = note.trim()
  if (trimmed === '') return state
  if (!state.term_types.some((t) => t.value === termValue)) return state
  return replaceTerm(state, termValue, (term) => ({
    ...term,
    clues: [...term.clues, { kind: 'manual', note: trimmed, by, at }],
  }))
}

/**
 * 手工新增一个实体类型。直接 accepted——用户自己敲进去的东西不需要他再审
 * 一遍；pending 的语义是"有人/有东西提议了，等你看"。
 */
export function addManualTermType(state: WorkspaceState, value: string): WorkspaceState {
  const trimmed = value.trim()
  if (trimmed === '') return state
  if (state.term_types.some((t) => t.value === trimmed)) return state
  return {
    ...state,
    term_types: [
      ...state.term_types,
      {
        value: trimmed,
        display_name: trimmed,
        provenance: 'manual',
        review: 'accepted',
        standard_name_value_type: 'string',
        extra_fields: [],
        // 名字本身当别名：下次扫表时同名列还能对上
        key_aliases: [trimmed],
        field_aliases: {},
        clues: [],
        data_match: null,
      },
    ],
  }
}
```

- [ ] **Step 4: 给 SkeletonPanel 加控件**

在 `SkeletonPanel.tsx` 顶部加 `import { useState } from 'react'`，props 增加
`onRename` / `onAddClue` / `onAddTerm` 三个回调，并在实体类型那一行的按钮后面
补上改名与旁证两个输入（用一个受控的本地草稿表，键是实体名）：

```tsx
export function SkeletonPanel(props: {
  state: WorkspaceState
  grounding: Grounding | null
  onReview: (kind: 'term' | 'relation', key: string, review: 'accepted' | 'rejected') => void
  onRename: (from: string, to: string) => void
  onAddClue: (termValue: string, note: string) => void
  onAddTerm: (value: string) => void
}) {
  const [renameDraft, setRenameDraft] = useState<Record<string, string>>({})
  const [clueDraft, setClueDraft] = useState<Record<string, string>>({})
  const [newTerm, setNewTerm] = useState('')
  // …（原有的 groundedTerms / groundedRelations / terms / relations 不变）
```

每个实体类型行内，接受/拒绝两个按钮之后追加：

```tsx
            <label className="sr-only" htmlFor={`rename-${term.value}`}>
              {`${term.value} 的新名字`}
            </label>
            <input
              id={`rename-${term.value}`}
              className="w-32 rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
              value={renameDraft[term.value] ?? term.value}
              onChange={(e) => setRenameDraft({ ...renameDraft, [term.value]: e.target.value })}
            />
            <button
              type="button"
              className={secondaryButtonClass}
              onClick={() => props.onRename(term.value, renameDraft[term.value] ?? term.value)}
            >
              {`改名 ${term.value}`}
            </button>
            <label className="sr-only" htmlFor={`clue-${term.value}`}>
              {`给 ${term.value} 加旁证`}
            </label>
            <input
              id={`clue-${term.value}`}
              className="w-40 rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
              placeholder="例如：数据下个月接"
              value={clueDraft[term.value] ?? ''}
              onChange={(e) => setClueDraft({ ...clueDraft, [term.value]: e.target.value })}
            />
            <button
              type="button"
              className={secondaryButtonClass}
              onClick={() => {
                props.onAddClue(term.value, clueDraft[term.value] ?? '')
                setClueDraft({ ...clueDraft, [term.value]: '' })
              }}
            >
              {`加旁证 ${term.value}`}
            </button>
```

实体类型那一段 `</section>` 之前追加手工新增：

```tsx
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="new-term">
            新实体类型名
          </label>
          <input
            id="new-term"
            className="w-40 rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
            placeholder="骨架里缺的概念"
            value={newTerm}
            onChange={(e) => setNewTerm(e.target.value)}
          />
          <button
            type="button"
            className={secondaryButtonClass}
            onClick={() => {
              props.onAddTerm(newTerm)
              setNewTerm('')
            }}
          >
            新增实体类型
          </button>
        </div>
```

`sr-only` 是项目里已有的类名（AdminLayout.tsx 的跳转链接在用）。**不要**把
这些标签删掉：三个控件在同一行里重复出现，没有标签时读屏软件只会念出一连串
同样的"文本框"，而测试也要靠 `getByLabelText` 找到它们。

- [ ] **Step 5: 页面接上三个回调**

`ModelingWorkbenchPage.tsx` 里 `handleReview` 之后加：

```tsx
  const handleRename = (from: string, to: string) => {
    if (!workspace) return
    const next = renameTermType(workspace.state, from, to)
    if (next === workspace.state) {
      setError(`改名没生效：新名字不能为空，也不能跟已有的实体类型重名（${to}）。`)
      return
    }
    void persist(next)
  }

  const handleAddClue = (termValue: string, note: string) => {
    if (!workspace) return
    const next = addManualClue(
      workspace.state,
      termValue,
      note,
      username ?? '',
      new Date().toISOString(),
    )
    if (next !== workspace.state) void persist(next)
  }

  const handleAddTerm = (value: string) => {
    if (!workspace) return
    const next = addManualTermType(workspace.state, value)
    if (next === workspace.state) {
      setError(`没有新增：名字不能为空，也不能跟已有的实体类型重名（${value}）。`)
      return
    }
    void persist(next)
  }
```

import 补 `import { addManualClue, addManualTermType, renameTermType } from './skeletonEdits'`，
并把 `useAdminAuth()` 的解构改成 `const { sessionToken, username } = useAdminAuth()`
（`username: string | null` 是 useAdminAuth 已有的字段，见 useAdminAuth.ts:16）。

`<SkeletonPanel …>` 的 props 补上 `onRename={handleRename}`、
`onAddClue={handleAddClue}`、`onAddTerm={handleAddTerm}`。

- [ ] **Step 6: 跑测试确认通过**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench --maxWorkers=2`
Expected: 全部 PASS

- [ ] **Step 7: 变异检查**

1. 把 `renameTermType` 里改 constraints 的那段删掉（只改 term_types）：
   `改名时把约束里的引用一起改掉` 必须变红。改回。
2. 把 `addManualTermType` 里的 `review: 'accepted'` 改成 `'pending'`：
   `手工新增的元素来源是 manual、状态直接是 accepted` 必须变红。改回。

- [ ] **Step 8: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/skeletonEdits.ts frontend/src/admin/modelingWorkbench/skeletonEdits.test.ts frontend/src/admin/modelingWorkbench/panels/SkeletonPanel.tsx frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx
git commit -m "feat(admin): 骨架审阅补上改名、人工旁证与手工新增

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## 任务顺序与依赖

| 任务 | 依赖 | 可并行？ |
|---|---|---|
| 1 skill 注册表 | — | — |
| 2 工作区存储 | 1（`OntologySkill`） | — |
| 3 落地推导 | — | 与 1/2 并行 |
| 4 草稿 diff | — | 与 1/2/3 并行 |
| 5 导出 skill | 1（往返测试用 `load_skill`） | 与 2/3/4 并行 |
| 6 路由 | 1–5 全部 | — |
| 7 前端类型/API | 6（接口形状） | — |
| 8 对齐 | 7 | — |
| 9 投影 | 7 | 与 8 并行 |
| 10 页面 | 7、8、9 | — |
| 11 下线旧页 | 10 | — |
| 12 骨架编辑补齐 | 10 | 在 11 之前或之后都行 |

按 1→12 顺序执行最省事；上表只是说明哪些任务之间没有真实依赖。

## 完成判据

- 后端全量 `pytest tests/ -q` 全绿；前端 `npx vitest run` 全绿。
- 侧边栏「建模工作台」，member 账号可用。
- 选内置 skill 起步 → 审骨架 → 传带表头行设置的表 → 看到对齐结果与未接住的列 →
  未落地清单列出没数据的概念 → 应用面板显示 diff（删除项醒目）→ 写入草稿 →
  表格导入页看到带 `sources:` 的映射。
- 「导出为领域模板」下载下来的 YAML 能被 `load_skill` 装回去（Task 5 的往返测试
  已经锁住这一点）。
