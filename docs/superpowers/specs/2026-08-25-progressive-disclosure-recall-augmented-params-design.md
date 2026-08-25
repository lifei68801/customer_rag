# Planner 工具调用渐进式披露 + 召回增强参数生成设计

## 背景

2026-08-25 复现"coke-cola公司有多少个订单"时确认：查询引擎/工具 schema 本身没问题（手工构造正确参数直接算得出结果），真正失败的是 **LLM 自己构造工具调用参数这一步不可靠**——它要在完全没有任何"这个 tenant 实际有哪些术语/关系/字段"参照的情况下，凭 system prompt 里的静态文字描述，一次性猜出 `anchor.term_type`、`constraints.hops` 里每一跳的 `relation_type`/`direction`/`target_term_type`、`target_field`、`target_value` 这些必须跟本体/图谱里真实存在的名字对上号的字段。猜错了只能等下一轮工具结果反馈回来再纠正，多跳关系要同时猜对好几个方向，试错成本比单跳高得多，很容易在纠正过来之前就把 `max_tool_call_rounds` 耗尽。

`app/graphrag/structured_filter_query.py` 已经有的 fuzzy constraint value resolution（2026-08-24 上线）能在执行前把 `standard_name` 字段的 `eq`/`ne` 值再解析一次，但那是**执行时的最后一道校验**，只能处理"生成的值文本本身能在术语表里精确命中"的情况——它解决不了"LLM 压根不知道这个 tenant 有哪些 term_type/relation_type/字段名，只能瞎猜"这个更早、更根本的问题。

这份设计要解决的正是这个更早的问题：**让 LLM 在生成参数之前，先看到跟这次问题相关的、真实存在于这个 tenant 本体里的候选名字**，把"凭空猜"变成"从候选里挑"。

## 目标

- 把"要不要调工具、调哪个"和"这个工具的参数具体填什么"拆成两次独立的 LLM 决策，避免让模型在还没决定用哪个工具时就要面对完整参数 schema 的认知负担。
- 针对 `structured_filter_query_tool`，在"填参数"这一步之前，基于用户问题（及本轮已经了解到的上下文）对本体做一次召回，把候选的 term_type、relation_type 三元组（带方向）、字段名、实体名字喂给 LLM 参考，而不是让它凭空生成。
- **两阶段拆分不能以破坏 KV cache 为代价**——`tools` 请求参数在整个会话生命周期内必须保持字节级别不变，渐进式披露通过指令层（prompt 内容）实现，不通过在不同调用里传不同的 `tools`/`parameters` 实现。这是这份设计除了"降低猜参数出错率"之外同等重要的第二个目标，详见"两阶段工具调用"一节。
- 不引入新的基础设施依赖（不新建 embedding 索引、不新增 LLM 调用次数之外的额外调用）。
- 跟已有的 `resolve_term()`/fuzzy constraint value resolution/`anchor.name` 消歧机制不冲突、互补共存，形成"生成前召回降低出错概率 → 执行前再校验一次 → 全部失败也有体面收场"的三层防御。

## 架构

### 两阶段工具调用

对 `vector_search_tool` 和 `structured_filter_query_tool` 统一生效。

**先说清楚一个前提，这个前提决定了下面所有机制怎么设计**：主流 OpenAI-compatible 协议（包括这个仓库实际用的 DeepSeek）的 KV cache 命中判定是分层的——`tools` 参数排在最前面（比 system prompt 还靠前），`tools` 数组只要有任何差异（哪怕只是某个工具的 `parameters` 从完整 schema 换成空 schema），这次请求的缓存就从最开头整体失效，之前积累的对话历史缓存全部作废（这是业界通用行为，不是这个仓库特有的限制）。所以**渐进式披露不能通过"给阶段1传一份精简过的 `tools`、给阶段2传完整版"来实现**——那样做的代价是每一轮都双倍浪费缓存。

**这份设计改用的机制**：`tools` 参数（两个工具的完整 schema，跟今天 `_TOOL_SCHEMAS` 完全一样）**从会话第一次调用到结束，字节级别永远不变**。"只看名字和描述、还不用纠结参数"这件事，通过**指令内容**做到，不通过"隐藏 schema"做到——这跟 Claude Code 自己的技能披露机制原理是一致的：Claude Code 的 `tools` 里始终只有一个 `Skill` 工具、schema 从不变化，每个技能的详细说明书是作为**普通对话内容**（工具执行结果）追加进历史的，不是塞进 `tools` schema 里的。

**阶段1——工具选择（探测性调用，不写入持久化历史）**：

- 请求：跟今天完全一样的 `tools`（两个工具的完整 schema，不做任何精简）、`tool_choice: "auto"`，`messages` 末尾追加一条只属于这次探测调用的指令（内容见下一节 `_PLANNER_STAGE1_SYSTEM_PROMPT`）：大意是"这一步只需要判断要不要调用工具、调用哪个/哪些，参数字段可以先随便填占位值，不用纠结——真正的参数会在你做出选择之后单独引导你填"。
- LLM 在这一步可以：不调用任何工具、直接回答（保留今天的行为，这种情况下这次调用本身就是最终结果，直接按今天的路径处理，不进入阶段2）；或者请求调用一个或多个工具（保留今天 `run_tool_calls` 已经支持的并发执行能力）。
- **这次调用的响应不会被追加进 `planner_messages`**——只在内存里读取两样东西：① 请求了哪些工具（`tool_calls[].name`，`arguments` 字段整体丢弃，不管里面填了什么）；② 伴随的自然语言叙述文字（`ProviderResult.text`，就是今天已经存在的"让我查一下xxx"那段话）。因为这次调用没有落进历史，阶段2看到的对话前缀（`tools` + 基础系统消息 + 到目前为止的真实历史）跟阶段1看到的**完全一样**，只是各自在末尾追加了不同的一小段指令——这是能做到的最大限度前缀复用。

**阶段2——参数生成**（对阶段1识别出的每个工具，各自按 `tool_calls[].id` 独立执行一次，多个工具/多次同工具调用并发跑；对话前缀沿用阶段1看到的那份真实历史，不包含阶段1本身）：

- 请求：跟阶段1一样的 `tools`（不变）、`tool_choice` 强制指定成这一个工具（`{"type": "function", "function": {"name": "..."}}`，标准 OpenAI-compatible 机制，不需要改动 `tools` 本身），`messages` 末尾追加这个工具专属的指令+参考信息。
- **按工具名分发到对应的参数解析方法**（用户要求的"识别到不同工具、调用对应的参数解析方法"，具体设计见下面"参数解析分发器"一节）：`structured_filter_query_tool` 才真正发起这次阶段2调用（追加 `_PLANNER_STAGE2_SYSTEM_PROMPT` + 召回候选参考文本）；`vector_search_tool` 直接跳过阶段2，不发起任何额外调用。
- 这次调用产出的才是真正会被执行的参数——追加进 `planner_messages` 的 assistant 消息（`tool_calls`）用的是这一步的结果，不是阶段1那次被丢弃的探测结果。

**不改动 `app/agent/graph.py` 的图结构**：整个两阶段流程封装在 `app/agent/planner.py` 的 `run_planner_turn`/`run_planner_turn_streaming` 内部——一次"回合"内部从今天的 1 次 LLM 调用变成"1 次探测（不落历史）+ N 次参数生成（N = 阶段1选中且需要参数解析的工具调用数，`vector_search_tool` 不算在内）"，但对外产出的返回形状（`pending_tool_calls`/`planner_messages`）跟今天完全一致，`route_after_planner` 不需要改。

### 参数解析分发器

对应用户要求的"方法里面可以根据识别到不同工具，调用对应的参数解析方法"——在 `app/agent/planner.py` 内新增一个按工具名分发的函数，跟今天已有的 `_dispatch_tool_call`（按工具名分发**执行**）是平行关系，这个新函数分发的是**参数解析**，发生在 `_dispatch_tool_call` 之前：

```python
async def _resolve_tool_arguments(
    tool_name: str, *, narration_query: str, messages: list[dict[str, Any]],
    terms: list[Term], term_type_schema: dict[str, TermTypeCategory],
    confirmed_relation_types: set[str],
    llm_registry: ProviderRegistry, llm_provider_name: str,
) -> str:
    """按工具名分发到对应的参数解析方法，返回这个工具调用最终使用的
    JSON 参数字符串。narration_query 是阶段1产出的叙述文字（为空则是
    原始用户问题），messages 是阶段1看到的那份真实历史（不含阶段1本身）。
    """
    if tool_name == "vector_search_tool":
        return _resolve_vector_search_arguments(narration_query)
    if tool_name == "structured_filter_query_tool":
        return await _resolve_structured_filter_query_arguments(
            narration_query, messages=messages, terms=terms,
            term_type_schema=term_type_schema,
            confirmed_relation_types=confirmed_relation_types,
            llm_registry=llm_registry, llm_provider_name=llm_provider_name,
        )
    raise ValueError(f"未知工具: {tool_name}")
```

- **`_resolve_vector_search_arguments`**：不调用 LLM，纯函数，直接把 `narration_query` 包成这个工具需要的 `{"query": narration_query}` 返回——这就是用户要求的"vector_tool没有入参则跳过参数解析"。之所以直接复用叙述文字而不是信任阶段1自己填的 `query` 参数，是为了保持"阶段1的参数字段一律丢弃、不被任何工具信任"这条规则没有例外，简单、好验证。
- **`_resolve_structured_filter_query_arguments`**：发起上面说的阶段2调用（`tool_choice` 强制指定 + 召回候选参考），返回真正解析出的参数。

### 阶段提示词的拆分与位置

今天 `app/agent/graph.py:76-90` 的 `_PLANNER_SYSTEM_PROMPT` 是一份混在一起的提示词，既管"要不要调工具"又管"参数具体怎么填"，**里面没有任何一句话要求 LLM 在决定调用工具时顺带写一句自包含、已经做过指代消解的叙述文字**——今天模型会说"让我查一下xxx"，纯粹是对话式模型自己"边想边说"的自然倾向，不是被提示词工程刻意要求、有质量保证的输出。"阶段1叙述文字直接当召回 query 复用"这个设计能不能成立，完全取决于这段文字的质量，所以这一节明确写清楚提示词怎么拆、写在哪里、里面必须包含什么，不能留给实现阶段临场发挥。

**拆成两份新的提示词常量**，定义位置不变——还是 `app/agent/graph.py`，紧挨着 `_PLANNER_SYSTEM_PROMPT` 现在的位置（第76-90行附近），延续这个文件"提示词常量集中定义在模块顶部"的现有组织方式：

- **`_PLANNER_STAGE1_SYSTEM_PROMPT`**：负责"要不要调工具、调哪个"这部分决策指导（今天 `_PLANNER_SYSTEM_PROMPT` 里跟"选工具"相关的部分，比如两个工具各自是干什么用的概述），**新增两条明确要求**：① 这一步不用纠结参数具体怎么填，参数字段可以先填占位值，真正的参数会在下一步单独引导你填；② 如果决定调用工具，必须在回复文字里用一句完整、自包含的话说明打算查什么——把"它""这个""刚才那个"之类的指代词换成前面已经了解到的具体名字（比如结合前面工具结果里查到的信息，把"它关联的公司"写成"Cola关联的公司"）；这句话会被用来检索相关的术语和关系作为参考，写得越具体、越自包含，参考信息就越准。
- **`_PLANNER_STAGE2_SYSTEM_PROMPT`**：保留今天 `_PLANNER_SYSTEM_PROMPT` 里"anchor.term_type + constraints 模式怎么用""matched_count 语义"这些参数填写层面的指导（今天第78-89行大部分内容），供 `_resolve_structured_filter_query_arguments` 发起阶段2调用时使用。

**接入方式**：沿用 `term_guard_context` 现成的"追加一条 system 消息"模式（`app/agent/graph.py:637` 的 `messages.append({"role": "system", "content": term_guard_context})`）——基础系统消息（今天 `wrap_system_prompt(_PLANNER_SYSTEM_PROMPT)` 这条，内容收窄成跟工具选择/参数填写都无关的、纯客服助手身份/编造禁令那部分）保留在对话最前面不变且字节级别不变，阶段1的探测调用在（不落历史的）临时 `messages` 副本末尾追加 `_PLANNER_STAGE1_SYSTEM_PROMPT`，阶段2调用在真实历史末尾追加 `_PLANNER_STAGE2_SYSTEM_PROMPT`（`structured_filter_query_tool` 还要再追加召回候选那段文本）——两段提示词都是"追加"关系，不替换、不修改前面已经存在的任何消息。

**预算语义不变**：`max_tool_call_rounds`（默认3）仍然按"回合"计数，不改默认值——语义上还是"最多去图谱/向量库查几轮"，不是"最多几次原始 LLM 调用"。代价是实际 LLM 调用预算从"最多3次"变成"最多6次"（多工具并发时更高，但那部分是并发发生，不叠加轮次预算）。这跟已经写好、正在等你审阅的另一份 spec（`docs/superpowers/specs/2026-08-25-planner-graceful-budget-exhaustion-design.md`，轮次耗尽时的"最后陈述"兵底）不冲突——那份 spec 处理的是"即使这份设计上线后，某些场景依然会把轮次用完，该怎么收场"，跟这里"怎么降低轮次被浪费在猜错参数上"是两个独立、互补的问题。

### 召回机制

四类候选统一走同一套召回逻辑，不区分"小池子直接全给"和"大池子才召回"——小池子（term_type/relation 三元组/字段名，demo 租户实测几十条量级）本来就会在召回时把自己基本全部召回回来，不需要单独分支处理。

**候选来源**（每次 Planner 回合从当次已经加载的数据现算，不做进程级缓存——`terms`/`term_type_schema`/`confirmed_relation_types` 本来就是 `app/api/agent_routes.py` 每次请求都重新 `list_terms`/`list_term_types`/`list_relation_types` 加载的新鲜数据，直接在这份数据上建候选索引是纯 CPU 计算，不会因为审核流程刚确认的新术语/新关系类型而召回不到，也不需要给任何写路径接失效钩子）：

- **term_type 候选**：`term_type_schema` 里每个已确认类型的名字
- **relation 候选**：`term_type_relation_allowlist` 表里每一条已确认的 `(subject_term_type, relation_type, object_term_type)` 三元组，召回时以完整三元组形式出现（比如"订单号 --BELONG_TO--> 产品"），不是只召回 `relation_type` 这个孤立字符串——这样 LLM 能直接读出方向，不需要自己再猜
- **字段名候选**：`term_type_schema` 里每个类型声明的 `extra_fields` 字段名
- **实体名候选**：`terms` 表里每条术语的 `standard_name`（连同它的 `term_type`，让 LLM 知道召回到的这个名字属于哪个类型）

**召回算法**：

1. 召回 query 文本 = 阶段1产出的叙述文字（为空则用原始用户问题）
2. 把 query 按 token 边界切成 1~4 词长的滑动窗口 n-gram（中英文混合分词：英文按 `[a-z0-9_]+` 整段切，中文按字切，复用 `app/retrieval/bm25.py` 里 `_TOKEN_PATTERN` 现成的正则规则，不用重新发明）
3. 对每个 n-gram 和每个候选名字，计算**最长公共连续子串**长度（大小写不敏感），除以候选名字长度得到 0~1 的归一化分数
4. 分数 ≥ 阈值（0.3，重叠长度至少2个字符，避免单字符/极短噪声匹配）的候选保留，按分数降序排列，每类候选各截断到 Top-K（K 的具体数值留给实现计划确定，实体名候选池子最大，K 应该比 term_type/relation/字段名候选的 K 更保守）
5. 不引入 embedding、不引入额外 LLM 调用——整个召回过程是纯字符串比较，不调用任何 provider

**召回结果不保证100%命中**：如果一个候选完全召不到（比如极端拼写偏差），这层设计不做任何特殊处理——继续依赖后面两层防御（fuzzy constraint value resolution 在执行前的最后一次校验、以及轮次耗尽时的体面收场）。

## 数据流（走一遍复现场景）

"coke-cola公司有多少个订单"：

1. 阶段1（探测调用，`tools` 完整不变，`tool_choice:"auto"`，末尾追加 `_PLANNER_STAGE1_SYSTEM_PROMPT`）：LLM 决定调用 `structured_filter_query_tool`，参数字段填了占位值（会被丢弃），同时说了句"让我查一下coke-cola公司的订单数量"（这句叙述文字流式展示给用户，被前端记录进 reasoningTrail；这次调用本身不写入 `planner_messages`）
2. 分发：`_resolve_tool_arguments("structured_filter_query_tool", narration_query="让我查一下coke-cola公司的订单数量", ...)` 命中 `_resolve_structured_filter_query_arguments` 分支
3. 召回：query = "让我查一下coke-cola公司的订单数量"，切出 n-gram（包含"coke"、"cola"、"公司"、"订单"等），跟四类候选比对：
   - term_type 候选命中"公司"（精确匹配，分数1.0）、"订单号"（"订单"是"订单号"的最长公共子串，分数约0.67）
   - relation 三元组候选命中"产品 --BELONG_TO--> 公司"、"订单号 --BELONG_TO--> 产品"（因为召回的是完整三元组，即使 query 里没提"产品"，这两条三元组本身的文本里包含"公司"/"订单号"，也会被召回进来）
   - 实体名候选命中"Cola"（"cola" n-gram 命中标准名"Cola"，分数1.0）、"Coca-Cola"（"cola" n-gram 命中"Coca-Cola"里的"Cola"子串，分数约0.44）
4. 阶段2调用（`tools` 仍然是同一份完整 schema，`tool_choice` 强制指定成 `structured_filter_query_tool`，`messages` 沿用阶段1看到的那份真实历史，末尾追加 `_PLANNER_STAGE2_SYSTEM_PROMPT` + 上面这些候选）：LLM 能直接看到"订单号 --BELONG_TO--> 产品"和"产品 --BELONG_TO--> 公司"这两条方向明确的关系三元组，不需要自己猜方向，也能看到"Cola"和"Coca-Cola"两个候选实体名供参考，填出正确的两跳 `constraints`——这次调用产出的参数才是真正会被执行的
5. 执行前，`_resolve_fuzzy_constraint_values`（已上线）再对 `target_value` 做一次最终校验/解析
6. 执行、返回结果——这次一轮（1次探测 + 1次参数生成）就应该能拿到正确参数，不需要像复现时那样反复试错到轮次耗尽

## 错误处理

- 召回本身不会失败（纯本地字符串计算，没有网络调用），唯一的"失败"是召回不到任何候选——不做特殊处理，阶段2照常执行（LLM 只是拿不到候选参考，退化成今天的行为），后两层防御继续兜底。
- 阶段2的参数生成如果解析失败（比如 LLM 仍然填了不存在的字段名）——沿用 `structured_filter_query.py` 现有的 `parse_structured_filter_query_args`/`validate_structured_filter_query` 全部校验逻辑，不做任何改动，这层设计只影响"喂给 LLM 参考什么"，不影响"喂进来的参数怎么校验"。
- 阶段1如果没选任何工具、直接回答——跟今天完全一样的行为，不受这次改动影响，这次探测调用的结果就直接当最终答案用（不会因为"没落历史"这条规则而把一个正常的直接回答也丢弃——"丢弃"只针对"请求了工具调用时伴随的参数"这一种情况）。
- 阶段1请求了工具调用、但阶段2（`_resolve_structured_filter_query_arguments`）这次调用本身失败（网络错误、provider 报错）——降级成这个工具调用的错误观察结果（`{"error": "..."}`），跟 `run_structured_filter_query` 今天对其他内部异常的降级方式一致，不会让整个 Planner 轮次崩溃，但会正常消耗掉这一轮的工具调用预算（LLM 下一轮会看到这个错误，可能重试或换个思路）。

## 测试

- 召回算法（`longest_common_substring_score` 及切 n-gram 的逻辑）：纯函数单元测试，覆盖复现场景里的具体案例（"coke-cola" vs "Cola"/"Coca-Cola" 打分）、大小写不敏感、阈值截断、Top-K 截断。
- relation 三元组候选：验证召回结果确实是完整三元组（带方向），不是裸的 `relation_type` 字符串。
- **KV cache 兼容性（这份设计最核心的不变量）**：验证阶段1和阶段2两次请求的 `tools` 参数**逐字节相等**（同一个对象/同一份序列化结果，不只是"内容等价"）；验证跨多个回合，每一次调用（不管是阶段1还是阶段2）传的 `tools` 都跟第一次调用完全一致；验证阶段1的探测调用产出的 assistant 消息**没有**出现在传给阶段2的 `messages` 里。
- 参数解析分发器：验证 `_resolve_tool_arguments("vector_search_tool", ...)` 不发起任何 LLM 调用，直接返回 `{"query": narration_query}`；验证 `_resolve_tool_arguments("structured_filter_query_tool", ...)` 发起了一次 `tool_choice` 强制指定的调用；验证未知工具名抛出异常。
- 召回算法（`longest_common_substring_score` 及切 n-gram 的逻辑）：纯函数单元测试，覆盖复现场景里的具体案例（"coke-cola" vs "Cola"/"Coca-Cola" 打分）、大小写不敏感、阈值截断、Top-K 截断。
- relation 三元组候选：验证召回结果确实是完整三元组（带方向），不是裸的 `relation_type` 字符串。
- 叙述文字复用：验证阶段1文本非空时用它做召回 query（同时也是 `vector_search_tool` 的 `query` 参数），为空时回退到原始用户问题。
- 端到端：用本轮复现的真实 tenant 数据（或等价的测试 fixture）构造"coke-cola公司有多少个订单"场景，验证召回候选里确实包含正确的两条 relation 三元组和 Cola/Coca-Cola 两个实体候选。
- 阶段提示词：验证阶段1的探测调用发出的 `messages` 末尾追加了 `_PLANNER_STAGE1_SYSTEM_PROMPT`（包含"参数先填占位值""写一句自包含、已指代消解的叙述"这两条要求）；验证阶段2调用发出的 `messages` 末尾追加了 `_PLANNER_STAGE2_SYSTEM_PROMPT`；验证两段提示词都是追加在真实历史/基础系统消息之后，不替换、不修改前面已有的任何消息。
- 流式路径：验证阶段1的叙述文字通过 `on_answer_chunk` 正常推送；验证阶段2不触发任何 `on_answer_chunk` 调用（即使 provider 在 `tool_choice` 强制指定时意外返回了文本，也不展示给用户——具体处理方式留给实现计划：忽略即可，不需要报错）。

## Non-Goals

- 不引入 embedding 索引或语义检索——现阶段 aliases 数据本身大多为空，语义层面的收益无法验证，且会引入新的基础设施/成本/延迟。
- 不新增独立配置开关——这次改动本身不引入比今天更差的失败模式（召回不到就是退化成今天的行为），不需要开关快速关掉。
- 不改动查询引擎（`app/graphrag/neo4j_client.py`）、不改动五层白名单校验（`validate_structured_filter_query`）——这层设计只影响"生成参数之前给 LLM 看什么参考信息"，不影响参数生成之后的校验/执行逻辑。
- 不做召回结果的持久化缓存——每次现算，避免过期问题，代价是每回合多一次纯 CPU 的字符串比较开销，认为这个代价可以接受。

## Global Constraints

- **`tools` 请求参数在整个会话生命周期内、每一次 LLM 调用（不管是探测调用还是参数生成调用）里必须逐字节保持一致**——不允许为任何一个工具在任何一次调用里传精简/不同的 `parameters` 或 `description`。这是这份设计能兼容 KV cache 的硬约束，不是性能优化建议，违反这一条等于让"渐进式披露"这个改动本身变成缓存的主要破坏源。
- 渐进式披露（"只关注要不要调工具，先不管参数"）通过 `_PLANNER_STAGE1_SYSTEM_PROMPT` 里的指令文字实现，不通过修改 `tools` schema 实现。
- 阶段1这次探测调用的响应（包括它自己填的参数、包括是否有 `tool_calls`）在"确实请求了工具调用"的分支下**不写入 `planner_messages`**——阶段2看到的历史必须是阶段1看到的那份历史，不多不少。
- 阶段2调用必须用 `tool_choice` 精确指定到分发目标工具（`{"type": "function", "function": {"name": ...}}`），不能继续用 `"auto"`。
- 参数解析按工具名分发（`_resolve_tool_arguments`，`app/agent/planner.py`，跟已有的 `_dispatch_tool_call` 是平行关系）：`vector_search_tool` 不发起任何额外 LLM 调用，直接复用阶段1的叙述文字作为 `query`；`structured_filter_query_tool` 才真正发起阶段2调用。
- 召回机制的调用入口封装在 `app/agent/planner.py` 内；不改 `app/agent/graph.py` 的图结构/路由。
- 召回算法本身不依赖任何外部服务调用（不调 embedding provider、不调 LLM）——必须是纯本地计算。
- 召回索引每次 Planner 回合基于当次已加载的 `terms`/`term_type_schema`/`confirmed_relation_types` 现算，不做进程级缓存。
- relation 候选必须以完整 `(subject_term_type, relation_type, object_term_type)` 三元组形式出现在召回结果里，不能只召回 `relation_type` 字符串本身。
- `max_tool_call_rounds` 默认值不变，语义仍然是"回合数"而非"LLM 调用次数"。
- 不改动 `structured_filter_query.py` 里已有的解析/校验/fuzzy resolution 逻辑本身。
- `_PLANNER_STAGE1_SYSTEM_PROMPT` 必须显式要求 LLM 在决定调用工具时，用一句自包含、已做指代消解的话说明查询意图——这不是可选的文风建议，是"阶段1叙述文字可以直接当召回 query（以及 `vector_search_tool` 的 `query` 参数）复用"这个设计成立的前提条件。`_PLANNER_STAGE1_SYSTEM_PROMPT`/`_PLANNER_STAGE2_SYSTEM_PROMPT` 定义在 `app/agent/graph.py`，采用追加（而非替换）现有基础系统消息的方式接入。
