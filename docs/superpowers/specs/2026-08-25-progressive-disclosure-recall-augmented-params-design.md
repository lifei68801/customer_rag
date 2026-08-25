# Planner 工具调用渐进式披露 + 召回增强参数生成设计

## 背景

2026-08-25 复现"coke-cola公司有多少个订单"时确认：查询引擎/工具 schema 本身没问题（手工构造正确参数直接算得出结果），真正失败的是 **LLM 自己构造工具调用参数这一步不可靠**——它要在完全没有任何"这个 tenant 实际有哪些术语/关系/字段"参照的情况下，凭 system prompt 里的静态文字描述，一次性猜出 `anchor.term_type`、`constraints.hops` 里每一跳的 `relation_type`/`direction`/`target_term_type`、`target_field`、`target_value` 这些必须跟本体/图谱里真实存在的名字对上号的字段。猜错了只能等下一轮工具结果反馈回来再纠正，多跳关系要同时猜对好几个方向，试错成本比单跳高得多，很容易在纠正过来之前就把 `max_tool_call_rounds` 耗尽。

`app/graphrag/structured_filter_query.py` 已经有的 fuzzy constraint value resolution（2026-08-24 上线）能在执行前把 `standard_name` 字段的 `eq`/`ne` 值再解析一次，但那是**执行时的最后一道校验**，只能处理"生成的值文本本身能在术语表里精确命中"的情况——它解决不了"LLM 压根不知道这个 tenant 有哪些 term_type/relation_type/字段名，只能瞎猜"这个更早、更根本的问题。

这份设计要解决的正是这个更早的问题：**让 LLM 在生成参数之前，先看到跟这次问题相关的、真实存在于这个 tenant 本体里的候选名字**，把"凭空猜"变成"从候选里挑"。

## 目标

- 把"要不要调工具、调哪个"和"这个工具的参数具体填什么"拆成两次独立的 LLM 决策，避免让模型在还没决定用哪个工具时就要面对完整参数 schema 的认知负担。
- 针对 `structured_filter_query_tool`，在"填参数"这一步之前，基于用户问题（及本轮已经了解到的上下文）对本体做一次召回，把候选的 term_type、relation_type 三元组（带方向）、字段名、实体名字喂给 LLM 参考，而不是让它凭空生成。
- 不引入新的基础设施依赖（不新建 embedding 索引、不新增 LLM 调用次数之外的额外调用）。
- 跟已有的 `resolve_term()`/fuzzy constraint value resolution/`anchor.name` 消歧机制不冲突、互补共存，形成"生成前召回降低出错概率 → 执行前再校验一次 → 全部失败也有体面收场"的三层防御。

## 架构

### 两阶段工具调用

对 `vector_search_tool` 和 `structured_filter_query_tool` 统一生效（`vector_search_tool` 参数简单，拆分本身没有召回收益，但保持两个工具走同一套流程，代码结构更统一，不针对某个工具特殊处理）。

**阶段1——工具选择**：

- 请求里传的 `tools` 参数，每个工具的 `parameters` 简化成 `{"type": "object", "properties": {}}`（空 schema，结构上不给它任何具体入参字段可填），`description` 换成更短的"这个工具是干什么用的"概述，不包含今天那种"应该怎么组合调用参数"的细节指导（那些指导只在阶段2、真正要填参数时才有意义）。
- LLM 在这一步可以：不调用任何工具、直接回答（保留今天的行为）；或者请求调用一个或多个工具（保留今天 `run_tool_calls` 已经支持的并发执行能力，不收紧成"每轮只能选一个"）。
- 这一步产出的自然语言叙述文字（`ProviderResult.text`，就是今天已经存在、每轮工具调用前 LLM 会说的"让我查一下xxx"那段话）**直接复用**，不新增字段——它会被阶段2当作召回的 query 文本（见下），为空时回退用原始用户问题。

**阶段2——参数生成**（对阶段1选中的每个工具各自执行一次，多个工具并发跑）：

- 把这个工具完整的参数 schema（今天 `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA`/`VECTOR_SEARCH_TOOL_SCHEMA` 里那份完整描述）连同 `tool_choice` 强制指定成这一个工具，发给 LLM，专门做"把这个工具的参数填出来"这一件事。
- 对 `structured_filter_query_tool`：在这一步的 prompt 里，额外插入一段"候选参考"（见下面召回机制），列出跟这次查询可能相关的 term_type/relation 三元组/字段名/实体名字，供 LLM 参照着填，而不是凭空生成。
- 对 `vector_search_tool`：阶段2就是把 `query` 字段填出来，不需要召回增强。

**不改动 `app/agent/graph.py` 的图结构**：这两阶段都封装在 `app/agent/planner.py` 的 `run_planner_turn`/`run_planner_turn_streaming` 内部——一次"回合"内部从今天的 1 次 LLM 调用变成最多 2 次（阶段1 + 阶段2，多工具时阶段2 并发跑多份），但对外产出的返回形状（`pending_tool_calls`/`planner_messages`）跟今天完全一致，`route_after_planner` 不需要改。

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

1. 阶段1：LLM 看到只有工具名+简短描述的两个选项，决定调用 `structured_filter_query_tool`，同时说了句"让我查一下coke-cola公司的订单数量"（这句话被前端展示为查询状态提示前的叙述，同时被记录为这轮的叙述文字）
2. 召回：query = "让我查一下coke-cola公司的订单数量"，切出 n-gram（包含"coke"、"cola"、"公司"、"订单"等），跟四类候选比对：
   - term_type 候选命中"公司"（精确匹配，分数1.0）、"订单号"（"订单"是"订单号"的最长公共子串，分数约0.67）
   - relation 三元组候选命中"产品 --BELONG_TO--> 公司"、"订单号 --BELONG_TO--> 产品"（因为召回的是完整三元组，即使 query 里没提"产品"，这两条三元组本身的文本里包含"公司"/"订单号"，也会被召回进来）
   - 实体名候选命中"Cola"（"cola" n-gram 命中标准名"Cola"，分数1.0）、"Coca-Cola"（"cola" n-gram 命中"Coca-Cola"里的"Cola"子串，分数约0.44）
3. 阶段2：LLM 拿到完整参数 schema + 上面这些候选，能直接看到"订单号 --BELONG_TO--> 产品"和"产品 --BELONG_TO--> 公司"这两条方向明确的关系三元组，不需要自己猜方向，也能看到"Cola"和"Coca-Cola"两个候选实体名供参考，填出正确的两跳 `constraints`
4. 执行前，`_resolve_fuzzy_constraint_values`（已上线）再对 `target_value` 做一次最终校验/解析
5. 执行、返回结果——这次一轮（2次 LLM 调用）就应该能拿到正确参数，不需要像复现时那样反复试错到轮次耗尽

## 错误处理

- 召回本身不会失败（纯本地字符串计算，没有网络调用），唯一的"失败"是召回不到任何候选——不做特殊处理，阶段2照常执行（LLM 只是拿不到候选参考，退化成今天的行为），后两层防御继续兜底。
- 阶段2的参数生成如果解析失败（比如 LLM 仍然填了不存在的字段名）——沿用 `structured_filter_query.py` 现有的 `parse_structured_filter_query_args`/`validate_structured_filter_query` 全部校验逻辑，不做任何改动，这层设计只影响"喂给 LLM 参考什么"，不影响"喂进来的参数怎么校验"。
- 阶段1如果没选任何工具、直接回答——跟今天完全一样的行为，不受这次改动影响。

## 测试

- 召回算法（`longest_common_substring_score` 及切 n-gram 的逻辑）：纯函数单元测试，覆盖复现场景里的具体案例（"coke-cola" vs "Cola"/"Coca-Cola" 打分）、大小写不敏感、阈值截断、Top-K 截断。
- relation 三元组候选：验证召回结果确实是完整三元组（带方向），不是裸的 `relation_type` 字符串。
- 阶段1/阶段2 拆分：验证阶段1请求的 `tools` 参数里每个工具的 `parameters` 确实是空 schema；验证阶段2请求强制 `tool_choice` 指定到具体工具。
- 叙述文字复用：验证阶段1文本非空时用它做召回 query，为空时回退到原始用户问题。
- 端到端：用本轮复现的真实 tenant 数据（或等价的测试 fixture）构造"coke-cola公司有多少个订单"场景，验证召回候选里确实包含正确的两条 relation 三元组和 Cola/Coca-Cola 两个实体候选。

## Non-Goals

- 不引入 embedding 索引或语义检索——现阶段 aliases 数据本身大多为空，语义层面的收益无法验证，且会引入新的基础设施/成本/延迟。
- 不新增独立配置开关——这次改动本身不引入比今天更差的失败模式（召回不到就是退化成今天的行为），不需要开关快速关掉。
- 不改动查询引擎（`app/graphrag/neo4j_client.py`）、不改动五层白名单校验（`validate_structured_filter_query`）——这层设计只影响"生成参数之前给 LLM 看什么参考信息"，不影响参数生成之后的校验/执行逻辑。
- 不做召回结果的持久化缓存——每次现算，避免过期问题，代价是每回合多一次纯 CPU 的字符串比较开销，认为这个代价可以接受。

## Global Constraints

- 阶段1/阶段2的拆分、召回机制的调用入口都封装在 `app/agent/planner.py` 内；不改 `app/agent/graph.py` 的图结构/路由。
- 召回算法本身不依赖任何外部服务调用（不调 embedding provider、不调 LLM）——必须是纯本地计算。
- 召回索引每次 Planner 回合基于当次已加载的 `terms`/`term_type_schema`/`confirmed_relation_types` 现算，不做进程级缓存。
- relation 候选必须以完整 `(subject_term_type, relation_type, object_term_type)` 三元组形式出现在召回结果里，不能只召回 `relation_type` 字符串本身。
- `max_tool_call_rounds` 默认值不变，语义仍然是"回合数"而非"LLM 调用次数"。
- 不改动 `structured_filter_query.py` 里已有的解析/校验/fuzzy resolution 逻辑本身。
