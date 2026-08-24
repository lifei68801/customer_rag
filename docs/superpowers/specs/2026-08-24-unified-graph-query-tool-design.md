# 统一图查询工具设计 — `structured_filter_query_tool` 吸收 `graph_query_tool`

## 背景

前序调研（本次会话）确认 `graph_query_tool` 和 `structured_filter_query_tool` 不是子集关系，而是能力交叉：

- `graph_query_tool` 独有：别名/模糊消歧（`resolve_term`）、返回邻居节点本身的数据、无需预先知道关系类型的任意类型+双向1跳遍历、`REQUIRES`/`PRECEDES`/`PART_OF` 三种关系类型专属的隐式2跳链式遍历。
- `structured_filter_query_tool` 独有：按属性/关系条件过滤一批未知具体身份的实体、数值比较运算符、`group_by` 聚合、`limit` 分页。

本文档设计把两者合并成一个工具：`structured_filter_query_tool` 吸收 `graph_query_tool` 的全部能力，`graph_query_tool` 整体移除。设计原则：新增能力拆成与现有参数正交的可选维度（`anchor` 二选一 + 新增 `expand` 对象），不做"要么填 A 组参数、要么填 B 组参数"的模式二选一——避免重蹈"合并成一个大 schema 反而增加 LLM 参数构造出错概率"的覆辙（这正是当初两个工具没有合并的原始理由）。

## 与另一份 spec 的关系

`docs/superpowers/specs/2026-08-24-structured-filter-numeric-value-type-design.md`（数值型 `standard_name` 过滤 + `matched_count` 修复）和本文档改的是**同一批文件**（`structured_filter_query.py`/`neo4j_client.py`/`tools.py`）。写实施计划时应该合并成一条连续的任务序列，**先落地数值类型 + `matched_count` 修复（改动范围小、风险低、且是本次统一设计的返回结构基础），再在此基础上做本次的接口统一重构**——避免同一批文件改两轮、测试两轮联调。本文档后续小节涉及 `matched_count`/`truncated` 的部分，直接假定前一份 spec 的修复已经落地。

## Global Constraints

- 全程没有第三方/外部调用方依赖 `graph_query_tool` 这个工具名——本次是一次性切换，不做过渡期并存、不留兼容别名。
- 新增能力不能削弱现有五层白名单校验（`relation_type` 格式+已确认成员、`field`/`target_field` 格式+已确认成员、`operator`-`value_type` 匹配、`term_type` 已确认成员）——`anchor.name` 走的是 `resolve_term()` 纯 Python 查找，不参与这条校验链，也不能绕过它：一旦解析出 `node_key`，后续 `constraints`/`expand` 涉及的字段/关系类型/类型名，一样要过现有校验。
- `expand.relation_type` 为空（任意类型）时，生成的 Cypher 这一跳不能包含任何 LLM 可控字符串插值——只省略类型段，不放行任意字符串进查询文本。
- 沿用现有"不做服务端硬性截断 `limit`"的既定风险接受：`expand` 引入的"每个锚点还要展开邻居"不额外加服务端强制上限，靠工具描述文案提示调用方设置合理的 `limit`。

## 详细设计

### 1. 工具 schema（`app/agent/tools.py`）

删除 `GRAPH_QUERY_TOOL_SCHEMA`、`graph_query_tool()`、`GraphQueryToolResult`。`STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 改成：

```python
STRUCTURED_FILTER_QUERY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "structured_filter_query_tool",
        "description": (
            "在知识图谱里查询实体——支持三种用法，可以组合使用：\n"
            "1. 已知实体名，查它是什么/关联着什么：anchor.name（会做别名模糊匹配）+ expand。\n"
            "2. 不知道具体实体名，按条件筛选一批满足条件的实体，"
            "适用于「有没有xx以上的」「比xx大的有哪些」「xx类目下有没有yy」"
            "「xx有多少个/数量是多少」这类问题：anchor.term_type + constraints。\n"
            "3. 上述两种可以叠加 expand，展开命中锚点的邻居关系。\n"
            "「xx类目/公司下有多少个yy」这类需要先确定xx是什么、再数yy数量的问题，"
            "通常需要 anchor.name 消歧 + constraints 筛选组合两次调用，"
            "或者一次调用里 anchor.term_type 直接按关系条件筛选（见 constraints.kind=relation）。"
        ),
        "parameters": {
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
                    "description": "过滤条件列表，条件之间是 AND 关系，可以为空（anchor.name 模式下留空表示不额外过滤，直接用解析出的锚点）",
                    "items": { ... 不变，同现有 constraints.items 定义 ... },
                },
                "expand": {
                    "type": ["object", "null"],
                    "description": "可选：展开命中锚点的邻居关系（原 graph_query_tool 的查邻域能力）",
                    "properties": {
                        "hops": {"type": "integer", "enum": [1, 2], "description": "展开几跳，默认1"},
                        "relation_type": {
                            "type": ["string", "null"],
                            "description": "只展开这种关系类型；不传或传 null 表示任意类型",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["outgoing", "incoming", "both"],
                            "description": "关系方向，默认 both（原 graph_query_tool 行为）",
                        },
                    },
                },
                "group_by": { ... 不变 ... },
                "limit": { ... 不变，默认20 ... },
            },
            "required": ["anchor"],
        },
    },
}
```

`constraints` 从"必填数组"改成允许省略/空数组（`anchor.term_type` + 空 `constraints` 目前会被 `parse_structured_filter_query_args` 拒绝——"constraints 不能为空"这条校验需要改成只在 `anchor.term_type` 模式下才强制，`anchor.name` 模式允许空）。

`structured_filter_query_tool()`（工具执行体）签名新增 `terms: list[Term]` 参数（`anchor.name` 模式解析需要）：

```python
async def structured_filter_query_tool(
    arguments: dict[str, Any],
    *,
    tenant_id: str,
    terms: list[Term],
    graph_client: GraphClientProtocol,
    confirmed_relation_types: set[str],
    term_type_schema: dict[str, TermTypeCategory],
) -> dict[str, Any]:
    return await run_structured_filter_query(
        arguments, terms=terms, graph_client=graph_client, tenant_id=tenant_id,
        confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
    )
```

### 2. 解析层（`structured_filter_query.py`）

`StructuredFilterQueryArgs` 的 `anchor_term_type: str` 换成一个 `Anchor` 联合类型：

```python
@dataclass(frozen=True)
class NameAnchor:
    name: str
    type_hint: str | None


@dataclass(frozen=True)
class TypeAnchor:
    term_type: str


@dataclass(frozen=True)
class ExpandSpec:
    hops: int
    relation_type: str | None
    direction: str  # "outgoing" | "incoming" | "both"


@dataclass(frozen=True)
class StructuredFilterQueryArgs:
    anchor: NameAnchor | TypeAnchor
    constraints: list[AttributeConstraint | RelationConstraint]
    expand: ExpandSpec | None
    group_by: GroupBy | None
    limit: int
```

`parse_structured_filter_query_args`：
- 解析 `raw["anchor"]`：有 `name` 键走 `NameAnchor`，有 `term_type` 键走 `TypeAnchor`，两者都没有或都有则报错。
- `constraints` 允许省略（默认 `[]`）；`TypeAnchor` 模式下空 `constraints` 一律拒绝，**不因为设了 `expand` 就放行**——沿用原设计"不做无约束全量扫描"的既定原则，`expand` 是给命中结果加的邻居展开，不是过滤条件的替代品，"对整个类型无约束扫描再逐个展开邻居"比单纯无约束扫描更费资源，没有理由放开。真要浏览一个基数很小的类型全体成员及其关联（比如"列出所有类目及每个类目下的产品数量"），现有 `group_by` 机制已经能覆盖（`anchor.term_type=产品` + `constraint kind=relation hop 到类目` + `group_by`），不需要靠放开这条规则实现。`NameAnchor` 模式（`anchor.name`）允许空 `constraints`——这种模式锚点本身就是唯一确定的，不存在"全量扫描"的问题。
- 解析 `raw.get("expand")`：`hops` 默认1、校验只能是1/2；`direction` 默认 `"both"`、校验只能是三选一；`relation_type` 允许 `None`。

`validate_structured_filter_query`：
- `TypeAnchor` 分支：跟现在的 `anchor_term_type` 校验完全一样（必须在 `term_type_schema` 里）。
- `NameAnchor` 分支：不查 `term_type_schema`（`type_hint` 只是喂给 `resolve_term` 的消歧提示，不是需要预先确认的 schema 成员——跟原 `graph_query_tool` 的 `entity_type` 参数语义完全一致，本来就不做校验）。
- `expand.relation_type`（非 None 时）：跟 `constraints` 里 hop 的 `relation_type` 一样过格式校验 + `confirmed_relation_types` 成员校验。

`run_structured_filter_query` 新增 `terms: list[Term]` 参数。为了让下游校验/执行逻辑不用重复区分"这次是哪种 anchor 模式"，新增一个统一的解析结果类型：

```python
@dataclass(frozen=True)
class ResolvedAnchor:
    term_type: str          # 两种模式统一后都有明确的 term_type，供 constraints 字段校验用
    node_key: str | None    # NameAnchor 解析成功时有值；TypeAnchor 模式恒为 None
```

`run_structured_filter_query` 的编排步骤：

1. `parse_structured_filter_query_args(raw_args)` 得到 `args`（`args.anchor` 仍是原始的 `NameAnchor | TypeAnchor`，用于分支判断）。
2. `isinstance(args.anchor, NameAnchor)` 时，调用 `resolve_term(args.anchor.name, terms, term_type_hint=args.anchor.type_hint)`：
   - 解析失败（`None`）：直接返回 `{"matched_count": 0, "truncated": False, "anchors": []}`，不发起任何 Neo4j 查询——语义上等价于原 `graph_query_tool` 的 `resolved: false`。
   - 解析成功：`resolved = ResolvedAnchor(term_type=term.term_type, node_key=term.node_key)`。
3. `isinstance(args.anchor, TypeAnchor)` 时：`resolved = ResolvedAnchor(term_type=args.anchor.term_type, node_key=None)`。
4. `validate_structured_filter_query(args, resolved=resolved, ...)` 统一用 `resolved.term_type` 做 anchor 相关校验（不再需要区分是哪种 anchor 模式）——包括一条防御性检查：`resolved.term_type` 必须在 `term_type_schema` 里（`TypeAnchor` 模式下这条和现状等价；`NameAnchor` 模式下理论上术语表里的 `term_type` 应该都在已确认 schema 里，但仍要检查，不能假定术语表和 schema 天然一致）。
5. `execute_structured_filter_query(args, resolved=resolved, tenant_id=..., term_type_schema=...)`——不再需要重新解析 anchor，直接用第4步校验过的 `resolved`。

### 3. 执行层（`neo4j_client.py`）

`execute_structured_filter_query` 签名新增 `term_type_schema` 参数（前一份 spec 已经要求，本次复用）和 `resolved: ResolvedAnchor` 参数（上一节定义，由 `run_structured_filter_query` 解析后传入，本方法不再关心 `args.anchor` 原始是哪种模式）；锚点 `MATCH` 子句按 `resolved.node_key` 是否为空二选一拼：

```python
if resolved.node_key is not None:
    anchor_match = "MATCH (anchor:Term {tenant_id: $tenant_id, node_key: $anchor_node_key})"
    params["anchor_node_key"] = resolved.node_key
else:
    anchor_match = "MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type})"
    params["anchor_term_type"] = resolved.term_type
```

`constraints`/`WHERE`/`matched_count`（沿用前一份 spec 的真实计数查询）逻辑完全不变，只是锚点 `MATCH` 换了一种拼法——两条路径共用同一套 `WHERE`/计数/`LIMIT` 逻辑，不重复实现。

`expand` 不为空时，在拿到锚点集合的基础上追加一段邻居展开，用变长关系模式一次表达1或2跳：

```python
# expand.relation_type 为 None（任意类型）时省略 ":TYPE"；
# expand.direction 决定箭头方向，"both" 时不带箭头。
rel_pattern = f":{expand.relation_type}" if expand.relation_type else ""
arrow_out, arrow_in = (
    ("->", "") if expand.direction == "outgoing" else
    ("", "<-") if expand.direction == "incoming" else
    ("", "")  # both：两端都不带箭头
)
expand_clause = (
    f"OPTIONAL MATCH p = (anchor){arrow_in}[r{rel_pattern}*1..{expand.hops}]{arrow_out}(neighbor:Term {{tenant_id: $tenant_id}}) "
    "WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id) AND neighbor <> anchor"
)
```

完整查询，以 `TypeAnchor`（`resolved.node_key is None`）+ `constraints` + `expand` 全部叠加为例——`{anchor_match}` 就是上面按 `resolved.node_key` 二选一拼出的那一行，`WITH anchor LIMIT $limit` 保证 `LIMIT` 约束的是锚点数，不是展开后的行数：

```cypher
{anchor_match}
WHERE <constraints where_sql>
WITH anchor
ORDER BY anchor.node_key
LIMIT $limit
OPTIONAL MATCH p = (anchor)-[r*1..2]-(neighbor:Term {tenant_id: $tenant_id})
  WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id) AND neighbor <> anchor
RETURN anchor.standard_name AS standard_name, anchor.node_key AS node_key,
       anchor.type AS term_type, properties(anchor) AS all_properties,
       collect(DISTINCT CASE WHEN neighbor IS NULL THEN NULL
               ELSE {related_name: neighbor.standard_name, relation_type: [rel IN r | type(rel)][-1], hops: length(p)}
               END) AS neighbors
```

`resolved.node_key is not None`（`NameAnchor` 解析成功）时，`{anchor_match}` 换成按 `node_key` 精确匹配那一行，其余 `WHERE`/`WITH...LIMIT`/`OPTIONAL MATCH`/`RETURN` 结构不变——`constraints` 为空时 `WHERE <constraints where_sql>` 这一行相应省略（`parse_structured_filter_query_args` 允许 `NameAnchor` 模式下空 `constraints`，见第2节），直接 `{anchor_match}` 后接 `WITH anchor ... OPTIONAL MATCH ...`。

（`ORDER BY anchor.node_key` 是新增的：`LIMIT` 在没有 `ORDER BY` 时哪些行被截断是不确定的，原 `structured_filter_query_tool` 现有实现也没有 `ORDER BY`——这是本次顺手发现的一个既有小缺陷，按 `node_key` 排序保证同一次查询多次执行、`LIMIT` 截断的是同一批锚点，结果可复现；不属于本次改动的必须项，但改 `LIMIT` 相关的查询时顺手带上，不单独立项。）

`collect(DISTINCT CASE WHEN neighbor IS NULL THEN NULL ELSE {...} END)` 处理"锚点没有任何邻居"的情况——`OPTIONAL MATCH` 匹配不到时 `neighbor`/`r`/`p` 全部是 `NULL`，用 `CASE` 过滤掉这个空壳条目，最终 `neighbors` 是空列表而不是 `[NULL]`。

`association` 文案（"关联"/"间接关联（经过 N 跳）"）的生成时机不变——不在 Cypher 里拼，还是在 `app/agent/tools.py::structured_filter_query_tool()` 或 `planner.py::_dispatch_tool_call` 组装最终观察结果时，对每个 `neighbors` 条目调用现有的 `describe_association(row["hops"])`（跟现在 `graph_query_tool` 分支的用法一致，平移过来）。

### 4. 返回结构

```jsonc
// group_by 时不变：{"groups": [...]}
// 非 group_by 时：
{
  "matched_count": 1,       // 真实总数（前一份 spec 的 matched_count 修复）
  "truncated": false,
  "anchors": [
    {
      "standard_name": "Coca-Cola", "node_key": "...", "term_type": "公司",
      "extra_properties": {...},
      "neighbors": [                                  // expand 为空时这个字段整体不出现
        {"related_name": "Cola", "relation_type": "BELONG_TO", "hops": 1, "association": "关联"}
      ]
    }
  ]
}
```

`anchors` 字段名替换现在的 `results`——这是一次故意的破坏性重命名，不是疏忽：`results` 这个名字现在被两种语义共用（原 `structured_filter_query_tool` 的"满足条件的实体列表"和新增的"消歧出来的单个锚点"），容易让 LLM 混淆"这是命中的一批实体"还是"这是我查的那一个东西"；`anchors` 更准确地传达"这些是本次查询的中心节点"，`expand` 才是"这些锚点各自的邻居"。

### 5. `_dispatch_tool_call`（`app/agent/planner.py`）

删除 `if name == "graph_query_tool":` 整个分支；`structured_filter_query_tool` 分支新增 `terms` 透传：

```python
if name == "structured_filter_query_tool":
    if graph_client is None or confirmed_relation_types is None or term_type_schema is None or not terms:
        return json.dumps({"error": "structured_filter_query_tool 未配置"}, ensure_ascii=False), []
    observation = await structured_filter_query_tool(
        arguments, tenant_id=tenant_id, terms=terms, graph_client=graph_client,
        confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
    )
    return json.dumps(observation, ensure_ascii=False), []
```

### 6. 系统提示词（`app/agent/graph.py::_PLANNER_SYSTEM_PROMPT`）

```python
_PLANNER_SYSTEM_PROMPT = (
    "你是客服问答助手。可以调用 vector_search_tool 检索知识库、"
    "structured_filter_query_tool 查询知识图谱——支持已知实体名查询关联信息"
    "（anchor.name，会做别名模糊匹配）、按数值区间/精确匹配/关系条件反查一批满足条件的实体"
    "（anchor.term_type + constraints，适用于「有没有xx以上的」「比xx大的有哪些」"
    "「xx有多少个/数量是多少」这类问题）、以及展开某个实体的关联关系（expand）。"
    "看到「多少个」「数量」等计数意图时，必须以这个工具的 matched_count 为准给出确定数字，"
    "不能仅凭检索到的文档片段或邻居关系列表猜测，也不能因为一次调用没查到就直接放弃——"
    "先消歧、再筛选计数，通常需要两次调用。"
    "有足够信息时直接给出最终答案，不要编造资料中没有的内容；"
    "信息不足以回答时也不要编造。"
)
```

（这版提示词把之前分开讨论的"补'有多少个'例句"和"强制以计数结果为准、不能中途放弃"两条建议，一并折叠进这次改动——两份 spec 本来就要合并执行，提示词只写一次最终版本，不做"先加例句再改结构"的两步走。）

### 7. `docs/AGENT_PLANNER_DESIGN.md`

第111-112行的工具能力表格，两行合并成一行，反映新 schema；正文里提到 `graph_query_tool`/`GraphQueryToolResult` 的地方（第27、203、232、249行附近）同步更新或删除。

## 迁移影响 / 破坏性变更清单

- `graph_query_tool` 工具名从此在 Planner 里不存在——任何依赖这个工具名的外部文档/演示脚本需要跟着更新（本仓库内没有发现除代码本身和文档外的其它引用）。
- `structured_filter_query_tool` 的入参 `anchor_term_type` 顶层字段消失，改成嵌套的 `anchor.term_type`——这是一次破坏性 schema 变更，不保留旧字段名的兼容读取（同样因为没有外部调用方，不需要过渡期）。
- 返回结构里 `results` 改名 `anchors`，同上不保留旧字段名。
- 原 `graph_query_tool` 返回结构里的顶层 `resolved`/`subgraph` 字段消失，语义分别折叠进 `matched_count==0`（未解析成功）和 `anchors[0].neighbors`（原 `subgraph`）。

## 测试

- `structured_filter_query.py`：`NameAnchor`/`TypeAnchor` 两种解析路径；`expand` 的 `hops`/`relation_type`/`direction` 解析与校验（含默认值：`hops` 默认1、`direction` 默认 `both`）；`NameAnchor` + 空 `constraints` 允许，`TypeAnchor` + 空 `constraints` + 无 `expand` 仍然拒绝；`expand.relation_type` 走已确认关系类型白名单校验（复用现有 `RelationConstraint` 的校验测试模式）。
- `neo4j_client.py`（复用 `FakeSession`/`FakeDriver`）：
  - `NameAnchor` 模式生成的 `MATCH` 用 `node_key` 而不是 `type`。
  - `expand.relation_type=None` 时生成的模式不含 `:TYPE` 段；给定具体类型时含对应类型段。
  - `expand.direction` 三种取值生成正确的箭头方向（复用现有 `incoming`/`outgoing` 方向测试的写法）。
  - `TypeAnchor + constraints + expand` 组合：`WITH anchor ... LIMIT $limit` 出现在 `OPTIONAL MATCH` 之前（保证 `LIMIT` 约束锚点数而不是展开后的行数）。
  - `neighbors` 为空时（`OPTIONAL MATCH` 没匹配到）返回空列表，不是 `[null]`（这个用 `FakeSession`/`FakeResult` 构造一行 `neighbor` 为 `None` 的返回数据来验证）。
- `tools.py`：`structured_filter_query_tool()` 新签名（新增 `terms` 参数）正确透传给 `run_structured_filter_query`；`NameAnchor` 解析失败时返回 `matched_count: 0`，不发起 Neo4j 调用（用一个不会真正连接的 fake `graph_client` 断言它的 `execute_structured_filter_query` 从未被调用）。
- `planner.py`：`_dispatch_tool_call` 不再识别 `graph_query_tool`（未知工具名分支）；`structured_filter_query_tool` 分支的"未配置"守卫新增 `terms` 检查。
- 端到端回归：把现有 `tests/agent/test_tools.py`/`tests/agent/test_planner.py`/`tests/agent/test_graph_planner.py` 里所有 `graph_query_tool` 相关的测试用例，改写成用新 schema 的 `anchor.name` + `expand` 等价调用，验证行为不丢失（不是删掉这些覆盖，是迁移到新接口下）。
- （可选，人工验证）用真实环境重新跑一遍"coke-cola类目下有多少个订单"，确认 Planner 现在能在合理轮次内正确完成"消歧+筛选计数"两步调用并给出真实数字。

## 落地顺序建议

1. 先落地 `2026-08-24-structured-filter-numeric-value-type-design.md`（数值类型 + `matched_count`），独立提交、独立回归。
2. 在此基础上做本文档的接口统一重构（`anchor`/`expand` 引入、`graph_query_tool` 移除、返回结构改名），独立提交、独立回归。
3. 两次改动分开提交但可以在同一轮实施计划里连续执行——不需要中间等一次人工验收再继续，只是保持"数据/校验层修复"和"接口重构"两类改动在 git 历史上可分辨、可单独回滚。
