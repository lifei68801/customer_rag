# Neo4j 知识图谱租户隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 Neo4j 知识图谱的关系事实（`RELATED_TO`/`BELONGS_TO_MODULE` 边）加上 `tenant_id` 隔离，防止不同租户的客户提问检索到彼此的图谱知识；术语节点（`:Term`）和别名边（`ALIAS_OF`）保持全局共享。

**Architecture:** `tenant_id` 作为属性写在 `merge_relation` 创建的边上，`query_subgraph` 用 `WHERE r.tenant_id = $tenant_id` 强制过滤；由于 `ALIAS_OF` 边从不写 `tenant_id`，这一条过滤天然把它们排除在过滤逻辑之外（`null = $tenant_id` 恒为假），不需要额外按关系类型区分。`tenant_id` 从两条已有的调用链自上而下透传：摄取流水线（`pipeline.py` → `graph_extraction.py` → `normalization.py` → `neo4j_client.py`）和 Agent 查询路径（`graph.py` 的 `term_guard_node`/`tool_call_node` → `term_guard.py`/`tools.py`+`planner.py` → `neo4j_client.py`）。

**Tech Stack:** Python 3.12、Neo4j（`neo4j` 驱动，测试用 fake driver/session，不需要真实 Neo4j 实例）、pytest（`asyncio_mode = "auto"`，测试函数直接写 `async def test_...` 不需要装饰器）。

## Global Constraints

- 严格 TDD：RED（写失败测试，确认失败原因正确）→ GREEN（最小实现）→ 跑全量测试 → git commit。
- `tenant_id` 一律作为**必填**关键字参数（不设默认值），与本项目其余所有 `tenant_id` 参数的既有约定一致（如 `vector_search_tool`/`_embed_and_upsert` 均是必填）——遗漏传参应该在开发阶段就报 `TypeError`，而不是悄悄退化成"不隔离"。
- 术语节点（`:Term`）、别名边（`ALIAS_OF`）、`sync_term`/`sync_terms`/`_SYNC_TERM_QUERY` **不改动**，保持全局共享（这是本次设计的明确决策，不是遗漏）。
- Cypher 关系类型（`RELATED_TO`/`BELONGS_TO_MODULE`）不能参数化，只能是查询字符串字面量，继续走现有的 `_ALLOWED_RELATION_TYPES` 白名单校验，这次不改这部分逻辑。
- Commit message 格式：一行摘要（`feat:`/`fix:` 前缀）+ 空行 + 中文详细说明（为什么这么做/复用了什么/刻意不做什么）+ 以 `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` 结尾。
- 本仓库当前在 `dev/0.1` 分支直接工作，不建 worktree。
- 测试命令统一用 `.venv/Scripts/python.exe -m pytest <path> -v`（Windows 环境，本仓库自带 `.venv`）。
- 设计依据：`docs/superpowers/specs/2026-08-07-neo4j-tenant-isolation-design.md`（已经用户批准，不要偏离其中的机制决策）。

---

### Task 1: `neo4j_client.py` — Cypher 模板与客户端方法签名

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Test: `tests/graphrag/test_neo4j_client.py`

**Interfaces:**
- Produces：
  - `async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict[str, Any]]`（原来没有 `tenant_id` 参数）
  - `async def merge_relation(self, *, subject_standard_name: str, object_standard_name: str, relation_type: str, source: str, tenant_id: str) -> None`（新增 `tenant_id`，其余参数不变）
  - `async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None`（新增 `tenant_id`，`source` 保持位置参数）
  - `sync_term`/`sync_terms` 签名不变，Task 2-5 不会用到它们的改动（因为没有改动）。

- [ ] **Step 1: 写失败测试**

打开 `tests/graphrag/test_neo4j_client.py`，把整个文件替换为以下内容（在原有基础上：3 个测试的调用/断言加上 `tenant_id`，新增 2 条断言验证 Cypher 查询串本身包含租户过滤条件，其余测试不变）：

```python
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term


class FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def data(self) -> list[dict]:
        return self._rows


class FakeSession:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.last_query: str | None = None
        self.last_parameters: dict | None = None
        self.calls: list[tuple[str, dict]] = []

    async def run(self, query: str, parameters: dict | None = None) -> FakeResult:
        self.last_query = query
        self.last_parameters = parameters
        self.calls.append((query, parameters))
        return FakeResult(self._rows)

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeDriver:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def session(self) -> FakeSession:
        return self._session


async def test_query_subgraph_returns_related_terms():
    session = FakeSession(
        rows=[
            {"related_name": "登录模块", "relation_type": "RELATED_TO"},
        ]
    )
    client = Neo4jGraphClient(driver=FakeDriver(session))

    results = await client.query_subgraph("错误码E502", tenant_id="t1")

    assert results == [{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    assert session.last_parameters == {"standard_name": "错误码E502", "tenant_id": "t1"}
    assert "tenant_id" in session.last_query


async def test_merge_relation_sends_expected_query_and_parameters():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.merge_relation(
        subject_standard_name="错误码E502",
        object_standard_name="登录模块",
        relation_type="RELATED_TO",
        source="a.md",
        tenant_id="t1",
    )

    assert session.last_parameters == {
        "subject_name": "错误码E502",
        "object_name": "登录模块",
        "source": "a.md",
        "tenant_id": "t1",
    }
    assert "RELATED_TO" in session.last_query
    assert "MERGE" in session.last_query
    assert "tenant_id" in session.last_query


async def test_delete_relations_by_source_sends_expected_query_and_parameters():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_relations_by_source("a.md", tenant_id="t1")

    assert session.last_parameters == {"source": "a.md", "tenant_id": "t1"}
    assert "DELETE" in session.last_query
    assert "tenant_id" in session.last_query


async def test_merge_relation_rejects_unrecognized_relation_type():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    try:
        await client.merge_relation(
            subject_standard_name="a",
            object_standard_name="b",
            relation_type="DROP TABLE",
            source="a.md",
            tenant_id="t1",
        )
        assert False, "应拒绝非法关系类型"
    except ValueError:
        pass


async def test_sync_term_writes_standard_node_properties_and_alias_edges():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        standard_name="错误码E502",
        aliases=["网关超时", "E502超时"],
        term_type="error_code",
        product_line="核心平台",
    )

    await client.sync_term(term)

    assert session.last_parameters == {
        "standard_name": "错误码E502",
        "type": "error_code",
        "product_line": "核心平台",
        "aliases": ["网关超时", "E502超时"],
    }
    assert "ALIAS_OF" in session.last_query
    assert "alias_name" in session.last_query


async def test_sync_term_with_no_aliases_sends_empty_alias_list():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        standard_name="登录模块",
        aliases=[],
        term_type="module",
        product_line="核心平台",
    )

    await client.sync_term(term)

    assert session.last_parameters["aliases"] == []


async def test_sync_terms_syncs_every_term_in_the_list():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    terms = [
        Term(standard_name="错误码E502", aliases=["网关超时"], term_type="error_code", product_line="核心平台"),
        Term(standard_name="登录模块", aliases=["认证模块"], term_type="module", product_line="核心平台"),
    ]

    await client.sync_terms(terms)

    assert len(session.calls) == 2
    synced_names = {call[1]["standard_name"] for call in session.calls}
    assert synced_names == {"错误码E502", "登录模块"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -v`
Expected: `test_query_subgraph_returns_related_terms`、`test_merge_relation_sends_expected_query_and_parameters`、`test_delete_relations_by_source_sends_expected_query_and_parameters`、`test_merge_relation_rejects_unrecognized_relation_type` 均报 `TypeError: ...got an unexpected keyword argument 'tenant_id'`（客户端方法还不接受这个参数）；`test_sync_term_*` 系列 3 个测试仍然通过（未改动的方法）。

- [ ] **Step 3: 写最小实现**

把 `app/graphrag/neo4j_client.py` 顶部的 `_SUBGRAPH_QUERY` 和 `_DELETE_RELATIONS_BY_SOURCE_QUERY` 常量替换为：

```python
_SUBGRAPH_QUERY = """
MATCH (t:Term {standard_name: $standard_name})-[r]-(related:Term)
WHERE r.tenant_id = $tenant_id
RETURN related.standard_name AS related_name, type(r) AS relation_type
"""
```

```python
# ALIAS_OF 边（sync_term 写入）从不设置 tenant_id，这条按 r.tenant_id 精确
# 匹配的过滤天然把它们排除在外（Cypher 里 null = $tenant_id 恒为假）——
# 不需要额外按关系类型区分"这条边要不要按租户过滤"。
_DELETE_RELATIONS_BY_SOURCE_QUERY = """
MATCH ()-[r]->() WHERE r.source = $source AND r.tenant_id = $tenant_id
DELETE r
"""
```

把 `query_subgraph` 方法体替换为：

```python
    async def query_subgraph(
        self, standard_name: str, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        async with self._driver.session() as session:
            result = await session.run(
                _SUBGRAPH_QUERY,
                {"standard_name": standard_name, "tenant_id": tenant_id},
            )
            return await result.data()
```

把 `merge_relation` 方法签名和方法体替换为：

```python
    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
        source: str,
        tenant_id: str,
    ) -> None:
        """幂等写入一条术语间关系（MERGE，不存在则创建，存在则不重复）。

        source 记录这条边是从哪个文档抽取出来的，写在边的属性上——
        重新摄取同一文档前先按 source+tenant_id 删掉它写过的旧边（见
        delete_relations_by_source），避免文档内容变更后旧关系永久
        残留在图谱里，和 vector_store.delete_by_source() 是同一个思路。

        tenant_id 同样写在边的属性上，query_subgraph 按它强制过滤，
        防止不同租户的关系事实互相可见。
        """
        if relation_type not in _ALLOWED_RELATION_TYPES:
            raise ValueError(
                f"不允许的关系类型: {relation_type!r}，"
                f"仅支持: {sorted(_ALLOWED_RELATION_TYPES)}"
            )
        query = (
            "MERGE (a:Term {standard_name: $subject_name}) "
            "MERGE (b:Term {standard_name: $object_name}) "
            f"MERGE (a)-[r:{relation_type}]->(b) "
            "SET r.source = $source, r.tenant_id = $tenant_id"
        )
        async with self._driver.session() as session:
            await session.run(
                query,
                {
                    "subject_name": subject_standard_name,
                    "object_name": object_standard_name,
                    "source": source,
                    "tenant_id": tenant_id,
                },
            )
```

把 `delete_relations_by_source` 方法体替换为：

```python
    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        """删除某个文档、某个租户抽取出的全部关系边，重新摄取该文档前调用。

        tenant_id 是必填过滤条件——不同租户即使摄取了相同相对路径的文档
        （source 字符串相同），也只会删自己那部分边，不会互相影响。
        """
        async with self._driver.session() as session:
            await session.run(
                _DELETE_RELATIONS_BY_SOURCE_QUERY,
                {"source": source, "tenant_id": tenant_id},
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -v`
Expected: 8 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除 `tests/graphrag/test_neo4j_client.py` 外，会有若干处因为调用了 `merge_relation`/`delete_relations_by_source`/`query_subgraph`（不传 `tenant_id`）而失败——这是预期的，属于 Task 2-5 要修的范围，本任务只需确认这些失败都是 `TypeError: ...unexpected keyword argument`/`missing 1 required keyword-only argument: 'tenant_id'` 这一类（说明失败原因是"调用方还没传参"，而不是本任务改动本身有 bug），不需要在这一步修复。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/neo4j_client.py tests/graphrag/test_neo4j_client.py
git commit -m "feat: add tenant_id isolation to Neo4j relation edges"
```

---

### Task 2: `normalization.py` — 归一化写入链路透传 `tenant_id`

**Files:**
- Modify: `app/graphrag/normalization.py`
- Test: `tests/graphrag/test_normalization.py`

**Interfaces:**
- Consumes：Task 1 的 `merge_relation(..., tenant_id: str)`。
- Produces：
  - `class GraphWriteClientProtocol(Protocol)` 的 `merge_relation`/`delete_relations_by_source` 签名同步加 `tenant_id: str`（这个 Protocol 是 Task 3 的 `extract_and_write_graph_relations` 的类型标注依据）。
  - `async def normalize_and_write_relations(relations, *, terms, graph_client, source, tenant_id: str, review_conn=None) -> int`（新增必填 `tenant_id`）。

- [ ] **Step 1: 写失败测试**

打开 `tests/graphrag/test_normalization.py`，把 `FakeGraphClient` 类和前 3 个测试函数替换为以下内容（`test_enqueues_*`/`test_does_not_enqueue_when_review_conn_not_provided` 这 3 个测试同样需要加 `tenant_id="t1"`，见后面完整替换块）：

```python
import aiosqlite

from app.graphrag.normalization import normalize_and_write_relations
from app.graphrag.ontology import Term
from app.graphrag.review_queue import ensure_review_schema, list_pending_reviews

_TERMS = [
    Term(
        standard_name="错误码E502",
        aliases=["网关超时"],
        term_type="error_code",
        product_line="核心平台",
    ),
    Term(
        standard_name="登录模块",
        aliases=["认证模块"],
        term_type="module",
        product_line="核心平台",
    ),
]


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []
        self.deleted_sources: list[str] = []

    async def merge_relation(
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
    ) -> None:
        if relation_type not in {"RELATED_TO", "BELONGS_TO_MODULE"}:
            raise ValueError("不允许的关系类型")
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
            }
        )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        self.deleted_sources.append(source)


async def test_writes_relation_when_both_sides_resolve_via_alias():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "RELATED_TO"}
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1"
    )

    assert written == 1
    assert graph_client.written == [
        {
            "subject": "错误码E502",
            "object": "登录模块",
            "relation_type": "RELATED_TO",
            "source": "a.md",
            "tenant_id": "t1",
        }
    ]


async def test_drops_relation_when_one_side_unresolved():
    graph_client = FakeGraphClient()
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1"
    )

    assert written == 0
    assert graph_client.written == []


async def test_drops_relation_with_invalid_relation_type_without_crashing_batch():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "非法类型"},
        {"subject": "网关超时", "object": "认证模块", "relation_type": "RELATED_TO"},
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1"
    )

    assert written == 1


async def test_enqueues_unresolved_candidate_for_review_when_review_conn_provided():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn)
    assert len(pending) == 1
    assert pending[0]["subject_candidate"] == "网关超时"
    assert pending[0]["object_candidate"] == "不存在的实体"
    assert pending[0]["reason"] == "object_unresolved"


async def test_enqueues_invalid_relation_type_for_review_when_review_conn_provided():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "非法类型"}
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn)
    assert len(pending) == 1
    assert pending[0]["reason"] == "invalid_relation_type"


async def test_does_not_enqueue_when_review_conn_not_provided():
    """默认行为保持不变：不传 review_conn 时仍然只是丢弃+记日志，不建表不写库。"""
    graph_client = FakeGraphClient()
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1"
    )

    assert written == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py -v`
Expected: 全部 6 个测试失败，报 `TypeError: normalize_and_write_relations() got an unexpected keyword argument 'tenant_id'`（函数还不接受这个参数）。

- [ ] **Step 3: 写最小实现**

把 `app/graphrag/normalization.py` 的 `GraphWriteClientProtocol` 替换为：

```python
class GraphWriteClientProtocol(Protocol):
    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
        source: str,
        tenant_id: str,
    ) -> None: ...

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None: ...
```

把 `normalize_and_write_relations` 的签名和函数体中调用 `merge_relation` 的部分替换为：

```python
async def normalize_and_write_relations(
    relations: list[dict[str, str]],
    *,
    terms: list[Term],
    graph_client: GraphWriteClientProtocol,
    source: str,
    tenant_id: str,
    review_conn: aiosqlite.Connection | None = None,
) -> int:
    """候选关系归一化对齐术语表后写入图谱，返回成功写入数。

    任一侧未能对齐标准术语、或关系类型不合法的候选不会自动入库。
    review_conn 为可选项：
    - 不传（默认）：候选只记日志后丢弃，保持阶段3落地时的行为不变；
    - 传入：候选改为写入持久化的人工待审核队列（见 review_queue.py），
      而不是随日志一起消失——对应架构文档"低置信度新实体进入人工待
      审核队列，而非直接自动入库/直接丢弃"的完整实现。
    """
    written = 0
    for relation in relations:
        subject_std = resolve_to_standard_name(relation["subject"], terms)
        object_std = resolve_to_standard_name(relation["object"], terms)
        if subject_std is None or object_std is None:
            logger.info(
                "关系候选未能对齐术语表，丢弃 subject=%s object=%s",
                relation["subject"],
                relation["object"],
            )
            if review_conn is not None:
                reason = "subject_unresolved" if subject_std is None else "object_unresolved"
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason=reason,
                )
            continue
        try:
            await graph_client.merge_relation(
                subject_standard_name=subject_std,
                object_standard_name=object_std,
                relation_type=relation["relation_type"],
                source=source,
                tenant_id=tenant_id,
            )
        except ValueError:
            logger.warning(
                "关系类型不合法，丢弃该候选 relation_type=%s",
                relation["relation_type"],
            )
            if review_conn is not None:
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason="invalid_relation_type",
                )
            continue
        written += 1
    return written
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py -v`
Expected: 6 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: `tests/graphrag/test_neo4j_client.py`、`tests/graphrag/test_normalization.py` 通过；`tests/ingestion/test_graph_extraction.py`、`tests/ingestion/test_ingest_pipeline.py`、`tests/ingestion/test_ingest_main.py`（Task 3 范围）和 `tests/graphrag/test_term_guard.py`、`tests/agent/test_tools.py`、`tests/agent/test_planner.py`、`tests/agent/test_graph_planner.py`（Task 4/5 范围）仍会因缺少 `tenant_id` 参数报错，属于预期，本任务不用管。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/normalization.py tests/graphrag/test_normalization.py
git commit -m "feat: thread tenant_id through graph relation normalization"
```

---

### Task 3: 摄取流水线透传 `tenant_id`（`graph_extraction.py` + `pipeline.py`）

**Files:**
- Modify: `app/ingestion/graph_extraction.py`
- Modify: `app/ingestion/pipeline.py:58-88`（`_maybe_extract_graph_relations` 函数及其在 `_ingest_chunks` 里的调用）
- Test: `tests/ingestion/test_graph_extraction.py`、`tests/ingestion/test_ingest_pipeline.py`、`tests/ingestion/test_ingest_main.py`

**Interfaces:**
- Consumes：Task 2 的 `normalize_and_write_relations(..., tenant_id: str)` 和 `GraphWriteClientProtocol`（`merge_relation`/`delete_relations_by_source` 均要求 `tenant_id`）。
- Produces：`async def extract_and_write_graph_relations(chunks, *, llm_registry, llm_provider_name, terms, graph_client, source, tenant_id: str, review_conn=None, extract_timeout_sec=2.0) -> int`（新增必填 `tenant_id`）。`_maybe_extract_graph_relations`/`_ingest_chunks` 内部透传，不改变 `_ingest_chunks` 及其上层 5 个 `ingest_*_file` 函数的对外签名（它们已经都有 `tenant_id` 参数，只是之前没有继续往图谱抽取这条路径传）。

**背景说明**：`_ingest_chunks`（`pipeline.py`）已经从调用方（`ingest_markdown_file`/`ingest_pdf_file`/`ingest_docx_file`/`ingest_image_file`/`ingest_ticket_csv_file`，均已有 `tenant_id: str` 必填参数）收到 `tenant_id`，目前只用于 `_embed_and_upsert`，没有继续传给 `_maybe_extract_graph_relations`。这个任务只需要在 `_ingest_chunks` 内部把已经有的 `tenant_id` 多传一份给 `_maybe_extract_graph_relations`，不需要改动任何一个 `ingest_*_file`/`ingest_directory` 的对外签名。

- [ ] **Step 1: 写失败测试**

打开 `tests/ingestion/test_graph_extraction.py`，把 `FakeGraphClient` 类和 3 个测试函数替换为：

```python
import aiosqlite

from app.graphrag.ontology import Term
from app.graphrag.review_queue import ensure_review_schema, list_pending_reviews
from app.ingestion.graph_extraction import extract_and_write_graph_relations
from app.ingestion.chunking import Chunk
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry

_TERMS = [
    Term(
        standard_name="示例错误码E502",
        aliases=["网关超时示例"],
        term_type="error_code",
        product_line="示例产品线",
    ),
    Term(
        standard_name="示例登录模块",
        aliases=["示例认证模块"],
        term_type="module",
        product_line="示例产品线",
    ),
]


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []
        self.deleted_sources: list[tuple[str, str]] = []

    async def merge_relation(
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
            }
        )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        self.deleted_sources.append((source, tenant_id))
        self.written = [
            item
            for item in self.written
            if not (item["source"] == source and item["tenant_id"] == tenant_id)
        ]


async def test_extracts_normalizes_and_writes_relations_from_chunks():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
        ),
    )
    graph_client = FakeGraphClient()
    chunks = [Chunk(text="网关超时示例通常与示例认证模块相关", heading_path=[], source="a.md")]

    written = await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
    )

    assert written == 1
    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
            "source": "a.md",
            "tenant_id": "t1",
        }
    ]
    assert graph_client.deleted_sources == [("a.md", "t1")]


async def test_unresolved_candidate_goes_to_review_queue_when_review_conn_provided():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "不存在的实体", "relation_type": "RELATED_TO"}]}'
        ),
    )
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    chunks = [Chunk(text="网关超时示例通常与不存在的实体相关", heading_path=[], source="a.md")]

    written = await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        review_conn=review_conn,
    )

    assert written == 0
    assert graph_client.written == []
    pending = await list_pending_reviews(review_conn)
    assert len(pending) == 1
    assert pending[0]["object_candidate"] == "不存在的实体"


async def test_reingesting_same_source_clears_stale_relations_no_longer_present():
    # 文档内容变更后重新摄取：旧版本抽取出的关系不应该在图谱里永久残留
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
        ),
    )
    graph_client = FakeGraphClient()
    chunks = [Chunk(text="网关超时示例通常与示例认证模块相关", heading_path=[], source="a.md")]
    await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
    )
    assert len(graph_client.written) == 1

    # 文档改版后不再提到这组关系，重新摄取应该把旧边清掉，而不是新旧并存
    llm_registry_v2 = ProviderRegistry()
    llm_registry_v2.register(
        ProviderCapability.LLM, "llm", FixedLLMProvider('{"relations": []}')
    )
    new_chunks = [Chunk(text="改版后的无关内容", heading_path=[], source="a.md")]

    await extract_and_write_graph_relations(
        new_chunks,
        llm_registry=llm_registry_v2,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
    )

    assert graph_client.written == []
    assert graph_client.deleted_sources == [("a.md", "t1"), ("a.md", "t1")]


async def test_reingesting_same_source_different_tenant_does_not_delete_other_tenants_edges():
    """跨租户隔离的正面验证：两个租户各自摄取相同相对路径的文档，
    租户 t2 重新摄取不应该删掉租户 t1 已经写入的边——这是本次改动
    顺带修复的 bug（此前 delete_relations_by_source 完全不看租户）。
    """
    llm_registry_t1 = ProviderRegistry()
    llm_registry_t1.register(
        ProviderCapability.LLM,
        "llm",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
        ),
    )
    graph_client = FakeGraphClient()
    chunks = [Chunk(text="网关超时示例通常与示例认证模块相关", heading_path=[], source="a.md")]
    await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry_t1,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
    )
    assert len(graph_client.written) == 1

    llm_registry_t2 = ProviderRegistry()
    llm_registry_t2.register(
        ProviderCapability.LLM, "llm", FixedLLMProvider('{"relations": []}')
    )
    await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry_t2,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t2",
    )

    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
            "source": "a.md",
            "tenant_id": "t1",
        }
    ]
```

打开 `tests/ingestion/test_ingest_pipeline.py`，把第 110-129 行的 `FakeGraphClient` 类替换为：

```python
class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []
        self.deleted_sources: list[tuple[str, str]] = []

    async def merge_relation(
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
            }
        )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        self.deleted_sources.append((source, tenant_id))
```

然后把第 178-185 行 `test_ingest_markdown_file_writes_graph_relations_when_configured` 的断言替换为：

```python
    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
            "source": str(md_file),
            "tenant_id": "t1",
        }
    ]
```

（`test_ingest_markdown_file_sends_unresolved_candidates_to_review_queue` 不需要改动断言，因为它断言的是 `graph_client.written == []`，与 `tenant_id` 无关；`ingest_markdown_file` 调用本身已经带 `tenant_id="t1"`，不需要改。）

打开 `tests/ingestion/test_ingest_main.py`，把第 60-79 行的 `FakeGraphClient` 类替换为：

```python
class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []
        self.synced_terms: list[Term] = []
        self.deleted_sources: list[tuple[str, str]] = []

    async def merge_relation(
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
            }
        )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        self.deleted_sources.append((source, tenant_id))

    async def sync_terms(self, terms: list[Term]) -> None:
        self.synced_terms.extend(terms)
```

（这个文件的两个测试 `test_main_sends_unresolved_graph_candidates_to_injected_review_conn`/`test_main_syncs_ontology_terms_into_graph_when_build_graph` 都不需要改动测试函数体本身，只是 `FakeGraphClient` 的方法签名要能接受新的 `tenant_id` 参数，否则调用方一传参就 `TypeError`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/test_graph_extraction.py tests/ingestion/test_ingest_pipeline.py tests/ingestion/test_ingest_main.py -v`
Expected: `test_graph_extraction.py` 里全部 4 个测试（含新增的跨租户验证）报 `TypeError: extract_and_write_graph_relations() got an unexpected keyword argument 'tenant_id'`；`test_ingest_pipeline.py`/`test_ingest_main.py` 里带 `graph_client=` 的测试因为 `_maybe_extract_graph_relations` 还没往下传 `tenant_id`，`FakeGraphClient.merge_relation`/`delete_relations_by_source` 会被调用时缺少 `tenant_id` 关键字参数而报 `TypeError: missing 1 required keyword-only argument: 'tenant_id'`。

- [ ] **Step 3: 写最小实现**

把 `app/ingestion/graph_extraction.py` 整个文件替换为：

```python
from __future__ import annotations

import aiosqlite

from app.graphrag.llm_extractor import extract_candidate_relations
from app.graphrag.normalization import GraphWriteClientProtocol, normalize_and_write_relations
from app.graphrag.ontology import Term
from app.ingestion.chunking import Chunk
from app.providers.registry import ProviderRegistry


async def extract_and_write_graph_relations(
    chunks: list[Chunk],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    terms: list[Term],
    graph_client: GraphWriteClientProtocol,
    source: str,
    tenant_id: str,
    review_conn: aiosqlite.Connection | None = None,
    extract_timeout_sec: float = 2.0,
) -> int:
    """摄取时的图谱构建：逐 chunk 做 LLM 关系抽取 + 术语表归一化 + 写入 Neo4j。

    这是可选步骤（未接入 ingest_markdown_file/ingest_pdf_file 的默认路径），
    调用方需要显式提供 llm_registry/terms/graph_client 才会执行；不提供
    则摄取流程只做向量化写入，与阶段2的行为保持完全兼容。

    写入前先删掉 source+tenant_id 这个文档、这个租户之前写过的全部关系边
    （delete_relations_by_source），再重新抽取写入——和
    vector_store.delete_by_source() 同样的道理：文档内容变更后，旧版本
    抽取出的关系不会永久残留在图谱里。对全新文档这是无害的空操作。
    tenant_id 同时保证了这个清理动作不会波及其它租户摄取过的同名文档。

    review_conn 同样可选：提供时，未能对齐术语表的候选关系会进入人工
    待审核队列而不是直接丢弃（见 normalize_and_write_relations）。
    """
    await graph_client.delete_relations_by_source(source, tenant_id=tenant_id)
    total_written = 0
    for chunk in chunks:
        relations = await extract_candidate_relations(
            chunk.text,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            timeout_sec=extract_timeout_sec,
        )
        total_written += await normalize_and_write_relations(
            relations,
            terms=terms,
            graph_client=graph_client,
            source=source,
            tenant_id=tenant_id,
            review_conn=review_conn,
        )
    return total_written
```

在 `app/ingestion/pipeline.py` 里，把 `_maybe_extract_graph_relations` 的签名和函数体（原第 58-88 行）替换为：

```python
async def _maybe_extract_graph_relations(
    chunks: list[Chunk],
    *,
    source: str,
    tenant_id: str,
    graph_llm_registry: ProviderRegistry | None,
    graph_llm_provider_name: str | None,
    graph_terms: list[Term] | None,
    graph_client: GraphWriteClientProtocol | None,
    graph_review_conn: aiosqlite.Connection | None,
) -> None:
    """图谱抽取为可选步骤，四项必需参数任一缺失则直接跳过，不影响向量化写入路径。

    graph_review_conn 独立于这四项之外是可选项：未能对齐术语表的候选
    关系会转入人工待审核队列而非直接丢弃（见 normalize_and_write_relations）。
    """
    if not (
        graph_llm_registry
        and graph_llm_provider_name
        and graph_terms
        and graph_client is not None
    ):
        return
    await extract_and_write_graph_relations(
        chunks,
        llm_registry=graph_llm_registry,
        llm_provider_name=graph_llm_provider_name,
        terms=graph_terms,
        graph_client=graph_client,
        source=source,
        tenant_id=tenant_id,
        review_conn=graph_review_conn,
    )
```

再把 `_ingest_chunks` 函数体里调用 `_maybe_extract_graph_relations` 的那一处（原第 119-127 行）替换为：

```python
    await _maybe_extract_graph_relations(
        chunks,
        source=str(path),
        tenant_id=tenant_id,
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=graph_llm_provider_name,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=graph_review_conn,
    )
```

（`_ingest_chunks` 函数签名本身不用改，`tenant_id` 参数已经存在，只是这次多传一份给 `_maybe_extract_graph_relations`。5 个 `ingest_*_file` 函数和 `ingest_directory` 都不需要改动。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/test_graph_extraction.py tests/ingestion/test_ingest_pipeline.py tests/ingestion/test_ingest_main.py -v`
Expected: 全部通过

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: `tests/graphrag/`、`tests/ingestion/` 全部通过；`tests/graphrag/test_term_guard.py`、`tests/agent/test_tools.py`、`tests/agent/test_planner.py`、`tests/agent/test_graph_planner.py`（Task 4/5 范围）仍会报 `tenant_id` 相关的 `TypeError`，属于预期，本任务不用管。

- [ ] **Step 6: 提交**

```bash
git add app/ingestion/graph_extraction.py app/ingestion/pipeline.py tests/ingestion/test_graph_extraction.py tests/ingestion/test_ingest_pipeline.py tests/ingestion/test_ingest_main.py
git commit -m "feat: thread tenant_id through the graph-extraction ingestion path"
```

---

### Task 4: TermGuard 查询路径透传 `tenant_id`（`term_guard.py` + `graph.py`）

**Files:**
- Modify: `app/graphrag/term_guard.py`
- Modify: `app/agent/graph.py:265-271`（`term_guard_node` 闭包）
- Test: `tests/graphrag/test_term_guard.py`、`tests/agent/test_graph.py`

**Interfaces:**
- Consumes：Task 1 的 `query_subgraph(standard_name, *, tenant_id: str)`。
- Produces：
  - `class GraphClientProtocol(Protocol)` 的 `query_subgraph` 签名同步加 `tenant_id: str`（Task 5 的 `graph_query_tool` 也用这个 Protocol，但那是独立任务，互不依赖）。
  - `async def build_term_guard_context(text, *, terms, tenant_id: str, graph_client) -> str | None`（新增必填 `tenant_id`）。

- [ ] **Step 1: 写失败测试**

把 `tests/graphrag/test_term_guard.py` 整个文件替换为：

```python
from app.graphrag.ontology import Term
from app.graphrag.term_guard import build_term_guard_context

_TERMS = [
    Term(
        standard_name="错误码E502",
        aliases=["网关超时"],
        term_type="error_code",
        product_line="核心平台",
    ),
]


class FakeGraphClient:
    def __init__(self, subgraph_rows: list[dict]) -> None:
        self._rows = subgraph_rows
        self.queried_names: list[str] = []
        self.queried_tenant_ids: list[str] = []

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        self.queried_names.append(standard_name)
        self.queried_tenant_ids.append(tenant_id)
        return self._rows


async def test_returns_none_when_no_term_matched():
    graph_client = FakeGraphClient(subgraph_rows=[])

    context = await build_term_guard_context(
        "今天天气怎么样", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert context is None
    assert graph_client.queried_names == []


async def test_returns_context_and_queries_graph_when_term_matched():
    graph_client = FakeGraphClient(
        subgraph_rows=[{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    )

    context = await build_term_guard_context(
        "我这边报了网关超时", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert context is not None
    assert "错误码E502" in context
    assert "登录模块" in context
    assert graph_client.queried_names == ["错误码E502"]
    assert graph_client.queried_tenant_ids == ["t1"]
```

在 `tests/agent/test_graph.py` 顶部 import 区（现有 `from app.agent.graph import build_agent_graph` 那一行）之后追加：

```python
from app.graphrag.ontology import Term
```

然后在文件末尾追加：

```python
class _FakeTermGuardGraphClient:
    def __init__(self) -> None:
        self.queried_tenant_ids: list[str] = []

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        self.queried_tenant_ids.append(tenant_id)
        return []


async def test_term_guard_node_forwards_tenant_id_to_graph_client():
    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(with_records=True, llm_text="重启路由器即可解决。")
    )
    terms = [
        Term(
            standard_name="错误码E502",
            aliases=[],
            term_type="error_code",
            product_line="核心平台",
        )
    ]
    graph_client = _FakeTermGuardGraphClient()
    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        terms=terms,
        graph_client=graph_client,
    )

    await graph.ainvoke({"question": "错误码E502是什么意思？", "tenant_id": "t2"})

    assert graph_client.queried_tenant_ids == ["t2"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_term_guard.py tests/agent/test_graph.py -v -k "term_guard or forwards_tenant_id"`
Expected: `tests/graphrag/test_term_guard.py` 里 `test_returns_context_and_queries_graph_when_term_matched` 报 `TypeError: build_term_guard_context() got an unexpected keyword argument 'tenant_id'`（`test_returns_none_when_no_term_matched` 因为没匹配到术语、根本不会走到需要 `tenant_id` 的分支，此时会因为函数签名不接受这个新关键字参数同样报 `TypeError`）；`test_term_guard_node_forwards_tenant_id_to_graph_client` 报同样的 `TypeError`（`term_guard_node` 还没往下传 `tenant_id`，`build_term_guard_context` 还不接受这个参数）。

- [ ] **Step 3: 写最小实现**

把 `app/graphrag/term_guard.py` 整个文件替换为：

```python
from __future__ import annotations

from typing import Any, Protocol

from app.graphrag.ontology import Term
from app.graphrag.term_matcher import match_terms


class GraphClientProtocol(Protocol):
    async def query_subgraph(
        self, standard_name: str, *, tenant_id: str
    ) -> list[dict[str, Any]]: ...


async def build_term_guard_context(
    text: str,
    *,
    terms: list[Term],
    tenant_id: str,
    graph_client: GraphClientProtocol,
) -> str | None:
    """术语安全网：命中术语表则强制查图谱并生成上下文，未命中返回 None。

    这是架构文档第3节 TermGuard 节点的核心逻辑，先作为独立函数实现
    （未绑定具体的 Agent 框架），阶段4构建 LangGraph 状态图时将其包装
    为一个图节点，而不必现在就搭一个只有单个节点的临时状态图。

    tenant_id 透传给 query_subgraph，保证强制注入的图谱上下文只包含
    当前租户自己的关系事实，不会把其它租户的知识泄露进来。
    """
    matched = match_terms(text, terms)
    if not matched:
        return None

    lines = ["检测到以下专有名词，已强制注入知识图谱上下文（回答时请使用标准名称）："]
    for term in matched:
        lines.append(
            f"- {term.standard_name}（类型: {term.term_type}, 产品线: {term.product_line}）"
        )
        subgraph = await graph_client.query_subgraph(
            term.standard_name, tenant_id=tenant_id
        )
        for row in subgraph:
            lines.append(
                f"  关联: {row['related_name']}（关系: {row['relation_type']}）"
            )
    return "\n".join(lines)
```

在 `app/agent/graph.py` 里，把 `term_guard_node` 函数体（原第 265-271 行）替换为：

```python
    async def term_guard_node(state: AgentState) -> dict[str, Any]:
        if not (terms and graph_client is not None):
            return {"term_guard_context": None}
        context = await build_term_guard_context(
            state["question"],
            terms=terms,
            tenant_id=state["tenant_id"],
            graph_client=graph_client,
        )
        return {"term_guard_context": context}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_term_guard.py tests/agent/test_graph.py -v`
Expected: 全部通过（`test_graph.py` 原有的 2 个测试 + 新增的 1 个）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: `tests/graphrag/`、`tests/ingestion/`、`tests/agent/test_graph.py` 全部通过；`tests/agent/test_tools.py`、`tests/agent/test_planner.py`、`tests/agent/test_graph_planner.py`（Task 5 范围）仍会报 `tenant_id` 相关的 `TypeError`，属于预期。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/term_guard.py app/agent/graph.py tests/graphrag/test_term_guard.py tests/agent/test_graph.py
git commit -m "feat: thread tenant_id through the TermGuard graph-query path"
```

---

### Task 5: Planner 工具调用路径透传 `tenant_id`（`tools.py` + `planner.py`）

**Files:**
- Modify: `app/agent/tools.py:104-123`（`graph_query_tool` 函数）
- Modify: `app/agent/planner.py:120-130`（`_dispatch_tool_call` 里 `graph_query_tool` 分支）
- Test: `tests/agent/test_tools.py`、`tests/agent/test_planner.py`、`tests/agent/test_graph_planner.py`

**Interfaces:**
- Consumes：Task 1 的 `query_subgraph(standard_name, *, tenant_id: str)`（通过 Task 4 已更新的 `GraphClientProtocol`，`app/agent/tools.py` 直接复用 `app.graphrag.term_guard.GraphClientProtocol`，不需要重复定义）。
- Produces：`async def graph_query_tool(entity_name, *, terms, tenant_id: str, graph_client) -> GraphQueryToolResult`（新增必填 `tenant_id`，插入在 `terms`/`graph_client` 之间以保持和其它函数"实体相关参数在前、身份/客户端参数在后"的顺序习惯）。

**背景说明**：`app/agent/planner.py::_dispatch_tool_call` 里 `tenant_id` 早已作为该函数的顶层参数存在（用于 `vector_search_tool` 那一支），本任务只是在 `graph_query_tool` 那一支的调用里补上这个已经在作用域内的值，不需要新增任何数据来源。

- [ ] **Step 1: 写失败测试**

在 `tests/agent/test_tools.py` 里，把 `FakeGraphClient` 类和 2 个 `graph_query_tool` 相关测试函数（原第 77-105 行）替换为：

```python
class FakeGraphClient:
    def __init__(self) -> None:
        self.queried_tenant_ids: list[str] = []

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        self.queried_tenant_ids.append(tenant_id)
        return [{"related_name": "示例登录模块", "relation_type": "RELATED_TO"}]


async def test_graph_query_tool_resolves_alias_and_returns_subgraph():
    graph_client = FakeGraphClient()
    result = await graph_query_tool(
        "网关超时示例", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert result.resolved is True
    assert result.standard_name == "示例错误码E502"
    assert result.subgraph == [
        {"related_name": "示例登录模块", "relation_type": "RELATED_TO"}
    ]
    assert graph_client.queried_tenant_ids == ["t1"]


async def test_graph_query_tool_returns_unresolved_without_querying_graph():
    class ExplodingGraphClient:
        async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
            raise AssertionError("未命中术语表时不应该查图谱")

    result = await graph_query_tool(
        "完全不认识的名字", terms=_TERMS, tenant_id="t1", graph_client=ExplodingGraphClient()
    )

    assert result.resolved is False
    assert result.standard_name is None
    assert result.subgraph == []
```

在 `tests/agent/test_planner.py` 里，把 `FakeGraphClient` 类（原第 209-211 行）和 `test_run_tool_calls_executes_graph_query_tool` 测试函数替换为：

```python
class FakeGraphClient:
    def __init__(self) -> None:
        self.queried_tenant_ids: list[str] = []

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        self.queried_tenant_ids.append(tenant_id)
        return [{"related_name": "示例登录模块", "relation_type": "RELATED_TO"}]


async def test_run_tool_calls_executes_graph_query_tool():
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([]))

    state = {
        "tenant_id": "t1",
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_2",
                "name": "graph_query_tool",
                "arguments": '{"entity_name": "网关超时示例"}',
            }
        ],
    }

    graph_client = FakeGraphClient()
    update = await run_tool_calls(
        state,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        terms=_TERMS,
        graph_client=graph_client,
    )

    tool_message = update["planner_messages"][-1]
    assert "示例错误码E502" in tool_message["content"]
    assert "示例登录模块" in tool_message["content"]
    assert graph_client.queried_tenant_ids == ["t1"]
```

在 `tests/agent/test_graph_planner.py` 里，把第 213-215 行的 `FakeGraphClient` 类替换为：

```python
    class FakeGraphClient:
        async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
            return [{"related_name": "示例登录模块", "relation_type": "RELATED_TO"}]
```

（这个测试的重点是 Planner 工具调用整体链路是否走通，不是租户隔离的正面验证——Task 4 和本任务在 `test_graph.py`/`test_planner.py` 里已经各自补了一条针对性的 `tenant_id` 断言，这里只需要让签名不报错即可，不用额外加断言，避免重复造轮子。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tools.py tests/agent/test_planner.py tests/agent/test_graph_planner.py -v -k "graph_query_tool or graph_planner_uses"`
Expected: `test_tools.py` 里 2 个测试报 `TypeError: graph_query_tool() got an unexpected keyword argument 'tenant_id'`；`test_planner.py::test_run_tool_calls_executes_graph_query_tool` 同样报这个 `TypeError`（`_dispatch_tool_call` 还没把 `tenant_id` 传给 `graph_query_tool`）；`test_graph_planner.py::test_planner_graph_uses_graph_query_tool_with_term_guard_context` 同样报这个 `TypeError`。

- [ ] **Step 3: 写最小实现**

把 `app/agent/tools.py` 的 `graph_query_tool` 函数（原第 104-123 行）替换为：

```python
async def graph_query_tool(
    entity_name: str,
    *,
    terms: list[Term],
    tenant_id: str,
    graph_client: GraphClientProtocol,
) -> GraphQueryToolResult:
    """graph_query_tool 的实际执行体：先对齐术语表，命中才查图谱。

    未命中术语表时直接返回 resolved=False，不发起图查询——和
    normalize_and_write_relations 的"先归一化再写入"是同一个原则：
    没有标准名就没有查询的意义，也避免拿一个不存在的名字去查图谱浪费一次调用。

    tenant_id 透传给 query_subgraph，防止返回给 LLM 的子图里混入其它
    租户的关系事实。
    """
    standard_name = resolve_to_standard_name(entity_name, terms)
    if standard_name is None:
        return GraphQueryToolResult(resolved=False, standard_name=None, subgraph=[])

    subgraph = await graph_client.query_subgraph(standard_name, tenant_id=tenant_id)
    return GraphQueryToolResult(
        resolved=True, standard_name=standard_name, subgraph=subgraph
    )
```

在 `app/agent/planner.py` 里，把 `_dispatch_tool_call` 函数体里 `graph_query_tool` 分支（原第 120-130 行）替换为：

```python
    if name == "graph_query_tool":
        if not (terms and graph_client is not None):
            return json.dumps({"error": "graph_query_tool 未配置"}, ensure_ascii=False), []
        entity_name = str(arguments.get("entity_name", ""))
        result = await graph_query_tool(
            entity_name, terms=terms, tenant_id=tenant_id, graph_client=graph_client
        )
        observation = {
            "resolved": result.resolved,
            "standard_name": result.standard_name,
            "subgraph": result.subgraph,
        }
        return json.dumps(observation, ensure_ascii=False), []
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tools.py tests/agent/test_planner.py tests/agent/test_graph_planner.py -v`
Expected: 全部通过

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过（此时全部 5 个任务的改动都已落地，不应该再有任何 `tenant_id` 相关的 `TypeError` 残留）。

- [ ] **Step 6: 提交**

```bash
git add app/agent/tools.py app/agent/planner.py tests/agent/test_tools.py tests/agent/test_planner.py tests/agent/test_graph_planner.py
git commit -m "feat: thread tenant_id through the Planner graph_query_tool path"
```

---

## 完成后

5 个任务全部提交后，Neo4j 知识图谱的关系事实（`RELATED_TO`/`BELONGS_TO_MODULE` 边）在写入（`merge_relation`）、查询（`query_subgraph`，覆盖 TermGuard 强制注入和 Planner 工具调用两条独立路径）、删除（`delete_relations_by_source`）三个动作上都强制按 `tenant_id` 隔离，且顺带修复了跨租户误删边的真实 bug；术语节点/别名边保持全局共享，符合本次设计的明确范围。
