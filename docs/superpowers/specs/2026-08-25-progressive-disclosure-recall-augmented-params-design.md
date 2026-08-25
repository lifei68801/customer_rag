# Planner 工具调用渐进式披露 + 召回增强参数生成设计

## 背景

2026-08-25 复现"coke-cola公司有多少个订单"时确认：查询引擎/工具 schema 本身没问题（手工构造正确参数直接算得出结果），真正失败的是 **LLM 自己构造工具调用参数这一步不可靠**——它要在完全没有任何"这个 tenant 实际有哪些术语/关系/字段"参照的情况下，凭 system prompt 里的静态文字描述，一次性猜出 `anchor.term_type`、`constraints.hops` 里每一跳的 `relation_type`/`direction`/`target_term_type`、`target_field`、`target_value` 这些必须跟本体/图谱里真实存在的名字对上号的字段。猜错了只能等下一轮工具结果反馈回来再纠正，多跳关系要同时猜对好几个方向，试错成本比单跳高得多，很容易在纠正过来之前就把 `max_tool_call_rounds` 耗尽。

`app/graphrag/structured_filter_query.py` 已经有的 fuzzy constraint value resolution（2026-08-24 上线）能在执行前把 `standard_name` 字段的 `eq`/`ne` 值再解析一次，但那是**执行时的最后一道校验**，只能处理"生成的值文本本身能在术语表里精确命中"的情况——它解决不了"LLM 压根不知道这个 tenant 有哪些 term_type/relation_type/字段名，只能瞎猜"这个更早、更根本的问题。

这份设计要解决的正是这个更早的问题：**让 LLM 在生成参数之前，先看到跟这次问题相关的、真实存在于这个 tenant 本体里的候选名字**，把"凭空猜"变成"从候选里挑"。

设计过程中参照了 Claude Code 自己的 Skill 渐进式披露机制做过一轮调研（结论见"架构"一节）：Skill 机制能做到"先只看名字+描述、需要时再展开细节"，前提是"用哪个技能"本身不需要结构化参数——`tools` 数组里永远只有一个 schema 极简且从不变化的 `Skill` 工具，每个技能的详细说明书是作为**普通对话内容**（工具执行结果）追加进历史的，不是塞进某个工具自己的 `parameters` schema 里。`structured_filter_query_tool` 有 `anchor`/`constraints.hops` 这些必须跟本体对得上号的结构化字段，天然不是"选择题"，Skill 机制本身没有直接回答"这种真正需要结构化参数的工具该怎么渐进式披露"——这份设计借用它的核心原理（详细能力说明放进稳定的、可复用的对话内容里，`tools` 参数本身保持极简且永不改变），但为 `structured_filter_query_tool` 的参数生成单独设计了一次独立调用。

## 目标

- 把"要不要调工具、调哪个、大致想查什么"和"这个工具的参数具体填什么结构化字段"拆成两次独立的 LLM 决策，避免让模型在同一次生成里既要判断意图又要精确对齐本体里的字段名/关系方向。
- 针对 `structured_filter_query_tool`，在"填参数"这一步之前，基于用户意图对本体做一次召回，把候选的 term_type、relation_type 三元组（带方向）、字段名、实体名字喂给 LLM 参考，而不是让它凭空生成。
- **这个拆分不能以破坏 KV cache 为代价**——`tools` 请求参数从会话第一次调用起就必须是最终形态，字节级别永不改变。这是这份设计除了"降低猜参数出错率"之外同等重要的第二个目标。
- 不引入新的基础设施依赖（不新建 embedding 索引、不新增本设计范围之外的额外 LLM 调用）。
- 跟已有的 `resolve_term()`/fuzzy constraint value resolution/`anchor.name` 消歧机制不冲突、互补共存，形成"生成前召回降低出错概率 → 执行前再校验一次 → 全部失败也有体面收场"的三层防御。

## 架构

### 工具 schema 的永久简化

主流 OpenAI-compatible 协议（包括这个仓库实际用的 DeepSeek，`app/providers/openai_compatible.py`）的 KV cache 命中判定是分层的——`tools` 参数排在最前面（比 system prompt 还靠前），`tools` 数组只要有任何差异，这次请求的缓存就从最开头整体失效，之前积累的对话历史缓存全部作废（业界通用行为，不是这个仓库特有的限制）。所以这份设计**不通过"给不同阶段传不同的 `tools`"来实现渐进式披露**，而是把 `structured_filter_query_tool` 在 `tools[]` 里的 `parameters` **永久性地大幅简化**，从第一次调用起就是这个样子，不再变化：

```python
STRUCTURED_FILTER_QUERY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "structured_filter_query_tool",
        "description": "在知识图谱里查询/筛选实体——具体能力和使用方式见对话上文的说明。",
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

今天 `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 里那一整套"anchor.name 怎么用、anchor.term_type+constraints 怎么组合、constraints.hops 最多几跳、matched_count 语义"的详细能力说明，从工具自己的 `description`/`parameters` 里搬出来，作为**稳定、每轮都在的纯文本**并入 `_PLANNER_SYSTEM_PROMPT`（`app/agent/graph.py:76-90`）——这样模型在决定要不要调用、以及写 `query_intent` 的时候，依然能看到完整能力说明，可以尽量写得精确（比如已经知道要查两跳关系，可以在 `query_intent` 里直接说清楚"通过订单号-产品-公司两跳关系筛选"），只是不需要在这次生成里同时产出结构化 JSON 字段。

`vector_search_tool` 的 schema **不变**（它本来就只有一个 `query` 字符串，没有需要简化的复杂度）。

**这不是"阶段1/阶段2用不同 tools"，是"这个工具的 schema 永久变简单了"**——从会话第一次调用到最后一次，`tools[]` 逐字节保持一致，不存在任何一次调用需要传跟其他调用不一样的版本，KV cache 约束因此被彻底满足，不需要"探测调用不落历史"这类额外机制。

### 参数解析分发器

对应用户要求的"方法里面可以根据识别到不同工具，调用对应的参数解析方法"——`app/agent/planner.py` 内新增一个按工具名分发的函数，跟今天已有的 `_dispatch_tool_call`（按工具名分发**执行**）是平行关系，发生在 `_dispatch_tool_call` 之前：

```python
async def _resolve_tool_arguments(
    tool_name: str, raw_arguments: dict[str, Any], *,
    terms: list[Term], term_type_schema: dict[str, TermTypeCategory],
    confirmed_relation_types: set[str],
    llm_registry: ProviderRegistry, llm_provider_name: str,
) -> dict[str, Any]:
    """按工具名分发到对应的参数解析方法，返回这个工具调用最终会被执行使用
    的参数字典。raw_arguments 是这一轮 ReAct 推理调用里模型自己产出的原始
    参数（vector_search_tool 是 {"query": ...}，structured_filter_query_tool
    是 {"query_intent": ...}）。"""
    if tool_name == "vector_search_tool":
        return raw_arguments  # 原样透传，不发起任何额外调用
    if tool_name == "structured_filter_query_tool":
        return await _resolve_structured_filter_query_arguments(
            raw_arguments.get("query_intent", ""),
            terms=terms, term_type_schema=term_type_schema,
            confirmed_relation_types=confirmed_relation_types,
            llm_registry=llm_registry, llm_provider_name=llm_provider_name,
        )
    raise ValueError(f"未知工具: {tool_name}")
```

- **`vector_search_tool`**：零额外 LLM 调用，`raw_arguments` 本身就是可以直接执行的参数——这就是用户要求的"vector_tool没有入参[复杂度]则跳过参数解析"。
- **`structured_filter_query_tool`**：发起下面说的独立参数生成调用。

调用时机：这一轮 ReAct 推理调用产出的 `tool_calls`（含 `raw_arguments`）**正常追加进 `planner_messages`**（今天的行为不变，不需要"探测调用、丢弃"这类机制）——模型自己说的那句 `query_intent` 会原样留在对话历史里，供未来轮次参考；`_resolve_tool_arguments` 解析出的**真正用于执行**的参数不会回填替换历史里的 `query_intent`，执行结果（`tool` role 消息）才是下一轮模型真正会看到的新信息。

### 独立参数生成调用

只有 `structured_filter_query_tool` 才会触发，`_resolve_structured_filter_query_arguments` 内部完成：

1. **召回**：以 `query_intent` 文本为 query，对本体做一次召回（见下节），得到候选 term_type/relation 三元组/字段名/实体名。
2. **调用**：发起一次**独立、自包含、不走 function-calling 协议**的 LLM 调用——不带 `tools`/`tool_choice`，只有一条 prompt：`structured_filter_query_tool` 完整参数 schema 的文字说明（今天 `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 那份详细描述）+ 上面召回到的候选参考 + `query_intent` 原文，要求模型直接输出一段匹配这个 schema 的 JSON。**不携带这一轮之前的对话历史**——`query_intent` 已经要求做过指代消解、本身自包含，召回候选也是当次现算的新鲜数据，不依赖历史上下文；不带历史让这次调用更聚焦，也不需要为它单独设计历史截断/传递机制。
3. **解析**：返回的 JSON 字符串复用今天已有的 `parse_structured_filter_query_args` 做形状校验（同一份校验代码，不新增校验逻辑）——解析失败的处理方式见"错误处理"。

这次独立调用因为每次都带着（随 `query_intent`/召回结果变化的）不同候选参考，本身不具备被复用的 KV cache 前缀——这是recall-augmented生成的固有代价，不是这个机制选择造成的，不需要额外优化。

### 承认的代价

`structured_filter_query_tool` 每次被调用，**固定需要2次 LLM 调用**（ReAct 推理决策 1 次 + 独立参数生成 1 次），不再有"模型自己很确定、一次调用就能把结构化参数填对"的快速路径——今天单次调用能做到的这件事，这份设计里永远要付出2次调用的延迟/成本。这是为了用"query_intent 只是自然语言，几乎不会填错"换掉"结构化字段容易猜错、猜错了要等下一轮才能纠正、且纠正本身也可能猜错方向"这个更贵的失败模式，本次设计明确选择正确率优先于单次调用延迟。

### 召回机制

四类候选统一走同一套召回逻辑，不区分"小池子直接全给"和"大池子才召回"——小池子（term_type/relation 三元组/字段名，demo 租户实测几十条量级）本来就会在召回时把自己基本全部召回回来，不需要单独分支处理。

**候选来源**（每次独立参数生成调用从当次已经加载的数据现算，不做进程级缓存——`terms`/`term_type_schema`/`confirmed_relation_types` 本来就是 `app/api/agent_routes.py` 每次请求都重新 `list_terms`/`list_term_types`/`list_relation_types` 加载的新鲜数据，直接在这份数据上建候选索引是纯 CPU 计算，不会因为审核流程刚确认的新术语/新关系类型而召回不到，也不需要给任何写路径接失效钩子）：

- **term_type 候选**：`term_type_schema` 里每个已确认类型的名字
- **relation 候选**：`term_type_relation_allowlist` 表里每一条已确认的 `(subject_term_type, relation_type, object_term_type)` 三元组，召回时以完整三元组形式出现（比如"订单号 --BELONG_TO--> 产品"），不是只召回 `relation_type` 这个孤立字符串——这样 LLM 能直接读出方向，不需要自己再猜
- **字段名候选**：`term_type_schema` 里每个类型声明的 `extra_fields` 字段名
- **实体名候选**：`terms` 表里每条术语的 `standard_name`（连同它的 `term_type`，让 LLM 知道召回到的这个名字属于哪个类型）

**召回算法**：

1. 召回 query 文本 = `query_intent`（理论上必填字段不该为空；防御性地，为空/空白时回退用原始用户问题）
2. 把 query 按 token 边界切成 1~4 词长的滑动窗口 n-gram（中英文混合分词：英文按 `[a-z0-9_]+` 整段切，中文按字切，复用 `app/retrieval/bm25.py` 里 `_TOKEN_PATTERN` 现成的正则规则，不用重新发明）
3. 对每个 n-gram 和每个候选名字，计算**最长公共连续子串**长度（大小写不敏感），除以候选名字长度得到 0~1 的归一化分数
4. 分数 ≥ 阈值（0.3，重叠长度至少2个字符，避免单字符/极短噪声匹配）的候选保留，按分数降序排列，每类候选各截断到 Top-K（K 的具体数值留给实现计划确定，实体名候选池子最大，K 应该比 term_type/relation/字段名候选的 K 更保守）
5. 不引入 embedding、不引入额外 LLM 调用——整个召回过程是纯字符串比较，不调用任何 provider

**召回结果不保证100%命中**：如果一个候选完全召不到（比如极端拼写偏差），这层设计不做任何特殊处理——继续依赖后面两层防御（fuzzy constraint value resolution 在执行前的最后一次校验、以及轮次耗尽时的体面收场）。

## 数据流（走一遍复现场景）

"coke-cola公司有多少个订单"：

1. ReAct 推理调用（`tools` 是今天简化后的固定版本，system prompt 已包含完整能力说明）：LLM 决定调用 `structured_filter_query_tool`，产出 `{"query_intent": "查询公司标准名为Coca-Cola的订单数量，需要通过订单号-产品-公司两跳BELONG_TO关系筛选"}`——这次调用（含这段文字，会流式展示给用户/进入 reasoningTrail）正常追加进 `planner_messages`。
2. 分发：`_resolve_tool_arguments("structured_filter_query_tool", {"query_intent": "..."}, ...)` 命中独立参数生成分支。
3. 召回：query = 上面那句 `query_intent`，切出 n-gram，跟四类候选比对：
   - term_type 候选命中"公司"（精确匹配）、"订单号"（"订单"是最长公共子串）
   - relation 三元组候选命中"产品 --BELONG_TO--> 公司"、"订单号 --BELONG_TO--> 产品"（完整三元组形式，即使 query 里没提"产品"，三元组文本本身包含"公司"/"订单号"也会被召回）
   - 实体名候选命中"Coca-Cola"（精确匹配标准名）
4. 独立参数生成调用（不带 `tools`、不带历史，只有 schema 说明+召回候选+`query_intent`）：产出正确的两跳 `constraints`，能直接看到"订单号 --BELONG_TO--> 产品"和"产品 --BELONG_TO--> 公司"这两条方向明确的三元组，不需要自己猜方向。
5. `parse_structured_filter_query_args` 校验通过；执行前 `_resolve_fuzzy_constraint_values`（已上线）再对 `target_value` 做一次最终校验/解析。
6. 执行、返回结果——这次一轮（2次 LLM 调用，固定成本）应该能拿到正确参数，不需要像复现时那样反复试错到轮次耗尽。

## 错误处理

- 召回本身不会失败（纯本地字符串计算，没有网络调用），唯一的"失败"是召回不到任何候选——不做特殊处理，独立参数生成调用照常执行（只是拿不到候选参考，退化成"没有召回增强"的效果），后两层防御继续兜底。
- 独立参数生成调用本身失败（网络错误、provider 报错）——降级成这个工具调用的错误观察结果（`{"error": "..."}`），跟 `run_structured_filter_query` 今天对其他内部异常的降级方式一致，不会让整个 Planner 轮次崩溃，正常消耗这一轮的工具调用预算。
- 独立参数生成调用返回的文本不是合法 JSON，或合法 JSON 但形状不对——`parse_structured_filter_query_args` 已有的校验会捕获并返回结构化错误，跟今天"LLM 直接产出错误参数"时的处理路径完全一致，不需要新增校验逻辑。
- `vector_search_tool` 因为不发起额外调用，没有这一层新的失败模式。

## 测试

- **KV cache 兼容性**：验证 `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 从模块加载起就是简化后的最终形态（`parameters` 只有 `query_intent`），不存在任何"运行时再简化一次"的代码路径；验证多轮调用之间传给 provider 的 `tools` 参数逐字节相等。
- 参数解析分发器：验证 `_resolve_tool_arguments("vector_search_tool", ...)` 不发起任何 LLM 调用，原样返回 `raw_arguments`；验证 `_resolve_tool_arguments("structured_filter_query_tool", ...)` 发起了一次不带 `tools` 的独立调用；验证未知工具名抛出异常。
- 独立参数生成调用：验证这次调用的请求里没有 `tools`/`tool_choice` 字段；验证请求的 `messages` 只包含 schema 说明+召回候选+`query_intent`，不包含本轮之前的对话历史。
- 召回算法（`longest_common_substring_score` 及切 n-gram 的逻辑）：纯函数单元测试，覆盖复现场景里的具体案例（"coke-cola" vs "Cola"/"Coca-Cola" 打分）、大小写不敏感、阈值截断、Top-K 截断。
- relation 三元组候选：验证召回结果确实是完整三元组（带方向），不是裸的 `relation_type` 字符串。
- `query_intent` 为空时的回退：验证召回 query 回退到原始用户问题。
- 端到端：用本轮复现的真实 tenant 数据（或等价的测试 fixture）构造"coke-cola公司有多少个订单"场景，验证召回候选里确实包含正确的两条 relation 三元组和 Cola/Coca-Cola 相关实体候选，验证独立参数生成调用最终产出的参数能正确执行。
- 历史持久化：验证 ReAct 推理调用产出的 `query_intent` 原样出现在 `planner_messages` 里（不被替换成解析后的结构化参数）。

## Non-Goals

- 不引入 embedding 索引或语义检索——现阶段 aliases 数据本身大多为空，语义层面的收益无法验证，且会引入新的基础设施/成本/延迟。
- 不新增独立配置开关——这次改动本身不引入比今天更差的失败模式（召回不到/独立调用失败都有明确降级路径），不需要开关快速关掉。
- 不改动查询引擎（`app/graphrag/neo4j_client.py`）、不改动五层白名单校验（`validate_structured_filter_query`）——这层设计只影响"生成参数之前给 LLM 看什么参考信息、通过几次调用生成"，不影响参数生成之后的校验/执行逻辑。
- 不做召回结果的持久化缓存——每次现算，避免过期问题，代价是每次独立参数生成调用多一次纯 CPU 的字符串比较开销，认为这个代价可以接受。
- 不保留"模型很确定时一次调用搞定"的快速路径——`structured_filter_query_tool` 固定走2次调用，见"承认的代价"一节，这是本次设计明确接受的取舍，不是遗漏。

## Global Constraints

- **`STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 的 `parameters` 永久只有 `query_intent` 一个字段，从会话第一次调用起就是这个形态**——不允许存在任何"运行时根据阶段切换 schema"的代码路径。今天那份详细能力说明搬进 `_PLANNER_SYSTEM_PROMPT`（常驻、稳定），不再放在工具自己的 `description`/`parameters` 里。
- `vector_search_tool` 的 schema 不变。
- `tools` 请求参数在整个会话生命周期内、每一次 ReAct 推理调用里必须逐字节保持一致——这是这份设计能兼容 KV cache 的硬约束。
- 独立参数生成调用**不使用 function-calling 协议**（不带 `tools`/`tool_choice`），**不携带本轮之前的对话历史**（只有 schema 说明+召回候选+`query_intent`）。
- 参数解析按工具名分发（`_resolve_tool_arguments`，`app/agent/planner.py`，跟已有的 `_dispatch_tool_call` 是平行关系，发生在它之前）：`vector_search_tool` 零额外调用直接透传；`structured_filter_query_tool` 触发独立参数生成调用。
- ReAct 推理调用产出的原始 `tool_calls`（含 `query_intent`）正常追加进 `planner_messages`，不做任何丢弃/替换——独立参数生成调用解析出的真正执行参数只用于 `_dispatch_tool_call` 的实际执行，不回填覆盖历史里模型自己说的 `query_intent`。
- 召回机制的调用入口封装在 `app/agent/planner.py` 内；不改 `app/agent/graph.py` 的图结构/路由。
- 召回算法本身不依赖任何外部服务调用（不调 embedding provider、不调 LLM）——必须是纯本地计算。
- 召回索引每次独立参数生成调用基于当次已加载的 `terms`/`term_type_schema`/`confirmed_relation_types` 现算，不做进程级缓存。
- relation 候选必须以完整 `(subject_term_type, relation_type, object_term_type)` 三元组形式出现在召回结果里，不能只召回 `relation_type` 字符串本身。
- `max_tool_call_rounds` 默认值不变，语义仍然是"回合数"而非"LLM 调用次数"。
- 不改动 `structured_filter_query.py` 里已有的解析/校验/fuzzy resolution 逻辑本身。
