# 知识图谱本体（术语表）管理后台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把术语表（知识图谱的封闭词表本体）从"只能手动改 YAML 文件+重启服务才生效"迁移成可以在管理后台在线增删改的数据，让业务/技术支持团队不用求助开发就能维护这份"基准真相"。

**Architecture:** 术语表迁移到 SQLite（复用图谱审核队列已有的 `graph_review_db_path`/`get_review_conn`，不新增数据库文件），新建 `app/graphrag/terms_store.py` 承载 schema+CRUD，全局共用不分租户。新增/编辑术语时同步调用 `Neo4jGraphClient.sync_term()` 把术语属性和别名节点写进图谱；改名（改 standard_name）额外做一次图节点属性级联更新（`MATCH+SET`，不是先删再建，已有的关系边不会断）；删除前检查该术语在图谱里是否已有关系边，有就拒绝删除。`app/api/deps.py::get_terms()` 从进程级内存缓存改成每次请求查库，FastAPI 的 `Depends()` 对 async/sync 依赖处理一致，已有的 7 处路由调用点和测试里遍布的 `dependency_overrides[deps.get_terms] = lambda: [...]` 都不需要改。原来临时加的只读接口 `GET /api/admin/graph-reviews/terms`（上周为审核页面自动补全加的）撤掉，前端改调新的完整 CRUD 接口。

**Tech Stack:** 后端 FastAPI + aiosqlite + Neo4j（`neo4j` 异步驱动）。前端 React + TypeScript——项目没有配置任何前端自动化测试框架，验证手段是 `npm run typecheck` + `npm run build` + 手动核对。

## Global Constraints

- 术语表全局共用，不按 `tenant_id` 隔离——这是本次改动刻意维持的现状，不是遗漏（详见方案讨论：迁移只换存储介质，不扩大语义范围）。
- `standard_name`（连同所有 `aliases`）必须在全表范围内唯一——新增/编辑提交时校验，和别的术语的 `standard_name` 或任意一个 `alias` 重复就拒绝（400），避免 `resolve_to_standard_name()` 按列表顺序遍历命中第一个匹配、结果不可预测的问题。
- 改名（把已存在术语的 `standard_name` 改成一个新值）允许，但如果新值恰好已经是另一个术语在用的名字，直接拒绝（400），不支持合并两个术语——这种情况留给后续单独的需求处理，不在本次范围内。
- 删除术语前必须检查该术语在 Neo4j 里是否已有关系边（`ALIAS_OF` 这种结构性同步边不算），有就拒绝删除（409），避免图谱里出现"词表说这个术语不存在了，但图谱边还在用它"的不一致状态。
- 新增/编辑术语提交成功后，必须同步调用 `Neo4jGraphClient.sync_term()` 把这个术语实时同步进图谱（属性、别名节点），不能只写 SQLite、让图谱异步落后。
- 已知接受的限制（不在本次修复范围内，写在这里是为了不被误当成本次改动引入的新问题）：`sync_term()` 现有实现只会新增别名节点，不会清理"编辑时被移除的别名"对应的旧别名节点——这是迁移前就存在的行为，本次改动只是让它被调用得更频繁，不改这个函数本身的语义。
- 旧的只读接口 `GET /api/admin/graph-reviews/terms` 在本次改动里彻底移除（连同它专属的两个测试），前端改成调新的 `GET /api/admin/terms`。

---

## Task 1: Neo4j client 新增术语节点的计数/改名/删除方法

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Test: `tests/graphrag/test_neo4j_client.py`

**Interfaces:**
- Produces:
  - `Neo4jGraphClient.count_relation_edges_for_term(standard_name: str) -> int`——统计该术语节点参与的、非 `ALIAS_OF` 的关系边数量，供删除前的守卫检查用。
  - `Neo4jGraphClient.rename_term_node(*, old_name: str, new_name: str) -> None`——对同一个节点对象做属性级联更新（`MATCH+SET`），节点已有的关系边不受影响。
  - `Neo4jGraphClient.delete_term_node(standard_name: str) -> None`——删除该术语节点及其别名节点（`DETACH DELETE`），只应该在确认没有关系边之后调用。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_neo4j_client.py` 文件末尾追加：

```python
async def test_count_relation_edges_for_term_returns_edge_count():
    session = FakeSession(rows=[{"edge_count": 3}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.count_relation_edges_for_term("错误码E502")

    assert count == 3
    assert session.last_parameters == {"standard_name": "错误码E502"}
    assert "ALIAS_OF" in session.last_query


async def test_count_relation_edges_for_term_returns_zero_when_no_rows():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.count_relation_edges_for_term("孤立术语")

    assert count == 0


async def test_rename_term_node_sends_expected_query_and_parameters():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.rename_term_node(old_name="错误码E502", new_name="错误码E502v2")

    assert session.last_parameters == {
        "old_name": "错误码E502",
        "new_name": "错误码E502v2",
    }
    assert "MATCH" in session.last_query
    assert "SET t.standard_name = $new_name" in session.last_query
    # 必须是 MATCH+SET 原地改属性，不能是先删再建——删了再建会让节点
    # 已有的关系边找不到挂载对象，变成孤儿边
    assert "DELETE" not in session.last_query
    assert "CREATE" not in session.last_query


async def test_delete_term_node_sends_detach_delete_query():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_term_node("废弃术语")

    assert session.last_parameters == {"standard_name": "废弃术语"}
    assert "DETACH DELETE" in session.last_query
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -v`
Expected: FAIL——`count_relation_edges_for_term`/`rename_term_node`/`delete_term_node` 都还不存在，`AttributeError`。

- [ ] **Step 3: 实现**

在 `app/graphrag/neo4j_client.py` 里，`_SYNC_TERM_QUERY` 常量（当前第 72-79 行）之后加三个新的模块级查询常量：

```python
_COUNT_TERM_RELATION_EDGES_QUERY = """
MATCH (t:Term {standard_name: $standard_name})-[r]-()
WHERE type(r) <> 'ALIAS_OF'
RETURN count(r) AS edge_count
"""
# ALIAS_OF 是术语表→图谱的结构性同步边（sync_term 写入，见上面
# _SYNC_TERM_QUERY），不代表"这个术语已经出现在真实知识图谱数据里"；
# 删除前的守卫检查只关心 LLM 抽取/人工审核产出的关系边（merge_relation
# 写入的 RELATED_TO/PART_OF/... 这些），排除 ALIAS_OF 避免每个术语只要
# 有别名就永远无法删除。

_RENAME_TERM_NODE_QUERY = """
MATCH (t:Term {standard_name: $old_name})
SET t.standard_name = $new_name
"""
# 必须是对同一个节点对象做属性 SET，不能先 DELETE 再 CREATE——Neo4j 的
# 关系边挂在节点对象上，不是按属性值查找的，原地改属性不会影响节点
# 已有的任何关系边；新名字如果已经是另一个节点在用，调用方必须在此之前
# 自己校验过（见 app/graphrag/terms_store.py 的唯一性校验），这里不做
# 校验，重复调用会导致两个不同节点各自拥有同一个 standard_name 属性值
# （Neo4j 不会阻止，只是后续按 standard_name MATCH 会命中两个节点）。

_DELETE_TERM_NODE_QUERY = """
MATCH (t:Term {standard_name: $standard_name})
OPTIONAL MATCH (a:Term)-[:ALIAS_OF]->(t)
DETACH DELETE t, a
"""
# 连同别名节点一起删——sync_term() 建的别名节点除了指向这个标准术语
# 没有其它用途，标准术语被删后别名节点留着就是纯垃圾数据。OPTIONAL
# MATCH 让"没有别名"的术语也能正常匹配到 t（DELETE 一个 null 值是
# Cypher 里的合法操作，不会报错）。
```

在 `sync_terms` 方法（当前第 206-208 行）之后加三个新方法：

```python
    async def count_relation_edges_for_term(self, standard_name: str) -> int:
        """统计该术语节点参与的、非结构性同步边（ALIAS_OF）的关系边数量，
        供管理后台删除术语前的守卫检查用——见 _COUNT_TERM_RELATION_EDGES_QUERY
        的说明。"""
        async with self._driver.session() as session:
            result = await session.run(
                _COUNT_TERM_RELATION_EDGES_QUERY, {"standard_name": standard_name}
            )
            rows = await result.data()
            return rows[0]["edge_count"] if rows else 0

    async def rename_term_node(self, *, old_name: str, new_name: str) -> None:
        """把一个术语节点的 standard_name 属性原地改成新值，不影响节点
        已有的关系边——见 _RENAME_TERM_NODE_QUERY 的说明。调用方必须自己
        先确认 new_name 不会跟另一个已存在的术语节点冲突。"""
        async with self._driver.session() as session:
            await session.run(
                _RENAME_TERM_NODE_QUERY, {"old_name": old_name, "new_name": new_name}
            )

    async def delete_term_node(self, standard_name: str) -> None:
        """删除一个术语节点及其别名节点——只应该在确认过
        count_relation_edges_for_term() 返回 0 之后调用，见
        _DELETE_TERM_NODE_QUERY 的说明。"""
        async with self._driver.session() as session:
            await session.run(_DELETE_TERM_NODE_QUERY, {"standard_name": standard_name})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -v`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/neo4j_client.py tests/graphrag/test_neo4j_client.py
git commit -m "feat(graphrag): add Neo4j term-node count/rename/delete for ontology management"
```

---

## Task 2: 术语表 SQLite 存储层（terms_store.py）

**Files:**
- Create: `app/graphrag/terms_store.py`
- Test: `tests/graphrag/test_terms_store.py`

**Interfaces:**
- Produces:
  - `TermNotFoundError(Exception)`、`TermNameConflictError(Exception)`。
  - `ensure_terms_schema(conn, *, seed_yaml_path: Path | None = None) -> None`——幂等建表；只在表首次创建、且传入的 `seed_yaml_path` 存在时，从这个 YAML 文件一次性导入历史内容。不传 `seed_yaml_path`（默认 `None`）只建表、不做任何导入。
  - `list_terms(conn) -> list[Term]`
  - `get_term(conn, standard_name) -> Term`（不存在抛 `TermNotFoundError`）
  - `create_term(conn, *, standard_name, aliases, term_type, product_line) -> None`（名字冲突抛 `TermNameConflictError`）
  - `update_term(conn, *, standard_name, new_standard_name, aliases, term_type, product_line) -> None`（`standard_name` 定位当前记录，`new_standard_name` 是提交的新名字，可以和 `standard_name` 相同即不改名；不存在抛 `TermNotFoundError`，名字冲突抛 `TermNameConflictError`）
  - `delete_term(conn, standard_name) -> None`（不存在抛 `TermNotFoundError`）

- [ ] **Step 1: 写失败的测试**

创建 `tests/graphrag/test_terms_store.py`：

```python
from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.ontology import Term
from app.graphrag.terms_store import (
    TermNameConflictError,
    TermNotFoundError,
    create_term,
    delete_term,
    ensure_terms_schema,
    get_term,
    list_terms,
    update_term,
)


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    return conn


async def test_ensure_terms_schema_without_seed_path_creates_empty_table():
    conn = await _connect()

    assert await list_terms(conn) == []


async def test_ensure_terms_schema_seeds_from_yaml_only_on_first_creation(tmp_path):
    yaml_path = tmp_path / "seed.yaml"
    yaml_path.write_text(
        "terms:\n"
        "  - standard_name: 种子术语\n"
        "    aliases: [别名A]\n"
        "    term_type: type1\n"
        "    product_line: line1\n",
        encoding="utf-8",
    )
    conn = await aiosqlite.connect(":memory:")

    await ensure_terms_schema(conn, seed_yaml_path=yaml_path)
    seeded = await list_terms(conn)
    assert [t.standard_name for t in seeded] == ["种子术语"]

    # 再次调用（模拟第二次进程启动）：表已存在，即使 YAML 内容变了也不
    # 重新导入——只在首次建表时导入一次
    yaml_path.write_text(
        "terms:\n  - standard_name: 另一个术语\n    aliases: []\n"
        "    term_type: t\n    product_line: p\n",
        encoding="utf-8",
    )
    await ensure_terms_schema(conn, seed_yaml_path=yaml_path)
    after_second_call = await list_terms(conn)
    assert [t.standard_name for t in after_second_call] == ["种子术语"]


async def test_ensure_terms_schema_skips_seeding_when_yaml_path_missing(tmp_path):
    conn = await aiosqlite.connect(":memory:")
    missing_path = tmp_path / "does-not-exist.yaml"

    await ensure_terms_schema(conn, seed_yaml_path=missing_path)

    assert await list_terms(conn) == []


async def test_create_term_then_list_returns_it():
    conn = await _connect()

    await create_term(
        conn, standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code", product_line="核心平台",
    )

    terms = await list_terms(conn)
    assert terms == [
        Term(
            standard_name="错误码E502", aliases=["网关超时"],
            term_type="error_code", product_line="核心平台",
        )
    ]


async def test_create_term_rejects_duplicate_standard_name():
    conn = await _connect()
    await create_term(
        conn, standard_name="错误码E502", aliases=[],
        term_type="error_code", product_line="核心平台",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, standard_name="错误码E502", aliases=[],
            term_type="other", product_line="other",
        )


async def test_create_term_rejects_alias_that_collides_with_another_terms_standard_name():
    conn = await _connect()
    await create_term(
        conn, standard_name="登录模块", aliases=[],
        term_type="module", product_line="核心平台",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, standard_name="错误码E502", aliases=["登录模块"],
            term_type="error_code", product_line="核心平台",
        )


async def test_create_term_rejects_alias_that_collides_with_another_terms_alias():
    conn = await _connect()
    await create_term(
        conn, standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code", product_line="核心平台",
    )

    with pytest.raises(TermNameConflictError):
        await create_term(
            conn, standard_name="登录模块", aliases=["网关超时"],
            term_type="module", product_line="核心平台",
        )


async def test_get_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await get_term(conn, "不存在的术语")


async def test_update_term_without_rename_changes_fields_in_place():
    conn = await _connect()
    await create_term(
        conn, standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code", product_line="核心平台",
    )

    await update_term(
        conn, standard_name="错误码E502", new_standard_name="错误码E502",
        aliases=["网关超时", "502错误"], term_type="error_code", product_line="新产品线",
    )

    term = await get_term(conn, "错误码E502")
    assert term.aliases == ["网关超时", "502错误"]
    assert term.product_line == "新产品线"


async def test_update_term_with_rename_moves_to_new_standard_name():
    conn = await _connect()
    await create_term(
        conn, standard_name="旧名字", aliases=[],
        term_type="t", product_line="p",
    )

    await update_term(
        conn, standard_name="旧名字", new_standard_name="新名字",
        aliases=[], term_type="t", product_line="p",
    )

    with pytest.raises(TermNotFoundError):
        await get_term(conn, "旧名字")
    renamed = await get_term(conn, "新名字")
    assert renamed.standard_name == "新名字"


async def test_update_term_rejects_rename_into_an_existing_name():
    conn = await _connect()
    await create_term(conn, standard_name="A", aliases=[], term_type="t", product_line="p")
    await create_term(conn, standard_name="B", aliases=[], term_type="t", product_line="p")

    with pytest.raises(TermNameConflictError):
        await update_term(
            conn, standard_name="A", new_standard_name="B",
            aliases=[], term_type="t", product_line="p",
        )


async def test_update_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await update_term(
            conn, standard_name="不存在", new_standard_name="不存在",
            aliases=[], term_type="t", product_line="p",
        )


async def test_delete_term_removes_it():
    conn = await _connect()
    await create_term(conn, standard_name="待删除", aliases=[], term_type="t", product_line="p")

    await delete_term(conn, "待删除")

    assert await list_terms(conn) == []


async def test_delete_term_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(TermNotFoundError):
        await delete_term(conn, "不存在")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py -v`
Expected: FAIL——`app/graphrag/terms_store.py` 还不存在，`ModuleNotFoundError`。

- [ ] **Step 3: 实现**

创建 `app/graphrag/terms_store.py`：

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from app.graphrag.ontology import Term, load_terminology

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terms (
    standard_name TEXT PRIMARY KEY,
    aliases TEXT NOT NULL,
    term_type TEXT NOT NULL,
    product_line TEXT NOT NULL
);
"""


class TermNotFoundError(Exception):
    """指定的 standard_name 在术语表里不存在。"""


class TermNameConflictError(Exception):
    """提交的 standard_name 或某个 alias，跟另一个已存在的术语的
    standard_name/alias 重复——resolve_to_standard_name() 按顺序遍历命中
    第一个匹配就返回，允许重叠会让抽取结果变成"看列表顺序"决定的、
    不可预测。"""


async def ensure_terms_schema(
    conn: aiosqlite.Connection, *, seed_yaml_path: Path | None = None
) -> None:
    """幂等建表。

    seed_yaml_path 只在传入且指向一个存在的文件、同时这张表是刚刚第一次
    被创建（不是已经存在）时才生效：从这个 YAML 文件里一次性导入内容，
    此后这份 YAML 不再被任何代码路径读取（术语表迁移到这张表之后的过渡
    措施）。不传（默认 None）只是单纯建表，不做任何导入——所有测试固定
    用这个默认行为，不会被本机真实存在的 terminology_seed.yaml 意外
    带入示例数据。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='terms'"
    )
    table_already_existed = await cursor.fetchone() is not None
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    if not table_already_existed and seed_yaml_path is not None and seed_yaml_path.exists():
        for term in load_terminology(seed_yaml_path):
            await conn.execute(
                "INSERT OR IGNORE INTO terms "
                "(standard_name, aliases, term_type, product_line) VALUES (?, ?, ?, ?)",
                (
                    term.standard_name,
                    json.dumps(term.aliases, ensure_ascii=False),
                    term.term_type,
                    term.product_line,
                ),
            )
        await conn.commit()


def _row_to_term(row: aiosqlite.Row) -> Term:
    return Term(
        standard_name=row["standard_name"],
        aliases=json.loads(row["aliases"]),
        term_type=row["term_type"],
        product_line=row["product_line"],
    )


async def list_terms(conn: aiosqlite.Connection) -> list[Term]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT standard_name, aliases, term_type, product_line "
        "FROM terms ORDER BY standard_name"
    )
    rows = await cursor.fetchall()
    return [_row_to_term(row) for row in rows]


async def get_term(conn: aiosqlite.Connection, standard_name: str) -> Term:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT standard_name, aliases, term_type, product_line "
        "FROM terms WHERE standard_name = ?",
        (standard_name,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise TermNotFoundError(f"术语不存在: {standard_name}")
    return _row_to_term(row)


async def _check_name_conflict(
    conn: aiosqlite.Connection,
    *,
    standard_name: str,
    aliases: list[str],
    exclude_standard_name: str | None = None,
) -> None:
    """检查 standard_name 和 aliases 有没有跟别的术语（编辑时排除自己）
    的 standard_name/alias 重叠。术语表规模是人工维护的封闭词表，量级
    不大，直接全表扫描比维护一张单独的"已用名字"索引表更简单，跟
    resolve_to_standard_name() 现有的 O(n) 扫描方式保持一致的复杂度假设。
    """
    all_terms = await list_terms(conn)
    candidate_names = {standard_name, *aliases}
    for term in all_terms:
        if term.standard_name == exclude_standard_name:
            continue
        existing_names = {term.standard_name, *term.aliases}
        overlap = candidate_names & existing_names
        if overlap:
            conflicting = next(iter(overlap))
            raise TermNameConflictError(
                f"{conflicting!r} 已经是术语 {term.standard_name!r} 的别名/标准名，不能重复使用"
            )


async def create_term(
    conn: aiosqlite.Connection,
    *,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
) -> None:
    await _check_name_conflict(conn, standard_name=standard_name, aliases=aliases)
    try:
        await conn.execute(
            "INSERT INTO terms (standard_name, aliases, term_type, product_line) "
            "VALUES (?, ?, ?, ?)",
            (standard_name, json.dumps(aliases, ensure_ascii=False), term_type, product_line),
        )
    except aiosqlite.IntegrityError:
        # _check_name_conflict 已经检查过 standard_name 冲突，这里是防御性
        # 兜底（比如并发写入的极端情况），不是主要校验路径。
        raise TermNameConflictError(f"{standard_name!r} 已经是已有术语的标准名，不能重复创建")
    await conn.commit()


async def update_term(
    conn: aiosqlite.Connection,
    *,
    standard_name: str,
    new_standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
) -> None:
    """standard_name 是当前（改名前）的名字，用来定位这条记录；
    new_standard_name 是提交的新名字，允许和 standard_name 相同（即不改名）。
    """
    await get_term(conn, standard_name)
    await _check_name_conflict(
        conn, standard_name=new_standard_name, aliases=aliases,
        exclude_standard_name=standard_name,
    )
    try:
        await conn.execute(
            "UPDATE terms SET standard_name=?, aliases=?, term_type=?, product_line=? "
            "WHERE standard_name=?",
            (
                new_standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                standard_name,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{new_standard_name!r} 已经是已有术语的标准名，不能重复使用")
    await conn.commit()


async def delete_term(conn: aiosqlite.Connection, standard_name: str) -> None:
    await get_term(conn, standard_name)
    await conn.execute("DELETE FROM terms WHERE standard_name=?", (standard_name,))
    await conn.commit()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py -v`
Expected: PASS，全部用例通过。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/terms_store.py tests/graphrag/test_terms_store.py
git commit -m "feat(graphrag): add SQLite-backed terms store with uniqueness validation"
```

---

## Task 3: 接入现有连接生命周期，get_terms() 改成查库，review_cli.py 同步切换

**Files:**
- Modify: `app/api/deps.py`
- Modify: `app/graphrag/review_factory.py`
- Modify: `app/graphrag/review_cli.py`
- Test: `tests/graphrag/test_review_cli.py`
- Test: `tests/api/test_admin_document_routes.py`（新增一条验证 get_terms 查库的测试）

**Interfaces:**
- Consumes: Task 2 的 `ensure_terms_schema`/`list_terms`。
- Produces: `deps.get_terms` 从 `def get_terms(settings) -> list[Term]`（进程级缓存）改成
  `async def get_terms(review_conn: aiosqlite.Connection = Depends(get_review_conn)) -> list[Term]`
  （每次请求查库）。FastAPI 的 `Depends()` 对 sync/async 依赖处理方式一致，已有的 7 处路由
  `Depends(deps.get_terms)` 调用点不需要改一个字；测试里遍布的
  `app.dependency_overrides[deps.get_terms] = lambda: [...]` 同理不需要改（`dependency_overrides`
  整体替换掉原依赖，不关心原函数签名）。

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_admin_document_routes.py` 文件末尾追加（验证 `deps.get_terms` 真的从
`get_review_conn` 指向的连接查库，而不是继续读旧的 YAML+进程缓存；这是对 `deps.get_terms`
这个纯函数本身的直接测试，不需要经过任何 HTTP 路由/`TestClient`）：

```python
def test_get_terms_queries_the_review_conn_terms_table():
    """回归测试：get_terms() 迁移前是进程级 YAML 缓存，迁移后必须真的从
    传入的连接查 terms 表——这里往一个全新连接插入一条术语，直接调用
    get_terms() 本身（不经过任何路由），验证它查到了这条数据（如果
    get_terms 还在读旧缓存/YAML，这条术语不会出现）。
    """
    from app.graphrag.terms_store import create_term, ensure_terms_schema

    review_conn = asyncio.run(aiosqlite.connect(":memory:"))
    try:
        asyncio.run(ensure_terms_schema(review_conn))
        asyncio.run(
            create_term(
                review_conn, standard_name="集成测试术语", aliases=[],
                term_type="t", product_line="p",
            )
        )

        resolved_terms = asyncio.run(deps.get_terms(review_conn=review_conn))
    finally:
        asyncio.run(review_conn.close())

    assert [t.standard_name for t in resolved_terms] == ["集成测试术语"]
```

（`import aiosqlite` 已经在这个文件顶部。）

在 `tests/graphrag/test_review_cli.py` 里，把 `test_cmd_approve_writes_relation_via_graph_client`
现有的调用方式（当前直接手写 `terms=[Term(...), Term(...)]` 列表传给 `cmd_approve`）保持不变——
`cmd_approve()` 本身签名不变，仍然接收一个 `terms: list[Term]`，这个测试不需要改。真正需要新增
的是验证 `_main()` 的 `approve` 分支不再调用 `load_terms_from_settings`、而是查 `review_conn`
指向的 terms 表这件事，但 `_main()` 是 CLI 入口（解析 `sys.argv`），现有测试文件从未测试过它
本身（只测 `cmd_list`/`cmd_approve`/`cmd_reject` 这几个可独立调用的函数）——这次也不新增
`_main()` 层面的测试，遵循现有测试覆盖边界；Step 3 的实现请人工核对 `_main()` 改动后
`python -m app.graphrag.review_cli --tenant-id demo approve ...` 手动跑一次确认不报错（Step 5
的提交前跑一次即可，不需要新增自动化测试）。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_document_routes.py -k test_get_terms_queries_the_review_conn_terms_table -v`
Expected: FAIL——`deps.get_terms` 目前是 `def get_terms(settings)`，不接受 `review_conn` 关键字参数，`TypeError`。

- [ ] **Step 3: 实现**

在 `app/api/deps.py` 里，把 import 区的这一行（当前第 32 行）：

```python
from app.graphrag.factory import build_graph_client_from_settings, load_terms_from_settings
```

改成：

```python
from app.graphrag.factory import build_graph_client_from_settings
```

在 import 区加一行（放在 `from app.graphrag.review_queue import ensure_review_schema` 附近）：

```python
from app.graphrag.terms_store import ensure_terms_schema, list_terms
```

删掉全局变量声明区（当前第 71-77 行）里的这一行：

```python
_terms_cache: list[Term] | None = None
```

把 `get_terms` 函数（当前第 213-224 行）：

```python
def get_terms(settings: Settings = Depends(get_settings)) -> list[Term]:
    """进程内单例：术语表文件在服务启动期间视为不变，避免逐请求重新解析。

    这意味着编辑术语表 YAML 文件后必须重启服务才能生效——不只影响摄取
    时的自动对齐，人工审核批准时的标准名校验（见
    review_queue.py::StandardNameNotInTermsError）现在也读的是这份缓存，
    没重启的话新加的术语在审核页面里查不到、批准也会被拒。
    """
    global _terms_cache
    if _terms_cache is None:
        _terms_cache = load_terms_from_settings(settings)
    return _terms_cache
```

改成：

```python
async def get_terms(
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
) -> list[Term]:
    """每次请求都查 terms 表，不再进程级缓存——术语表现在可以通过管理
    后台在线增删改（见 app/api/admin_terms_routes.py），继续用进程级
    缓存会导致改了却要重启服务才能生效，这正是引入这份缓存之前留下的
    真实痛点。"""
    return await list_terms(review_conn)
```

把 `get_review_conn` 函数（当前第 308-321 行）：

```python
async def get_review_conn(
    settings: Settings = Depends(get_settings),
) -> aiosqlite.Connection:
    """进程内单例 SQLite 连接，模式同 get_memory_conn。"""
    global _review_conn_cache
    if _review_conn_cache is None:
        async with _review_conn_lock:
            if _review_conn_cache is None:
                db_path = Path(settings.graph_review_db_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(str(db_path))
                await ensure_review_schema(conn)
                _review_conn_cache = conn
    return _review_conn_cache
```

改成：

```python
async def get_review_conn(
    settings: Settings = Depends(get_settings),
) -> aiosqlite.Connection:
    """进程内单例 SQLite 连接，模式同 get_memory_conn。"""
    global _review_conn_cache
    if _review_conn_cache is None:
        async with _review_conn_lock:
            if _review_conn_cache is None:
                db_path = Path(settings.graph_review_db_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(str(db_path))
                await ensure_review_schema(conn)
                await ensure_terms_schema(
                    conn, seed_yaml_path=Path(settings.terminology_path)
                )
                _review_conn_cache = conn
    return _review_conn_cache
```

在 `app/graphrag/review_factory.py` 里，把整个文件内容：

```python
from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.config.settings import Settings
from app.graphrag.review_queue import ensure_review_schema


async def build_review_conn_from_settings(settings: Settings) -> aiosqlite.Connection:
    db_path = Path(settings.graph_review_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    await ensure_review_schema(conn)
    return conn
```

改成：

```python
from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.config.settings import Settings
from app.graphrag.review_queue import ensure_review_schema
from app.graphrag.terms_store import ensure_terms_schema


async def build_review_conn_from_settings(settings: Settings) -> aiosqlite.Connection:
    db_path = Path(settings.graph_review_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    await ensure_review_schema(conn)
    await ensure_terms_schema(conn, seed_yaml_path=Path(settings.terminology_path))
    return conn
```

在 `app/graphrag/review_cli.py` 里，把 import 行（当前第 11 行）：

```python
from app.graphrag.factory import build_graph_client_from_settings, load_terms_from_settings
```

改成：

```python
from app.graphrag.factory import build_graph_client_from_settings
from app.graphrag.terms_store import list_terms
```

把 `_main()` 里 `elif args.command == "approve":` 分支（当前第 118-129 行）：

```python
    elif args.command == "approve":
        graph_client = build_graph_client_from_settings(settings)
        terms = load_terms_from_settings(settings)
        await cmd_approve(
            review_conn=review_conn,
            review_id=args.review_id,
            subject_standard_name=args.subject,
            object_standard_name=args.object,
            tenant_id=args.tenant_id,
            graph_client=graph_client,
            terms=terms,
        )
```

改成：

```python
    elif args.command == "approve":
        graph_client = build_graph_client_from_settings(settings)
        terms = await list_terms(review_conn)
        await cmd_approve(
            review_conn=review_conn,
            review_id=args.review_id,
            subject_standard_name=args.subject,
            object_standard_name=args.object,
            tenant_id=args.tenant_id,
            graph_client=graph_client,
            terms=terms,
        )
```

（`review_conn` 在这个函数里已经通过 `build_review_conn_from_settings(settings)` 拿到，本步骤
改完后它已经自带 `ensure_terms_schema` 建好的 terms 表——`list_terms(review_conn)` 直接复用
同一个连接，不需要再新开一个。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_document_routes.py tests/graphrag/test_review_cli.py -v`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了已知无关的 `test_returns_none_when_tts_not_configured` 之外全部通过——这一步
专门确认全仓库里所有依赖 `deps.get_terms`/`app.dependency_overrides[deps.get_terms]` 的既有
测试都还是绿的（本任务改动理论上不会影响它们，但 `deps.get_terms` 是个被至少 7 个路由
依赖的高频函数，值得跑一次全量套件而不是只跑touched文件）。

- [ ] **Step 5: 提交**

```bash
git add app/api/deps.py app/graphrag/review_factory.py app/graphrag/review_cli.py \
  tests/api/test_admin_document_routes.py
git commit -m "refactor(graphrag): get_terms() queries the terms table instead of a process cache"
```

---

## Task 4: 术语库管理 API 路由，撤掉旧的只读接口

**Files:**
- Create: `app/api/admin_terms_routes.py`
- Modify: `app/api/admin_graph_review_routes.py`
- Modify: `app/main.py`
- Test: `tests/api/test_admin_terms_routes.py`
- Test: `tests/api/test_admin_graph_review_routes.py`

**Interfaces:**
- Consumes: Task 1 的 `count_relation_edges_for_term`/`rename_term_node`/`delete_term_node`，
  Task 2 的 `create_term`/`update_term`/`delete_term`/`list_terms`/`TermNotFoundError`/
  `TermNameConflictError`。
- Produces:
  - `GET /api/admin/terms` → `{"terms": [{"standard_name", "aliases", "term_type", "product_line"}]}`
  - `POST /api/admin/terms` → 请求体同上（不含外层 `terms` 包装，单条），成功返回该术语；
    名字冲突 400。
  - `PUT /api/admin/terms/{standard_name}` → 请求体同上；`standard_name` 路径参数是当前名字，
    请求体里的 `standard_name` 是提交的新名字（允许相同即不改名）；不存在 404，名字冲突 400；
    如果确实改了名字，会先调用 `rename_term_node` 再调用 `sync_term`。
  - `DELETE /api/admin/terms/{standard_name}` → 不存在 404（优先于下面的 409 检查）；存在但
    在图谱里有关系边则 409；否则删除 SQLite 记录和 Neo4j 节点。
  - 移除 `GET /api/admin/graph-reviews/terms`（连同 `TermResponse`/`TermListResponse` 定义
    和它专属的两个测试）。

- [ ] **Step 1: 写失败的测试**

创建 `tests/api/test_admin_terms_routes.py`：

```python
import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.graphrag.terms_store import create_term, ensure_terms_schema, list_terms
from app.main import app


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
        admin_token="tok",
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def _open_terms_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    return conn


@pytest.fixture
def terms_conn():
    """术语表库连接（复用 graph_review_db_path 的连接，测试里用独立的
    :memory: 连接，只建 terms 表——路由层依赖的是 deps.get_review_conn，
    这里的 fixture 名字叫 terms_conn 只是强调这次测试关注的是术语表这
    部分，物理上和 review_conn 是同一类连接）。必须显式 close：见
    test_admin_graph_review_routes.py 里 review_conn fixture 的同款说明。
    """
    conn = asyncio.run(_open_terms_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _authed_headers(session_store: AdminSessionStore) -> dict[str, str]:
    token = session_store.create_session()
    return {"Authorization": f"Bearer {token}"}


class SpyGraphClient:
    def __init__(self, *, edge_count: int = 0) -> None:
        self.synced: list[dict] = []
        self.renamed: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self._edge_count = edge_count

    async def sync_term(self, term) -> None:
        self.synced.append(
            {
                "standard_name": term.standard_name,
                "aliases": term.aliases,
                "term_type": term.term_type,
                "product_line": term.product_line,
            }
        )

    async def rename_term_node(self, *, old_name: str, new_name: str) -> None:
        self.renamed.append((old_name, new_name))

    async def count_relation_edges_for_term(self, standard_name: str) -> int:
        return self._edge_count

    async def delete_term_node(self, standard_name: str) -> None:
        self.deleted.append(standard_name)


def test_list_terms_returns_all_terms(terms_conn):
    asyncio.run(
        create_term(
            terms_conn, standard_name="错误码E502", aliases=["网关超时"],
            term_type="error_code", product_line="核心平台",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    try:
        client = TestClient(app)
        response = client.get("/api/admin/terms", headers=_authed_headers(session_store))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["terms"] == [
        {
            "standard_name": "错误码E502", "aliases": ["网关超时"],
            "term_type": "error_code", "product_line": "核心平台",
        }
    ]


def test_list_terms_without_session_token_returns_401(terms_conn):
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    try:
        client = TestClient(app)
        response = client.get("/api/admin/terms")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_create_term_syncs_to_graph_client(terms_conn):
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/terms",
            json={
                "standard_name": "新术语", "aliases": ["别名1"],
                "term_type": "t", "product_line": "p",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(graph_client.synced) == 1
    assert graph_client.synced[0]["standard_name"] == "新术语"


def test_create_term_with_conflicting_name_returns_400(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="已存在", aliases=[], term_type="t", product_line="p")
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/terms",
            json={"standard_name": "已存在", "aliases": [], "term_type": "t", "product_line": "p"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_update_term_without_rename_syncs_to_graph_client(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="术语A", aliases=[], term_type="t", product_line="p")
    )
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/terms/术语A",
            json={
                "standard_name": "术语A", "aliases": ["新别名"],
                "term_type": "t2", "product_line": "p2",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.renamed == []
    assert len(graph_client.synced) == 1
    assert graph_client.synced[0]["aliases"] == ["新别名"]


def test_update_term_with_rename_calls_rename_then_sync(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="旧名字", aliases=[], term_type="t", product_line="p")
    )
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/terms/旧名字",
            json={"standard_name": "新名字", "aliases": [], "term_type": "t", "product_line": "p"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.renamed == [("旧名字", "新名字")]
    assert graph_client.synced[0]["standard_name"] == "新名字"


def test_update_term_rename_into_existing_name_returns_400(terms_conn):
    asyncio.run(create_term(terms_conn, standard_name="A", aliases=[], term_type="t", product_line="p"))
    asyncio.run(create_term(terms_conn, standard_name="B", aliases=[], term_type="t", product_line="p"))
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/terms/A",
            json={"standard_name": "B", "aliases": [], "term_type": "t", "product_line": "p"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_update_nonexistent_term_returns_404(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/terms/不存在",
            json={"standard_name": "不存在", "aliases": [], "term_type": "t", "product_line": "p"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_delete_term_without_graph_edges_succeeds(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="待删除", aliases=[], term_type="t", product_line="p")
    )
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient(edge_count=0)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.delete(
            "/api/admin/terms/待删除", headers=_authed_headers(session_store)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.deleted == ["待删除"]


def test_delete_nonexistent_term_returns_404_even_when_graph_has_edges(terms_conn):
    """404 优先于 409：一个 SQLite 里根本不存在的名字，即使图谱里凑巧有
    同名的边（比如迁移前遗留的孤儿数据），也应该报"不存在"而不是"已在
    图谱中使用"——后者会误导管理员去查一个其实从未在词表里存在过的
    术语。"""
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient(edge_count=5)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.delete(
            "/api/admin/terms/不存在", headers=_authed_headers(session_store)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_delete_term_with_graph_edges_returns_409(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="使用中", aliases=[], term_type="t", product_line="p")
    )
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient(edge_count=2)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.delete(
            "/api/admin/terms/使用中", headers=_authed_headers(session_store)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert graph_client.deleted == []
    # SQLite 记录也不该被删掉——409 之后术语表和图谱两边都保持原样
    remaining = asyncio.run(list_terms(terms_conn))
    assert [t.standard_name for t in remaining] == ["使用中"]
```

在 `tests/api/test_admin_graph_review_routes.py` 里，删掉这两个测试（当前第 405-444 行，从
`def test_list_terms_returns_all_terms_from_settings():` 开始到
`assert response.status_code == 401` 结束，含中间的空行）：

```python
def test_list_terms_returns_all_terms_from_settings():
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_terms] = lambda: [
        Term(
            standard_name="示例错误码E502", aliases=["网关超时"],
            term_type="error_code", product_line="核心平台",
        ),
        Term(
            standard_name="示例登录模块", aliases=["认证模块"],
            term_type="module", product_line="核心平台",
        ),
    ]
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/graph-reviews/terms",
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["terms"] == [
        {"standard_name": "示例错误码E502", "aliases": ["网关超时"]},
        {"standard_name": "示例登录模块", "aliases": ["认证模块"]},
    ]


def test_list_terms_without_session_token_returns_401():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    try:
        client = TestClient(app)
        response = client.get("/api/admin/graph-reviews/terms")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_terms_routes.py -v`
Expected: FAIL——`app/api/admin_terms_routes.py` 还不存在，`ModuleNotFoundError`。

- [ ] **Step 3: 实现**

创建 `app/api/admin_terms_routes.py`：

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.terms_store import (
    TermNameConflictError,
    TermNotFoundError,
    create_term,
    delete_term,
    get_term,
    list_terms,
    update_term,
)

router = APIRouter(prefix="/api/admin/terms", dependencies=[Depends(deps.require_admin_session)])


class TermResponse(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str


class TermListResponse(BaseModel):
    terms: list[TermResponse]


class TermWriteRequest(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str


def _to_response(term: Term) -> TermResponse:
    return TermResponse(
        standard_name=term.standard_name,
        aliases=term.aliases,
        term_type=term.term_type,
        product_line=term.product_line,
    )


@router.get("", response_model=TermListResponse)
async def list_all_terms(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TermListResponse:
    terms = await list_terms(review_conn)
    return TermListResponse(terms=[_to_response(term) for term in terms])


@router.post("", response_model=TermResponse)
async def create_new_term(
    payload: TermWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> TermResponse:
    try:
        await create_term(
            review_conn,
            standard_name=payload.standard_name,
            aliases=payload.aliases,
            term_type=payload.term_type,
            product_line=payload.product_line,
        )
    except TermNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    term = Term(
        standard_name=payload.standard_name,
        aliases=payload.aliases,
        term_type=payload.term_type,
        product_line=payload.product_line,
    )
    # 新增成功后立即同步进图谱（属性+别名节点），不留图谱异步落后的窗口。
    await graph_client.sync_term(term)
    return _to_response(term)


@router.put("/{standard_name}", response_model=TermResponse)
async def update_existing_term(
    standard_name: str,
    payload: TermWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> TermResponse:
    try:
        await update_term(
            review_conn,
            standard_name=standard_name,
            new_standard_name=payload.standard_name,
            aliases=payload.aliases,
            term_type=payload.term_type,
            product_line=payload.product_line,
        )
    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="术语不存在")
    except TermNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if payload.standard_name != standard_name:
        # 改名：先对同一个图节点做属性级联更新（保留已有关系边），再用
        # sync_term 刷新 type/product_line/别名——顺序不能反过来，
        # sync_term 是按"当前"standard_name MERGE 匹配节点的。
        await graph_client.rename_term_node(old_name=standard_name, new_name=payload.standard_name)
    term = Term(
        standard_name=payload.standard_name,
        aliases=payload.aliases,
        term_type=payload.term_type,
        product_line=payload.product_line,
    )
    await graph_client.sync_term(term)
    return _to_response(term)


@router.delete("/{standard_name}")
async def delete_existing_term(
    standard_name: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict[str, bool]:
    # 先确认术语本身存在——404 的优先级要在 409 之前：一个根本不存在的
    # 名字不该因为图谱里凑巧有同名孤儿边就返回"已在图谱中使用"这种
    # 误导性的错误。确认存在之后再查图谱：这个术语已经被真实关系边
    # 使用的话拒绝删除，避免"词表说不存在了，但图谱边还在用它"的不
    # 一致状态——这一步必须在 delete_term() 之前，不能删完 SQLite 记录
    # 才发现图谱不允许删。
    try:
        await get_term(review_conn, standard_name)
    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="术语不存在")
    edge_count = await graph_client.count_relation_edges_for_term(standard_name)
    if edge_count > 0:
        raise HTTPException(status_code=409, detail="该术语已在图谱中使用，无法删除")
    await delete_term(review_conn, standard_name)
    await graph_client.delete_term_node(standard_name)
    return {"deleted": True}
```

在 `app/api/admin_graph_review_routes.py` 里，删掉 `TermResponse`/`TermListResponse` 类定义
（当前第 35-41 行）：

```python
class TermResponse(BaseModel):
    standard_name: str
    aliases: list[str]


class TermListResponse(BaseModel):
    terms: list[TermResponse]
```

删掉 `list_terms` 路由（当前第 87-94 行）：

```python
@router.get("/terms", response_model=TermListResponse)
async def list_terms(terms: list[Term] = Depends(deps.get_terms)) -> TermListResponse:
    return TermListResponse(
        terms=[
            TermResponse(standard_name=term.standard_name, aliases=term.aliases)
            for term in terms
        ]
    )
```

（`Term` 这个 import 保留——`approve` 路由的 `terms: list[Term] = Depends(deps.get_terms)`
参数还在用它。）

在 `app/main.py` 里，import 区加一行（放在 `from app.api.admin_graph_review_routes import router as admin_graph_review_router` 之后）：

```python
from app.api.admin_terms_routes import router as admin_terms_router
```

在路由注册区（`app.include_router(admin_graph_review_router)` 那一行之后）加一行：

```python
app.include_router(admin_terms_router)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_terms_routes.py tests/api/test_admin_graph_review_routes.py -v`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/api/admin_terms_routes.py app/api/admin_graph_review_routes.py app/main.py \
  tests/api/test_admin_terms_routes.py tests/api/test_admin_graph_review_routes.py
git commit -m "feat(api): add ontology CRUD routes, retire the read-only terms endpoint"
```

---

## Task 5: 前端术语库管理页面

**Files:**
- Modify: `frontend/src/admin/termsApi.ts`
- Create: `frontend/src/admin/TermsPage.tsx`
- Modify: `frontend/src/admin/AdminLayout.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: Task 4 的 `GET/POST/PUT/DELETE /api/admin/terms[/{standard_name}]`。

- [ ] **Step 1: 扩展 termsApi.ts**

把 `frontend/src/admin/termsApi.ts` 整个文件内容：

```tsx
import { adminFetch, extractErrorDetail } from './adminApi'

export interface GraphTerm {
  standard_name: string
  aliases: string[]
}

export async function fetchGraphTerms(sessionToken: string): Promise<GraphTerm[]> {
  const response = await adminFetch('/api/admin/graph-reviews/terms', sessionToken)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载术语表失败'))
  }
  const data = (await response.json()) as { terms: GraphTerm[] }
  return data.terms
}
```

改成：

```tsx
import { adminFetch, extractErrorDetail } from './adminApi'

export interface GraphTerm {
  standard_name: string
  aliases: string[]
}

export interface TermRecord extends GraphTerm {
  term_type: string
  product_line: string
}

export async function fetchGraphTerms(sessionToken: string): Promise<GraphTerm[]> {
  return fetchTerms(sessionToken)
}

export async function fetchTerms(sessionToken: string): Promise<TermRecord[]> {
  const response = await adminFetch('/api/admin/terms', sessionToken)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载术语表失败'))
  }
  const data = (await response.json()) as { terms: TermRecord[] }
  return data.terms
}

export async function createTerm(sessionToken: string, term: TermRecord): Promise<TermRecord> {
  const response = await adminFetch('/api/admin/terms', sessionToken, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(term),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '新增术语失败'))
  }
  return (await response.json()) as TermRecord
}

export async function updateTerm(
  sessionToken: string,
  currentStandardName: string,
  term: TermRecord,
): Promise<TermRecord> {
  const response = await adminFetch(
    `/api/admin/terms/${encodeURIComponent(currentStandardName)}`,
    sessionToken,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(term),
    },
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '更新术语失败'))
  }
  return (await response.json()) as TermRecord
}

export async function deleteTerm(sessionToken: string, standardName: string): Promise<void> {
  const response = await adminFetch(
    `/api/admin/terms/${encodeURIComponent(standardName)}`,
    sessionToken,
    { method: 'DELETE' },
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '删除术语失败'))
  }
}
```

（`fetchGraphTerms` 保留原函数名和签名不变——`GraphReviewsPage.tsx` 的 `StandardNameInput`
自动补全调用的就是这个函数，改成内部调用新的 `fetchTerms()` 即可，`GraphReviewsPage.tsx`
本身不需要改一行。`GraphTerm`/`TermRecord` 用 `extends` 是因为自动补全那边只需要
`standard_name`/`aliases` 两个字段，管理页面需要全部四个字段，结构化类型下 `TermRecord`
天然可以当 `GraphTerm` 用。）

- [ ] **Step 2: 创建 TermsPage.tsx**

```tsx
import { useCallback, useEffect, useState } from 'react'
import { useAdminAuth } from './useAdminAuth'
import { createTerm, deleteTerm, fetchTerms, updateTerm, type TermRecord } from './termsApi'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface TermDraft {
  standard_name: string
  aliases: string
  term_type: string
  product_line: string
}

function toDraft(term: TermRecord): TermDraft {
  return {
    standard_name: term.standard_name,
    aliases: term.aliases.join(', '),
    term_type: term.term_type,
    product_line: term.product_line,
  }
}

function draftToRecord(draft: TermDraft): TermRecord {
  return {
    standard_name: draft.standard_name.trim(),
    aliases: draft.aliases
      .split(',')
      .map((alias) => alias.trim())
      .filter((alias) => alias.length > 0),
    term_type: draft.term_type.trim(),
    product_line: draft.product_line.trim(),
  }
}

const emptyDraft: TermDraft = { standard_name: '', aliases: '', term_type: '', product_line: '' }

export function TermsPage() {
  const { sessionToken } = useAdminAuth()
  const [terms, setTerms] = useState<TermRecord[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [newDraft, setNewDraft] = useState<TermDraft>(emptyDraft)
  const [creating, setCreating] = useState(false)

  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState<TermDraft | null>(null)
  const [savingKey, setSavingKey] = useState<string | null>(null)
  const [deletingKey, setDeletingKey] = useState<string | null>(null)

  useEffect(() => {
    document.title = '术语库管理 · 管理后台'
  }, [])

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    try {
      const data = await fetchTerms(sessionToken)
      setTerms(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载术语表失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken])

  useEffect(() => {
    refresh().catch((err) => {
      console.error('术语表刷新失败', err)
    })
  }, [refresh])

  const handleCreate = async () => {
    if (!sessionToken || creating) return
    if (!newDraft.standard_name.trim()) return
    setError(null)
    setCreating(true)
    try {
      await createTerm(sessionToken, draftToRecord(newDraft))
      setNewDraft(emptyDraft)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '新增术语失败')
    } finally {
      setCreating(false)
    }
  }

  const handleStartEdit = (term: TermRecord) => {
    if (editingKey !== null) return
    setEditingKey(term.standard_name)
    setEditDraft(toDraft(term))
  }

  const handleCancelEdit = () => {
    setEditingKey(null)
    setEditDraft(null)
  }

  const handleSaveEdit = async (originalStandardName: string) => {
    if (!sessionToken || !editDraft || savingKey !== null) return
    if (!editDraft.standard_name.trim()) return
    setError(null)
    setSavingKey(originalStandardName)
    try {
      await updateTerm(sessionToken, originalStandardName, draftToRecord(editDraft))
      setEditingKey(null)
      setEditDraft(null)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新术语失败')
    } finally {
      setSavingKey(null)
    }
  }

  const handleDelete = async (standardName: string) => {
    if (!sessionToken || deletingKey !== null) return
    if (!window.confirm(`确定要删除术语「${standardName}」吗？此操作不可撤销。`)) return
    setError(null)
    setDeletingKey(standardName)
    try {
      await deleteTerm(sessionToken, standardName)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除术语失败')
    } finally {
      setDeletingKey(null)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">术语库管理</h1>

      <div className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal">
        <p className="text-sm font-bold text-ink">新增术语</p>
        <div className="flex flex-wrap gap-3">
          <input
            value={newDraft.standard_name}
            onChange={(event) =>
              setNewDraft((prev) => ({ ...prev, standard_name: event.target.value }))
            }
            placeholder="标准名"
            aria-label="标准名"
            className="min-w-[10rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
          />
          <input
            value={newDraft.aliases}
            onChange={(event) => setNewDraft((prev) => ({ ...prev, aliases: event.target.value }))}
            placeholder="别名（逗号分隔）"
            aria-label="别名"
            className="min-w-[10rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
          />
          <input
            value={newDraft.term_type}
            onChange={(event) =>
              setNewDraft((prev) => ({ ...prev, term_type: event.target.value }))
            }
            placeholder="类型"
            aria-label="类型"
            className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
          />
          <input
            value={newDraft.product_line}
            onChange={(event) =>
              setNewDraft((prev) => ({ ...prev, product_line: event.target.value }))
            }
            placeholder="产品线"
            aria-label="产品线"
            className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={handleCreate}
          disabled={!newDraft.standard_name.trim() || creating}
          className={`min-h-[44px] cursor-pointer self-start border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {creating ? '新增中…' : '新增术语'}
        </button>
      </div>

      {error && (
        <p
          role="alert"
          className="border-2 border-status-error bg-card px-3 py-2 text-sm text-ink shadow-brutal-sm"
        >
          {error}
        </p>
      )}

      {!loaded && <p className="text-ink-soft">加载中…</p>}
      {loaded &&
        terms.map((term) => {
          const isEditing = editingKey === term.standard_name
          return (
            <div
              key={term.standard_name}
              className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal-sm"
            >
              {!isEditing && (
                <div className="flex items-center justify-between gap-3">
                  <span className="text-ink">
                    <span className="font-bold">{term.standard_name}</span>
                    {term.aliases.length > 0 && (
                      <span className="text-ink-soft">（别名：{term.aliases.join('、')}）</span>
                    )}
                    <span className="text-ink-soft">
                      {' '}
                      · {term.term_type || '（无类型）'} · {term.product_line || '（无产品线）'}
                    </span>
                  </span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => handleStartEdit(term)}
                      disabled={editingKey !== null || deletingKey !== null}
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDelete(term.standard_name)}
                      disabled={editingKey !== null || deletingKey !== null}
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {deletingKey === term.standard_name ? '删除中…' : '删除'}
                    </button>
                  </div>
                </div>
              )}
              {isEditing && editDraft && (
                <>
                  <div className="flex flex-wrap gap-3">
                    <input
                      value={editDraft.standard_name}
                      onChange={(event) =>
                        setEditDraft((prev) =>
                          prev ? { ...prev, standard_name: event.target.value } : prev,
                        )
                      }
                      placeholder="标准名"
                      aria-label="标准名"
                      className="min-w-[10rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
                    />
                    <input
                      value={editDraft.aliases}
                      onChange={(event) =>
                        setEditDraft((prev) => (prev ? { ...prev, aliases: event.target.value } : prev))
                      }
                      placeholder="别名（逗号分隔）"
                      aria-label="别名"
                      className="min-w-[10rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
                    />
                    <input
                      value={editDraft.term_type}
                      onChange={(event) =>
                        setEditDraft((prev) => (prev ? { ...prev, term_type: event.target.value } : prev))
                      }
                      placeholder="类型"
                      aria-label="类型"
                      className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
                    />
                    <input
                      value={editDraft.product_line}
                      onChange={(event) =>
                        setEditDraft((prev) =>
                          prev ? { ...prev, product_line: event.target.value } : prev,
                        )
                      }
                      placeholder="产品线"
                      aria-label="产品线"
                      className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
                    />
                  </div>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => handleSaveEdit(term.standard_name)}
                      disabled={!editDraft.standard_name.trim() || savingKey !== null}
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {savingKey === term.standard_name ? '保存中…' : '保存'}
                    </button>
                    <button
                      type="button"
                      onClick={handleCancelEdit}
                      disabled={savingKey !== null}
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      取消
                    </button>
                  </div>
                </>
              )}
            </div>
          )
        })}
      {loaded && terms.length === 0 && (
        <p className="text-ink-soft">还没有任何术语，用上面的表单新增一个。</p>
      )}
    </div>
  )
}
```

- [ ] **Step 3: 接入导航和路由**

在 `frontend/src/admin/AdminLayout.tsx` 里，把导航区（当前第 32-37 行）：

```tsx
            <NavLink to="/admin/documents" className={navLinkClass}>
              文档管理
            </NavLink>
            <NavLink to="/admin/graph-reviews" className={navLinkClass}>
              知识图谱审核
            </NavLink>
```

改成：

```tsx
            <NavLink to="/admin/documents" className={navLinkClass}>
              文档管理
            </NavLink>
            <NavLink to="/admin/graph-reviews" className={navLinkClass}>
              知识图谱审核
            </NavLink>
            <NavLink to="/admin/terms" className={navLinkClass}>
              术语库管理
            </NavLink>
```

在 `frontend/src/App.tsx` 里，把 import 区（当前第 1-6 行）：

```tsx
import { Navigate, Route, Routes } from 'react-router-dom'
import { ChatPage } from './pages/ChatPage'
import { AdminLayout } from './admin/AdminLayout'
import { LoginPage } from './admin/LoginPage'
import { DocumentsPage } from './admin/DocumentsPage'
import { GraphReviewsPage } from './admin/GraphReviewsPage'
```

改成：

```tsx
import { Navigate, Route, Routes } from 'react-router-dom'
import { ChatPage } from './pages/ChatPage'
import { AdminLayout } from './admin/AdminLayout'
import { LoginPage } from './admin/LoginPage'
import { DocumentsPage } from './admin/DocumentsPage'
import { GraphReviewsPage } from './admin/GraphReviewsPage'
import { TermsPage } from './admin/TermsPage'
```

把路由定义区（当前第 13-17 行）：

```tsx
      <Route path="/admin" element={<AdminLayout />}>
        <Route index element={<Navigate to="documents" replace />} />
        <Route path="documents" element={<DocumentsPage />} />
        <Route path="graph-reviews" element={<GraphReviewsPage />} />
      </Route>
```

改成：

```tsx
      <Route path="/admin" element={<AdminLayout />}>
        <Route index element={<Navigate to="documents" replace />} />
        <Route path="documents" element={<DocumentsPage />} />
        <Route path="graph-reviews" element={<GraphReviewsPage />} />
        <Route path="terms" element={<TermsPage />} />
      </Route>
```

- [ ] **Step 4: 类型检查 + 构建**

Run（在 `frontend/` 目录下）:
```bash
npm run typecheck
npm run build
```
Expected: 两条命令都无错误退出。

- [ ] **Step 5: 手动验证**

1. 确认后端已经跑了 Task 1-4。
2. 登录管理后台，确认侧边栏出现"术语库管理"入口，点进去。
3. 用新增表单加一个术语（标准名+别名+类型+产品线），确认列表刷新后出现这条记录。
4. 点"编辑"，改一下别名和产品线（不改标准名），保存，确认列表更新、图谱同步没有报错。
5. 再编辑一次，这次改标准名（改名），保存，确认列表里名字变了、旧名字消失。
6. 尝试把一个术语改名成另一个已存在术语的名字，确认收到明确的冲突错误提示。
7. 删除一个从没在知识图谱审核里被批准过的术语，确认删除成功。
8. 找一个已经通过知识图谱审核批准过（真的写进 Neo4j 图谱）的术语，尝试删除，确认收到
   "该术语已在图谱中使用，无法删除"的提示，删除被拒绝。
9. 回到"知识图谱审核"页面，确认标准名自动补全依然正常工作（验证撤掉旧接口、改用新接口
   之后没有破坏原有功能）。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/termsApi.ts frontend/src/admin/TermsPage.tsx \
  frontend/src/admin/AdminLayout.tsx frontend/src/App.tsx
git commit -m "feat(admin): add ontology (terms) management page with full CRUD"
```

---

## 全部任务完成后

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了已知无关的 `test_returns_none_when_tts_not_configured` 之外全部通过。

Run（`frontend/` 目录下）: `npm run typecheck && npm run build`
Expected: 均无错误退出。
