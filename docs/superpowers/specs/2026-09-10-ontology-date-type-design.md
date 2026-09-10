# 本体日期类型设计

> 日期：2026-09-10
> 取代：`2026-09-02-guided-ontology-modeling-design.md` §3.4「日期列的已知限制」

## 1. 要解决什么

今天数据模型只有 `string / number / integer / number[]`
（`app/graphrag/ontology_categories.py:31`）。日期列被存成 `string`，于是
「上个月的订单」「今年以来的销量」这类问题在图谱层做不了范围过滤，只能精确匹配。

这不是设计上的否决，是一次记过账的延期：`2026-09-02` 那份 spec 的 §3.4 明写了这个
限制、§「不做的事」明写了「日期类型支持。只做提示，不改数据模型」，理由是要动数据
模型和查询层。本次把它补上。

**成功判据**：管理员把一列声明成日期类型之后，用户问「上个月的订单有多少」，图谱层
用 `purchase_date >= '2026-08-01' AND purchase_date <= '2026-08-31'` 答得出来；问
「三月的订单」时，一个写成 `2026/3/1` 的源数据行不会被静默漏掉——要么在导入时就
被归一成 `2026-03-01`，要么进跳过行明细让管理员看得见。

## 2. 四个定型决策

| 决策 | 选择 | 被否掉的选项和理由 |
|---|---|---|
| 时间粒度 | **只到天** | 到秒/跨时区要上 Neo4j 原生 `datetime`，写入路径、驱动类型转换、API 序列化都要改，存量字符串日期得真迁移一次。业务日期（下单日、入库日）到天就够 |
| 相对时间在哪算 | **后端**，加具名区间算子 | 让 LLM 拿着「今天是几号」自己算绝对值：跨年、月末、闰年算错是出了名的，而算错之后结果看上去完全正常——静默失败 |
| 非 ISO 值怎么办 | **归一无歧义格式，拒掉有歧义的** | 「尽量猜」要靠启发式判日/月顺序（比如看整列有没有 >12 的值），整列都 ≤ 12 时猜错且无从发现 |
| 存量值怎么办 | **确认本体时先校验，不合格就拒** | 「顺手归一」会让一次「声明类型」的操作改写图里的真实数据；「不管存量」则让不合格行在范围过滤里静默漏掉 |

## 3. 数据模型

### 3.1 新增 `date` value_type

`ontology_categories.py:31`：

```python
_VALID_EXTRA_FIELD_VALUE_TYPES = frozenset({"string", "number", "integer", "number[]", "date"})
```

`_VALID_STANDARD_NAME_VALUE_TYPES`（第 32 行）**不加 `date`**。`standard_name` 是实体
的名字，一个日期不该当实体的名字。两张白名单从此不再对称，代码里要写明这是有意的
——否则下次读到的人会以为是漏了。

#### 加一个 value_type 要改四处，不是一处

这一节最初只写了 `ontology_categories.py`，实现之后的评审实测发现另外三处也各自
独立维护着一份类型枚举，漏改任何一处都会让新类型在某条路径上不可用，而且**都不会
在单元测试里暴露**——它们分别在四个不同的模块里：

| 位置 | 管什么 | 漏改的后果 |
|---|---|---|
| `ontology_categories.py::_VALID_EXTRA_FIELD_VALUE_TYPES` | 单条创建/更新 term_type | 后台单条声明就被 400 拒掉 |
| `ontology_lifecycle.py::_VALID_EXTRA_FIELD_VALUE_TYPES` | `replace_draft`（引导页一次写入整套本体的唯一入口） | 单条接口放行、引导页拒绝。该模块顶部的注释自己就预言过这种不一致 |
| `terms_store.py::_extra_property_value_matches_type` | 写库前的类型闸 | 值已经归一正确，这一步仍判类型不匹配抛 `InvalidExtraPropertyTypeError`，**整批导入挂掉** |
| `schema_etl_sample.py::_example_values_for` | 「下载 ETL 示例文件」 | 抛通用 `ValueError`，该租户的示例文件下载整体 500 |

后两处尤其隐蔽：`terms_store` 那个函数落不到任何分支时**隐式返回 `None`**（falsy），
也就是「不认识的类型一律判为不匹配」，没有任何提示说它不认识这个类型。

配套的一处：`schema_etl.py::_write_entity_mapping` 的 except 元组要包含
`InvalidExtraPropertyTypeError`。它和 `UnknownCategoryError` 是同一处代码抛出的姊妹
异常，都是**单行数据的形状问题**，归宿是跳过行明细；不纳入的话，一行坏数据会杀掉
整批导入，而管理员看到的是一个 500，不是「第 N 行的这个值不对」。

**测试上的教训**：四处里有三处的缺陷，在各自模块的单元测试里全是绿的——因为每个
模块只测自己。抓住它们的是一条跨过模块边界的端到端测试（`run_schema_etl` 一路写到
图里）。以后加类型时，那条端到端测试比四处白名单的单元测试更值钱。

### 3.2 值的不变量

**图里存的日期值一律是补零的 `YYYY-MM-DD`，10 个字符。**

这是 `date` 类型存在的**全部**意义：字典序等于时间序，于是 `>=` / `<=` 直接可用。
`2026-1-5` 混进来就把这个前提毁掉了，而且毁得无声无息——它排在 `2026-10-05` 前面，
范围过滤漏行、排序错位，一个错误都不报。

所以每一条写入路径都必须保证这个不变量。目前只有一条（ETL，见 §5）；将来新增写入
路径时这条约束必须一起带上。

### 3.3 存储与索引

Neo4j 里仍是**字符串属性**，不用原生 `date` 类型——字典序已经给了正确的顺序，换原生
类型只是把同一件事换个更贵的做法。

但 `neo4j_client.py:1489` 的 `_SCALAR_VALUE_TYPES` 必须加 `"date"`：

```python
_SCALAR_VALUE_TYPES = {"string", "number", "integer", "date"}
```

漏掉它功能照样对，只是日期字段建不出索引，而范围过滤恰恰是最需要索引的那类查询
——每次全表扫，压测之前谁也看不出来。

## 4. 查询层

### 4.1 算子表

`structured_filter_query.py:44` 附近：

```python
_DATE_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "eq", "ne", "in_period"})
_OPERATORS_BY_VALUE_TYPE["date"] = _DATE_OPERATORS
_VALID_OPERATORS = _STRING_OPERATORS | _NUMERIC_OPERATORS | _ARRAY_OPERATORS | _DATE_OPERATORS
```

**不给 date 开 `starts_with`。** `starts_with("2026-08")` 确实能表达「8 月」，但那是把
日期当字符串用的旁门。有了 `in_period` 和绝对区间，第三种写法只会让 LLM 在三条路
之间摇摆，而三条路的边界行为并不完全一致（`starts_with` 表达不了跨月区间）。

`in_period` 用在非 date 字段上，由现有的
`_validate_operator_for_value_type`（第 297 行）自动拒掉，不用另写。

### 4.2 具名区间

新模块 **`app/graphrag/date_periods.py`**：

```python
class InvalidPeriodError(ValueError):
    """区间名不在白名单里。"""


def resolve_period(name: str, *, today: date) -> tuple[str, str]:
    """把具名区间算成 [start, end] 两个 ISO 日期字符串，**两端都含**。

    today 是注入的，不是函数里 date.today()：跨年（1 月问 last_month 要得到
    去年 12 月）、月末（3 月 31 日问 last_month 要得到 2 月 1–28/29 日）、
    闰年这几条边界只有注入才钉得住。
    """
```

白名单 12 个：

| 名字 | 语义 | 2026-09-10 为例 |
|---|---|---|
| `today` | 今天 | 2026-09-10 ~ 2026-09-10 |
| `this_week` | 本周（周一起） | 2026-09-07 ~ 2026-09-13 |
| `last_week` | 上周 | 2026-08-31 ~ 2026-09-06 |
| `this_month` | 本自然月 | 2026-09-01 ~ 2026-09-30 |
| `last_month` | 上一个自然月 | 2026-08-01 ~ 2026-08-31 |
| `this_quarter` | 本季度 | 2026-07-01 ~ 2026-09-30 |
| `last_quarter` | 上一季度 | 2026-04-01 ~ 2026-06-30 |
| `this_year` | 今年以来到年底 | 2026-01-01 ~ 2026-12-31 |
| `last_year` | 去年 | 2025-01-01 ~ 2025-12-31 |
| `last_7_days` | 含今天往回 7 天 | 2026-09-04 ~ 2026-09-10 |
| `last_30_days` | 含今天往回 30 天 | 2026-08-12 ~ 2026-09-10 |
| `last_90_days` | 含今天往回 90 天 | 2026-06-13 ~ 2026-09-10 |

**「上个月」和「最近一个月」不是一回事**，这是这张表存在的理由：前者是自然月
（`last_month`），后者是滚动窗口（`last_30_days`）。中文里用户说「上个月的订单」几乎
总是指自然月。两种语义各有各的名字，就不会混。

`this_week` / `this_month` / `this_quarter` / `this_year` 的**结束端取到周期末尾而不是
今天**：数据里不会有未来的日期，取到末尾和取到今天在结果上等价；而取到末尾在语义上
才对得住「本月」这个词——万一真有预约、排期这类未来日期的数据，取到今天会把它们
漏掉，而用户问「本月的排期」时要的正是那些。

周起点定为**周一**（国内业务惯例），写进 docstring。

「最近 45 天」这类任意窗口不在白名单里，走绝对区间——LLM 拿到当前日期就能产出，
那条路径本来就要留着。

### 4.3 展开

在 `validate_structured_filter_query` 通过之后、执行之前，加一步
`_expand_period_constraints`：把一条 `in_period` 约束换成**两条**（`gte` start +
`lte` end）。关系约束同理，展开成两条 hops 相同的 `RelationConstraint`。

这样做的关键好处是 **`neo4j_client.py` 一行不用改**：
`_COMPARISON_OPERATOR_TO_CYPHER`（第 219 行）已经有 `>=` 和 `<=`，多条约束本来就是
`" AND ".join(where_clauses)`（第 953 行）。新语义完全落在校验+展开这一层，图谱执行
层的攻击面不变。

`today` 由 `run_structured_filter_query` 新增的 `today: date | None = None` 参数提供，
`None` 时取服务器本地日期。

区间名不在白名单 → `StructuredFilterQueryError`，消息里列出全部可用值（跟
`_validate_operator_for_value_type` 同一个路子：报错要让 LLM 下一次能产出对的）。

### 4.4 LLM 工具契约

`app/agent/tools/structured_filter_query/tool.py` 的 `operator`（第 96 行）和
`target_operator`（第 120 行）：enum 加 `"in_period"`，描述里说明它只对日期类型字段
可用、值是具名区间，并列出 12 个名字。

工具描述里同时给出**当前日期**，让 LLM 在需要任意窗口（「最近 45 天」）时能自己算
绝对区间。

## 5. 写入层

### 5.1 归一化

新模块 **`app/graphrag/date_normalization.py`**：

```python
class InvalidDateValueError(ValueError):
    """认不出、或者格式合法但不是真实日期。"""


def normalize_date(raw: str) -> str:
    """把无歧义的日期写法归一成 YYYY-MM-DD。"""
```

**认**（一律四位年份在前，所以日/月谁在前不存在歧义）：

| 写法 | 例 |
|---|---|
| `YYYY-M-D` / `YYYY-MM-DD` | `2026-1-5`、`2026-01-05` |
| `YYYY/M/D` | `2026/1/5` |
| `YYYY.M.D` | `2026.1.5` |
| `YYYY年M月D日` | `2026年1月5日` |
| `YYYYMMDD` | `20260105` |

补零之后还要用 `date(y, m, d)` 真构造一次：`2026-02-30` 格式合法但不是日期，不拦住
它就会在图里躺一个永远排不对序的值。

**拒**：

- `03/04/2026`、`15-01-2026` 这类日/月在前的两位数写法——分不出三月四日还是四月三日
- `Jan 15, 2026` 英文月名——其实无歧义，但要引入 locale 表，先不做，需要时再加

**Excel 序列号不认。** 序列号本身在数据上跟一个普通整数无法区分，认它就是在猜。
（未验证：`convert_excel_cell_to_string`——`schema_etl_row_processing.py:114-117`——
对零点整的 `datetime` 转成 `%Y-%m-%d`，对带时间的转成 `%Y-%m-%d %H:%M:%S`，不是统一
转成 `%Y-%m-%d`；两种格式 `normalize_date` 都会拒，但不能据此断言"序列号只在未格式化
时才冒出来"。）

### 5.2 接入点

`convert_field_value`（`schema_etl_row_processing.py:65`）加 `date` 分支调
`normalize_date`，`InvalidDateValueError` 转成 `RowProcessingError`。

空值不用管：`etl_projection.py:115` 的
`if source_column in row and row[source_column]` 已经把空串滤掉了。

失败走**已有的**跳过行明细（`etl_skipped_rows.py`），管理员在报错明细页看得见是哪一
行、哪个值、为什么。这条路径已经存在，不新建。

## 6. 存量校验

`ontology_term_types` 的主键是 `(tenant_id, value, status)`，schema 改动走
draft → confirm；`update_term_type`（`ontology_categories.py:374`）写的是草稿。所以
「查询开始照着新类型跑」的分界点只有一个：`POST /{tenant_id}/confirm`
（`admin_ontology_routes.py:599`）。

在那里插一步：

1. 比对新旧两份 schema，找出**这次从非 date 变成 date** 的字段。没变的不查——否则
   每次确认本体都要扫一遍全图。
2. 对每个这样的字段查一次 Neo4j：新增 graph_client 方法
   `count_non_iso_date_values(tenant_id, term_type, field) -> tuple[int, list[str]]`，
   返回不合格数量和最多 3 个样例值。
3. 有不合格的 → **409**，消息点名：

> `purchase_date` 还有 12 个实体的值不是 YYYY-MM-DD，例如 `2026/1/15`、`待定`。
> 改成日期类型之前先重新导入这份数据，否则这些实体在按时间过滤时会被静默漏掉。

跟删组织那条 409（`admin_org_routes.py`）同一个路子：说清楚下一步做什么，而不是
「确认失败」。

**判定「合格」的口径**：值已经是补零 ISO——`normalize_date(v)` 不抛异常**且**返回值
等于 `v` 本身。不是「能归一」：`2026/1/15` 能归一，但它此刻躺在图里的样子仍然会破坏
字典序，放行等于把问题留在数据里。抛异常（认不出）和归一后不等于原值（写法对但没
补零）都算不合格，两者在报错里不区分——对管理员来说下一步都是同一件事：重新导入。

## 7. UI

| 文件 | 改动 |
|---|---|
| `frontend/src/admin/OntologySchemaPage.tsx:101` | `VALUE_TYPES` 加 `'date'` |
| `frontend/src/admin/guidedOntology/draftProposal.ts:132` | `measureValueType` 里 `date` 角色从返回 `'string'` 改成 `'date'`，删掉「数据模型没有日期类型」那条注释 |
| `frontend/src/admin/guidedOntology/ProposalReview.tsx:337` | 「日期列的限制」整节改写：不再是限制，而是说明这列会存成日期类型、可以问「上个月的」，并列出认得的写法 |

引导流程的**判定逻辑不用动**：`columnStats.ts` 的 `DATE_PATTERN` 是
`^\d{4}[-/]\d{1,2}[-/]\d{1,2}`，四位年份在前——它判成日期的列，`normalize_date` 必然
认得，两边天然一致。

**已知缺口**：`2026年1月5日` 和 `20260105` 这两种 `normalize_date` 认得的写法，
`DATE_PATTERN` 判不出来，那样的列会落进 freetext（在「没有用到的列」里可见，不是
静默丢弃）。管理员可以在本体结构页手工把它加成日期字段。要收口的话是扩
`DATE_PATTERN`，不在本次范围。

## 8. 测试

每条断言都要能回答「什么错误实现能让它照样绿」。

**`resolve_period`**（纯函数，`today` 注入）
- 1 月 15 日问 `last_month` → 2025-12-01 ~ 2025-12-31（跨年）
- 3 月 31 日问 `last_month` → 2026-02-01 ~ 2026-02-28（月末，且上个月更短）
- 2024 年 3 月 31 日问 `last_month` → 2024-02-01 ~ 2024-02-29（闰年）
- 周日问 `this_week` → 该周周一到周日（周起点是周一，不是周日）
- `last_7_days` 含今天（7 天窗口，不是 8 天）
- 白名单外的名字抛 `InvalidPeriodError`，消息列出可用值

**`normalize_date`**
- `2026-1-5` → `2026-01-05`，**并断言它字符串小于 `2026-10-05`**。只断言相等的话，一个
  「原样返回」的实现能过掉一半用例；排序断言才是这个函数存在的理由
- 五种认得的写法各一条
- `2026-02-30` 被拒（格式合法但不是日期）
- `03/04/2026` 被拒，且消息说得出为什么（有歧义），不是笼统的「格式错误」
- `45678`（Excel 序列号）被拒

**查询层**
- `in_period: last_month` 展开成两条约束，运算符分别是 `gte` / `lte`，值是那个月的
  首末日
- 关系约束里的 `in_period` 展开成两条 hops 相同的约束
- 日期字段用 `starts_with` 被拒
- `in_period` 用在 number 字段上被拒

**ETL**
- 一行 `2026/1/15` 归一成 `2026-01-15` 写进图
- 一行 `03/04/2026` 进跳过行明细，且明细里能看到原值和原因

**确认本体**
- 字段从 string 改成 date、图里有不合格值 → 409，消息里有数量和样例
- 同上但值全合格 → 放行
- 字段本来就是 date（这次没变） → 不查图（用一个会抛异常的假 graph_client 钉住）

## 9. 不做的事

- **时区。** 「今天」按服务器本地日期算。没有租户时区概念。工具描述里把当前日期告诉
  LLM，让它在需要时改用绝对区间
- **时分秒。** 只到天
- **`datetime` 类型**、**`standard_name` 用日期类型**
- **英文月名、Excel 序列号**的归一化
- **存量数据自动归一化。** 选的是「拒改」而不是「顺手改」——一次「声明类型」的操作
  不该改写图里的真实数据
- **扩 `DATE_PATTERN`** 让引导流程认出 `2026年1月5日`（见 §7 已知缺口）
