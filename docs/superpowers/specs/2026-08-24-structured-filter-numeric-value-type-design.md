# structured_filter_query_tool 支持"值即节点"类型的数值区间过滤 — 设计文档

## 背景

用户报告："销量大于50的有多少个订单"和"coke-cola类目有多少个订单"这两个问题，Agent 没有基于知识图谱回答，而是转人工。

调研（用真实请求 + 临时调试日志复现，日志已清理）确认这是两个独立问题：

1. **"销量大于50的有多少个订单"——结构性缺陷，无法通过换问法绕过。** demo 租户的知识图谱由表格 ETL 生成，把"销量"这类数值列建模成了**独立的实体类型**（每个销量取值如 100、101、102… 都是图里单独的 `Term` 节点，通过 `BELONG_TO` 关系挂到对应的"订单号"节点上），而不是"订单号"节点上的一个数值属性。`structured_filter_query_tool` 里，比较"某类型自身的取值"只能用保留字段 `standard_name`，而 `app/graphrag/structured_filter_query.py::_resolve_field_value_type` 把 `standard_name` **硬编码为 `value_type="string"`**，导致 `gt/gte/lt/lte` 这些数值运算符对 `standard_name` 一律被拒绝（只允许 `eq/ne/starts_with`）。日志显示 LLM 的探索路径其实是对的：发现"销量"类型只有 `standard_name` 一个字段，正要用它做 `> 50` 比较时被规则拒绝，3 轮工具调用预算耗尽后放弃。
2. **"coke-cola类目有多少个订单"——主要是 LLM 策略问题，不在本次修复范围。** 已用手写查询验证：`anchor_term_type=订单号`，1 跳 `BELONG_TO→产品`，`target_field=standard_name, operator=eq, target_value="Cola"` 能通过校验、是可行路径。日志显示 LLM 全程只调用了 `graph_query_tool` 和 `vector_search_tool`，从未尝试 `structured_filter_query_tool` 去数数量，3 轮用完后诚实地回答"查不到"。这是 prompt/工具描述/轮次预算层面的问题，本次不处理。

调研中还发现一个相关但独立的缺陷：`structured_filter_query_tool` 返回的 `matched_count` 实际上是"受 `limit`（默认20）截断后的行数"，不是真实总数——即使问题 1 修好，"有多少个"这类计数问题如果真实匹配数超过 `limit`，也会被系统性地报少。本次一并修复。

## Global Constraints

- 老数据/老 term type 必须保持行为不变：新字段默认值必须让所有现存 term type 的查询行为与修复前完全一致，不需要强制迁移或人工介入。
- Neo4j 里 `standard_name` 属性物理上仍然按字符串写入（`_SYNC_TERM_QUERY` 不变，不做存储格式迁移）——数值比较通过 Cypher 层的运行时类型转换（`toFloat`/`toInteger`）实现，不改写入路径。
- `structured_filter_query.py::validate_structured_filter_query`/`_resolve_field_value_type` 现有的字段名/关系类型白名单校验（注入防线）不能削弱——新增的类型解析逻辑只是把"返回值从哪来"从硬编码换成读 schema 声明，不改变"值必须来自已确认 schema"这个前提。
- `matched_count` 修复不能改变现有 `results` 数组本身受 `limit` 截断的行为（LLM 上下文成本控制），只改 `matched_count` 这个数字本身的语义，并显式标注是否发生了截断。

## 架构总览

三层配合：

1. **本体层**（`ontology_categories.py` + `ontology_term_types` 表）：给每个 term type 新增一个可选声明"自身取值的类型"（`standard_name_value_type`，默认 `"string"`）。
2. **查询校验层**（`structured_filter_query.py`）：`_resolve_field_value_type` 遇到保留字段 `standard_name` 时，读这个新声明而不是硬编码 `"string"`。
3. **Cypher 执行层**（`neo4j_client.py`）：生成比较表达式时，如果这次比较的目标字段被声明成 `number`/`integer`，用 `toFloat(...)`/`toInteger(...)` 包裹属性访问，让 Cypher 运行时把字符串属性转成数字再比较——不这样做的话，即使放开了校验，`"60" > 50` 这种字符串跟数字的比较在 Neo4j 里不报错、只是静默返回 `null`（不匹配），问题会从"报错拒绝"变成更隐蔽的"静默查出 0 条"。

再加一个独立但顺带修的点：

4. **`matched_count` 修复**（`neo4j_client.py` + `structured_filter_query.py`）：非 `group_by` 分支的执行逻辑额外跑一次不带 `LIMIT` 的 `count(anchor)` 查询，`matched_count` 用这个真实总数，而不是当前的 `len(受limit截断的行)`；当真实总数大于返回的行数时，结果里加一个 `truncated: true` 标记，让 LLM/最终用户清楚"这个数字是真的，但下面列出的明细只是前 N 条"。

管理后台（`OntologySchemaPage.tsx`）需要加一个表单控件，让人工能声明某个 term type 的 `standard_name_value_type`——没有这个入口，即使代码修好了，demo 租户的"销量"/"收入"这两个类型也没法被标成 `number`，问题 1 实际上还是打不开。

## 详细设计

### 1. 本体层：`ontology_categories.py`

```python
@dataclass(frozen=True)
class TermTypeCategory:
    value: str
    extra_fields: list[ExtraFieldSpec]
    standard_name_value_type: str = "string"
```

新增校验常量（复用 extra_fields 的类型集合，但排除 `number[]`——一个节点自身的名字/取值只能是标量，不可能是数组）：

```python
_VALID_STANDARD_NAME_VALUE_TYPES = frozenset({"string", "number", "integer"})
```

`create_term_type`/`update_term_type` 签名新增 `standard_name_value_type: str = "string"` 参数，写入前校验：

```python
def _validate_standard_name_value_type(value_type: str) -> None:
    if value_type not in _VALID_STANDARD_NAME_VALUE_TYPES:
        raise InvalidExtraFieldTypeError(
            f"term type 自身取值类型 {value_type!r} 不合法，"
            f"仅支持: {sorted(_VALID_STANDARD_NAME_VALUE_TYPES)}"
        )
```

`_row_to_term_type`/`_SCHEMA_SQL`/`list_term_types` 的 SELECT 都要带上这一列。

DB 迁移函数（复用文件里已有的"检测列是否存在→ALTER TABLE 加列"模式，比前几个迁移函数简单，不需要建新表复制数据，因为 SQLite 的 `ALTER TABLE ADD COLUMN ... DEFAULT ...` 本身就是就地、向后兼容的）：

```python
async def _migrate_term_types_add_standard_name_value_type_if_needed(
    conn: aiosqlite.Connection,
) -> None:
    cursor = await conn.execute("PRAGMA table_info(ontology_term_types)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "standard_name_value_type" in existing_columns:
        return
    await conn.execute(
        "ALTER TABLE ontology_term_types "
        "ADD COLUMN standard_name_value_type TEXT NOT NULL DEFAULT 'string'"
    )
    await conn.commit()
```

在 `ensure_categories_schema` 里接在其它迁移函数之后调用。`_SCHEMA_SQL` 里的 `CREATE TABLE IF NOT EXISTS` 也要加上这一列（给全新数据库用，迁移函数只服务已存在的老库）。

### 2. 查询校验层：`structured_filter_query.py`

`_resolve_field_value_type` 当前实现：

```python
def _resolve_field_value_type(
    *, term_type: str, field: str, term_type_schema: dict[str, TermTypeCategory]
) -> str:
    if field == _RESERVED_FIELD_NAME:
        return "string"
    ...
```

改成：

```python
def _resolve_field_value_type(
    *, term_type: str, field: str, term_type_schema: dict[str, TermTypeCategory]
) -> str:
    category = term_type_schema.get(term_type)
    if category is None:
        raise StructuredFilterQueryError(
            f"term_type {term_type!r} 不在已确认 schema 里，"
            f"可用的 term_type: {sorted(term_type_schema.keys())}"
        )
    if field == _RESERVED_FIELD_NAME:
        return category.standard_name_value_type
    for spec in category.extra_fields:
        ...  # 其余不变
```

（原来 `field == _RESERVED_FIELD_NAME` 分支在查 `category` 之前就直接返回，本次顺手把"term_type 是否在 schema 里"这个检查提到最前面、两条分支共用——调用方目前确实总是先校验过 term_type 才会走到这里，这个改动不改变现有行为，只是让函数自身也对得上它文档字符串里"不查 schema 是否已确认"这句话已经不准确的部分做了小范围更新用词，不需要额外测试这个理论上不可达的分支）。

### 3. Cypher 执行层：`neo4j_client.py`

`_comparison_expression` 加一个可选的 `cast` 参数：

```python
_CAST_BY_VALUE_TYPE = {"number": "toFloat", "integer": "toInteger"}

def _comparison_expression(
    *, prop_expr: str, operator: str, param_name: str, cast: str | None = None
) -> str:
    if cast is not None:
        prop_expr = f"{cast}({prop_expr})"
    if operator == "starts_with":
        return f"{prop_expr} STARTS WITH ${param_name}"
    ...  # 其余分支不变，全部用替换后的 prop_expr
```

`execute_structured_filter_query` 现在需要知道每个约束的目标字段是不是数值型 `standard_name`，因此签名新增 `term_type_schema` 参数（`run_structured_filter_query` 已经持有这份数据，直接透传）：

```python
async def execute_structured_filter_query(
    self,
    args: StructuredFilterQueryArgs,
    *,
    tenant_id: str,
    term_type_schema: dict[str, TermTypeCategory],
) -> list[dict[str, Any]] | dict[str, Any]:
```

两处生成比较表达式的调用点分别按各自的 term_type 解析 cast：

- `AttributeConstraint` 分支：`term_type=args.anchor_term_type, field=constraint.field`
- `RelationConstraint` 分支：`term_type=last_hop.target_term_type, field=constraint.target_field`

```python
def _resolve_cast(
    *, term_type: str, field: str, term_type_schema: dict[str, TermTypeCategory]
) -> str | None:
    if field != _RESERVED_FIELD_NAME:
        return None  # 只有比较"节点自身取值"才可能需要转换；extra_fields 数值属性
                      # 在 Neo4j 里本来就是按声明类型写入的（见 merge_term 的 SET t += 逻辑），
                      # 不需要运行时转换
    category = term_type_schema.get(term_type)
    if category is None:
        return None
    return _CAST_BY_VALUE_TYPE.get(category.standard_name_value_type)
```

（`_RESERVED_FIELD_NAME` 常量目前定义在 `structured_filter_query.py`，`neo4j_client.py` 需要 import 它，或者本地复制一份同款字符串常量——参照文件里已有的"关系类型名格式正则两处独立定义，不跨模块导入"先例，本次用同样的处理方式：`neo4j_client.py` 里独立定义 `_RESERVED_FIELD_NAME = "standard_name"`，理由同源：两处校验的是不同职责层，各自演化不构成重复劳动。）

`group_by` 分支不受影响——分组统计的是"按这个字段的不同取值分组计数"，不涉及数值比较，字符串形态的取值一样能正确分组。

### 4. `matched_count` 修复

`execute_structured_filter_query` 非 `group_by` 分支，`where_sql` 拼好之后，除了现有的按 `limit` 取行的查询，多跑一次不带 `LIMIT` 的计数查询：

```python
count_query = (
    "MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type}) "
    f"WHERE {where_sql} "
    "RETURN count(anchor) AS total"
)
async with self._driver.session() as session:
    count_result = await session.run(count_query, {k: v for k, v in params.items() if k != "limit"})
    total_count = (await count_result.data())[0]["total"]
    rows_result = await session.run(query, params)
    rows = await rows_result.data()
return {"rows": rows, "total_count": total_count}
```

返回类型从"裸 list"改成一个带 `rows`/`total_count` 的 dict（`group_by` 分支已经是返回 dict 的 `{"groups": [...]}` 形态，二者在 `run_structured_filter_query` 里已经要靠 `isinstance(result, dict)` 分支处理，这次让非 group_by 分支也统一走 dict，不再有"裸 list vs dict"两种返回形态，`run_structured_filter_query` 的分支判断相应简化）。

`run_structured_filter_query` 组装最终结果：

```python
if isinstance(result, dict) and "groups" in result:
    return result
rows = result["rows"]
total_count = result["total_count"]
payload: dict[str, Any] = {
    "matched_count": total_count,
    "results": [... 跟现在一样的 dict 推导式，遍历 rows ...],
}
if total_count > len(rows):
    payload["truncated"] = True
return payload
```

### 5. 管理后台：`OntologySchemaPage.tsx`

`TermType` interface 加字段：

```typescript
interface TermType {
  value: string
  extra_fields: ExtraFieldSpec[]
  standard_name_value_type: string
}
```

`emptyTermTypeDraft` 默认 `standard_name_value_type: 'string'`。表单里"类型名"输入框下方加一个下拉框（复用现有 `VALUE_TYPES` 常量，但要去掉 `number[]`——新增一个 `STANDARD_NAME_VALUE_TYPES = ['string', 'number', 'integer'] as const`，跟 extra_fields 那个下拉分开定义，二者允许的取值集合本来就不同）：

```tsx
<label className="flex flex-col gap-1 text-sm font-bold text-ink">
  自身取值类型
  <select
    value={draft.standard_name_value_type}
    onChange={(e) => setDraft((prev) => ({ ...prev, standard_name_value_type: e.target.value }))}
    className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:shadow-soft focus:outline-none ${focusRing}`}
  >
    {STANDARD_NAME_VALUE_TYPES.map((t) => (
      <option key={t} value={t}>{t}</option>
    ))}
  </select>
  <span className="text-xs text-ink-soft">
    这个类型的实例本身代表什么类型的值（比如"销量""收入"这类每个取值都是独立节点的类型，
    应该声明成 number，才能用"大于/小于"做区间查询；大多数类型（产品名、公司名…）保持默认的 string 即可）
  </span>
</label>
```

列表表格也顺手加一列展示当前值（在"属性字段数"旁边），方便管理员一眼看出哪些类型已经声明成数值型。

`app/api/admin_ontology_routes.py`：`TermTypeWriteRequest` 加 `standard_name_value_type: str = "string"` 字段，`create_term_type_category`/`update_term_type_category` 透传给 `create_term_type`/`update_term_type`，响应体（`payload.model_dump()`）自然带上这个字段，前端不需要额外改列表接口的解析逻辑。

## 错误处理

- `standard_name_value_type` 值非法（不在 `{"string","number","integer"}` 里）：复用现有 `InvalidExtraFieldTypeError` → 路由层已有的 400 处理分支不用改。
- `execute_structured_filter_query` 的计数查询本身失败（Neo4j 连接问题等）：跟现有的行查询失败处理方式一致——`run_structured_filter_query` 外层已经有一层 `except Exception` 把执行阶段的异常统一降级成 `{"error": ...}` 观察结果返回给 LLM，不需要给计数查询单独加 try/except。
- 老 term type（`standard_name_value_type` 迁移后是默认值 `"string"`）的查询行为完全不变：`_resolve_cast` 对 `"string"` 返回 `None`，`_comparison_expression` 不做任何包裹，跟修复前生成的 Cypher 逐字节相同。

## 测试

- `ontology_categories.py`：新字段的 `create_term_type`/`update_term_type` 读写往返；非法值拒绝；`_migrate_term_types_add_standard_name_value_type_if_needed` 的幂等性（跑两次不报错、不重复加列）；老库迁移后默认值正确。
- `structured_filter_query.py`：`_resolve_field_value_type` 对声明成 `number`/`integer` 的 term type，`standard_name` 现在返回对应类型而不是硬编码 `"string"`；`gt/gte/lt/lte` 对这类 term type 的 `standard_name` 字段现在通过校验（之前会被拒绝）；对默认 `"string"` 的 term type，`gt` 等运算符依然被拒绝（行为不变，防回归）。
- `neo4j_client.py`（复用现有 `FakeSession`/`FakeDriver`）：
  - 数值型 `standard_name` 比较时，`session.last_query` 里能看到 `toFloat(anchor.standard_name)` 或 `toInteger(...)` 包裹；字符串型（默认）时看不到任何 cast 包裹，跟修复前生成的查询文本一致。
  - `matched_count` 修复：需要先给 `FakeSession` 加一点能力——现在它对每次 `.run()` 调用都返回同一份 `self._rows`，测"真实总数 > 返回行数"这个场景需要计数查询和取行查询返回不同形状的数据。建议给 `FakeSession` 加一个可选的按调用顺序消费的结果队列（不影响其它现有测试，它们都只传一次 `rows` 且没有用到这个新能力，行为不变）。用这个能力验证：`matched_count` 等于计数查询返回的 `total`，不等于 `len(rows)`；`total_count > len(rows)` 时结果带 `truncated: true`；相等时不带这个字段（不给 LLM 制造"是不是漏了什么"的疑惑）。
  - `group_by` 分支不受影响：加一个回归测试确认它的返回形态和生成的 Cypher 没有变化。
- `admin_ontology_routes.py`：创建/更新 term type 时能正确读写 `standard_name_value_type`；非法值时返回 400。
- 端到端（可选，不强制）：用真实/复现环境跑一遍"销量大于50的有多少个订单"，确认在把 demo 租户"销量"类型手动标成 `number` 之后，Agent 能给出基于图谱的真实数字答案，不再转人工。

## 落地后的手动步骤（不在代码修复范围内）

代码修好之后，`demo` 租户需要有人在管理后台把"销量"和"收入"这两个 term type 的"自身取值类型"编辑成 `number`，这条修复才会对现有数据生效——这是数据 owner 的判断（要不要真的把这两个类型标成数值型），不是代码能替他做的决定，实现计划完成后可以单独问用户要不要顺手做这一步。
