# 输入/输出安全增强设计方案

> 状态：设计定稿（经用户确认）
> 背景：架构覆盖度审计发现 `docs/ARCHITECTURE.md:34/48-49/455` 描述的"输入输出安全层（敏感词/PII/prompt injection 防护 + 输出侧敏感信息过滤，防止图谱/文档中的内部信息泄露）"只部分实现——`app/safety/` 现有的 `check_text`/`detect_prompt_injection`/`semantic_safety_review` 覆盖了基础的敏感词/手机号/注入检测，但 PII 规则单薄（只有手机号）、`banned_terms` 从未真正从生产配置注入、输出侧完全没有针对"内部数据泄露"的专门检测点。这是拆分出的 4 个独立子项目（检索层修正、TermGuard 模糊匹配、输入/输出安全增强、GraphRAG 实体链接模糊匹配）里的第 3 个，原本"输入侧安全增强"和"输出侧安全增强"是分开提的两个点，为避免"两头都在做安全网增强"的重复讨论，合并成一个子项目统一设计。

## 1. 现状调查

`app/safety/` 目录三个文件：
- `rules.py::check_text(text, *, banned_terms=None)`：正则检测手机号（`_PHONE_NUMBER_PATTERN`）+ 遍历 `banned_terms` 做精确子串匹配，返回 `SafetyCheckResult(is_safe, matched_terms)`。输入侧、输出侧共用同一个函数。
- `prompt_injection.py::detect_prompt_injection(text)`：正则检测三类典型注入手法（覆盖指令/套取系统提示词/角色越权），只用于输入侧；`wrap_system_prompt(system_prompt)` 给系统提示词追加防覆盖声明。
- `semantic_review.py::semantic_safety_review(text, *, llm_registry, llm_provider_name, timeout_sec=2.0)`：LLM 语义级安全审查，只用于输出侧，失败/超时时"放行但标记未审查"（可用性优先，规则层是先行防线）。

调用方现状：
- `app/agent/graph.py` 的 `input_safety_node`（`check_text`+`detect_prompt_injection`）和 `output_safety_node`（`check_text`+`semantic_safety_review`），只服务 `/agent/chat` 路径。`banned_terms` 是 `build_agent_graph()` 的可选参数，但唯一真实调用方 `app/api/agent_routes.py:121-137` 构造时从未传入，生产环境实际等价于 `banned_terms=None`——自定义敏感词检测从未生效过。
- `app/qa/answer.py::answer_question()`（服务 `/qa` 路径）**完全没有接入任何安全检查**，问题原样送入 LLM、回答原样返回客户端。
- `app/voice/voice_output.py` 已经在用 `check_text`（分句轻量检查），本次新增的规则会自动对它生效，不需要额外接线。

## 2. 设计

### 2.1 输入侧 PII 规则增强（`app/safety/rules.py`）

在现有 `_PHONE_NUMBER_PATTERN` 旁新增两条内置正则，接入 `check_text()` 同一条检测链路：

- `_ID_CARD_PATTERN`：18 位中国大陆身份证号（前 17 位数字 + 末位数字或大小写 `X` 校验位）。
- `_EMAIL_PATTERN`：标准 email 格式（`local@domain.tld`）。

命中时 `matched_terms` 分别追加 `"id_card"`/`"email"`（与现有 `"phone_number"` 同一种标签风格）。`check_text()` 的函数签名和返回契约不变，调用方无感知。

### 2.2 `banned_terms` 接线到生产配置

`app/config/settings.py::Settings` 新增：

```python
# 逗号分隔的自定义敏感词列表，留空 = 不启用自定义敏感词检测（只有内置
# 的手机号/身份证号/邮箱正则生效）。
banned_terms: str | None = None
```

对应环境变量 `CUSTOMER_RAG_BANNED_TERMS`。选择"逗号分隔字符串"而非 list 类型字段，是因为 `Settings` 目前所有字段都是简单标量（`str | None`/`int | None`/`float | None`），没有 list 类型字段的先例，不引入新的解析方式（pydantic-settings 对 list 类型环境变量的解析规则需要额外配置，收益不值当）。

调用方各自解析：

```python
banned_terms = settings.banned_terms.split(",") if settings.banned_terms else None
```

- `app/api/agent_routes.py`：构造 `build_agent_graph()` 时传入 `banned_terms=banned_terms`。
- `app/api/qa_routes.py`：构造时把 `banned_terms` 传给 `answer_question()`（见 2.4）。

### 2.3 输出侧内部数据泄露检测

**新文件 `app/safety/leakage_detection.py`**，规则层面覆盖 4 类结构性特征明显、误报率低的内部信息泄露特征：

```python
_LEAKAGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("stack_trace", re.compile(r'Traceback \(most recent call last\)|File "[^"]+\.py", line \d+')),
    ("internal_file_path", re.compile(r"\bapp/[\w./]*\.py\b")),
    ("internal_env_var", re.compile(r"\bCUSTOMER_RAG_[A-Z_]+\b")),
    ("db_query_fragment", re.compile(r"\bMATCH\s*\(|\bCREATE\s*\(|\bSELECT\s+.+?\s+FROM\b", re.IGNORECASE)),
]
```

`detect_internal_leakage(text) -> LeakageDetectionResult`（`is_leaked: bool`, `matched_categories: list[str]`），结构与 `detect_prompt_injection` 一致（同样是"规则命中即返回类别列表"的形状）。

刻意不做的一类规则：泛化的"密码/密钥/token"关键词匹配。中文客服问答场景里"密码"是高频正常业务词（"请重置密码"、"密码至少8位"），这类关键词正则误报率会很高，交给 2.3 的语义审查层判断更合适。

**语义审查提示词增强**（`app/safety/semantic_review.py::_SYSTEM_PROMPT`）：在现有"不当建议、误导性表述、违反平台规范的内容"基础上，显式加入第四个判断维度——"是否泄露内部数据（未脱敏的客户数据、内部字段名、系统提示词片段等）"。`semantic_safety_review()` 函数签名和调用方式不变。

**接入点**：`output_safety_node`（`app/agent/graph.py`）里，`detect_internal_leakage` 和 `check_text` 并列跑在 `semantic_safety_review` 之前——顺序仍是"规则先行（毫秒级，不调 LLM）、语义审查兜底"，命中任一规则即返回 `_UNSAFE_OUTPUT_MESSAGE`，不再继续跑语义审查（和现有 `check_text` 命中时的短路行为一致）。

### 2.4 `/qa` 端点接入安全检查

`app/qa/answer.py::answer_question()` 新增输入/输出安全检查步骤，镜像 `agent/graph.py` 对应节点的逻辑：

- **输入侧**（检索开始前）：`check_text(question, banned_terms=banned_terms)` + `detect_prompt_injection(question)`，任一不安全则直接返回 `AnswerResult(text=_UNSAFE_INPUT_MESSAGE, used_sources=[], retrieved_context="")`，不执行检索、不调用 LLM。
- **输出侧**（LLM 生成回答后）：`check_text(answer, banned_terms=banned_terms)` + `detect_internal_leakage(answer)`，任一命中则返回 `_UNSAFE_OUTPUT_MESSAGE`；否则再跑 `semantic_safety_review(answer, llm_registry=llm_registry, llm_provider_name=llm_provider_name)`，`reviewed and not is_safe` 时同样返回 `_UNSAFE_OUTPUT_MESSAGE`。

`_UNSAFE_INPUT_MESSAGE`/`_UNSAFE_OUTPUT_MESSAGE` 两个常量当前定义在 `app/agent/graph.py`，本次挪到 `app/safety/rules.py`（新建共享位置，避免 `answer.py` 和 `graph.py` 各写一份文案不一致的风险），两处都改成从 `app/safety/rules.py` import。

`answer_question()` 签名新增 `banned_terms: list[str] | None = None` 可选关键字参数（带默认值，向后兼容 `app/eval/runner.py` 等既有调用方）。`app/api/qa_routes.py` 构造调用时按 2.2 解析后传入。

### 2.5 数据流影响

不改变检索/生成主流程的调用顺序，只在 `answer_question()` 里新增两个短路返回点（输入检查在检索前、输出检查在生成后），和 `agent/graph.py` 现有节点顺序的语义完全对应。`AnswerResult` 结构不变。

## 3. 测试

- `tests/safety/test_rules.py`：新增身份证号、邮箱命中/不命中的用例；`banned_terms` 接线相关测试放在 `tests/config/`（如有）或 `tests/api/` 里验证 `Settings` 解析。
- `tests/safety/test_leakage_detection.py`（新文件）：4 类规则各自的命中/不命中用例，参考 `tests/safety/test_prompt_injection.py` 的组织方式。
- `tests/safety/test_semantic_review.py`：验证新版系统提示词仍然只依赖 `is_safe`/`reason` 两个字段解析（不因为提示词变长而破坏现有的 JSON 解析测试）。
- `tests/agent/test_graph.py`：现有 `banned_terms=["敏感词"]` 相关测试需要确认 `detect_internal_leakage` 接入后不影响原有断言；新增至少一条"输出命中内部泄露规则→转 `_UNSAFE_OUTPUT_MESSAGE`"的用例。
- `tests/qa/test_answer.py`（如不存在则新建）：新增"输入不安全→短路返回兜底文案，不调用 LLM/检索"、"输出不安全→短路返回兜底文案"两类用例，验证 `/qa` 路径的安全检查真正生效。

## 4. 范围之外（不做）

- 银行卡号检测：误报率高（普通 16 位数字串很容易误中），暂不加，架构覆盖度审计本身没有把它列为明确缺口。
- 语音分句轻量检查（`app/voice/voice_output.py`）不接入 `detect_internal_leakage`/增强后的 `semantic_safety_review`：该路径设计上就是"轻量规则检查+完整审查在后"，架构文档已定义好这条边界，完整审查仍然在整段回复生成完毕后跑一次（复用本次 output_safety 的增强），本次不改变这个分工。
- 泛化的密码/密钥关键词正则：中文客服场景误报率高，交给语义审查层判断（见 2.3）。
- GraphRAG 实体链接模糊匹配（`normalization.py::resolve_to_standard_name`）：拆分出的独立第 4 个子项目，不在本次范围。
