# 输入/输出安全增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 `docs/ARCHITECTURE.md` 描述、但当前只部分实现的输入/输出安全层：输入侧 PII 规则从"只有手机号"扩展到身份证号/邮箱，`banned_terms` 首次真正从生产配置接线；输出侧新增内部数据泄露规则检测+语义审查提示词增强；`/qa` 端点（此前完全没有安全检查）补齐与 `/agent/chat` 对齐的输入/输出安全检查。

**Architecture:** 4 个任务，每个任务独立可测试：① `app/safety/rules.py` PII 正则扩充 + `_UNSAFE_*` 常量搬迁；② `app/config/settings.py`/`app/api/deps.py` 新增 `banned_terms` 配置解析并接入 `app/api/agent_routes.py`；③ 新文件 `app/safety/leakage_detection.py` + `semantic_review.py` 提示词增强，接入 `app/agent/graph.py` 的 `output_safety_node`；④ `app/qa/answer.py::answer_question()` 补齐输入/输出安全检查，接入 `app/api/qa_routes.py`。

**Tech Stack:** Python 3.12、stdlib `re`（不引入新依赖）、pytest（`asyncio_mode = "auto"`）、pydantic-settings。

## Global Constraints

- 严格 TDD：RED（写失败测试，确认失败原因正确）→ GREEN（最小实现）→ 跑全量测试 → git commit。
- 本仓库当前在 `dev/0.1` 分支直接工作（非 main/master），不使用隔离 worktree。
- Commit message 格式：一行摘要（`feat:`/`fix:`/`refactor:` 前缀）+ 空行 + 中文详细说明（为什么这么做/复用了什么/刻意不做什么）+ 以 `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` 结尾。
- 测试命令统一用 `.venv/Scripts/python.exe -m pytest <path> -v`（Windows 环境，本仓库自带 `.venv`）。
- 全量测试跑 `.venv/Scripts/python.exe -m pytest tests/ -q`，预期除了 1 个已知的、与本计划改动完全无关的预先存在失败（`tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured`，本地 `.env` 里真实 TTS 凭证泄漏导致的环境问题，历史上反复确认与代码改动无关）之外全部通过。如果发现除此之外还有其他测试失败，必须先排查原因，不要跳过或忽略。
- 设计依据：`docs/superpowers/specs/2026-08-07-input-output-safety-enhancement-design.md`（已经用户批准，不要偏离其中的机制决策），尤其是这两条明确排除项：**不做**泛化的密码/密钥关键词正则（中文客服场景容易和"请重置密码"这类正常业务话术撞车）；**不改动** `app/agent/graph.py::responder_node` 里流式分句的轻量检查（`check_text(sentence, banned_terms=banned_terms)`，约在文件的 359-375 行区域）——那是"轻量规则检查+完整审查在后"的架构分工，`detect_internal_leakage`/语义审查增强只接入本计划新增/修改的 `output_safety_node`，不接入这条分句流式路径。
- 银行卡号检测不在本次范围（设计文档已确认误报率高，不加）。
- GraphRAG 实体链接模糊匹配（`app/graphrag/normalization.py::resolve_to_standard_name`）不在本次范围——那是拆分出的独立第 4 个子项目。

---

### Task 1: 输入侧 PII 规则扩充 + `_UNSAFE_*` 常量搬迁

**Files:**
- Modify: `app/safety/rules.py`
- Modify: `app/agent/graph.py`（第 41-48 行区域：import 语句 + 常量定义）
- Test: `tests/safety/test_rules.py`

**Interfaces:**
- Consumes：无新依赖。
- Produces：`check_text()` 的 `matched_terms` 新增可能值 `"id_card"`/`"email"`（函数签名/返回类型不变）；`app/safety/rules.py` 新增两个模块级常量 `UNSAFE_INPUT_MESSAGE = "您的问题包含无法处理的敏感内容，请修改后重新提问。"` 和 `UNSAFE_OUTPUT_MESSAGE = "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"`（注意：搬到 `rules.py` 后去掉前导下划线，因为要被 `app/agent/graph.py` 和后续 Task 4 的 `app/qa/answer.py` 一起 import，不再是模块私有）。后续任务（Task 3、Task 4）都从 `app.safety.rules` import 这两个常量，不再各自定义。

- [ ] **Step 1: 写失败测试**

打开 `tests/safety/test_rules.py`，在文件末尾追加（不改动已有的 3 个测试）：

```python
def test_check_text_flags_id_card_number():
    result = check_text("我的身份证号是11010519491231002X，麻烦核对一下")

    assert result.is_safe is False
    assert "id_card" in result.matched_terms


def test_check_text_flags_all_digit_id_card_number():
    result = check_text("身份证号440524188001010014可以查一下吗")

    assert result.is_safe is False
    assert "id_card" in result.matched_terms


def test_check_text_flags_email_address():
    result = check_text("请发到 test.user+tag@example.com 谢谢")

    assert result.is_safe is False
    assert "email" in result.matched_terms


def test_check_text_does_not_flag_unrelated_text_as_pii():
    result = check_text("今天天气怎么样")

    assert result.is_safe is True
    assert result.matched_terms == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/safety/test_rules.py -v`
Expected: 原有 3 个测试通过；新增的 `test_check_text_flags_id_card_number`、`test_check_text_flags_all_digit_id_card_number`、`test_check_text_flags_email_address` 3 个失败（`matched_terms` 里没有 `"id_card"`/`"email"`，因为还没实现对应正则）；`test_check_text_does_not_flag_unrelated_text_as_pii` 此时应该已经通过（这是正常的，用于在 Step 4 之后继续保护"不误报"这条行为）。

- [ ] **Step 3: 实现 PII 正则扩充**

把 `app/safety/rules.py` 整个文件替换为：

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field

_PHONE_NUMBER_PATTERN = re.compile(r"1[3-9]\d{9}")
# 18 位中国大陆身份证号：17 位数字 + 末位数字或大小写 X 校验位。和手机号
# 正则一样不做完整性校验（不验证省份码/生日合法性/校验位算法），只做
# 结构性识别——客服场景够用，过度校验会增加复杂度但不提升实际拦截效果。
_ID_CARD_PATTERN = re.compile(r"\d{17}[\dXx]")
# 标准 email 格式，不追求穷尽 RFC 5322 的所有合法形式。
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# 输入/输出安全网命中时的兜底话术。原定义在 app/agent/graph.py，现搬到
# 这里作为共享位置：Task 4 的 app/qa/answer.py 需要同一份文案，此前两处
# 各写一份有文案不一致的风险。
UNSAFE_INPUT_MESSAGE = "您的问题包含无法处理的敏感内容，请修改后重新提问。"
UNSAFE_OUTPUT_MESSAGE = "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"


@dataclass(frozen=True)
class SafetyCheckResult:
    is_safe: bool
    matched_terms: list[str] = field(default_factory=list)


def check_text(
    text: str,
    *,
    banned_terms: list[str] | None = None,
) -> SafetyCheckResult:
    matched: list[str] = []
    if _PHONE_NUMBER_PATTERN.search(text):
        matched.append("phone_number")
    if _ID_CARD_PATTERN.search(text):
        matched.append("id_card")
    if _EMAIL_PATTERN.search(text):
        matched.append("email")
    for term in banned_terms or []:
        if term in text:
            matched.append(term)
    return SafetyCheckResult(is_safe=not matched, matched_terms=matched)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/safety/test_rules.py -v`
Expected: 7 passed（原有 3 个 + 新增 4 个）

- [ ] **Step 5: 把 `app/agent/graph.py` 的 `_UNSAFE_*` 常量改成从 `rules.py` import**

`app/agent/graph.py` 第 41-48 行区域，当前是：

```python
from app.safety.prompt_injection import detect_prompt_injection, wrap_system_prompt
from app.safety.rules import check_text
from app.safety.semantic_review import semantic_safety_review
from app.voice.streaming_responder import stream_sentences

_PROMPT_TEMPLATE = "根据以下资料回答问题。\n资料：\n{context}\n\n问题：{question}"
_UNSAFE_INPUT_MESSAGE = "您的问题包含无法处理的敏感内容，请修改后重新提问。"
_UNSAFE_OUTPUT_MESSAGE = "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"
_FALLBACK_MESSAGE = "抱歉，暂时没有找到确切答案，已为您转接人工客服处理。"
```

改成：

```python
from app.safety.prompt_injection import detect_prompt_injection, wrap_system_prompt
from app.safety.rules import UNSAFE_INPUT_MESSAGE, UNSAFE_OUTPUT_MESSAGE, check_text
from app.safety.semantic_review import semantic_safety_review
from app.voice.streaming_responder import stream_sentences

_PROMPT_TEMPLATE = "根据以下资料回答问题。\n资料：\n{context}\n\n问题：{question}"
_FALLBACK_MESSAGE = "抱歉，暂时没有找到确切答案，已为您转接人工客服处理。"
```

然后在文件里搜索所有 `_UNSAFE_INPUT_MESSAGE` 和 `_UNSAFE_OUTPUT_MESSAGE` 的使用处（`output_safety_node` 函数体内，共 3 处：`return {"is_output_safe": True, "final_text": _UNSAFE_INPUT_MESSAGE}`、`return {"is_output_safe": False, "final_text": _UNSAFE_OUTPUT_MESSAGE}`、`"final_text": _UNSAFE_OUTPUT_MESSAGE,`），把 `_UNSAFE_INPUT_MESSAGE` 替换成 `UNSAFE_INPUT_MESSAGE`、`_UNSAFE_OUTPUT_MESSAGE` 替换成 `UNSAFE_OUTPUT_MESSAGE`（去掉前导下划线，因为现在是从 `rules.py` import 的公开常量，不是本文件私有变量）。这一步不改变任何行为，纯粹是把两个字符串常量的定义位置挪到共享模块。

- [ ] **Step 6: 跑全量测试确认没有破坏现有行为**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了 1 个已知无关的预先存在失败（`tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured`）之外全部通过——尤其要关注 `tests/agent/test_graph.py` 里断言 `result["final_text"] == "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"` 这类硬编码文案字符串的测试（比如 `test_semantic_review_flags_output_as_unsafe`），因为常量值没变、只是搬了位置，这些测试不需要改动就应该继续通过；如果失败说明常量替换有遗漏，需要排查。

- [ ] **Step 7: 提交**

```bash
git add app/safety/rules.py app/agent/graph.py tests/safety/test_rules.py
git commit -m "feat: extend PII rules to id-card/email, relocate unsafe-message constants to safety module"
```

---

### Task 2: `banned_terms` 从 Settings 真正接线到生产配置

**Files:**
- Modify: `app/config/settings.py`
- Modify: `app/api/deps.py`
- Modify: `app/api/agent_routes.py`
- Modify: `.env.example`
- Test: `tests/api/test_deps.py`

**Interfaces:**
- Consumes：无新依赖（Task 1 的 `check_text` 签名不变，`banned_terms` 参数早已存在）。
- Produces：`app/api/deps.py` 新增 `parse_banned_terms(raw: str | None) -> list[str] | None` 纯函数（`raw` 为 `None` 或空字符串时返回 `None`；否则按逗号切分并返回非空片段的列表）。Task 4 的 `app/api/qa_routes.py` 会复用这个函数，不用各自重复实现一遍解析逻辑。

- [ ] **Step 1: 写失败测试**

打开 `tests/api/test_deps.py`，看一下文件顶部的 import 写法（本仓库已有这个测试文件，需要先确认现有内容再追加，不要覆盖已有测试）。在文件末尾追加：

```python
def test_parse_banned_terms_returns_none_when_unset():
    assert deps.parse_banned_terms(None) is None


def test_parse_banned_terms_returns_none_when_empty_string():
    assert deps.parse_banned_terms("") is None


def test_parse_banned_terms_splits_comma_separated_values():
    assert deps.parse_banned_terms("敏感词1,敏感词2,敏感词3") == [
        "敏感词1",
        "敏感词2",
        "敏感词3",
    ]


def test_parse_banned_terms_strips_whitespace_around_each_term():
    assert deps.parse_banned_terms(" 敏感词1 , 敏感词2 ") == ["敏感词1", "敏感词2"]
```

如果文件顶部现有的 import 不是 `from app.api import deps`（比如是 `from app.api.deps import xxx` 这种具名导入），请改成 `from app.api import deps` 这种模块导入方式，上面 4 个新测试都用 `deps.parse_banned_terms(...)` 调用；已有测试用到的具名导入不用动，两种导入方式可以在同一个文件里共存。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_deps.py -v`
Expected: 新增的 4 个 `test_parse_banned_terms_*` 测试失败（`AttributeError: module 'app.api.deps' has no attribute 'parse_banned_terms'`），已有测试全部继续通过。

- [ ] **Step 3: 实现 `parse_banned_terms` + Settings 字段**

在 `app/config/settings.py` 的 `Settings` 类里，找到这一段（`gateway_shared_secret` 字段附近，是当前文件最后一个字段）：

```python
    gateway_shared_secret: str | None = None
```

在它后面追加：

```python
    # 逗号分隔的自定义敏感词列表，留空 = 不启用自定义敏感词检测（只有
    # check_text() 内置的手机号/身份证号/邮箱正则生效）。解析逻辑见
    # app/api/deps.py::parse_banned_terms。
    banned_terms: str | None = None
```

在 `app/api/deps.py` 里找到 `resolve_tenant_id` 函数定义（这是当前文件里已有的、类似性质的"纯解析函数"），在它后面新增：

```python
def parse_banned_terms(raw: str | None) -> list[str] | None:
    """把 Settings.banned_terms 的逗号分隔字符串解析成列表。

    留空返回 None（check_text() 的 banned_terms=None 等价于不启用自定义
    敏感词检测，只有内置正则生效）；每个词两端的空白会被去掉，方便配置
    时随意加空格。
    """
    if not raw:
        return None
    return [term.strip() for term in raw.split(",") if term.strip()]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_deps.py -v`
Expected: 全部通过（已有测试数量 + 新增 4 个）

- [ ] **Step 5: 接入 `app/api/agent_routes.py`**

打开 `app/api/agent_routes.py`，找到 `build_agent_graph(` 调用（第 121-137 行区域），当前是：

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
            graph_client=graph_client,
            memory_conn=memory_conn,
            ticket_conn=memory_conn,
            min_relevance_score=settings.agent_min_relevance_score,
            enable_autonomous_planning=enable_autonomous_planning,
            max_tool_call_rounds=settings.agent_max_tool_call_rounds,
            on_answer_chunk=on_answer_chunk,
        )
```

改成（新增一行 `banned_terms=deps.parse_banned_terms(settings.banned_terms),`）：

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
            graph_client=graph_client,
            memory_conn=memory_conn,
            ticket_conn=memory_conn,
            min_relevance_score=settings.agent_min_relevance_score,
            enable_autonomous_planning=enable_autonomous_planning,
            max_tool_call_rounds=settings.agent_max_tool_call_rounds,
            on_answer_chunk=on_answer_chunk,
            banned_terms=deps.parse_banned_terms(settings.banned_terms),
        )
```

这个函数所在的端点已经通过 `settings: Settings = Depends(deps.get_settings)` 拿到了 `settings`（如果变量名不同，请用 Read 工具确认这个端点函数签名里 `Settings` 依赖注入的实际变量名，用那个名字而不是假设是 `settings`）。

- [ ] **Step 6: 更新 `.env.example`**

打开 `.env.example`，找到文件末尾"多租户/网关"那一段（`CUSTOMER_RAG_GATEWAY_SHARED_SECRET=` 那一行）之前，插入新的一段：

```
# 内容安全：自定义敏感词（逗号分隔，留空 = 不启用，只有内置的手机号/
# 身份证号/邮箱正则生效）。见 app/safety/rules.py::check_text、
# app/api/deps.py::parse_banned_terms。
CUSTOMER_RAG_BANNED_TERMS=

```

- [ ] **Step 7: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了 1 个已知无关的预先存在失败之外全部通过。

- [ ] **Step 8: 提交**

```bash
git add app/config/settings.py app/api/deps.py app/api/agent_routes.py .env.example tests/api/test_deps.py
git commit -m "feat: wire banned_terms from Settings into agent_routes"
```

---

### Task 3: 输出侧内部数据泄露检测

**Files:**
- Create: `app/safety/leakage_detection.py`
- Modify: `app/safety/semantic_review.py`
- Modify: `app/agent/graph.py`（`output_safety_node` 函数，约在 423-451 行区域）
- Test: `tests/safety/test_leakage_detection.py`（新文件）
- Test: `tests/safety/test_semantic_review.py`
- Test: `tests/agent/test_graph.py`

**Interfaces:**
- Consumes：Task 1 的 `app.safety.rules.check_text`（已存在，不用改）。
- Produces：`app/safety/leakage_detection.py::detect_internal_leakage(text: str) -> LeakageDetectionResult`，`LeakageDetectionResult` 是 `@dataclass(frozen=True)`，字段 `is_leaked: bool`、`matched_categories: list[str] = field(default_factory=list)`（结构和 `app/safety/prompt_injection.py::PromptInjectionResult` 一致）。这是本任务对外的唯一新接口，Task 4 会在 `app/qa/answer.py` 里同样调用它。

- [ ] **Step 1: 写失败测试（新文件）**

创建 `tests/safety/test_leakage_detection.py`：

```python
from app.safety.leakage_detection import detect_internal_leakage


def test_detects_python_stack_trace():
    text = (
        'Traceback (most recent call last):\n'
        '  File "app/graphrag/term_matcher.py", line 12, in match_terms'
    )
    result = detect_internal_leakage(text)

    assert result.is_leaked is True
    assert "stack_trace" in result.matched_categories


def test_detects_internal_file_path():
    result = detect_internal_leakage("报错发生在 app/graphrag/term_matcher.py 里")

    assert result.is_leaked is True
    assert "internal_file_path" in result.matched_categories


def test_detects_internal_env_var_name():
    result = detect_internal_leakage("请检查 CUSTOMER_RAG_LLM_API_KEY 是否配置正确")

    assert result.is_leaked is True
    assert "internal_env_var" in result.matched_categories


def test_detects_cypher_query_fragment():
    result = detect_internal_leakage("MATCH (t:Term) RETURN t")

    assert result.is_leaked is True
    assert "db_query_fragment" in result.matched_categories


def test_detects_sql_query_fragment():
    result = detect_internal_leakage("SELECT * FROM users WHERE id = 1")

    assert result.is_leaked is True
    assert "db_query_fragment" in result.matched_categories


def test_does_not_flag_normal_customer_support_reply():
    result = detect_internal_leakage("根据资料所述，重启路由器即可解决。")

    assert result.is_leaked is False
    assert result.matched_categories == []


def test_does_not_flag_chinese_create_account_instruction():
    # "创建"是正常客服业务用词，不应该被 db_query_fragment 规则误伤
    # （规则只匹配英文关键词 CREATE/SELECT/MATCH，不匹配中文）。
    result = detect_internal_leakage("请在设置页面创建一个新账号")

    assert result.is_leaked is False
    assert result.matched_categories == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/safety/test_leakage_detection.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.safety.leakage_detection'`），因为文件还不存在。

- [ ] **Step 3: 实现 `leakage_detection.py`**

创建 `app/safety/leakage_detection.py`：

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 只覆盖结构性特征明显、误报率低的内部信息泄露特征——刻意不做泛化的
# "密码/密钥/token"关键词匹配：中文客服问答场景里"密码"是高频正常业务
# 词（"请重置密码"、"密码至少8位"），这类关键词正则误报率会很高，交给
# semantic_safety_review 的语义审查层判断更合适。这和 prompt_injection.py
# "规则兜底、不追求完备"的设计取向一致。
_LEAKAGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "stack_trace",
        re.compile(r'Traceback \(most recent call last\)|File "[^"]+\.py", line \d+'),
    ),
    (
        "internal_file_path",
        re.compile(r"\bapp/[\w./]*\.py\b"),
    ),
    (
        "internal_env_var",
        re.compile(r"\bCUSTOMER_RAG_[A-Z_]+\b"),
    ),
    (
        "db_query_fragment",
        re.compile(r"\bMATCH\s*\(|\bCREATE\s*\(|\bSELECT\s+.+?\s+FROM\b", re.IGNORECASE),
    ),
]


@dataclass(frozen=True)
class LeakageDetectionResult:
    is_leaked: bool
    matched_categories: list[str] = field(default_factory=list)


def detect_internal_leakage(text: str) -> LeakageDetectionResult:
    """规则级检测输出文本里典型的内部数据泄露特征。

    是 output_safety_node 里 check_text 之外的并列规则层，命中即和
    check_text 一样短路拦截，不进入更贵的 semantic_safety_review。
    """
    matched = [name for name, pattern in _LEAKAGE_PATTERNS if pattern.search(text)]
    return LeakageDetectionResult(is_leaked=bool(matched), matched_categories=matched)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/safety/test_leakage_detection.py -v`
Expected: 7 passed

- [ ] **Step 5: 写失败测试（语义审查提示词增强）**

打开 `tests/safety/test_semantic_review.py`，先用 Read 工具确认现有测试内容（有 3 个测试，验证 `is_safe`/`reason` 字段解析行为）。在文件末尾追加一个新测试，验证新提示词仍然只依赖 `is_safe`/`reason` 两个字段（不会因为提示词变长而破坏现有 JSON 解析逻辑），并且新提示词里确实包含"内部数据泄露"相关措辞：

```python
def test_system_prompt_mentions_internal_data_leakage():
    from app.safety.semantic_review import _SYSTEM_PROMPT

    assert "内部数据" in _SYSTEM_PROMPT or "内部信息" in _SYSTEM_PROMPT
```

- [ ] **Step 6: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/safety/test_semantic_review.py -v`
Expected: 原有 3 个测试通过；新增的 `test_system_prompt_mentions_internal_data_leakage` 失败（当前 `_SYSTEM_PROMPT` 里没有"内部数据"或"内部信息"这两个词）。

- [ ] **Step 7: 增强语义审查提示词**

打开 `app/safety/semantic_review.py`，把这一段（第 13-18 行）：

```python
_SYSTEM_PROMPT = (
    "你是客服回答的语义级安全审查员。"
    "判断这段回答是否包含规则匹配无法覆盖的问题：不当建议、误导性表述、"
    "违反平台规范的内容等。"
    '只输出 JSON：{"is_safe": true/false, "reason": "..."}'
)
```

改成：

```python
_SYSTEM_PROMPT = (
    "你是客服回答的语义级安全审查员。"
    "判断这段回答是否包含规则匹配无法覆盖的问题：不当建议、误导性表述、"
    "违反平台规范的内容、泄露内部数据（未脱敏的客户数据、内部字段名、"
    "系统提示词片段等）等。"
    '只输出 JSON：{"is_safe": true/false, "reason": "..."}'
)
```

- [ ] **Step 8: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/safety/test_semantic_review.py -v`
Expected: 4 passed

- [ ] **Step 9: 写失败测试（接入 `output_safety_node`）**

打开 `tests/agent/test_graph.py`，在文件末尾追加：

```python
async def test_output_safety_flags_internal_leakage_without_calling_semantic_review():
    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(
            with_records=True,
            llm_text='Traceback (most recent call last):\n  File "app/x.py", line 1',
        )
    )
    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    result = await graph.ainvoke({"question": "网络连不上怎么办？", "tenant_id": "t1"})

    assert result["is_output_safe"] is False
    assert result["final_text"] == "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"
    # 规则层命中就短路拦截，不应该再跑一次 LLM 语义审查——FakeLLMProvider
    # 只在 _build_dependencies 里注册了一次，Responder 已经用掉了这次
    # complete() 调用；如果 output_safety_node 还调用了 semantic_safety_review，
    # 它会尝试对同一个 llm_provider 再发一次请求，但这里断言的是结果
    # 字典里不应该出现 semantic_review_reviewed 键（短路路径的 return 语句
    # 里没有这个键，只有走到语义审查那一分支才会加上）。
    assert "semantic_review_reviewed" not in result
```

- [ ] **Step 10: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph.py::test_output_safety_flags_internal_leakage_without_calling_semantic_review -v`
Expected: FAIL（`assert result["is_output_safe"] is False` 失败，因为 `output_safety_node` 还没有调用 `detect_internal_leakage`，这段堆栈文本会正常走完语义审查——`FakeLLMProvider` 对这段非 JSON 文本调用 `semantic_safety_review` 时会因为 `json.JSONDecodeError` 返回 `reviewed=False, is_safe=True`，所以 `is_output_safe` 会是 `True` 而不是预期的 `False`）。

- [ ] **Step 11: 接入 `output_safety_node`**

打开 `app/agent/graph.py`，在 import 区域（第 41-43 行区域，Task 1 已经把这几行改成了从 `rules.py` 导入 `UNSAFE_INPUT_MESSAGE`/`UNSAFE_OUTPUT_MESSAGE`），新增一行：

```python
from app.safety.leakage_detection import detect_internal_leakage
```

放在 `from app.safety.prompt_injection import ...` 和 `from app.safety.rules import ...` 之间（按字母序，`leakage_detection` 排在 `prompt_injection` 之前）：

```python
from app.safety.leakage_detection import detect_internal_leakage
from app.safety.prompt_injection import detect_prompt_injection, wrap_system_prompt
from app.safety.rules import UNSAFE_INPUT_MESSAGE, UNSAFE_OUTPUT_MESSAGE, check_text
from app.safety.semantic_review import semantic_safety_review
```

然后找到 `output_safety_node` 函数体（约 423-451 行区域），当前是：

```python
    async def output_safety_node(state: AgentState) -> dict[str, Any]:
        if not state.get("is_input_safe", True):
            return {"is_output_safe": True, "final_text": UNSAFE_INPUT_MESSAGE}
        answer = state.get("answer_text", "")
        result = check_text(answer, banned_terms=banned_terms)
        if not result.is_safe:
            return {"is_output_safe": False, "final_text": UNSAFE_OUTPUT_MESSAGE}

        if state.get("fallback_triggered"):
            # 兜底话术是固定文案，不含 LLM 生成内容，跳过语义审查节省一次
            # 无意义的 LLM 调用。
            return {"is_output_safe": True, "final_text": answer}

        semantic_result = await semantic_safety_review(
            answer,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        if semantic_result.reviewed and not semantic_result.is_safe:
            return {
                "is_output_safe": False,
                "final_text": UNSAFE_OUTPUT_MESSAGE,
                "semantic_review_reviewed": True,
            }
        return {
            "is_output_safe": True,
            "final_text": answer,
            "semantic_review_reviewed": semantic_result.reviewed,
        }
```

（注意：上面这段里的 `UNSAFE_INPUT_MESSAGE`/`UNSAFE_OUTPUT_MESSAGE` 已经是 Task 1 改完之后的样子；如果你在这一步看到的仍然是带下划线前缀的 `_UNSAFE_INPUT_MESSAGE`/`_UNSAFE_OUTPUT_MESSAGE`，说明 Task 1 还没执行或者没执行完整，需要先确认 Task 1 已经完成再继续。）

改成（在 `check_text` 判断之后，`fallback_triggered` 判断之前，插入 `detect_internal_leakage` 检查）：

```python
    async def output_safety_node(state: AgentState) -> dict[str, Any]:
        if not state.get("is_input_safe", True):
            return {"is_output_safe": True, "final_text": UNSAFE_INPUT_MESSAGE}
        answer = state.get("answer_text", "")
        result = check_text(answer, banned_terms=banned_terms)
        if not result.is_safe:
            return {"is_output_safe": False, "final_text": UNSAFE_OUTPUT_MESSAGE}
        leakage_result = detect_internal_leakage(answer)
        if leakage_result.is_leaked:
            return {"is_output_safe": False, "final_text": UNSAFE_OUTPUT_MESSAGE}

        if state.get("fallback_triggered"):
            # 兜底话术是固定文案，不含 LLM 生成内容，跳过语义审查节省一次
            # 无意义的 LLM 调用。
            return {"is_output_safe": True, "final_text": answer}

        semantic_result = await semantic_safety_review(
            answer,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        if semantic_result.reviewed and not semantic_result.is_safe:
            return {
                "is_output_safe": False,
                "final_text": UNSAFE_OUTPUT_MESSAGE,
                "semantic_review_reviewed": True,
            }
        return {
            "is_output_safe": True,
            "final_text": answer,
            "semantic_review_reviewed": semantic_result.reviewed,
        }
```

- [ ] **Step 12: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph.py -v`
Expected: 全部通过（含新增的 `test_output_safety_flags_internal_leakage_without_calling_semantic_review`）

- [ ] **Step 13: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了 1 个已知无关的预先存在失败之外全部通过。

- [ ] **Step 14: 提交**

```bash
git add app/safety/leakage_detection.py app/safety/semantic_review.py app/agent/graph.py tests/safety/test_leakage_detection.py tests/safety/test_semantic_review.py tests/agent/test_graph.py
git commit -m "feat: add internal-data-leakage detection to output safety layer"
```

---

### Task 4: `/qa` 端点接入输入/输出安全检查

**Files:**
- Modify: `app/qa/answer.py`
- Modify: `app/api/qa_routes.py`
- Test: `tests/qa/test_answer.py`

**Interfaces:**
- Consumes：Task 1 的 `app.safety.rules.check_text`/`UNSAFE_INPUT_MESSAGE`/`UNSAFE_OUTPUT_MESSAGE`；`app.safety.prompt_injection.detect_prompt_injection`（已存在，`answer.py` 之前没 import 过）；Task 2 的 `app.api.deps.parse_banned_terms`；Task 3 的 `app.safety.leakage_detection.detect_internal_leakage`；`app.safety.semantic_review.semantic_safety_review`（已存在，`answer.py` 之前没 import 过）。
- Produces：`answer_question()` 新增 `banned_terms: list[str] | None = None` 关键字参数（带默认值，向后兼容 `app/eval/runner.py` 等既有调用方，它们不传这个参数时行为完全不变）。`AnswerResult` 的字段结构不变。

- [ ] **Step 1: 写失败测试**

`answer_question()` 加上输出安全检查后，会在生成回答之后额外调用一次 `semantic_safety_review()`，这个函数内部也会通过同一个 `llm_registry` 再发一次 LLM 请求（语义审查请求）。现有测试文件里的 `FakeLLMProvider` 只记录"最后一次" `last_request`，这次改动会让它被语义审查那次调用覆盖，导致原有 2 个测试断言 prompt 内容的地方失败。所以这一步要把 `FakeLLMProvider` 改成记录"每一次"请求的列表，并同步把原有 2 个测试的断言改成检查"第一次"请求（也就是真正的问答生成请求，语义审查是第二次）——这不是弱化断言，`requests[0]` 和原来的 `last_request` 在改动前是同一个值，只是现在有了第二次调用，需要精确指向"哪一次"。

把 `tests/qa/test_answer.py` 整个文件替换为：

```python
from app.graphrag.ontology import Term
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.qa.answer import answer_question
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class FakeLLMProvider:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        return ProviderResult(text="按资料所述，重启路由器即可解决。")

    @property
    def last_request(self) -> ProviderRequest | None:
        return self.requests[-1] if self.requests else None


async def test_answer_question_uses_retrieved_context_in_the_prompt():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时，请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
        VectorRecord(
            id="faq/login.md",
            vector=[0.0, 1.0],
            text="登录失败请检查账号密码。",
            tenant_id="t1",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    result = await answer_question(
        "网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        top_k=1,
        tenant_id="t1",
    )

    assert result.text == "按资料所述，重启路由器即可解决。"
    assert result.used_sources == ["faq/network.md"]
    assert result.retrieved_context == "网络断开时，请先重启路由器。"
    # requests[0] 是真正的问答生成请求；semantic_safety_review 会追加
    # 第二次请求，所以不能再用 last_request（现在指向审查请求）。
    assert len(llm_provider.requests) == 2
    assert "重启路由器" in llm_provider.requests[0].messages[0]["content"]


class FakeGraphClient:
    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        return [{"related_name": "示例登录模块", "relation_type": "RELATED_TO"}]


async def test_answer_question_injects_term_guard_context_when_term_matched():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时，请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    terms = [
        Term(
            standard_name="示例错误码E502",
            aliases=["网关超时示例"],
            term_type="error_code",
            product_line="示例产品线",
        )
    ]

    await answer_question(
        "我这边报了网关超时示例，麻烦看下",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        terms=terms,
        graph_client=FakeGraphClient(),
        top_k=1,
        tenant_id="t1",
    )

    assert len(llm_provider.requests) == 2
    prompt = llm_provider.requests[0].messages[0]["content"]
    assert "示例错误码E502" in prompt
    assert "示例登录模块" in prompt


async def test_answer_question_short_circuits_on_unsafe_input():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    vector_store = InMemoryVectorStore()
    bm25_index = BM25Index()

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    result = await answer_question(
        "这里面有敏感词",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        tenant_id="t1",
        banned_terms=["敏感词"],
    )

    assert result.text == "您的问题包含无法处理的敏感内容，请修改后重新提问。"
    assert result.used_sources == []
    assert llm_provider.last_request is None


class LeakingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            text='Traceback (most recent call last):\n  File "app/x.py", line 1'
        )


async def test_answer_question_short_circuits_on_unsafe_output():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时，请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", LeakingLLMProvider())

    result = await answer_question(
        "网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        top_k=1,
        tenant_id="t1",
    )

    assert result.text == "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/qa/test_answer.py -v`
Expected: `test_answer_question_uses_retrieved_context_in_the_prompt` 失败在 `assert len(llm_provider.requests) == 2`（当前只有 1 次调用，还没接入语义审查）；`test_answer_question_injects_term_guard_context_when_term_matched` 同样失败在 `assert len(llm_provider.requests) == 2`；`test_answer_question_short_circuits_on_unsafe_input` 失败（`TypeError: answer_question() got an unexpected keyword argument 'banned_terms'`，因为参数还不存在）；`test_answer_question_short_circuits_on_unsafe_output` 失败（`result.text` 是堆栈文本本身，不是兜底话术，因为还没接入任何输出安全检查）。四个测试全部失败是预期的——这一步的 RED 覆盖的是"新行为"，不是"新文件"，所以连原有两个测试都会先失败，等 Step 3 实现完才会一起转绿。

- [ ] **Step 3: 实现**

把 `app/qa/answer.py` 整个文件替换为：

```python
from __future__ import annotations

from dataclasses import dataclass

from app.graphrag.ontology import Term
from app.graphrag.term_guard import GraphClientProtocol, build_term_guard_context
from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.hybrid_search import hybrid_search
from app.retrieval.vector_store import VectorStore
from app.safety.leakage_detection import detect_internal_leakage
from app.safety.prompt_injection import detect_prompt_injection
from app.safety.rules import UNSAFE_INPUT_MESSAGE, UNSAFE_OUTPUT_MESSAGE, check_text
from app.safety.semantic_review import semantic_safety_review

_PROMPT_TEMPLATE = "根据以下资料回答问题。\n资料：\n{context}\n\n问题：{question}"


@dataclass(frozen=True)
class AnswerResult:
    text: str
    used_sources: list[str]
    retrieved_context: str


async def answer_question(
    question: str,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    tenant_id: str,
    rerank_provider: RerankProvider | None = None,
    query_rewrite_enabled: bool = True,
    terms: list[Term] | None = None,
    graph_client: GraphClientProtocol | None = None,
    top_k: int = 3,
    banned_terms: list[str] | None = None,
) -> AnswerResult:
    input_result = check_text(question, banned_terms=banned_terms)
    injection_result = detect_prompt_injection(question)
    if not input_result.is_safe or injection_result.is_suspicious:
        return AnswerResult(
            text=UNSAFE_INPUT_MESSAGE, used_sources=[], retrieved_context=""
        )

    term_guard_context: str | None = None
    if terms and graph_client is not None:
        term_guard_context = await build_term_guard_context(
            question, terms=terms, tenant_id=tenant_id, graph_client=graph_client
        )

    records = await hybrid_search(
        question,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        rerank_provider=rerank_provider,
        query_rewrite_enabled=query_rewrite_enabled,
        final_top_k=top_k,
        tenant_id=tenant_id,
    )
    retrieved_context = "\n\n".join(record.text for record in records)
    prompt_context = retrieved_context
    if term_guard_context:
        prompt_context = f"{term_guard_context}\n\n{retrieved_context}"

    prompt = _PROMPT_TEMPLATE.format(context=prompt_context, question=question)
    llm_result = await llm_registry.run(
        ProviderCapability.LLM,
        ProviderRequest(messages=[{"role": "user", "content": prompt}]),
        provider_name=llm_provider_name,
    )
    answer_text = llm_result.text

    output_result = check_text(answer_text, banned_terms=banned_terms)
    leakage_result = detect_internal_leakage(answer_text)
    if not output_result.is_safe or leakage_result.is_leaked:
        return AnswerResult(
            text=UNSAFE_OUTPUT_MESSAGE,
            used_sources=[record.id for record in records],
            retrieved_context=retrieved_context,
        )

    semantic_result = await semantic_safety_review(
        answer_text, llm_registry=llm_registry, llm_provider_name=llm_provider_name
    )
    if semantic_result.reviewed and not semantic_result.is_safe:
        return AnswerResult(
            text=UNSAFE_OUTPUT_MESSAGE,
            used_sources=[record.id for record in records],
            retrieved_context=retrieved_context,
        )

    return AnswerResult(
        text=answer_text,
        used_sources=[record.id for record in records],
        retrieved_context=retrieved_context,
    )
```

这段固定回答文本"按资料所述，重启路由器即可解决。"不含任何 PII/堆栈/内部路径特征，`check_text`/`detect_internal_leakage` 都不会命中，会正常走到 `semantic_safety_review`；`FakeLLMProvider.complete()` 第二次被调用时同样返回这段非 JSON 文本，`semantic_safety_review` 内部 `json.loads()` 会抛 `JSONDecodeError`，按现有实现返回 `reviewed=False, is_safe=True`（放行），最终 `result.text` 仍然是这段固定文本——Step 1 里两个原有测试的断言已经按这个行为改写好了，不需要在这一步再额外调整。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/qa/test_answer.py -v`
Expected: 4 passed（原有 2 个 + 新增 2 个）

- [ ] **Step 5: 接入 `app/api/qa_routes.py`**

打开 `app/api/qa_routes.py`，当前 `qa_endpoint` 函数体是：

```python
@router.post("/qa", response_model=QAResponse)
async def qa_endpoint(
    payload: QARequest,
    gateway_tenant_id: str | None = Depends(deps.get_gateway_tenant_id),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    bm25_index: BM25Index = Depends(deps.get_bm25_index),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    rerank_provider: RerankProvider | None = Depends(deps.get_rerank_provider),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    terms: list[Term] = Depends(deps.get_terms),
) -> QAResponse:
    tenant_id = deps.resolve_tenant_id(
        gateway_tenant_id, payload.tenant_id, source="qa"
    )
    result = await answer_question(
        payload.question,
        embedding_registry=embedding_registry,
        embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
        rerank_provider=rerank_provider,
        terms=terms,
        graph_client=graph_client,
        tenant_id=tenant_id,
    )
    return QAResponse(text=result.text, used_sources=result.used_sources)
```

改成（新增 `settings: Settings = Depends(deps.get_settings)` 依赖注入 + `banned_terms=deps.parse_banned_terms(settings.banned_terms)` 参数）：

```python
@router.post("/qa", response_model=QAResponse)
async def qa_endpoint(
    payload: QARequest,
    gateway_tenant_id: str | None = Depends(deps.get_gateway_tenant_id),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    bm25_index: BM25Index = Depends(deps.get_bm25_index),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    rerank_provider: RerankProvider | None = Depends(deps.get_rerank_provider),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    terms: list[Term] = Depends(deps.get_terms),
    settings: Settings = Depends(deps.get_settings),
) -> QAResponse:
    tenant_id = deps.resolve_tenant_id(
        gateway_tenant_id, payload.tenant_id, source="qa"
    )
    result = await answer_question(
        payload.question,
        embedding_registry=embedding_registry,
        embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
        rerank_provider=rerank_provider,
        terms=terms,
        graph_client=graph_client,
        tenant_id=tenant_id,
        banned_terms=deps.parse_banned_terms(settings.banned_terms),
    )
    return QAResponse(text=result.text, used_sources=result.used_sources)
```

还需要在文件顶部 import 区域新增 `from app.config.settings import Settings`（放在 `from app.api import deps` 之后，按字母序插入其余 import 之间）。

- [ ] **Step 6: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了 1 个已知无关的预先存在失败之外全部通过。`tests/api/test_qa_routes.py` 里的测试已经在 `app.dependency_overrides[deps.get_settings] = lambda: _settings()` 覆盖了这个依赖（构造 `qa_endpoint` 请求前的既有 fixture 模式），新增的 `settings: Settings = Depends(deps.get_settings)` 参数会自动拿到这个覆盖值，`_settings()` 默认不设置 `banned_terms`（等价于 `None`），预期不需要改动这个测试文件；如果实际跑起来有失败，再排查具体原因。

- [ ] **Step 7: 提交**

```bash
git add app/qa/answer.py app/api/qa_routes.py tests/qa/test_answer.py
git commit -m "feat: wire input/output safety checks into /qa endpoint"
```

---

## 完成后

任务全部提交后，输入侧 PII 规则从"只有手机号"扩展到身份证号/邮箱，自定义敏感词 `banned_terms` 首次真正在生产配置里生效；输出侧新增内部数据泄露规则检测（堆栈追踪/内部文件路径/内部环境变量/数据库查询片段）+ 语义审查提示词显式覆盖"内部数据泄露"判断维度；`/qa` 端点从"完全没有安全检查"变成和 `/agent/chat` 对齐的输入/输出双层安全网。架构覆盖度审计标记的这一项缺口解决。第 4 个独立子项目（GraphRAG 实体链接模糊匹配）待后续讨论是否要做，不在本计划范围内。
