# 结构化过滤查询工具设计

**状态**：设计定稿，待写执行计划。
**上游依赖**：`docs/superpowers/specs/2026-08-15-etl-driven-schema-construction-design.md` 第8节（"结构化过滤查询工具"标记为必需任务）；`docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md`（ETL 写入引擎，为本工具提供可查询的数据）。
**评估依据**：`docs/MUJI_知识图谱_Schema设计方案_v6.md` "典型问答走法"一节列出的10种问法。

## 0. 问题陈述

`app/agent/tools.py::graph_query_tool` 目前只支持一种查询形状：给定一个已知实体名，先对齐术语表，再查这个节点的1-2跳关联子图（`neo4j_client.py::query_subgraph`）。这是"从一个已知起点出发的邻居遍历"，要求调用方已经知道具体实体名。

MUJI 文档"典型问答走法"列出的10种问法里，多数并不满足这个前提——用户问的是"有哪些满足某个条件的东西"，而不是"这个已知东西周围有什么"。按查询形状分类：

| # | 问法 | 形状 |
|---|---|---|
| 2 | 有500ml以上的吗 | 纯属性过滤，无起点 |
| 3 | 比M码大的有哪些 | 纯属性过滤，无起点 |
| 4 | 能塞进80cm空隙吗 | 纯属性过滤（数组逐元素），无起点 |
| 1 | 唇膏都有什么颜色 | 单跳关系+属性过滤，GROUP BY |
| 5 | 这个咖喱什么口味 | 单跳关系+属性过滤（起点已知，理论上 graph_query_tool 现有子图也能覆盖，只是不做过滤） |
| 6 | 那个红色的多少钱 | 单跳关系+属性过滤，起点是属性值反查、方向与1相反 |
| 7 | 这个JAN是什么 | 两跳链式遍历，起点已知 |
| 8 | 童装有没有法兰绒 | 两跳链式遍历+字符串前缀匹配 |
| 9 | 有哪些春夏的男装 | 同一锚点的两个独立关系分支约束（不是线性链） |
| 10 | 同系列还有什么 | 两跳链式遍历（去程+回程） |

这些形状的共同点：都需要"按属性/关系条件筛选出一批满足条件的节点"，而不是"展开一个已知节点的邻域"。这是 `graph_query_tool` 结构性做不到的能力，必须新增工具补齐——本文档评估确认为**必需任务**：没有它，即使本体 schema 建对了、ETL 也写对了，Agent 依然回答不了 MUJI 文档里的核心问法。

## 1. 关键发现：extra_properties 已经是 Neo4j 顶层属性

`neo4j_client.py::sync_term`（`_SYNC_TERM_QUERY`）用 `SET t += $extra_properties` 把 `extra_properties` map 展开写入 `:Term` 节点——这是 Cypher 的 map 展开赋值语义，`extra_properties` 里的每个 key（如 `numeric_value`、`dims`、`order_rank`）会成为节点的**顶层属性**（`t.numeric_value`、`t.dims`），不是嵌套在某个 map 属性里。

这解决了上游 spec（`2026-08-16-schema-etl-engine-design.md` 第94行）留下的悬念——"这类值目前存储在一个属性 map 里，Neo4j 对 map 内部字段建索引有限制"这个顾虑不成立，可以直接对这些字段建标准 Neo4j property index（见第6节）。

## 2. 范围：覆盖全部10种问法，不只是数值谓词 MVP

架构文档原本只点名了数值/区间谓词过滤（问法2/3/4）为必需能力。评估过程中确认：其余7种问法（关系遍历+属性过滤、GROUP BY、多跳链式遍历、同锚点多分支约束）用现有 `graph_query_tool` 无法覆盖，且都是 MUJI 文档明确列出的"典型问答走法"，不是假设性需求——因此本次设计一并覆盖全部10种问法，而不是先做 MVP 再补。

**范围外**：本工具不做全文本模糊搜索（如 `contains`/相似度匹配），不做 3 跳以上遍历，不做跨租户查询，不做写操作。这些如果未来有真实需求再评估。

## 3. 工具接口：`structured_filter_query_tool`

新增独立工具，不扩展 `graph_query_tool`——两者的必需参数集合互斥（一个要求已知 `entity_name`，一个反而是"不知道具体是哪个实体，按条件筛"），硬塞进一个 schema 会让大部分参数变成"看情况填不填"，增加 LLM 调用出错的概率。`graph_query_tool` 的职责保持不变："已知实体 → 查邻域"；新工具的职责："按条件 → 筛实体"。

### 3.1 JSON Schema（OpenAI function-calling 格式）

```python
STRUCTURED_FILTER_QUERY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "structured_filter_query_tool",
        "description": (
            "按结构化条件（数值区间、精确匹配、关系约束）在知识图谱里筛选满足条件的实体，"
            "适用于「有没有xx以上的」「比xx大的有哪些」「xx类目下有没有yy」这类不知道具体"
            "实体名、需要按条件查找的问题。与 graph_query_tool 不同：graph_query_tool 用于"
            "已知实体名、查它的关联信息；本工具用于按条件反查一批满足条件的实体。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "anchor_term_type": {
                    "type": "string",
                    "description": "要筛选的实体类型（如 SKU、Product、Category），结果就是这个类型的实体列表",
                },
                "constraints": {
                    "type": "array",
                    "description": "过滤条件列表，条件之间是 AND 关系，至少提供一个",
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
                                "description": "比较运算符，实际可用范围取决于字段类型（见下）",
                            },
                            "value": {
                                "description": "kind=attribute 时必填：比较的目标值（字符串/数字/数组，取决于字段类型）",
                            },
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
                            "target_value": {
                                "description": "kind=relation 时必填：比较的目标值",
                            },
                        },
                        "required": ["kind"],
                    },
                },
                "group_by": {
                    "type": ["object", "null"],
                    "description": "可选：按某个字段做 distinct 值统计而不是返回实体列表本身（对应「都有什么颜色」这类问法）",
                    "properties": {
                        "constraint_index": {
                            "type": "integer",
                            "description": "指向 constraints 数组里某个 kind=relation 约束的下标，按它的 target_field 分组",
                        },
                    },
                },
                "limit": {
                    "type": "integer",
                    "description": "返回结果的最大条数，默认20",
                },
            },
            "required": ["anchor_term_type", "constraints"],
        },
    },
}
```

### 3.2 参数设计要点

- **`constraints` 至少一项**：不允许空约束的全量扫描（"列出所有 Product"不是本工具要解决的问题，且 MUJI 单表18万+行，无约束扫描没有实际意义）。
- **`kind=attribute` 覆盖问法2/3/4**：直接比较锚点自己的 `extra_properties` 字段或 `standard_name`。
- **`kind=relation` 覆盖问法1/5/6/8/9/10**：`hops` 最多2跳（MUJI 文档最深的例子是"Category→Product→Material"两跳，没有更深的证据需求，按 YAGNI 不预留更多）。多个 `kind=relation` 约束之间是独立的存在性检查，不是拼成一条更长的链——问法9"春夏的男装"需要 Product 同时满足"连到 Season=春夏"和"连到 TargetGender=男装"两个独立分支，不是线性路径，这是 Cypher 构造策略（第5节）里 `EXISTS` 子查询要处理的关键点。
- **`hops` 显式 `direction`**：关系边写入时是有向的（`MERGE (a)-[:TYPE]->(b)`），问法1（SKU→VariantValue，outgoing）和问法6（VariantValue→SKU，incoming）方向相反，不给默认值——默认值猜错会静默返回空结果，误导 LLM 以为数据不存在。
- **`field`/`target_field` 的合法取值**：`"standard_name"`（保留字，配 `eq`/`ne`/`starts_with`）或该 `term_type` 在 `ontology_categories` 里已确认的 `extra_fields` 成员（配的运算符取决于该字段声明的 `value_type`，见3.3）。
- **不设服务端硬上限的 `limit`**：完全交给调用方（LLM）传参控制返回条数，服务端不做二次截断。这是有意的风险接受，不是遗漏——MUJI 单表18万+行，一次未加限制的宽条件查询理论上可能返回数千条塞满 LLM 上下文；工具描述文案里明确提示"结果较多时请设置合理的 limit"来引导调用方，但不做服务端强制。

### 3.3 字段类型 → 合法运算符

| value_type | 合法 operator |
|---|---|
| `string`（含 `standard_name`） | `eq`, `ne`, `starts_with` |
| `number` / `integer` | `gt`, `gte`, `lt`, `lte`, `eq`, `ne` |
| `number[]` | `all_lte`, `all_gte`, `any_lte`, `any_gte` |

`all_lte`/`all_gte`：数组里每个元素都满足比较（如"能塞进80cm空隙"→ `dims` 每一边都 `<= 80` 用 `all_lte`）。
`any_lte`/`any_gte`：数组里至少一个元素满足比较。

## 4. 验证链（先校验，全部通过才拼 Cypher）

`field`/`relation_type` 都是 LLM 可控参数，最终会构造进 Neo4j 查询——这是继 `merge_relation` 之后第二个需要认真对待注入防线的写入/读取路径。验证分五层，任一失败**返回结构化错误**给 LLM（不是抛异常），让它有机会调整参数重试：

1. **`relation_type` 格式校验**：复用 `neo4j_client.py::_RELATION_TYPE_NAME_PATTERN`（`^[A-Z][A-Z0-9_]{0,63}$`）。
2. **`relation_type` 必须是该租户已确认的关系类型**：`ontology_relations.py::list_relation_types(conn, tenant_id, status="confirmed")` 查一次，构造进内存 set 校验，不逐个约束单独查库。
3. **`field`/`target_field` 合法性**：`== "standard_name"` 直接放行；否则必须是 `anchor_term_type`（或 `kind=relation` 约束里对应 `target_term_type`）在 `ontology_categories` 里已确认的 `extra_fields` 成员——查一次 `list_term_types(conn, tenant_id)`，对每个用到的 term_type 建一份 `{field_name: value_type}` 映射复用。
4. **`operator` 与字段声明的 `value_type` 匹配**：按第3.3节的表校验，运算符和字段类型对不上直接拒绝（如对 `string` 字段传 `gt` 是非法组合）。
5. **`anchor_term_type`/`target_term_type` 必须是该租户已确认的 term_type**：同第3步查询结果里带出来，不额外查库。

**`operator` 本身不需要格式校验**——它在 JSON Schema 里就是固定枚举（`enum`），不是自由字符串，模型只能从这11个值里选，从协议层面就杜绝了任意字符串注入的可能。

## 5. Cypher 构造策略

### 5.1 字段名走参数化动态属性访问，不做字符串插值

`relation_type` 无法参数化绑定（Cypher 关系类型语法层面要求字面量），必须走"格式校验+白名单成员校验"这条防线（同 `merge_relation`）。但**属性字段名可以**：Neo4j Cypher 支持 `n[$propName]` 动态属性访问语法，`$propName` 是一个普通查询参数（字符串），完全避免把字段名拼进查询文本。

这意味着 `field`/`target_field` 即使通过了第4节的成员资格校验，也不需要额外校验"这个字段名本身是不是合法 Cypher 标识符"——它从来不会被当作标识符解析，只是作为参数值传给 `t[$field]` 做运行时属性查找。

（附带发现，不在本次改动范围内：`ontology_categories.py::ExtraFieldSpec` 目前对 `name` 没有任何字符集校验，理论上业务方可以在管理后台声明一个包含特殊字符的字段名。因为查询侧走参数化动态属性访问，这不构成本工具的注入风险；但如果未来有其它地方需要把字段名直接拼进 Cypher 文本，需要重新评估。）

### 5.2 单一 MATCH + 多个独立 EXISTS 子查询

```cypher
MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type})
WHERE
  anchor[$attr_field_0] > $attr_value_0                                  // kind=attribute 约束，直接内联
  AND EXISTS {                                                            // 每个 kind=relation 约束一个独立子查询
    MATCH (anchor)-[:HAS_VARIANT]->(hop1:Term {tenant_id: $tenant_id, type: $hop1_type})
    WHERE hop1[$target_field_1] = $target_value_1
  }
  AND EXISTS {
    MATCH (anchor)<-[:BELONGS_TO_CATEGORY]-(hop2:Term {tenant_id: $tenant_id, type: $hop2_type})
    WHERE hop2.standard_name STARTS WITH $target_value_2
  }
RETURN anchor.standard_name AS standard_name, anchor.node_key AS node_key,
       anchor.type AS term_type, anchor.product_line AS product_line,
       properties(anchor) AS all_properties
LIMIT $limit
```

- `relation_type` 是`EXISTS`子块内 `MATCH` 语句里唯一必须字符串插值的部分（f-string，已过第4节校验）；两端节点类型、字段名、比较值全部走 `$parameter`。
- 每个 `kind=relation` 约束独立成一个 `EXISTS {}` 块——这是问法9"春夏的男装"能正确表达为"两个独立分支都满足"而不是"拼成一条更长的链"的关键：`EXISTS` 子查询各自独立匹配，不会互相污染绑定变量，也不会产生笛卡尔积。
- 2跳的 `kind=relation` 约束（问法7/8/10），`EXISTS {}` 块内部是一条2跳的线性 `MATCH`，`hop1`/`hop2` 变量作用域局限在这个子查询块内。
- 运算符到 Cypher 比较符的映射：`gt`→`>`、`gte`→`>=`、`lt`→`<`、`lte`→`<=`、`eq`→`=`、`ne`→`<>`、`starts_with`→`STARTS WITH`；`all_lte`/`all_gte`/`any_lte`/`any_gte` 用 Cypher 的列表推导谓词 `all(x IN anchor[$field] WHERE x <= $value)` / `any(...)`。

### 5.3 group_by 走单独的查询形态

`group_by` 非空时不返回实体列表，改成对指定约束的 `target_field` 做聚合：

```cypher
MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type})
MATCH (anchor)-[:HAS_VARIANT]->(hop1:Term {tenant_id: $tenant_id, type: $hop1_type})
WHERE hop1[$dim_field] = $dim_value                          // group_by 指向的约束仍然生效
  AND <其余 constraints 的 WHERE/EXISTS 照常拼>
RETURN hop1[$group_field] AS value, count(DISTINCT anchor) AS count
ORDER BY count DESC
```

`group_by.constraint_index` 指向的那个 `kind=relation` 约束，从"EXISTS 存在性检查"变成"实际 MATCH 出来参与分组"，其余约束保持 EXISTS 检查不变。

## 6. Neo4j 索引策略

`neo4j_client.py::ensure_tenant_scoped_schema()` 目前只建了 `(tenant_id, node_key)` 和 `(tenant_id, type)` 两个索引（`_SCHEMA_STATEMENTS`）。本工具引入的按 `extra_properties` 字段过滤，需要额外索引才能在18万+行规模下不做全表扫描。

**策略：跟随 schema confirm 动态建索引。** 在 `ontology_lifecycle.py::confirm_ontology` 确认某个 term_type 的 `extra_fields` 定义之后，为每个 `value_type` 是 `string`/`number`/`integer` 的字段（`number[]` 不建——Neo4j 对列表属性的 range 索引支持有限，`all_lte`/`any_gte` 这类逐元素谓词也用不上标量索引）额外发一条：

```cypher
CREATE INDEX IF NOT EXISTS FOR (t:Term) ON (t.tenant_id, t.type, t.<field_name>)
```

`<field_name>` 走字符串插值（`CREATE INDEX` 语句的属性名同样无法参数化），但这里的字段名来源是**已经通过 confirm 流程、业务方在管理后台声明的字段名**，不是 LLM 运行时可控参数，风险性质与 `field`/`target_field` 完全不同——不需要走第4节那套 LLM 输入校验链，但为避免管理后台声明阶段引入的怪异字符破坏 `CREATE INDEX` 语句本身，应在 `ontology_categories.py::_validate_extra_field_specs` 里补一条字段名格式校验（如 `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`，具体规则留给实施计划阶段确定）——这是本次设计识别出的一个真实但独立的小缺口，不属于本工具自身范围，留给实施计划阶段一并排期。

## 7. 返回形状（工具执行结果 → LLM 观察结果 JSON）

**非 `group_by`**：

```json
{
  "matched_count": 3,
  "results": [
    {"standard_name": "圆角收纳盒 500ml", "node_key": "SKU:4901234567890",
     "term_type": "SKU", "product_line": "MUJI",
     "extra_properties": {"numeric_value": 500, "price": 99}}
  ]
}
```

`matched_count` 是本次返回的条数（不是全库命中总数——没有服务端硬上限，"全库命中总数"和"本次返回条数"在没有截断时是同一个数，这个字段只是让 LLM 不用自己数 `results` 数组长度）。

**`group_by`**：

```json
{"groups": [{"value": "红色", "count": 12}, {"value": "白色", "count": 8}]}
```

**校验失败**（第4节任一层不通过）：

```json
{"error": "字段 'unknown_field' 不是 SKU 已确认的属性字段", "field": "unknown_field"}
```

结构化错误，不是异常——让 LLM 有机会读到具体原因、调整参数重试，与 `_dispatch_tool_call` 现有的 `{"error": ...}` 观察结果模式一致（`app/agent/planner.py:123`）。

## 8. 与现有工具注册机制的接入点

跟随 `graph_query_tool` 的现有模式（不新增机制）：

- `app/agent/tools.py`：新增 `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 常量 + `structured_filter_query_tool(...)` 执行体函数。
- `app/agent/planner.py`：`_TOOL_SCHEMAS` 追加新 schema；`_dispatch_tool_call` 追加 `if name == "structured_filter_query_tool":` 分支，`tenant_id` 同样只用调用方从 `AgentState` 注入的值，完全不采信 `arguments` 里任何同名字段。
- `app/graphrag/neo4j_client.py`：新增 `Neo4jGraphClient.run_structured_filter_query(...)` 方法（或类似命名），封装第5节的 Cypher 构造+执行逻辑，供 `structured_filter_query_tool` 调用。

## 9. 范围外事项（留给未来评估）

- 全文本模糊搜索/相似度匹配（`contains` 运算符）——MUJI 文档没有对应问法证据，YAGNI。
- 3跳以上遍历——没有证据需求。
- 跨 `kind=relation` 约束共享同一个中间节点变量（如"经过同一个 Product 既满足A又满足B"而不是"两个独立的 EXISTS"）——问法9的语义是"独立分支都满足"，不需要共享变量，暂不支持更复杂的变量绑定共享。
- `ontology_categories.py::ExtraFieldSpec.name` 缺少字符集校验——第6节已识别，留给实施计划阶段与索引建立一并处理。
- 结果排序（如"价格从低到高"）——本次只支持 `group_by` 里的 `count DESC` 固定排序，任意字段排序留给未来评估。
