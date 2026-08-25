# Planner 渐进式披露 + 召回增强参数生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `structured_filter_query_tool` 从"LLM 一次生成完整结构化参数"改成"先用自然语言表达查询意图（query_intent），再用一次独立、召回增强的调用把它翻译成结构化参数"，让参数生成有本体里真实存在的候选可以参照，而不是凭空猜测本体里的类型名/关系方向/字段名。

**Architecture:** `app/graphrag/ontology_recall.py` 新增一个纯函数召回模块（最长公共连续子串打分，四类候选统一处理）；`app/agent/tools.py` 里 `structured_filter_query_tool` 的 `parameters` 永久简化成只有 `query_intent` 一个字段，详细能力说明挪成模块级常量供后续独立调用引用；`app/agent/planner.py` 新增 `_resolve_tool_arguments` 按工具名分发（`vector_search_tool` 直通、`structured_filter_query_tool` 触发独立参数生成调用），接在 ReAct 推理调用和实际工具执行之间；`agent_routes.py`/`graph.py` 把新增的 `term_type_relation_allowlist` 已确认三元组数据一路传下去。

**Tech Stack:** Python 3.12 / FastAPI（`app/agent/`、`app/graphrag/`），pytest。

**Spec:** `docs/superpowers/specs/2026-08-25-progressive-disclosure-recall-augmented-params-design.md`

## Global Constraints

- **`tools` 请求参数在整个会话生命周期内、每一次 LLM 调用里必须逐字节保持一致**——不允许为任何一个工具在任何一次调用里传精简/不同的 `parameters` 或 `description`。`structured_filter_query_tool` 的 `parameters` 从这次改动落地起就是永久形态（只有 `query_intent`），不存在"运行时再简化一次"的代码路径。
- `vector_search_tool` 的 schema 不变。
- 独立参数生成调用**不使用 function-calling 协议**（不带 `tools`/`tool_choice`），**不携带本轮之前的对话历史**（只有 schema 说明+召回候选+`query_intent`）。
- `_PLANNER_SYSTEM_PROMPT` 最多保留一句"看到计数意图应该用这个工具"级别的触发线索，不能携带 anchor/hops/constraints/matched_count 这套深层机制说明——那套说明只能出现在独立参数生成调用的 prompt 里。
- 召回机制不依赖任何外部服务调用（不调 embedding provider、不调 LLM），纯本地字符串计算；每次独立参数生成调用基于当次已加载的 `terms`/`term_type_schema`/已确认三元组现算，不做进程级缓存。
- relation 候选必须以完整 `(subject_term_type, relation_type, object_term_type)` 三元组形式出现，不能只召回 `relation_type` 字符串本身。
- `max_tool_call_rounds` 默认值不变，语义仍然是"回合数"而非"LLM 调用次数"。
- 不改动 `app/graphrag/structured_filter_query.py` 里已有的解析/校验/fuzzy resolution 逻辑，不改动 `app/graphrag/neo4j_client.py`。

---

### Task 1: 召回模块（`app/graphrag/ontology_recall.py`）

**Files:**
- Create: `app/graphrag/ontology_recall.py`
- Test: `tests/graphrag/test_ontology_recall.py`

**Interfaces:**
- Consumes：`Term`（`app/graphrag/ontology.py`，字段 `standard_name`/`term_type`）；`TermTypeCategory`/`ExtraFieldSpec`（`app/graphrag/ontology_categories.py`，字段 `extra_fields: list[ExtraFieldSpec]`，`ExtraFieldSpec.name`）；`AllowedCombination`（`app/graphrag/ontology_constraints.py`，字段 `subject_term_type`/`relation_type`/`object_term_type`）。
- Produces：`RecallCandidates`（dataclass，字段 `term_types: list[str]`、`relations: list[AllowedCombination]`、`fields: list[tuple[str, str]]`、`entities: list[Term]`）；`recall_ontology_candidates(query_text, *, terms, term_type_schema, allowed_combinations) -> RecallCandidates`；`format_recall_candidates(candidates: RecallCandidates) -> str`；`longest_common_substring_score(a: str, b: str) -> float`（Task 3 的测试会直接用这个函数验证复现场景的具体打分）。

- [ ] **Step 1: 写失败的测试**

创建 `tests/graphrag/test_ontology_recall.py`：

```python
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination
from app.graphrag.ontology_recall import (
    format_recall_candidates,
    longest_common_substring_score,
    recall_ontology_candidates,
)


def test_longest_common_substring_score_exact_match():
    assert longest_common_substring_score("coke-cola", "Cola") == 1.0


def test_longest_common_substring_score_partial_match():
    score = longest_common_substring_score("coke-cola", "Coca-Cola")
    assert abs(score - 5 / 9) < 1e-9


def test_longest_common_substring_score_case_insensitive():
    assert longest_common_substring_score("COLA", "cola") == 1.0


def test_longest_common_substring_score_below_min_overlap_returns_zero():
    # "x" 和 "xyz" 最长公共子串只有1个字符，低于最小重叠长度阈值，直接判0分，
    # 不能因为候选名字短就让归一化分数虚高。
    assert longest_common_substring_score("x", "xyz") == 0.0


def test_longest_common_substring_score_empty_candidate_returns_zero():
    assert longest_common_substring_score("cola", "") == 0.0


_COLA_TERM = Term(
    tenant_id="demo", node_key="产品:Cola", standard_name="Cola",
    aliases=[], term_type="产品",
)
_COCA_COLA_TERM = Term(
    tenant_id="demo", node_key="公司:Coca-Cola", standard_name="Coca-Cola",
    aliases=[], term_type="公司",
)
_UNRELATED_TERM = Term(
    tenant_id="demo", node_key="用户名:Alice", standard_name="Alice",
    aliases=[], term_type="用户名",
)
_TERM_TYPE_SCHEMA = {
    "订单号": TermTypeCategory(value="订单号", extra_fields=[]),
    "产品": TermTypeCategory(value="产品", extra_fields=[ExtraFieldSpec(name="price", value_type="number")]),
    "公司": TermTypeCategory(value="公司", extra_fields=[]),
    "用户名": TermTypeCategory(value="用户名", extra_fields=[]),
}
_ALLOWED_COMBINATIONS = [
    AllowedCombination(subject_term_type="订单号", relation_type="BELONG_TO", object_term_type="产品"),
    AllowedCombination(subject_term_type="产品", relation_type="BELONG_TO", object_term_type="公司"),
    AllowedCombination(subject_term_type="订单号", relation_type="ORDER_BY", object_term_type="用户名"),
]


def test_recall_ontology_candidates_finds_relevant_term_types_relations_and_entities():
    candidates = recall_ontology_candidates(
        "查询Coca-Cola这家公司名下有多少个订单",
        terms=[_COLA_TERM, _COCA_COLA_TERM, _UNRELATED_TERM],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert "公司" in candidates.term_types
    assert "订单号" in candidates.term_types
    assert AllowedCombination(
        subject_term_type="产品", relation_type="BELONG_TO", object_term_type="公司",
    ) in candidates.relations
    assert AllowedCombination(
        subject_term_type="订单号", relation_type="BELONG_TO", object_term_type="产品",
    ) in candidates.relations
    assert _COCA_COLA_TERM in candidates.entities
    assert _UNRELATED_TERM not in candidates.entities


def test_recall_ontology_candidates_relation_matches_on_any_component():
    # query 里完全没提"产品"，但"订单号 --BELONG_TO--> 产品"这条三元组因为
    # subject_term_type="订单号"跟query有重叠，也应该被召回（不要求三元组
    # 三个组成部分都命中才算相关）。
    candidates = recall_ontology_candidates(
        "订单号有多少个",
        terms=[],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert AllowedCombination(
        subject_term_type="订单号", relation_type="BELONG_TO", object_term_type="产品",
    ) in candidates.relations


def test_recall_ontology_candidates_finds_field_names():
    candidates = recall_ontology_candidates(
        "price大于100的产品",
        terms=[],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=[],
    )

    assert ("产品", "price") in candidates.fields


def test_recall_ontology_candidates_truncates_to_top_k():
    many_terms = [
        Term(tenant_id="demo", node_key=f"产品:Cola{i}", standard_name=f"Cola{i}", aliases=[], term_type="产品")
        for i in range(50)
    ]
    candidates = recall_ontology_candidates(
        "cola", terms=many_terms, term_type_schema={}, allowed_combinations=[],
    )

    assert len(candidates.entities) <= 20


def test_recall_ontology_candidates_no_match_returns_empty_lists():
    candidates = recall_ontology_candidates(
        "完全不相关的问题内容",
        terms=[_COLA_TERM],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert candidates.term_types == []
    assert candidates.relations == []
    assert candidates.fields == []
    assert candidates.entities == []


def test_format_recall_candidates_includes_relation_direction():
    candidates = recall_ontology_candidates(
        "订单号属于哪个产品",
        terms=[],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    text = format_recall_candidates(candidates)
    assert "订单号 --BELONG_TO--> 产品" in text


def test_format_recall_candidates_empty_returns_placeholder_text():
    from app.graphrag.ontology_recall import RecallCandidates

    text = format_recall_candidates(RecallCandidates(term_types=[], relations=[], fields=[], entities=[]))
    assert text
    assert "候选" in text or "谨慎" in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_recall.py -v`
Expected: 全部 FAIL（`ModuleNotFoundError: No module named 'app.graphrag.ontology_recall'`）。

- [ ] **Step 3: 实现**

创建 `app/graphrag/ontology_recall.py`：

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination

# 跟 app/retrieval/bm25.py 的 _TOKEN_PATTERN 用同一套规则（英文按
# [a-z0-9_]+ 整段切、中文按字切），这里复制这一行正则常量而不是跨模块
# import 一个下划线开头的私有名字——两边各自独立维护同一份简单规则，
# 比引入模块间私有耦合更清晰。
_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[一-鿿]")

_MIN_SCORE = 0.3
_MIN_OVERLAP_LENGTH = 2
_NGRAM_MAX_LEN = 4
_TERM_TYPE_TOP_K = 10
_RELATION_TOP_K = 10
_FIELD_TOP_K = 10
_ENTITY_TOP_K = 20


def _tokenize_ngrams(text: str, *, max_len: int = _NGRAM_MAX_LEN) -> list[str]:
    """把 query 文本切成 token，再拼出 1~max_len 个 token 长的滑动窗口
    n-gram，作为跟候选名字比对的基本单位。"""
    tokens = _TOKEN_PATTERN.findall(text.lower())
    ngrams: list[str] = []
    for start in range(len(tokens)):
        for length in range(1, max_len + 1):
            end = start + length
            if end > len(tokens):
                break
            ngrams.append("".join(tokens[start:end]))
    return ngrams


def _longest_common_substring_length(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best


def longest_common_substring_score(a: str, b: str) -> float:
    """最长公共连续子串长度（大小写不敏感）除以 b 的长度，归一化成 0~1
    分数——a 是 query 里切出来的 n-gram，b 是候选名字。重叠长度小于
    _MIN_OVERLAP_LENGTH 个字符时直接返回0，避免单字符/极短噪声匹配
    （否则短候选名字下归一化分数会虚高）。"""
    if not b:
        return 0.0
    overlap = _longest_common_substring_length(a, b)
    if overlap < _MIN_OVERLAP_LENGTH:
        return 0.0
    return overlap / len(b)


def _best_score(ngrams: list[str], *names: str) -> float:
    """ngrams 对多个候选名字（比如一个关系三元组的三个组成部分）分别
    打分，取最高的一个——命中任意一个组成部分就算这个候选跟 query
    相关，不要求全部命中。"""
    if not ngrams:
        return 0.0
    return max(
        (longest_common_substring_score(ngram, name) for ngram in ngrams for name in names),
        default=0.0,
    )


def _rank(scored: list[tuple[float, object]], *, top_k: int) -> list[object]:
    kept = [(score, payload) for score, payload in scored if score >= _MIN_SCORE]
    kept.sort(key=lambda item: item[0], reverse=True)
    return [payload for _, payload in kept[:top_k]]


@dataclass(frozen=True)
class RecallCandidates:
    term_types: list[str]
    relations: list[AllowedCombination]
    fields: list[tuple[str, str]]  # (term_type, field_name)
    entities: list[Term]


def recall_ontology_candidates(
    query_text: str,
    *,
    terms: list[Term],
    term_type_schema: dict[str, TermTypeCategory],
    allowed_combinations: list[AllowedCombination],
) -> RecallCandidates:
    """针对 query_text，从本体的四类信息里各自召回最相关的候选，供独立
    参数生成调用参考——term_type/relation 三元组/字段名池子通常很小，
    召回时会把自己基本全部召回回来；实体名池子可能很大（数万条），
    真正需要靠打分+截断收窄候选范围。"""
    ngrams = _tokenize_ngrams(query_text)

    term_types = _rank(
        [(_best_score(ngrams, name), name) for name in term_type_schema],
        top_k=_TERM_TYPE_TOP_K,
    )
    relations = _rank(
        [
            (
                _best_score(ngrams, combo.subject_term_type, combo.relation_type, combo.object_term_type),
                combo,
            )
            for combo in allowed_combinations
        ],
        top_k=_RELATION_TOP_K,
    )
    fields = _rank(
        [
            (_best_score(ngrams, field.name), (term_type, field.name))
            for term_type, category in term_type_schema.items()
            for field in category.extra_fields
        ],
        top_k=_FIELD_TOP_K,
    )
    entities = _rank(
        [(_best_score(ngrams, term.standard_name), term) for term in terms],
        top_k=_ENTITY_TOP_K,
    )

    return RecallCandidates(term_types=term_types, relations=relations, fields=fields, entities=entities)


def format_recall_candidates(candidates: RecallCandidates) -> str:
    """把召回结果格式化成人类可读的文本块，塞进独立参数生成调用的 prompt。"""
    lines: list[str] = []
    if candidates.term_types:
        lines.append("可能相关的实体类型：" + "、".join(candidates.term_types))
    if candidates.relations:
        lines.append("可能相关的关系（方向：subject --relation_type--> object）：")
        for combo in candidates.relations:
            lines.append(f"  - {combo.subject_term_type} --{combo.relation_type}--> {combo.object_term_type}")
    if candidates.fields:
        lines.append("可能相关的字段：")
        for term_type, field_name in candidates.fields:
            lines.append(f"  - {term_type}.{field_name}")
    if candidates.entities:
        lines.append("可能相关的已知实体（标准名/类型）：")
        for term in candidates.entities:
            lines.append(f"  - {term.standard_name}（{term.term_type}）")
    if not lines:
        return "（本体里没有召回到明显相关的候选，请谨慎作答，字段/关系名要用已知的、不要凭空发明）"
    return "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_recall.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add app/graphrag/ontology_recall.py tests/graphrag/test_ontology_recall.py
git commit -m "feat(graphrag): add pure-function ontology recall for structured filter query params"
```

---

### Task 2: `structured_filter_query_tool` schema 永久简化 + 系统提示词收窄

**Files:**
- Modify: `app/agent/tools.py:41-177`（`STRUCTURED_FILTER_QUERY_TOOL_SCHEMA`）
- Modify: `app/agent/graph.py:76-90`（`_PLANNER_SYSTEM_PROMPT`）
- Test: `tests/agent/test_tools.py:127-132`（更新已有测试）

**Interfaces:**
- Consumes：无新依赖。
- Produces：`STRUCTURED_FILTER_QUERY_USAGE_GUIDE: str`、`STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA: dict[str, Any]`（新的模块级常量，`app/agent/tools.py`，Task 3 会 import 它们构造独立参数生成调用的 prompt——`STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA` 就是今天 `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA["function"]["parameters"]` 那份完整内容，只是改成公开的模块级常量、不再是对外暴露的 schema 本身）。

- [ ] **Step 1: 写失败的测试**

把 `tests/agent/test_tools.py` 里的 `test_structured_filter_query_tool_schema_supports_anchor_name_and_expand`（第 127-132 行）整体替换成：

```python
def test_structured_filter_query_tool_schema_only_exposes_query_intent():
    from app.agent.tools import STRUCTURED_FILTER_QUERY_TOOL_SCHEMA

    properties = STRUCTURED_FILTER_QUERY_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert set(properties) == {"query_intent"}
    assert STRUCTURED_FILTER_QUERY_TOOL_SCHEMA["function"]["parameters"]["required"] == ["query_intent"]
    # 详细能力说明（anchor/constraints/hops 这套结构化机制）不应该出现在
    # 对外暴露的 description 里——这是渐进式披露的核心：第一次推理调用
    # 只看到"用自然语言描述想查什么"，不需要理解结构化字段本身。
    description = STRUCTURED_FILTER_QUERY_TOOL_SCHEMA["function"]["description"]
    for forbidden in ("anchor", "constraints", "hops", "matched_count"):
        assert forbidden not in description
    assert "graph_query_tool" not in str(STRUCTURED_FILTER_QUERY_TOOL_SCHEMA)


def test_structured_filter_query_usage_guide_and_full_schema_preserve_detail():
    from app.agent.tools import (
        STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA,
        STRUCTURED_FILTER_QUERY_USAGE_GUIDE,
    )

    # 详细机制说明搬到这两个常量里，供独立参数生成调用引用——内容本身
    # 还在，只是不再暴露在对外的工具 schema 里。
    assert "anchor" in STRUCTURED_FILTER_QUERY_USAGE_GUIDE
    assert "constraints" in STRUCTURED_FILTER_QUERY_USAGE_GUIDE
    properties = STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA["properties"]
    assert "anchor" in properties
    assert "constraints" in properties
    assert "expand" in properties
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tools.py -v`
Expected: `test_structured_filter_query_tool_schema_only_exposes_query_intent` FAIL（今天的 `properties` 是 `{"anchor", "constraints", "expand", "group_by", "limit"}`，不是 `{"query_intent"}`）；`test_structured_filter_query_usage_guide_and_full_schema_preserve_detail` FAIL（`ImportError`，这两个常量还不存在）。

- [ ] **Step 3: 实现**

把 `app/agent/tools.py` 第 41-177 行（`STRUCTURED_FILTER_QUERY_TOOL_SCHEMA = {...}` 整段）替换成：

```python
STRUCTURED_FILTER_QUERY_USAGE_GUIDE = (
    "在知识图谱里查询实体——支持三种用法，可以组合使用：\n"
    "1. 已知实体名，查它是什么/关联着什么：anchor.name（会做别名模糊匹配）+ expand。\n"
    "2. 不知道具体实体名，按条件筛选一批满足条件的实体，"
    "适用于「有没有xx以上的」「比xx大的有哪些」「xx类目下有没有yy」"
    "「xx有多少个/数量是多少」这类问题：anchor.term_type + constraints。\n"
    "3. 上述两种可以叠加 expand，展开命中锚点的邻居关系。\n"
    "看到「多少个/数量」等计数意图时，必须以 anchor.term_type + constraints 模式返回的 "
    "matched_count 为准给出确定数字（anchor.name 模式的 matched_count 只表示"
    "「是否找到了这个实体」，是 0 或 1，不是数量答案）——不能仅凭检索到的文档片段或邻居关系"
    "列表猜测。constraints 里 standard_name 字段的 eq/ne 比较值支持别名/模糊匹配，"
    "不要求填精确的标准名称。"
)

STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "anchor": {
            "type": "object",
            "description": "起点定位方式，二选一",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "已知的实体名称或别名"},
                        "type_hint": {
                            "type": "string",
                            "description": "该实体的类型（可选，同名实体存在多个类型时用于消歧）",
                        },
                    },
                    "required": ["name"],
                },
                {
                    "type": "object",
                    "properties": {
                        "term_type": {
                            "type": "string",
                            "description": "要筛选的实体类型（如 SKU、Product、Category），结果就是这个类型的实体列表",
                        },
                    },
                    "required": ["term_type"],
                },
            ],
        },
        "constraints": {
            "type": "array",
            "description": "过滤条件列表，条件之间是 AND 关系，可以为空（anchor.name 模式下留空表示不额外过滤，"
                           "直接用解析出的锚点）。standard_name 字段的 eq/ne 比较值支持别名/模糊匹配，"
                           "不要求填精确的标准名称——比如用户说的口语化名字可以直接填进来，系统会自动解析。",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["attribute", "relation"],
                        "description": "attribute：直接比较锚点实体自己的字段；relation：经过关系跳到目标实体再比较",
                    },
                    "field": {
                        "type": "string",
                        "description": "kind=attribute 时必填：要比较的字段名（standard_name 或该实体类型已声明的属性字段名）",
                    },
                    "operator": {
                        "type": "string",
                        "enum": ["gt", "gte", "lt", "lte", "eq", "ne", "starts_with",
                                 "all_lte", "all_gte", "any_lte", "any_gte"],
                        "description": "比较运算符，实际可用范围取决于字段类型",
                    },
                    "value": {"description": "kind=attribute 时必填：比较的目标值"},
                    "hops": {
                        "type": "array",
                        "description": "kind=relation 时必填：从锚点出发的关系跳数组，最多2跳",
                        "items": {
                            "type": "object",
                            "properties": {
                                "relation_type": {"type": "string", "description": "关系类型，如 HAS_VARIANT"},
                                "direction": {"type": "string", "enum": ["outgoing", "incoming"]},
                                "target_term_type": {"type": "string", "description": "这一跳到达的实体类型"},
                            },
                            "required": ["relation_type", "direction", "target_term_type"],
                        },
                    },
                    "target_field": {
                        "type": "string",
                        "description": "kind=relation 时必填：在最后一跳到达的实体上比较哪个字段",
                    },
                    "target_operator": {
                        "type": "string",
                        "enum": ["gt", "gte", "lt", "lte", "eq", "ne", "starts_with",
                                 "all_lte", "all_gte", "any_lte", "any_gte"],
                        "description": "kind=relation 时必填：对 target_field 用的运算符",
                    },
                    "target_value": {"description": "kind=relation 时必填：比较的目标值"},
                },
                "required": ["kind"],
            },
        },
        "expand": {
            "type": ["object", "null"],
            "description": "可选：展开命中锚点的邻居关系",
            "properties": {
                "hops": {"type": "integer", "enum": [1, 2], "description": "展开几跳，默认1"},
                "relation_type": {
                    "type": ["string", "null"],
                    "description": "只展开这种关系类型；不传或传 null 表示任意类型",
                },
                "direction": {
                    "type": "string",
                    "enum": ["outgoing", "incoming", "both"],
                    "description": "关系方向，默认 both",
                },
            },
        },
        "group_by": {
            "type": ["object", "null"],
            "description": "可选：按某个字段做 distinct 值统计而不是返回实体列表本身",
            "properties": {
                "constraint_index": {
                    "type": "integer",
                    "description": "指向 constraints 数组里某个 kind=relation 约束的下标，按它的 target_field 分组",
                },
            },
        },
        "limit": {
            "type": "integer",
            "description": "返回结果的最大条数，默认20——预期命中数量较多时"
                           "（如宽泛的数值区间过滤），请设置一个合理的值避免返回过多结果",
        },
    },
    "required": ["anchor"],
}

STRUCTURED_FILTER_QUERY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "structured_filter_query_tool",
        "description": (
            "在知识图谱里查询实体数量/满足条件的实体列表——用自然语言描述"
            "你想查什么就行，不需要给出结构化参数，后续步骤会引导你把它"
            "转成实际能执行的查询。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query_intent": {
                    "type": "string",
                    "description": (
                        "用自然语言描述这次想查询/筛选的内容：想找什么类型的实体、"
                        "有什么筛选条件、涉及哪些已知的名字。写得越具体、越自包含"
                        "（把'它''这个'之类的指代词换成前面已经了解到的具体名字）"
                        "越好——这句话会被用来检索本体里相关的术语和关系作为参考，"
                        "帮你把接下来的实际查询参数填对。"
                    ),
                },
            },
            "required": ["query_intent"],
        },
    },
}
```

- [ ] **Step 4: 改 `_PLANNER_SYSTEM_PROMPT`**

把 `app/agent/graph.py` 第 76-90 行：

```python
_PLANNER_SYSTEM_PROMPT = (
    "你是客服问答助手。可以调用 vector_search_tool 检索知识库、"
    "structured_filter_query_tool 查询知识图谱——支持已知实体名查询关联信息"
    "（anchor.name，会做别名模糊匹配）、按数值区间/精确匹配/关系条件反查一批满足条件的实体"
    "（anchor.term_type + constraints，适用于「有没有xx以上的」「比xx大的有哪些」"
    "「xx有多少个/数量是多少」这类问题）、以及展开某个实体的关联关系（expand）。"
    "看到「多少个」「数量」等计数意图时，必须以 anchor.term_type + constraints 模式返回的"
    "matched_count 为准给出确定数字（anchor.name 模式的 matched_count 只表示"
    "「是否找到了这个实体」，是 0 或 1，不是数量答案）——不能仅凭检索到的文档片段或邻居关系"
    "列表猜测，也不能因为一次调用没查到就直接放弃。多数情况下约束条件里可以直接填口语化的名字"
    "（系统会自动解析成标准名），一次调用就够；只有 anchor.name 消歧本身有歧义、需要先确认"
    "具体是哪个实体时，才需要先消歧、再用消歧结果发起第二次调用。"
    "有足够信息时直接给出最终答案，不要编造资料中没有的内容；"
    "信息不足以回答时也不要编造。"
)
```

改成：

```python
_PLANNER_SYSTEM_PROMPT = (
    "你是客服问答助手。可以调用 vector_search_tool 检索知识库、"
    "structured_filter_query_tool 查询知识图谱里的实体数量/满足条件的实体列表。"
    "看到「多少个」「数量」等计数意图时，应该用 structured_filter_query_tool 给出确定数字，"
    "不能仅凭检索到的文档片段猜测，也不能因为一次调用没查到就直接放弃。"
    "有足够信息时直接给出最终答案，不要编造资料中没有的内容；"
    "信息不足以回答时也不要编造。"
    # anchor/constraints/hops/matched_count 这套结构化机制的详细说明不放在这里——
    # structured_filter_query_tool 现在只暴露 query_intent 一个自然语言字段
    # （见 app/agent/tools.py），深层机制只在独立参数生成调用（app/agent/planner.py
    # 的 _resolve_structured_filter_query_arguments）的 prompt 里出现，见
    # docs/superpowers/specs/2026-08-25-progressive-disclosure-recall-augmented-
    # params-design.md。这里常驻的这段提示词每一轮 ReAct 推理调用都会被完整看到，
    # 必须保持轻量——这正是这次改动要对齐的渐进式披露原则。
)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tools.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: 跑一下 `test_graph_planner.py`，确认系统提示词收窄没有连带破坏别的断言**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph_planner.py -v`
Expected: 全部 PASS（这个文件里没有对 `_PLANNER_SYSTEM_PROMPT` 具体文字内容的断言，改动不应该影响它）。

- [ ] **Step 7: Commit**

```bash
git add app/agent/tools.py app/agent/graph.py tests/agent/test_tools.py
git commit -m "feat(agent): shrink structured_filter_query_tool schema to a single query_intent field"
```

---

### Task 3: 参数解析分发器 + 独立参数生成调用（`app/agent/planner.py`）

**Files:**
- Modify: `app/agent/planner.py`（imports、新增 `ToolArgumentResolutionError`/`_resolve_tool_arguments`/`_resolve_structured_filter_query_arguments`/`_strip_json_code_fence`/`_build_structured_filter_query_prompt`，`run_tool_calls`/`_execute_one` 接入）
- Test: `tests/agent/test_planner.py`（新增测试，更新两个既有测试）

**Interfaces:**
- Consumes：Task 1 的 `recall_ontology_candidates`/`format_recall_candidates`；Task 2 的 `STRUCTURED_FILTER_QUERY_USAGE_GUIDE`/`STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA`；`AllowedCombination`（`app/graphrag/ontology_constraints.py`）。
- Produces：`ToolArgumentResolutionError(Exception)`；`_resolve_tool_arguments(tool_name, raw_arguments, *, fallback_query, terms, term_type_schema, allowed_combinations, llm_registry, llm_provider_name) -> dict[str, Any]`（Plan 3——工具插件化——的 `Tool.resolve_arguments` 协议会以这个函数的行为为准，这个 Plan 不需要预先设计插件形态）；`run_tool_calls(...)` 新增关键字参数 `allowed_combinations: list[AllowedCombination] | None = None`。

- [ ] **Step 1: 写失败的测试**

在 `tests/agent/test_planner.py` 顶部 import 区域补充：

```python
from app.graphrag.ontology_constraints import AllowedCombination
```

在 `_TERMS`/`FakeGraphClientForStructuredQuery` 定义（第 213-232 行）之后、`test_run_tool_calls_executes_structured_filter_query_tool_with_name_anchor` 定义之前，新增一批测试：

```python
async def test_resolve_tool_arguments_passes_through_vector_search_tool_without_llm_call():
    from app.agent.planner import _resolve_tool_arguments

    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([]))

    resolved = await _resolve_tool_arguments(
        "vector_search_tool", {"query": "网络连不上怎么办"},
        fallback_query="网络连不上怎么办？",
        terms=[], term_type_schema={}, allowed_combinations=[],
        llm_registry=llm_registry, llm_provider_name="fake-llm",
    )

    assert resolved == {"query": "网络连不上怎么办"}


async def test_resolve_tool_arguments_triggers_independent_call_for_structured_filter_query_tool():
    from app.agent.planner import _resolve_tool_arguments

    llm_registry = ProviderRegistry()
    provider = ScriptedLLMProvider(
        [ProviderResult(text='{"anchor": {"term_type": "订单号"}}')]
    )
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)

    resolved = await _resolve_tool_arguments(
        "structured_filter_query_tool", {"query_intent": "查一下订单号有多少个"},
        fallback_query="查一下订单号有多少个",
        terms=[], term_type_schema={}, allowed_combinations=[],
        llm_registry=llm_registry, llm_provider_name="fake-llm",
    )

    assert resolved == {"anchor": {"term_type": "订单号"}}
    assert len(provider.requests) == 1
    # 独立参数生成调用不走 function-calling 协议、不带历史。
    assert provider.requests[0].tools is None
    assert len(provider.requests[0].messages) == 1


async def test_resolve_tool_arguments_falls_back_to_original_question_when_query_intent_empty():
    from app.agent.planner import _resolve_tool_arguments

    llm_registry = ProviderRegistry()
    provider = ScriptedLLMProvider([ProviderResult(text='{"anchor": {"term_type": "订单号"}}')])
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)

    await _resolve_tool_arguments(
        "structured_filter_query_tool", {"query_intent": "   "},
        fallback_query="coke-cola公司有多少个订单",
        terms=[], term_type_schema={}, allowed_combinations=[],
        llm_registry=llm_registry, llm_provider_name="fake-llm",
    )

    # 召回 query 用的是原始问题的兜底值，不是空白字符串——通过 prompt 里
    # 是否出现原始问题文本来验证。
    assert "coke-cola公司有多少个订单" in provider.requests[0].messages[0]["content"]


async def test_resolve_tool_arguments_strips_markdown_code_fence():
    from app.agent.planner import _resolve_tool_arguments

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "fake-llm",
        ScriptedLLMProvider([ProviderResult(text='```json\n{"anchor": {"term_type": "订单号"}}\n```')]),
    )

    resolved = await _resolve_tool_arguments(
        "structured_filter_query_tool", {"query_intent": "查订单"},
        fallback_query="查订单",
        terms=[], term_type_schema={}, allowed_combinations=[],
        llm_registry=llm_registry, llm_provider_name="fake-llm",
    )

    assert resolved == {"anchor": {"term_type": "订单号"}}


async def test_resolve_tool_arguments_raises_when_independent_call_returns_invalid_json():
    from app.agent.planner import ToolArgumentResolutionError, _resolve_tool_arguments

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "fake-llm",
        ScriptedLLMProvider([ProviderResult(text="这不是 JSON")]),
    )

    try:
        await _resolve_tool_arguments(
            "structured_filter_query_tool", {"query_intent": "查订单"},
            fallback_query="查订单",
            terms=[], term_type_schema={}, allowed_combinations=[],
            llm_registry=llm_registry, llm_provider_name="fake-llm",
        )
        assert False, "应该抛出 ToolArgumentResolutionError"
    except ToolArgumentResolutionError:
        pass


async def test_resolve_tool_arguments_raises_for_unknown_tool():
    from app.agent.planner import ToolArgumentResolutionError, _resolve_tool_arguments

    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([]))

    try:
        await _resolve_tool_arguments(
            "unknown_tool", {},
            fallback_query="问题",
            terms=[], term_type_schema={}, allowed_combinations=[],
            llm_registry=llm_registry, llm_provider_name="fake-llm",
        )
        assert False, "应该抛出 ToolArgumentResolutionError"
    except ToolArgumentResolutionError:
        pass


async def test_resolve_tool_arguments_includes_recall_candidates_in_prompt():
    """端到端验证复现场景：query_intent 提到"公司"，召回到的候选（term_type
    "公司"、relation 三元组、实体名 Coca-Cola）应该出现在独立参数生成调用
    看到的 prompt 里。"""
    from app.agent.planner import _resolve_tool_arguments
    from app.graphrag.ontology import Term
    from app.graphrag.ontology_categories import TermTypeCategory

    llm_registry = ProviderRegistry()
    provider = ScriptedLLMProvider([ProviderResult(text='{"anchor": {"term_type": "订单号"}}')])
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)

    coca_cola_term = Term(
        tenant_id="demo", node_key="公司:Coca-Cola", standard_name="Coca-Cola",
        aliases=[], term_type="公司",
    )

    await _resolve_tool_arguments(
        "structured_filter_query_tool",
        {"query_intent": "查询Coca-Cola这家公司名下有多少个订单"},
        fallback_query="查询Coca-Cola这家公司名下有多少个订单",
        terms=[coca_cola_term],
        term_type_schema={
            "订单号": TermTypeCategory(value="订单号", extra_fields=[]),
            "公司": TermTypeCategory(value="公司", extra_fields=[]),
        },
        allowed_combinations=[
            AllowedCombination(subject_term_type="订单号", relation_type="BELONG_TO", object_term_type="产品"),
            AllowedCombination(subject_term_type="产品", relation_type="BELONG_TO", object_term_type="公司"),
        ],
        llm_registry=llm_registry, llm_provider_name="fake-llm",
    )

    prompt = provider.requests[0].messages[0]["content"]
    assert "公司" in prompt
    assert "Coca-Cola" in prompt
    assert "BELONG_TO" in prompt
```

紧接着，把已有的 `test_run_tool_calls_executes_structured_filter_query_tool_with_name_anchor`（第 235-273 行）整体替换成：

```python
async def test_run_tool_calls_executes_structured_filter_query_tool_with_name_anchor():
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "fake-llm",
        ScriptedLLMProvider([ProviderResult(text='{"anchor": {"name": "网关超时示例"}}')]),
    )

    state = {
        "tenant_id": "t1",
        "question": "网关超时示例是什么",
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_2",
                "name": "structured_filter_query_tool",
                "arguments": '{"query_intent": "查网关超时示例是什么"}',
            }
        ],
    }

    graph_client = FakeGraphClientForStructuredQuery(rows=[{
        "standard_name": "示例错误码E502", "node_key": "示例错误码E502", "term_type": "error_code",
        "all_properties": {},
    }])
    update = await run_tool_calls(
        state,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        terms=_TERMS,
        graph_client=graph_client,
        confirmed_relation_types=set(),
        term_type_schema={"error_code": TermTypeCategory(value="error_code", extra_fields=[])},
    )

    tool_message = update["planner_messages"][-1]
    assert "示例错误码E502" in tool_message["content"]
    assert graph_client.queried_tenant_ids == ["t1"]
```

同样把 `test_run_tool_calls_annotates_expand_neighbors_with_association`（第 276-317 行）里的 `state`/`llm_registry` 部分改成：

```python
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "fake-llm",
        ScriptedLLMProvider([ProviderResult(text='{"anchor": {"name": "网关超时示例"}, "expand": {"hops": 2}}')]),
    )

    state = {
        "tenant_id": "t1",
        "question": "网关超时示例关联什么",
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_2",
                "name": "structured_filter_query_tool",
                "arguments": '{"query_intent": "网关超时示例关联什么，展开2跳"}',
            }
        ],
    }
```

（这个测试函数其余部分——`graph_client`/`update = await run_tool_calls(...)`/断言——不用改，`run_tool_calls` 的调用参数已经跟上面那个测试一致。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -k "resolve_tool_arguments or structured_filter_query_tool_with_name_anchor or annotates_expand" -v`
Expected: 新增的 `test_resolve_tool_arguments_*` 全部 FAIL（`ImportError`，`_resolve_tool_arguments`/`ToolArgumentResolutionError` 还不存在）；两个改过的 `test_run_tool_calls_*` 测试 FAIL（`ScriptedLLMProvider([])`/没有 LLM 调用这件事跟今天的实现"直接透传 arguments"矛盾——今天的实现会拿 `{"query_intent": "..."}` 直接当 `structured_filter_query_tool()` 的 `arguments` 用，`anchor` 字段缺失，`validate_structured_filter_query` 应该会报错而不是命中 `graph_client.queried_tenant_ids == ["t1"]` 这个断言）。

- [ ] **Step 3: 实现**

在 `app/agent/planner.py` 顶部，把：

```python
from app.agent.tools import (
    STRUCTURED_FILTER_QUERY_TOOL_SCHEMA,
    VECTOR_SEARCH_TOOL_SCHEMA,
    structured_filter_query_tool,
    vector_search_tool,
)
```

改成：

```python
from app.agent.tools import (
    STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA,
    STRUCTURED_FILTER_QUERY_TOOL_SCHEMA,
    STRUCTURED_FILTER_QUERY_USAGE_GUIDE,
    VECTOR_SEARCH_TOOL_SCHEMA,
    structured_filter_query_tool,
    vector_search_tool,
)
from app.graphrag.ontology_constraints import AllowedCombination
from app.graphrag.ontology_recall import format_recall_candidates, recall_ontology_candidates
```

紧接着，在 `_dispatch_tool_call` 函数定义（今天大约第 211 行 `async def _dispatch_tool_call(`）之前，插入：

```python
class ToolArgumentResolutionError(Exception):
    """_resolve_tool_arguments 失败时抛出——调用方（run_tool_calls）捕获后
    降级成这次工具调用的 {"error": ...} 观察结果，不会让整个 Planner 轮次
    崩溃。"""


def _strip_json_code_fence(text: str) -> str:
    """独立参数生成调用不走 function-calling 协议，纯靠指令要求模型直接
    输出 JSON——即便提示词明确要求不要用代码块包裹，个别时候模型还是会
    习惯性地包一层 ```json ... ``` 或 ``` ... ```，这里做一次防御性剥离，
    不影响本身就没有代码块的正常情况。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped[3:]
        if stripped.startswith("json"):
            stripped = stripped[4:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    return stripped


def _build_structured_filter_query_prompt(query_intent: str, candidates) -> str:
    schema_text = json.dumps(STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA, ensure_ascii=False, indent=2)
    return (
        "你是一个把自然语言查询意图转成结构化查询参数的助手。给定下面的查询意图、"
        "使用说明、JSON Schema、以及召回到的本体候选参考，输出一段严格匹配这个 "
        "JSON Schema 的 JSON 对象作为你的完整回复——不要输出任何 JSON 之外的文字，"
        "也不要用 markdown 代码块包裹。\n\n"
        f"使用说明：\n{STRUCTURED_FILTER_QUERY_USAGE_GUIDE}\n\n"
        f"JSON Schema：\n{schema_text}\n\n"
        "constraints.hops 里的 relation_type/target_term_type、constraints 里的 "
        "field/target_field，以及 anchor.term_type，都应该优先使用下面候选参考里"
        "出现过的名字，不要凭空发明没见过的名字。\n\n"
        f"候选参考：\n{format_recall_candidates(candidates)}\n\n"
        f"查询意图：{query_intent}"
    )


async def _resolve_structured_filter_query_arguments(
    query_intent: str,
    *,
    terms: list[Term],
    term_type_schema: dict[str, TermTypeCategory],
    allowed_combinations: list[AllowedCombination],
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
) -> dict[str, Any]:
    """structured_filter_query_tool 的独立参数生成调用：召回本体候选 +
    完整 schema 说明 + query_intent，不走 function-calling 协议、不带
    历史，要求模型直接输出匹配 schema 的 JSON。"""
    candidates = recall_ontology_candidates(
        query_intent, terms=terms, term_type_schema=term_type_schema,
        allowed_combinations=allowed_combinations,
    )
    prompt = _build_structured_filter_query_prompt(query_intent, candidates)
    try:
        result = await llm_registry.run(
            ProviderCapability.LLM,
            ProviderRequest(messages=[{"role": "user", "content": prompt}]),
            provider_name=llm_provider_name,
        )
    except Exception as exc:
        raise ToolArgumentResolutionError(f"参数生成调用失败：{exc}") from exc
    try:
        return json.loads(_strip_json_code_fence(result.text))
    except json.JSONDecodeError as exc:
        raise ToolArgumentResolutionError(
            f"参数生成调用返回的内容不是合法 JSON：{result.text[:200]!r}"
        ) from exc


async def _resolve_tool_arguments(
    tool_name: str,
    raw_arguments: dict[str, Any],
    *,
    fallback_query: str,
    terms: list[Term],
    term_type_schema: dict[str, TermTypeCategory],
    allowed_combinations: list[AllowedCombination],
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
) -> dict[str, Any]:
    """按工具名分发到对应的参数解析方法，返回这个工具调用最终会被执行
    使用的参数字典。跟 _dispatch_tool_call（按工具名分发执行）是平行
    关系，发生在它之前。

    fallback_query 是本轮用户原始问题——vector_search_tool 直接复用
    raw_arguments 里的 query；structured_filter_query_tool 的
    query_intent 理论上是必填字段，但防御性地在它为空/空白时回退用
    fallback_query 作为召回 query。
    """
    if tool_name == "vector_search_tool":
        return raw_arguments
    if tool_name == "structured_filter_query_tool":
        query_intent = str(raw_arguments.get("query_intent") or "").strip() or fallback_query
        return await _resolve_structured_filter_query_arguments(
            query_intent,
            terms=terms, term_type_schema=term_type_schema,
            allowed_combinations=allowed_combinations,
            llm_registry=llm_registry, llm_provider_name=llm_provider_name,
        )
    raise ToolArgumentResolutionError(f"未知工具: {tool_name}")
```

最后，在 `run_tool_calls` 里接入。把函数签名（今天大约第 268-283 行）：

```python
async def run_tool_calls(
    state: dict[str, Any],
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    rerank_provider: RerankProvider | None = None,
    query_rewrite_enabled: bool = True,
    terms: list[Term] | None = None,
    graph_client: GraphClientProtocol | None = None,
    confirmed_relation_types: set[str] | None = None,
    term_type_schema: dict[str, TermTypeCategory] | None = None,
) -> dict[str, Any]:
```

改成（新增最后一个参数）：

```python
async def run_tool_calls(
    state: dict[str, Any],
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    rerank_provider: RerankProvider | None = None,
    query_rewrite_enabled: bool = True,
    terms: list[Term] | None = None,
    graph_client: GraphClientProtocol | None = None,
    confirmed_relation_types: set[str] | None = None,
    term_type_schema: dict[str, TermTypeCategory] | None = None,
    allowed_combinations: list[AllowedCombination] | None = None,
) -> dict[str, Any]:
```

再把 `_execute_one` 内部（今天大约第 296-325 行）：

```python
    async def _execute_one(call: dict[str, Any]) -> tuple[dict, list[VectorRecord]]:
        try:
            arguments = json.loads(call["arguments"]) if call["arguments"] else {}
        except json.JSONDecodeError:
            content = json.dumps({"error": "arguments 不是合法 JSON"}, ensure_ascii=False)
            return (
                {"tool_call_id": call["id"], "name": call["name"], "content": content},
                [],
            )
        content, new_records = await _dispatch_tool_call(
            call["name"],
            arguments,
            tenant_id=tenant_id,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            terms=terms,
            graph_client=graph_client,
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
        )
        return (
            {"tool_call_id": call["id"], "name": call["name"], "content": content},
            new_records,
        )
```

改成：

```python
    async def _execute_one(call: dict[str, Any]) -> tuple[dict, list[VectorRecord]]:
        try:
            arguments = json.loads(call["arguments"]) if call["arguments"] else {}
        except json.JSONDecodeError:
            content = json.dumps({"error": "arguments 不是合法 JSON"}, ensure_ascii=False)
            return (
                {"tool_call_id": call["id"], "name": call["name"], "content": content},
                [],
            )
        try:
            resolved_arguments = await _resolve_tool_arguments(
                call["name"], arguments,
                fallback_query=state.get("question", ""),
                terms=terms or [],
                term_type_schema=term_type_schema or {},
                allowed_combinations=allowed_combinations or [],
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
            )
        except ToolArgumentResolutionError as exc:
            content = json.dumps({"error": str(exc)}, ensure_ascii=False)
            return (
                {"tool_call_id": call["id"], "name": call["name"], "content": content},
                [],
            )
        content, new_records = await _dispatch_tool_call(
            call["name"],
            resolved_arguments,
            tenant_id=tenant_id,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            terms=terms,
            graph_client=graph_client,
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
        )
        return (
            {"tool_call_id": call["id"], "name": call["name"], "content": content},
            new_records,
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -k "resolve_tool_arguments or structured_filter_query_tool_with_name_anchor or annotates_expand" -v`
Expected: 全部 PASS。

- [ ] **Step 5: 跑这个文件的全部测试**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -v`
Expected: 全部 PASS（含 `test_dispatch_tool_call_routes_structured_filter_query_tool`/`test_dispatch_tool_call_reports_unconfigured_when_schema_data_missing`——这两个直接调 `_dispatch_tool_call`，不经过 `_resolve_tool_arguments`，不受这次改动影响）。

- [ ] **Step 6: 修 `test_graph_planner.py` 里唯一一个会被这次改动破坏的测试**

`tests/agent/test_graph_planner.py` 的 `test_planner_graph_uses_structured_filter_query_tool_with_term_guard_context`（第 187-255 行）用 `arguments='{"anchor": {"name": "网关超时示例"}}'`（旧形状）+ 只有 2 个响应（工具调用请求、最终答案）的 `ScriptedLLMProvider` 验证这条路径。按这次改动，第 1 轮工具调用会先触发一次独立参数生成调用，原本的第 2 个响应会被这次独立调用消费掉，第 2 轮 ReAct 推理决策调用就没有响应可弹、会 `IndexError`。

把第 191-210 行的 `llm_registry`/`ScriptedLLMProvider` 部分：

```python
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="structured_filter_query_tool",
                            arguments='{"anchor": {"name": "网关超时示例"}}',
                        )
                    ],
                ),
                ProviderResult(text="已确认标准名称是示例错误码E502。"),
            ]
        ),
    )
```

改成：

```python
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="structured_filter_query_tool",
                            arguments='{"query_intent": "查网关超时示例的标准名称"}',
                        )
                    ],
                ),
                ProviderResult(text='{"anchor": {"name": "网关超时示例"}}'),  # 独立参数生成调用
                ProviderResult(text="已确认标准名称是示例错误码E502。"),
            ]
        ),
    )
```

（这个测试函数其余部分——`terms`/`FakeGraphClient`/`build_agent_graph(...)`/最终断言——不用改。）

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph_planner.py -v`
Expected: 全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add app/agent/planner.py tests/agent/test_planner.py tests/agent/test_graph_planner.py
git commit -m "feat(agent): dispatch structured_filter_query_tool args through a recall-augmented resolver"
```

---

### Task 4: 把已确认关系三元组一路传到 Planner（`agent_routes.py` → `graph.py` → `planner.py`）

**Files:**
- Modify: `app/api/agent_routes.py`（新增 `list_allowed_combinations` 调用，传给 `build_agent_graph`）
- Modify: `app/agent/graph.py`（`build_agent_graph` 新增 `allowed_combinations` 参数，`tool_call_node` 传给 `run_tool_calls`）

**Interfaces:**
- Consumes：Task 3 的 `run_tool_calls(..., allowed_combinations=...)`；`list_allowed_combinations(conn, tenant_id, *, status) -> list[AllowedCombination]`（已有，`app/graphrag/ontology_constraints.py:42-52`）。

- [ ] **Step 1: `build_agent_graph` 新增参数并传给 `tool_call_node`**

在 `app/agent/graph.py` 顶部 import 区域，把：

```python
from app.graphrag.ontology_categories import TermTypeCategory
```

改成：

```python
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination
```

在 `build_agent_graph` 签名里（第 111-135 行），把：

```python
    confirmed_relation_types: set[str] | None = None,
    term_type_schema: dict[str, TermTypeCategory] | None = None,
```

改成：

```python
    confirmed_relation_types: set[str] | None = None,
    term_type_schema: dict[str, TermTypeCategory] | None = None,
    allowed_combinations: list[AllowedCombination] | None = None,
```

在 `tool_call_node`（第 660-675 行）里，把：

```python
    async def tool_call_node(state: AgentState) -> dict[str, Any]:
        return await run_tool_calls(
            state,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            terms=terms,
            graph_client=graph_client,
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
        )
```

改成：

```python
    async def tool_call_node(state: AgentState) -> dict[str, Any]:
        return await run_tool_calls(
            state,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            terms=terms,
            graph_client=graph_client,
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
            allowed_combinations=allowed_combinations,
        )
```

- [ ] **Step 2: `agent_routes.py` 加载并传入**

在 `app/api/agent_routes.py` 顶部 import 区域，把：

```python
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_relations import list_relation_types
```

改成：

```python
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations
from app.graphrag.ontology_relations import list_relation_types
```

在第 106-113 行（加载 `terms`/`confirmed_relation_types`/`term_type_schema` 那段）之后，补上：

```python
    terms = await list_terms(review_conn, tenant_id)
    confirmed_relation_types = {
        rt.relation_type
        for rt in await list_relation_types(review_conn, tenant_id, status="confirmed")
    }
    term_type_schema = {
        c.value: c for c in await list_term_types(review_conn, tenant_id, status="confirmed")
    }
    allowed_combinations = await list_allowed_combinations(review_conn, tenant_id, status="confirmed")
```

（只新增最后一行，前三行保持不变。）

在 `build_agent_graph(...)` 调用处（第 149-162 行附近），把：

```python
        graph = build_agent_graph(
            embedding_registry=embedding_registry,
            embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
            rerank_provider=rerank_provider,
            terms=terms,
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
            graph_client=graph_client,
            memory_conn=memory_conn,
            ticket_conn=memory_conn,
```

改成：

```python
        graph = build_agent_graph(
            embedding_registry=embedding_registry,
            embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
            rerank_provider=rerank_provider,
            terms=terms,
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
            allowed_combinations=allowed_combinations,
            graph_client=graph_client,
            memory_conn=memory_conn,
            ticket_conn=memory_conn,
```

- [ ] **Step 3: 跑全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/ tests/api/test_agent_chat_routes.py tests/graphrag/ -q`
Expected: 除了本轮会话已知的、跟这份计划无关的 5 个预先存在的失败（`tests/api/test_agent_chat_routes.py` 里 4 个 SSE 流式相关、`tests/providers/test_voice_factory.py` 1 个 TTS 默认配置相关）之外，全部 PASS。

- [ ] **Step 4: 手动端到端验证**

```powershell
powershell -File scripts/start-backend.ps1
```

用 httpx 直接发一个会触发 `structured_filter_query_tool` 的问题（比如"coke-cola公司有多少个订单"），确认能拿到一个具体数字，而不是转人工/空结果。检查后端日志确认这次请求确实发起了两类 LLM 调用（ReAct 推理决策 + 独立参数生成），而不是只有一次。

- [ ] **Step 5: Commit**

```bash
git add app/agent/graph.py app/api/agent_routes.py
git commit -m "feat(agent): thread confirmed relation triples through to the ontology recall step"
```

---

## Self-Review Notes（写完计划后的自查记录）

- **Spec coverage**：spec 的"工具 schema 的永久简化"→ Task 2；"参数解析分发器"→ Task 3；"独立参数生成调用"→ Task 3；"召回机制"→ Task 1；"阶段提示词的拆分与位置"（已改名为"系统提示词收窄"，机制已经不是拆两份提示词，而是把详细说明整体挪进独立调用）→ Task 2/3 共同覆盖。
- **Placeholder scan**：无 TBD/TODO，所有代码块（含 Task 3 Step 6 对 `test_planner_graph_uses_structured_filter_query_tool_with_term_guard_context` 的修改）都已读过原文件、给出精确到字面的替换内容。
- **Type consistency**：`RecallCandidates`/`recall_ontology_candidates`/`format_recall_candidates` 在 Task 1 定义、Task 3 使用，签名一致；`allowed_combinations: list[AllowedCombination] | None` 在 Task 3（`run_tool_calls`）、Task 4（`build_agent_graph`/`tool_call_node`）之间签名/默认值一致；`ToolArgumentResolutionError`/`_resolve_tool_arguments` 只在 Task 3 定义和使用。
