# 多跳计数扇出陷阱检测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `structured_filter_query_tool` 在做多跳计数时自动发现"路径经过多对多关系、计数被放大"的情况，把返回结构里那句肯定语气的说明换成带放大倍数的警告。

**Architecture:** 不新增任何 schema、不引入人工基数声明。在两个图后端各加一个廉价聚合方法 `probe_relation_fanout`，问"这一跳的起点节点最多能走到几个终点节点"；`run_structured_filter_query` 只在计数意图（`limit == 0` 或 `group_by`）且存在两跳及以上的 `RelationConstraint` 时，对**第一跳之后**的每一跳探测，命中就改写返回结构。

**Tech Stack:** Python 3.12 / Neo4j Cypher / Neptune openCypher / pytest（`asyncio_mode=auto`）

**Spec:** `docs/superpowers/specs/2026-08-29-fan-trap-detection-design.md`

## Global Constraints

- **只探测第一跳之后的跳。** 第一跳是被计数实体自身的关系，它的多对多性质正是查询语义的一部分（"Coca-Cola 卖多少种产品"合法），探测它会误报。
- **`relation_type` 走字符串插值拼进查询文本，不参数化。** Cypher/openCypher 无法参数化关系类型。安全性依赖调用方已跑过 `validate_structured_filter_query`（格式正则 `^[A-Z][A-Z0-9_]{0,63}\Z` + 该租户已确认 `relation_type` 成员校验）——这跟 `neo4j_client.py::execute_structured_filter_query` 现有的插值理由完全一致，见那个方法的 docstring。`term_type` 一律参数化（它们是普通属性值，可以参数化）。
- **两个后端都必须真实实现。** `probe_relation_fanout` 在查询主链路上，不能像 `NeptuneGraphClient.count_relation_edges_for_term` 那样抛 `NotImplementedError`——那会让 `graph_backend="neptune"` 时所有多跳计数直接崩溃。
- **有扇出警告时不能同时保留原来那句肯定语气的 note。** 两条互相矛盾的措辞并存会让模型无所适从。无警告时现有 note 原样保留。
- **不做缓存。** 先测实际延迟；不可接受时再单独设计失效逻辑（spec"不在本次范围内"）。
- **`_MAX_HOPS = 2`**（`structured_filter_query.py:31`），所以实际上每个 `RelationConstraint` 至多探测一跳。代码仍写成遍历 `hops[1:]` 的循环，这样上限调整时无需改逻辑。
- 测试命令：`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`。Windows 下 pytest 打完汇总行后进程会挂在 teardown（aiosqlite worker 线程和已关闭的 event loop 竞争），**读汇总行即可，不要等进程退出**。任何带中文输出的命令都要加 `PYTHONIOENCODING=utf-8`，否则 cp1252 会抛 `UnicodeEncodeError`。
- 当前基线：**1373 passed**。

## File Structure

| 文件 | 职责 | 本计划的改动 |
|---|---|---|
| `app/graphrag/neo4j_client.py` | Neo4j 后端的图查询封装 | 新增 `_FANOUT_QUERY_TEMPLATE` 常量 + `probe_relation_fanout()` 方法 |
| `app/graphrag/neptune_client.py` | Neptune 后端的独立实现（刻意不与 Neo4j 共享内部细节） | 新增同名同签名的 openCypher 实现 |
| `app/graphrag/structured_filter_query.py` | 解析→校验→执行→格式化的编排层 | 新增 `_FANOUT_WARNING_NOTE` 常量 + `_probe_fanout_warning()` 协程；`run_structured_filter_query` 在两个计数分支上接入 |
| `tests/graphrag/test_neo4j_client.py` | Neo4j 客户端单测（`FakeSession`/`FakeDriver`） | 新增 3 个用例 |
| `tests/graphrag/test_neptune_client.py` | Neptune 客户端单测（`FakeNeptuneClient`） | 新增 3 个用例 |
| `tests/graphrag/test_structured_filter_query.py` | 编排层单测（`_FakeGraphClient`） | 给 `_FakeGraphClient` 加 `probe_relation_fanout`；新增 5 个用例 |

三个任务的边界：Task 1/2 是两个互相独立的后端方法（一个被驳回不影响另一个），Task 3 是消费它们的编排层。

---

### Task 1: Neo4j 后端的扇出探测

**Files:**
- Modify: `app/graphrag/neo4j_client.py`（在 `_DELETE_TERM_NODE_QUERY` 常量之后加新常量；在 `count_relation_edges_for_term` 方法之后加新方法，当前在第 567-577 行）
- Test: `tests/graphrag/test_neo4j_client.py`

**Interfaces:**
- Consumes: 无（本计划第一个任务）
- Produces:
  ```python
  async def probe_relation_fanout(
      self, *, tenant_id: str, relation_type: str,
      from_term_type: str, to_term_type: str, direction: str,
  ) -> int
  ```
  `direction` 取值 `"outgoing"`（边从 from 指向 to）或 `"incoming"`（边从 to 指向 from）。返回"单个 from 节点最多能走到几个不同的 to 节点"；没有任何匹配边时返回 0。Task 3 依赖这个签名和这个语义。

- [ ] **Step 1: 写失败测试**

追加到 `tests/graphrag/test_neo4j_client.py` 末尾：

```python
async def test_probe_relation_fanout_returns_max_distinct_targets():
    session = FakeSession(rows=[{"fanout": 3}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    fanout = await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="产品", to_term_type="公司", direction="outgoing",
    )

    assert fanout == 3
    assert session.last_parameters == {
        "tenant_id": "demo", "from_term_type": "产品", "to_term_type": "公司",
    }
    # relation_type 只能插值（Cypher 不支持参数化关系类型），term_type 必须参数化。
    assert "[r:BELONG_TO]" in session.last_query
    assert "$from_term_type" in session.last_query
    assert "(a:Term)-[r:BELONG_TO]->(b:Term)" in session.last_query
    # 关系边本身也要按租户过滤，跟 query_subgraph 的 WHERE r.tenant_id 一致。
    assert "r.tenant_id = $tenant_id" in session.last_query


async def test_probe_relation_fanout_flips_the_pattern_for_incoming():
    session = FakeSession(rows=[{"fanout": 1}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="公司", to_term_type="产品", direction="incoming",
    )

    assert "(a:Term)<-[r:BELONG_TO]-(b:Term)" in session.last_query


async def test_probe_relation_fanout_returns_zero_when_no_edges_match():
    # 没有任何匹配边时，WITH 阶段产出 0 行，max() 在空输入上返回 null——
    # Cypher 仍然会给出一行、fanout 为 None，不能直接返回 None。
    session = FakeSession(rows=[{"fanout": None}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    assert await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="产品", to_term_type="公司", direction="outgoing",
    ) == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -q -p no:cacheprovider -k probe_relation_fanout`
Expected: FAIL，`AttributeError: 'Neo4jGraphClient' object has no attribute 'probe_relation_fanout'`

- [ ] **Step 3: 加查询模板常量**

在 `app/graphrag/neo4j_client.py` 里 `_DELETE_TERM_NODE_QUERY` 常量及其注释之后插入：

```python
_FANOUT_QUERY_TEMPLATE = """
MATCH (a:Term){arrow_left}[r:{relation_type}]{arrow_right}(b:Term)
WHERE a.tenant_id = $tenant_id AND a.type = $from_term_type
  AND b.tenant_id = $tenant_id AND b.type = $to_term_type
  AND r.tenant_id = $tenant_id
WITH a, count(DISTINCT b) AS k
RETURN max(k) AS fanout
"""
# 扇出探测：单个 from 节点最多能走到几个不同的 to 节点。fanout > 1 说明这一跳
# 不是函数关系，沿它做计数聚合会把归属放大——见 docs/superpowers/specs/
# 2026-08-29-fan-trap-detection-design.md。
#
# relation_type 走 str.format 插值（Cypher 无法参数化关系类型），安全性依赖
# 调用方已跑过 validate_structured_filter_query 的格式正则 + 已确认成员校验，
# 跟 execute_structured_filter_query 的插值理由完全一致。term_type 是普通
# 属性值，一律参数化，并且走 (tenant_id, type) 复合索引
# term_tenant_term_type_idx（见 _ENSURE_INDEXES_QUERIES）。
```

- [ ] **Step 4: 实现方法**

在 `app/graphrag/neo4j_client.py::Neo4jGraphClient.count_relation_edges_for_term` 方法之后插入：

```python
    async def probe_relation_fanout(
        self,
        *,
        tenant_id: str,
        relation_type: str,
        from_term_type: str,
        to_term_type: str,
        direction: str,
    ) -> int:
        """单个 from_term_type 节点沿这条关系最多能走到几个不同的
        to_term_type 节点。返回 > 1 表示这一跳不是函数关系。

        direction="outgoing" 表示边从 from 指向 to，"incoming" 表示反向——
        跟 structured_filter_query.Hop.direction 的取值一致。

        没有任何匹配边时返回 0：Cypher 的 max() 在空输入上返回 null，仍然会
        给出一行，不能把 None 直接透出去。
        """
        arrow_left, arrow_right = ("-", "->") if direction == "outgoing" else ("<-", "-")
        query = _FANOUT_QUERY_TEMPLATE.format(
            arrow_left=arrow_left, arrow_right=arrow_right, relation_type=relation_type,
        )
        async with self._driver.session() as session:
            result = await session.run(
                query,
                {
                    "tenant_id": tenant_id,
                    "from_term_type": from_term_type,
                    "to_term_type": to_term_type,
                },
            )
            rows = await result.data()
        if not rows:
            return 0
        return rows[0]["fanout"] or 0
```

- [ ] **Step 5: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -q -p no:cacheprovider`
Expected: PASS，且该文件原有用例全部不受影响

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/neo4j_client.py tests/graphrag/test_neo4j_client.py
git commit -m "feat(graphrag): probe relation fanout on the Neo4j backend"
```

---

### Task 2: Neptune 后端的扇出探测

**Files:**
- Modify: `app/graphrag/neptune_client.py`（在 `NeptuneGraphClient.count_relation_edges_for_term` 之后，当前在第 342-346 行）
- Test: `tests/graphrag/test_neptune_client.py`

**Interfaces:**
- Consumes: 无。**不要 import `neo4j_client` 的任何内部细节**——`NeptuneGraphClient` 的 class docstring 明确写了两个实现刻意独立、即使查询文本高度相似也不共享，等真的接入 Neptune 环境实测后再决定要不要重构（YAGNI）。这条约束在本任务里必须遵守。
- Produces: 与 Task 1 完全相同的签名和语义：
  ```python
  async def probe_relation_fanout(
      self, *, tenant_id: str, relation_type: str,
      from_term_type: str, to_term_type: str, direction: str,
  ) -> int
  ```

- [ ] **Step 1: 写失败测试**

追加到 `tests/graphrag/test_neptune_client.py` 末尾：

```python
async def test_probe_relation_fanout_returns_max_distinct_targets():
    client_stub = FakeNeptuneClient(rows=[{"fanout": 3}])
    client = NeptuneGraphClient(client=client_stub)

    fanout = await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="产品", to_term_type="公司", direction="outgoing",
    )

    assert fanout == 3
    assert client_stub.last_parameters == {
        "tenant_id": "demo", "from_term_type": "产品", "to_term_type": "公司",
    }
    assert "(a:Term)-[r:BELONG_TO]->(b:Term)" in client_stub.last_query
    assert "$from_term_type" in client_stub.last_query
    assert "r.tenant_id = $tenant_id" in client_stub.last_query


async def test_probe_relation_fanout_flips_the_pattern_for_incoming():
    client_stub = FakeNeptuneClient(rows=[{"fanout": 1}])
    client = NeptuneGraphClient(client=client_stub)

    await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="公司", to_term_type="产品", direction="incoming",
    )

    assert "(a:Term)<-[r:BELONG_TO]-(b:Term)" in client_stub.last_query


async def test_probe_relation_fanout_returns_zero_when_no_edges_match():
    client_stub = FakeNeptuneClient(rows=[{"fanout": None}])
    client = NeptuneGraphClient(client=client_stub)

    assert await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="产品", to_term_type="公司", direction="outgoing",
    ) == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_neptune_client.py -q -p no:cacheprovider -k probe_relation_fanout`
Expected: FAIL，`AttributeError: 'NeptuneGraphClient' object has no attribute 'probe_relation_fanout'`

- [ ] **Step 3: 加查询模板常量**

在 `app/graphrag/neptune_client.py` 里模块级常量区（其它 `_..._QUERY` 常量附近）插入：

```python
_FANOUT_QUERY_TEMPLATE = """
MATCH (a:Term){arrow_left}[r:{relation_type}]{arrow_right}(b:Term)
WHERE a.tenant_id = $tenant_id AND a.type = $from_term_type
  AND b.tenant_id = $tenant_id AND b.type = $to_term_type
  AND r.tenant_id = $tenant_id
WITH a, count(DISTINCT b) AS k
RETURN max(k) AS fanout
"""
# 扇出探测：单个 from 节点最多能走到几个不同的 to 节点。fanout > 1 说明这一跳
# 不是函数关系，沿它做计数聚合会把归属放大——见 docs/superpowers/specs/
# 2026-08-29-fan-trap-detection-design.md。
#
# 查询文本跟 neo4j_client 的同名常量高度相似，这是刻意的重复而不是应该抽取的
# 公共部分——见 NeptuneGraphClient 的 class docstring：两个后端实现完全独立，
# 等真的接入 Neptune 环境实测确认语义一致之后再谈重构。
#
# relation_type 走 str.format 插值（openCypher 无法参数化关系类型），安全性
# 依赖调用方已跑过 validate_structured_filter_query 的格式正则 + 已确认成员
# 校验，跟 execute_structured_filter_query 的插值理由完全一致。
```

- [ ] **Step 4: 实现方法**

替换 `app/graphrag/neptune_client.py::NeptuneGraphClient.count_relation_edges_for_term` 之后的位置，插入新方法（**不要动 `count_relation_edges_for_term` 本身的 `NotImplementedError`**——那是管理后台路径，跟本计划无关）：

```python
    async def probe_relation_fanout(
        self,
        *,
        tenant_id: str,
        relation_type: str,
        from_term_type: str,
        to_term_type: str,
        direction: str,
    ) -> int:
        """单个 from_term_type 节点沿这条关系最多能走到几个不同的
        to_term_type 节点。返回 > 1 表示这一跳不是函数关系。

        这个方法在查询主链路上（run_structured_filter_query 的计数分支会调
        它），所以必须真实实现，不能像本类其它管理后台方法那样抛
        NotImplementedError——那会让 graph_backend="neptune" 时所有多跳计数
        直接崩溃。

        direction="outgoing" 表示边从 from 指向 to，"incoming" 表示反向。
        没有任何匹配边时返回 0：openCypher 的 max() 在空输入上返回 null。
        """
        arrow_left, arrow_right = ("-", "->") if direction == "outgoing" else ("<-", "-")
        query = _FANOUT_QUERY_TEMPLATE.format(
            arrow_left=arrow_left, arrow_right=arrow_right, relation_type=relation_type,
        )
        rows = await self._client.execute_open_cypher(
            query,
            {
                "tenant_id": tenant_id,
                "from_term_type": from_term_type,
                "to_term_type": to_term_type,
            },
        )
        if not rows:
            return 0
        return rows[0]["fanout"] or 0
```

- [ ] **Step 5: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_neptune_client.py -q -p no:cacheprovider`
Expected: PASS，且该文件原有用例全部不受影响

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/neptune_client.py tests/graphrag/test_neptune_client.py
git commit -m "feat(graphrag): probe relation fanout on the Neptune backend"
```

---

### Task 3: 在计数分支接入扇出警告

**Files:**
- Modify: `app/graphrag/structured_filter_query.py`
  - 模块级常量区新增 `_FANOUT_WARNING_NOTE`
  - 新增 `_probe_fanout_warning()` 协程
  - `run_structured_filter_query` 的 group_by 分支（当前第 597-598 行）和 `limit == 0` 分支（当前第 602-616 行）接入
- Test: `tests/graphrag/test_structured_filter_query.py`（`_FakeGraphClient` 当前在第 550-568 行）

**Interfaces:**
- Consumes: Task 1/2 产出的
  ```python
  async def probe_relation_fanout(
      self, *, tenant_id: str, relation_type: str,
      from_term_type: str, to_term_type: str, direction: str,
  ) -> int
  ```
- Produces: `run_structured_filter_query` 在计数场景命中扇出时，返回结构里多一个 `fanout_warning` 键，形如
  ```python
  {"hop": "产品 --BELONG_TO--> 公司", "fanout": 3, "note": "..."}
  ```
  且此时**不再包含**原来那句 `"matched_count 是精确完整的计数……"` 的 `note`。

- [ ] **Step 1: 给共享的 `_FakeGraphClient` 加探测方法**

`tests/graphrag/test_structured_filter_query.py` 第 550-568 行的 `_FakeGraphClient` 是全文件共用的假客户端，现在没有 `probe_relation_fanout`。给它加上，默认返回 1（无扇出），并记录调用参数：

```python
class _FakeGraphClient:
    def __init__(self, *, rows=None, group_result=None, error=None, total_count=None,
                 fanout=1) -> None:
        self._rows = rows if rows is not None else []
        self._group_result = group_result
        self._error = error
        self._total_count = total_count if total_count is not None else len(self._rows)
        self._fanout = fanout
        self.last_args = None
        self.last_resolved = None
        self.last_tenant_id = None
        self.fanout_probes: list[dict] = []

    async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
        self.last_args = args
        self.last_resolved = resolved
        self.last_tenant_id = tenant_id
        if self._error is not None:
            raise self._error
        if self._group_result is not None:
            return self._group_result
        return {"rows": self._rows, "total_count": self._total_count}

    async def probe_relation_fanout(self, *, tenant_id, relation_type,
                                    from_term_type, to_term_type, direction):
        self.fanout_probes.append({
            "tenant_id": tenant_id, "relation_type": relation_type,
            "from_term_type": from_term_type, "to_term_type": to_term_type,
            "direction": direction,
        })
        return self._fanout
```

默认 `fanout=1` 保证所有既有用例行为不变。

- [ ] **Step 2: 写失败测试**

追加到 `tests/graphrag/test_structured_filter_query.py` 末尾：

```python
from app.graphrag.ontology_categories import TermTypeCategory

_FANOUT_SCHEMA = {
    "订单号": TermTypeCategory(value="订单号", extra_fields=[]),
    "产品": TermTypeCategory(value="产品", extra_fields=[]),
    "公司": TermTypeCategory(value="公司", extra_fields=[]),
}

_TWO_HOP_COUNT_ARGS = {
    "anchor": {"term_type": "订单号"},
    "constraints": [
        {
            "kind": "relation",
            "hops": [
                {"relation_type": "BELONG_TO", "direction": "outgoing", "target_term_type": "产品"},
                {"relation_type": "BELONG_TO", "direction": "outgoing", "target_term_type": "公司"},
            ],
            "target_field": "standard_name",
            "target_operator": "eq",
            "target_value": "Coca-Cola",
        }
    ],
    "limit": 0,
}


async def test_two_hop_count_warns_when_the_second_hop_fans_out():
    """2026-08-29 真实事故回归：订单号→产品→公司 两跳计数返回 10000，而
    产品→公司 是多对多（10 个产品各自都被 3 家公司卖过），真实值是 3353。
    见 docs/superpowers/specs/2026-08-29-fan-trap-detection-design.md。"""
    from app.graphrag.structured_filter_query import run_structured_filter_query

    client = _FakeGraphClient(total_count=10000, fanout=3)

    result = await run_structured_filter_query(
        _TWO_HOP_COUNT_ARGS,
        graph_client=client, tenant_id="demo", terms=[],
        confirmed_relation_types={"BELONG_TO"}, term_type_schema=_FANOUT_SCHEMA,
    )

    assert result["matched_count"] == 10000
    assert result["fanout_warning"]["fanout"] == 3
    assert result["fanout_warning"]["hop"] == "产品 --BELONG_TO--> 公司"
    assert "推导" in result["fanout_warning"]["note"]
    # 肯定语气的原 note 必须消失——两条矛盾措辞并存会让模型无所适从。
    assert "note" not in result

    # 只探测第一跳之后的跳：第一跳 订单号→产品 是被计数实体自己的关系，
    # 它的多对多性质是查询语义的一部分，探测它会误报。
    assert client.fanout_probes == [
        {"tenant_id": "demo", "relation_type": "BELONG_TO",
         "from_term_type": "产品", "to_term_type": "公司", "direction": "outgoing"}
    ]


async def test_two_hop_count_keeps_the_plain_note_when_no_hop_fans_out():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    client = _FakeGraphClient(total_count=3353, fanout=1)

    result = await run_structured_filter_query(
        _TWO_HOP_COUNT_ARGS,
        graph_client=client, tenant_id="demo", terms=[],
        confirmed_relation_types={"BELONG_TO"}, term_type_schema=_FANOUT_SCHEMA,
    )

    assert result["matched_count"] == 3353
    assert "fanout_warning" not in result
    assert "精确完整的计数" in result["note"]


async def test_single_hop_count_never_probes_fanout():
    """"Coca-Cola 卖多少种产品"——被计数实体就是多对多边的起点，一跳，
    结果正确，不能误报。"""
    from app.graphrag.structured_filter_query import run_structured_filter_query

    client = _FakeGraphClient(total_count=10, fanout=3)

    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "产品"},
            "constraints": [
                {
                    "kind": "relation",
                    "hops": [{"relation_type": "BELONG_TO", "direction": "outgoing",
                              "target_term_type": "公司"}],
                    "target_field": "standard_name",
                    "target_operator": "eq",
                    "target_value": "Coca-Cola",
                }
            ],
            "limit": 0,
        },
        graph_client=client, tenant_id="demo", terms=[],
        confirmed_relation_types={"BELONG_TO"}, term_type_schema=_FANOUT_SCHEMA,
    )

    assert result["matched_count"] == 10
    assert "fanout_warning" not in result
    assert client.fanout_probes == []


async def test_listing_query_never_probes_fanout():
    """limit > 0 的列举查询不触发探测：扇出会让结果里出现重复实体，但这些
    关联真实存在，只是"经中转推导"而非直接归属，不属于伪造数字的范畴。"""
    from app.graphrag.structured_filter_query import run_structured_filter_query

    client = _FakeGraphClient(
        rows=[{"standard_name": "1-143-51064-X", "node_key": "1-143-51064-X",
               "term_type": "订单号", "extra_properties": {}}],
        total_count=1, fanout=3,
    )

    result = await run_structured_filter_query(
        {**_TWO_HOP_COUNT_ARGS, "limit": 20},
        graph_client=client, tenant_id="demo", terms=[],
        confirmed_relation_types={"BELONG_TO"}, term_type_schema=_FANOUT_SCHEMA,
    )

    assert "fanout_warning" not in result
    assert client.fanout_probes == []


async def test_group_by_result_also_carries_the_fanout_warning():
    """group_by 也是聚合语义，同样受扇出影响。"""
    from app.graphrag.structured_filter_query import run_structured_filter_query

    client = _FakeGraphClient(group_result={"groups": [{"value": "Coca-Cola", "count": 10000}]},
                              fanout=3)

    result = await run_structured_filter_query(
        {**_TWO_HOP_COUNT_ARGS, "limit": 20, "group_by": {"constraint_index": 0}},
        graph_client=client, tenant_id="demo", terms=[],
        confirmed_relation_types={"BELONG_TO"}, term_type_schema=_FANOUT_SCHEMA,
    )

    assert result["groups"] == [{"value": "Coca-Cola", "count": 10000}]
    assert result["fanout_warning"]["fanout"] == 3
```

- [ ] **Step 3: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -q -p no:cacheprovider -k "fanout"`
Expected: FAIL —— `test_two_hop_count_warns_when_the_second_hop_fans_out` 报 `KeyError: 'fanout_warning'`；`test_group_by_result_also_carries_the_fanout_warning` 同样。另外三个（`keeps_the_plain_note` / `single_hop` / `listing`）此时就该通过（它们钉的是"不许误报"，实现前本来就不会报）——这是有意的，它们是防止实现过度触发的护栏。

- [ ] **Step 4: 加警告文案常量**

在 `app/graphrag/structured_filter_query.py` 模块级常量区（`_CONSTRAINT_FUZZY_MIN_MARGIN` 附近，当前第 362-363 行）插入：

```python
_FANOUT_WARNING_NOTE = (
    "这条路径的第 {hop_index} 跳「{hop}」是多对多关系（单个「{from_term_type}」"
    "最多关联 {fanout} 个「{to_term_type}」），计数经过它中转后会把归属放大："
    "matched_count 不是「{to_term_type}」名下的真实数量，最多可能被放大到 "
    "{fanout} 倍。回答时必须说明这个数字是经中转推导出的关联数、不是精确归属"
    "计数，不要把它当作确定答案给出。"
)
```

- [ ] **Step 5: 实现探测协程**

在 `app/graphrag/structured_filter_query.py` 里 `_correct_hop_directions()` 之后插入：

```python
async def _probe_fanout_warning(
    constraints: list[AttributeConstraint | RelationConstraint],
    *,
    graph_client: "Neo4jGraphClient",
    tenant_id: str,
    anchor_term_type: str,
) -> dict[str, Any] | None:
    """计数场景下检查路径上有没有扇出陷阱，有就返回一份警告，没有返回 None。

    只检查【第一跳之后】的跳：第一跳是被计数实体自身的关系，它的多对多性质
    正是查询语义的一部分（"Coca-Cola 卖多少种产品"走一跳 产品→公司，那条边
    确实是多对多，但结果是对的），探测它会误报。第二跳起就不一样了——路径
    A→B→C 只有在 B→C 是函数关系时才保得住 A 对 C 的归属，B→C 是多对多时，
    路径会凭空捏造归属：2026-08-29 实测 订单号→产品→公司 让每一笔订单都能
    走到每一家公司，三家公司的计数全都等于订单总数 10000，真实值是
    3353/3330/3317。

    见 docs/superpowers/specs/2026-08-29-fan-trap-detection-design.md。

    _MAX_HOPS 目前是 2，所以实际上每个约束至多探测一跳；写成循环是为了上限
    调整时无需改这里的逻辑。命中第一个扇出跳就返回，不继续探测。
    """
    for constraint in constraints:
        if not isinstance(constraint, RelationConstraint):
            continue
        current_term_type = anchor_term_type
        for index, hop in enumerate(constraint.hops):
            if index == 0:
                current_term_type = hop.target_term_type
                continue
            fanout = await graph_client.probe_relation_fanout(
                tenant_id=tenant_id,
                relation_type=hop.relation_type,
                from_term_type=current_term_type,
                to_term_type=hop.target_term_type,
                direction=hop.direction,
            )
            if fanout > 1:
                arrow = (
                    f"--{hop.relation_type}-->"
                    if hop.direction == "outgoing"
                    else f"<--{hop.relation_type}--"
                )
                hop_label = f"{current_term_type} {arrow} {hop.target_term_type}"
                return {
                    "hop": hop_label,
                    "fanout": fanout,
                    "note": _FANOUT_WARNING_NOTE.format(
                        hop_index=index + 1,
                        hop=hop_label,
                        from_term_type=current_term_type,
                        to_term_type=hop.target_term_type,
                        fanout=fanout,
                    ),
                }
            current_term_type = hop.target_term_type
    return None
```

- [ ] **Step 6: 接入两个计数分支**

`app/graphrag/structured_filter_query.py::run_structured_filter_query` 里，把当前第 597-616 行的两个分支改成：

```python
    if "groups" in result:
        # group_by 分支：{"groups": [...]}。它也是聚合语义，同样受扇出影响。
        warning = await _probe_fanout_warning(
            args.constraints, graph_client=graph_client,
            tenant_id=tenant_id, anchor_term_type=resolved.term_type,
        )
        return {**result, "fanout_warning": warning} if warning else result

    rows = result["rows"]
    total_count = result["total_count"]
    if args.limit == 0:
        # 纯计数场景：不能返回 "anchors": [] ——2026-08-28 实测，Planner 会把
        # 空列表读成"没有匹配到任何实体"，进而认定 matched_count 不可信，
        # 明明拿到了精确答案却回复"无法给出确定数字"。这里改成不给 anchors
        # 键、并附一句自描述说明：Planner 看不到工具的 _USAGE_GUIDE（那是
        # 渐进式披露里只给深层参数生成看的），观察结果必须自己解释自己。
        #
        # 但这句自描述只有在路径确实没有扇出陷阱时才成立。命中扇出时换成
        # fanout_warning，且【不能同时保留】原来这句肯定语气的说明——两条
        # 互相矛盾的措辞并存会让模型无所适从。
        warning = await _probe_fanout_warning(
            args.constraints, graph_client=graph_client,
            tenant_id=tenant_id, anchor_term_type=resolved.term_type,
        )
        if warning is not None:
            return {"matched_count": total_count, "fanout_warning": warning}
        return {
            "matched_count": total_count,
            "note": (
                "本次只做计数（limit=0），未返回样本实体。matched_count 是"
                "精确完整的计数，不是上限值、也不是截断值，可以直接作为"
                "确定数字回答用户。"
            ),
        }
```

注意 `args` 在这个位置已经过 `_correct_hop_directions()` 重写（当前第 566-572 行），所以 `hop.direction` 拿到的是纠正后的方向，探测的正是实际会被遍历的那个方向。

- [ ] **Step 7: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -q -p no:cacheprovider`
Expected: PASS，含新增 5 个用例

- [ ] **Step 8: 跑全量测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`
Expected: `1384 passed`（基线 1373 + Task 1 的 3 + Task 2 的 3 + Task 3 的 5）

- [ ] **Step 9: 提交**

```bash
git add app/graphrag/structured_filter_query.py tests/graphrag/test_structured_filter_query.py
git commit -m "feat(graphrag): warn when a multi-hop count traverses a fan trap"
```

---

## 端到端验收

三个任务都完成后，按 spec 的"验收标准"实测。演示数据现在**已经有** `订单号→公司` 直连边（2026-08-29 补的），所以要验证两种状态：

- [ ] **有直连边时不误报**

重启后端，问"coke-cola公司有多少个订单"。期望：仍然返回 3353，观察结果里**没有** `fanout_warning`。

```bash
PYTHONIOENCODING=utf-8 nohup .venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/uvicorn.log 2>&1 &
# 中文 payload 必须走 --data-binary @file，直接 -d '中文' 在这台机器上会被
# 损坏成 ????（本次会话实测过，曾经导致误判为"LLM 失败"）
curl -s -m 180 -X POST http://127.0.0.1:8000/agent/chat \
  -H "Content-Type: application/json" --data-binary @payload.json
```

- [ ] **强制走两跳时正确报警**

用一个直接调用 `run_structured_filter_query` 的脚本（不经过 Planner，避免它自己选了直连路径），显式传入 `订单号→产品→公司` 两跳 + `limit: 0`，连真实 Neo4j。期望：`matched_count == 10000`，`fanout_warning["fanout"] == 3`，`fanout_warning["hop"] == "产品 --BELONG_TO--> 公司"`，且返回结构里没有 `note` 键。

## Self-Review

**1. Spec 覆盖**

| spec 章节 | 对应任务 |
|---|---|
| 核心机制（Cypher 聚合） | Task 1 Step 3-4 / Task 2 Step 3-4 |
| 只查第一跳之后 | Task 3 Step 5（`if index == 0: continue`），Task 3 Step 2 的 `single_hop` 与 `probes == [...]` 断言 |
| 触发条件（`limit == 0` + `group_by`，hops ≥ 2） | Task 3 Step 6；`listing_query` 用例钉住 `limit > 0` 不触发 |
| 呈现档位"降级 + 并报" | Task 3 Step 4-6，`fanout_warning` 含 `fanout` 倍数 |
| 有警告时移除原 note | Task 3 Step 6 的 early return，Step 2 的 `assert "note" not in result` |
| 改动点表格里的 5 个文件 | Task 1（neo4j_client）、Task 2（neptune_client）、Task 3（structured_filter_query）；`tool.py` 和 `ontology_recall.py` 两处文案已在 9f4eb1d 完成，本计划按要求不含 |
| 性能：不做缓存 | Global Constraints 已声明 |
| 验收标准 5 条 | "端到端验收"两条 + Task 1/2/3 的单测覆盖其余三条（Neptune 与 Neo4j 同签名同语义由两份对称测试保证） |

无遗漏。

**2. 占位符扫描**

无 TBD/TODO；每个代码步骤都给了完整可粘贴的代码；没有"参照 Task N"的引用（Task 2 的查询常量和方法体完整重写了一遍，没有让实现者回头看 Task 1）。

**3. 类型一致性**

`probe_relation_fanout` 的关键字参数在 Task 1（实现）、Task 2（实现）、Task 3（`_FakeGraphClient` 桩 + `_probe_fanout_warning` 调用点 + 测试断言的 `fanout_probes` 字典）四处出现，均为 `tenant_id / relation_type / from_term_type / to_term_type / direction`，返回 `int`。`fanout_warning` 的三个键 `hop / fanout / note` 在 Step 2 的测试断言和 Step 5 的构造处一致。
