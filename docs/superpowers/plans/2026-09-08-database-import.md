# 数据库导入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从关系型数据库直接拉数据进图谱，连接与列映射存下来可重跑，**库里零密码**。

**Architecture:** 拉到行之后整条管线跟「表格导入」完全共用——本计划新增的只有一个连接器和一张 `db_sources` 表。密码不入库（spec D3），点「重新同步」时弹密码框。这是唯一引入新外部依赖（数据库驱动）的一个阶段，所以排在最后。

**Tech Stack:** FastAPI · aiosqlite · SQLAlchemy Core（新依赖）· React 18 + TypeScript · vitest

**Spec:** `docs/superpowers/specs/2026-09-08-interaction-redesign-design.md`

**Depends on:** `docs/superpowers/plans/2026-09-08-nav-restructure-and-dashboard.md`（页面落在「数据导入」组，占位页在那里建好了）

## Global Constraints

同 `2026-09-08-multi-persona-foundation.md` 的十二条，逐字适用。额外三条：

13. **密码绝不落库、绝不进日志、绝不进错误消息。** 连接失败时报的是「连不上 10.0.0.5:3306」，不是把 DSN 原样吐出来。
14. **只读连接。** 生成的 SQL 只允许 SELECT。这个功能没有任何理由写客户的生产库。
15. **不修改 `.env`。** 新依赖加进 `pyproject.toml` 的依赖列表（已核对，这个项目没有 requirements.txt），不碰配置。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `app/ingestion/db_connector.py`（新） | 连通性测试、预览取行、全量取行。唯一碰数据库驱动的地方 |
| `app/graphrag/db_sources_store.py`（新） | `db_sources` 表 DDL 与读写（**无密码列**） |
| `app/api/admin_db_import_routes.py`（新） | 测连通、预览、保存数据源、重新同步 |
| `frontend/src/admin/DatabaseImportPage.tsx`（新） | 数据源列表 + 新建向导 |
| `frontend/src/admin/dbImport/PasswordPrompt.tsx`（新） | 重新同步时的密码框 |

---

## Task 1: 数据库连接器

**Files:**
- Create: `app/ingestion/db_connector.py`
- Modify: `pyproject.toml`（加 `sqlalchemy`，以及一到两个驱动）
- Test: `tests/ingestion/test_db_connector.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class DbConnectionSpec`：`driver: str` · `host: str` · `port: int` · `database: str` · `username: str`（**没有 password 字段**——它作为独立参数传，永远不进这个可序列化的对象）
  - `class UnsupportedDriverError(Exception)`
  - `class UnsafeQueryError(Exception)`
  - `async def test_connection(spec: DbConnectionSpec, password: str) -> None`——连不通抛异常
  - `async def fetch_rows(spec: DbConnectionSpec, password: str, query: str, *, limit: int | None = None) -> tuple[list[str], list[tuple[Any, ...]]]`——返回 `(列名, 行)`

**支持的 driver**：`mysql` · `postgresql`。写死白名单，不接受任意 driver 字符串
——那等于让调用方控制 SQLAlchemy 的 URL scheme。

- [ ] **Step 1: 写失败测试**

用 SQLite 做被测数据库（`driver="sqlite"` 只在测试里允许，通过一个显式的
`_TEST_DRIVERS` 常量开口），这样不需要起 MySQL 容器。

```python
def test_an_unsupported_driver_is_refused_before_any_connection_attempt():
    """白名单之外的 driver 立刻拒绝。放行的话，driver 字符串就成了
    SQLAlchemy URL scheme 的注入点。"""
    with pytest.raises(UnsupportedDriverError):
        asyncio.run(test_connection(_spec(driver="'; DROP TABLE"), "pw"))


def test_a_non_select_query_is_refused():
    """只读。这个功能没有任何理由写客户的生产库。

    批次里同时有 SELECT 和非 SELECT——全是非 SELECT 的话，
    「一律拒绝」的实现也能变绿。
    """
    for bad in ["DELETE FROM users", "UPDATE t SET a=1", "DROP TABLE t"]:
        with pytest.raises(UnsafeQueryError):
            asyncio.run(fetch_rows(_spec(), "pw", bad))
    # 这一条必须通过
    asyncio.run(fetch_rows(_spec(), "pw", "SELECT * FROM goods"))


def test_a_select_with_a_trailing_write_is_refused():
    """「SELECT 1; DROP TABLE t」不能因为开头是 SELECT 就放行。
    只看首个单词的实现在这条上必红。"""
    with pytest.raises(UnsafeQueryError):
        asyncio.run(fetch_rows(_spec(), "pw", "SELECT 1; DROP TABLE t"))


def test_fetch_rows_returns_column_names_alongside_the_rows():
    """列名是列映射那一步的输入。只返回行的话，用户得自己数第几列是什么。"""
    columns, rows = asyncio.run(fetch_rows(_spec(), "pw", "SELECT id, name FROM goods"))
    assert columns == ["id", "name"]


def test_limit_caps_the_preview():
    """预览只取前 N 行。不限制的话，「预览一下」会把两千万行拉进内存。"""
    _, rows = asyncio.run(fetch_rows(_spec(), "pw", "SELECT * FROM goods", limit=2))
    assert len(rows) == 2


def test_a_connection_failure_message_does_not_contain_the_password():
    """密码绝不进错误消息。错误消息会进日志、会显示给用户、会被截图。

    这条用例是本计划最重要的一条。
    """
    try:
        asyncio.run(test_connection(_spec(host="10.0.0.254"), "hunter2"))
    except Exception as exc:
        assert "hunter2" not in str(exc)
        assert "hunter2" not in repr(exc)
    else:
        pytest.fail("应该连不上")


def test_the_spec_object_cannot_carry_a_password():
    """DbConnectionSpec 是要被序列化进库的那个对象。它有 password 字段的话，
    早晚有人把整个对象 json.dumps 进 db_sources。"""
    assert not hasattr(DbConnectionSpec("mysql", "h", 3306, "d", "u"), "password")
```

- [ ] **Step 2: 跑测试确认它红**

- [ ] **Step 3: 加依赖**

依赖文件是 `pyproject.toml`（已核对，这个项目没有 `requirements.txt`）。
照抄它现有依赖的写法加三条：`sqlalchemy>=2.0`、`pymysql`、`psycopg[binary]`。

`psycopg[binary]` 是 v3，需要 Python ≥ 3.7——本项目跑在 3.12（`.venv` 里是
`Python312`），没有问题。装 v2（`psycopg2-binary`）也可以，但 SQLAlchemy 2.0 的
文档默认用 v3，跟着走省事。

装完确认：
```bash
.venv/Scripts/python.exe -c "import sqlalchemy, pymysql, psycopg; print(sqlalchemy.__version__)"
```

**这是六个阶段里唯一引入新外部依赖的一步。** 装之前先确认这三个包在客户的部署
环境里能装上——离线环境下这一整个阶段可能得改成「导出 CSV 走表格导入」。

- [ ] **Step 4: 写实现**

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: 支持的数据库。写死白名单而不是接受任意字符串：driver 会拼进 SQLAlchemy
#: 的 URL scheme，让调用方控制它等于开一个注入点。
_DRIVER_URLS: dict[str, str] = {
    "mysql": "mysql+pymysql",
    "postgresql": "postgresql+psycopg",
}

#: 只读判据。这个功能没有任何理由写客户的生产库。
#:
#: 判据是「去掉注释和空白之后以 SELECT 或 WITH 开头，且不含分号后的第二条
#: 语句」。只看首个单词不够——「SELECT 1; DROP TABLE t」的首个单词也是 SELECT。
_SELECT_PATTERN = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


@dataclass(frozen=True)
class DbConnectionSpec:
    """一个数据源的连接信息。

    **没有 password 字段**，这是刻意的：这个对象会被序列化进 db_sources 表
    （spec D3 定的是「存连接不存密码」）。留一个 password 字段的话，早晚有人
    把整个对象 json.dumps 进库——而那时没有任何东西会报错。

    密码作为独立参数传给每个需要它的函数，永远不进这个对象。
    """

    driver: str
    host: str
    port: int
    database: str
    username: str
```

`_assert_safe_query` / `_build_url` / `test_connection` / `fetch_rows` 照上面的
用例实现。**`_build_url` 里的密码要 URL-encode**，且构造出的 URL 绝不进日志。

- [ ] **Step 5: 跑测试确认它绿 + 变异**

```bash
# 变异 A：driver 白名单去掉，直接拼进 URL
#   预期红：test_an_unsupported_driver_is_refused_before_any_connection_attempt
# 变异 B：只读判据改成只看首个单词
#   预期红：test_a_select_with_a_trailing_write_is_refused
# 变异 C：错误消息里带上完整 URL
#   预期红：test_a_connection_failure_message_does_not_contain_the_password
# 变异 D：给 DbConnectionSpec 加一个 password 字段
#   预期红：test_the_spec_object_cannot_carry_a_password
# 变异 E：limit 参数被忽略
#   预期红：test_limit_caps_the_preview
```

- [ ] **Step 6: 提交**

```bash
git add app/ingestion/db_connector.py tests/ingestion/test_db_connector.py pyproject.toml
git commit -m "feat(import): 数据库连接器，只读、driver 白名单、密码不进任何可序列化对象"
```

---

## Task 2: `db_sources` 表

**Files:**
- Create: `app/graphrag/db_sources_store.py`
- Modify: `app/main.py`
- Test: `tests/graphrag/test_db_sources_store.py`

**Interfaces:**
- Produces:
  - `async def ensure_db_sources_schema(conn) -> None`
  - `async def create_db_source(conn, *, tenant_id, source_id, name, spec: DbConnectionSpec, query: str, mapping: dict[str, Any]) -> None`
  - `async def list_db_sources(conn, *, tenant_id) -> list[dict[str, Any]]`
  - `async def get_db_source(conn, *, tenant_id, source_id) -> dict[str, Any] | None`
  - `async def delete_db_source(conn, *, tenant_id, source_id) -> None`
  - `async def touch_last_sync(conn, *, tenant_id, source_id, row_count: int) -> None`

DDL（**注意没有任何密码列**）：

```sql
CREATE TABLE IF NOT EXISTS db_sources (
    tenant_id      TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    driver         TEXT NOT NULL,
    host           TEXT NOT NULL,
    port           INTEGER NOT NULL,
    database       TEXT NOT NULL,
    username       TEXT NOT NULL,
    query          TEXT NOT NULL,
    mapping        TEXT NOT NULL,
    last_sync_at   TEXT,
    last_sync_rows INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, source_id)
);
```

- [ ] **Step 1: 写失败测试**

```python
def test_the_table_has_no_password_column():
    """本计划的第二条核心断言。将来有人加一列存密码时，这条会红。

    PRAGMA table_info 里出现任何带 password / passwd / secret / credential
    的列名都算违约。
    """

    async def run():
        conn = await _conn()
        try:
            cursor = await conn.execute("PRAGMA table_info(db_sources)")
            names = [row[1].lower() for row in await cursor.fetchall()]
            for banned in ("password", "passwd", "secret", "credential", "dsn"):
                assert not any(banned in n for n in names), f"db_sources 出现了 {banned} 列"
        finally:
            await conn.close()

    asyncio.run(run())


def test_create_and_read_back_a_source():
    """连接信息、SQL、列映射都要能原样读回来——它们是重跑时最贵的部分。"""


def test_sources_are_scoped_to_the_tenant():
    """两个租户各建一个同 source_id 的，各自只看到自己的。
    复合主键让同名并存是合法的。"""


def test_mapping_round_trips_as_a_dict():
    """mapping 存 JSON 读回来是 dict。存成 str 让调用方自己解析的话，
    解析代码会在三个地方各写一遍。"""


def test_touch_last_sync_records_when_and_how_many():
    """「上次同步 2 小时前，导入 4,712 行」——两个信息缺一不可。
    只有时间的话，用户不知道那次同步是成功导了数据还是导了个空。"""


def test_delete_only_removes_that_one_source():
    """两个数据源，删一个，另一个还在。"""
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

- [ ] **Step 5: 变异 + 提交**

```bash
# 变异 A：给表加一个 password 列
#   预期红：test_the_table_has_no_password_column
# 变异 B：list 去掉 tenant_id 过滤
#   预期红：test_sources_are_scoped_to_the_tenant
# 变异 C：mapping 读回来仍是字符串
#   预期红：test_mapping_round_trips_as_a_dict
```

```bash
git add app/graphrag/db_sources_store.py tests/graphrag/test_db_sources_store.py app/main.py
git commit -m "feat(import): 数据源表，一条用例钉死它永远不许有密码列"
```

---

## Task 3: 数据库导入 API

**Files:**
- Create: `app/api/admin_db_import_routes.py`
- Modify: `app/main.py`（挂载）
- Test: `tests/api/test_admin_db_import_routes.py`

**Interfaces:**
- Consumes: Task 1 的连接器、Task 2 的 store、既有的 ETL 映射管线
  （`app/graphrag/schema_etl.py`）
- Produces:
  - `POST /api/admin/{tenant_id}/db-import/test-connection` `{driver, host, port, database, username, password}` → 200 / 400
  - `POST /api/admin/{tenant_id}/db-import/preview` `{...连接信息, password, query}` → `{columns: [str], rows: [[...]], row_count: int}`（前 100 行）
  - `POST /api/admin/{tenant_id}/db-import/sources` `{name, ...连接信息, query, mapping}` → 建数据源（**请求体里没有 password**）
  - `GET /api/admin/{tenant_id}/db-import/sources` → 列表
  - `POST /api/admin/{tenant_id}/db-import/sources/{source_id}/sync` `{password}` → 跑一次同步
  - `DELETE /api/admin/{tenant_id}/db-import/sources/{source_id}`

- [ ] **Step 1: 写失败测试**

```python
def test_creating_a_source_rejects_a_request_body_that_carries_a_password(db_conn):
    """建数据源的请求体里出现 password 字段要报 400，不是静默忽略。

    静默忽略的话，前端某次改动不小心把密码发上来，我们会一直不知道——
    而它已经进了访问日志。
    """
    assert _create_source(db_conn, extra={"password": "hunter2"}).status_code == 400


def test_sync_requires_a_password_in_the_request(db_conn):
    """密码不在库里，所以每次同步必须现给。缺了要 400 而不是拿空密码去连
    ——空密码在某些配置下真的能连上，那会连到一个错误的账号下。"""
    assert _sync(db_conn, password=None).status_code == 400


def test_preview_returns_columns_and_caps_the_rows(db_conn):
    """预览最多 100 行。不限制的话，「预览一下」会把两千万行拉进内存
    然后序列化成 JSON。"""


def test_a_failed_connection_returns_400_with_a_message_that_has_no_password(db_conn):
    """错误消息进响应体、进日志、被截图。密码不能在里面。"""
    body = _test_connection(db_conn, password="hunter2", host="10.0.0.254")
    assert body.status_code == 400
    assert "hunter2" not in body.text


def test_sync_reuses_the_stored_query_and_mapping(db_conn):
    """同步用的是存下来的 SQL 和映射，不是请求里现给的。

    允许请求覆盖的话，「重新同步」这个动作的语义就不是「再跑一次同样的」了，
    而用户点它的时候以为是。
    """


def test_sync_records_when_and_how_many(db_conn):
    """同步完 last_sync_at 和 last_sync_rows 都要更新。"""


def test_a_failed_sync_does_not_update_last_sync_at(db_conn):
    """失败的同步不该刷新时间戳。刷了的话，列表上显示「上次同步 2 分钟前」
    而实际上那次同步一行都没导进去。"""


def test_every_endpoint_is_tenant_scoped(db_conn):
    """六个端点逐个断言 403。"""


def test_sync_goes_through_the_same_mapping_pipeline_as_sheet_import(db_conn):
    """拉到行之后走的是跟表格导入同一条管线——同一份校验、同一份报告、
    同一套跳过行记录。另写一条的话，同样的坏数据在两个入口下会有两种表现。
    """
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

实现要点：`SourceCreateRequest` 用 `model_config = ConfigDict(extra="forbid")`
——多余字段（含 `password`）直接 422/400，不是静默忽略。

同步路径：`fetch_rows` 拿到 `(columns, rows)` → 转成跟表格导入一样的行序列 →
调既有的 `run_schema_etl`（或它内部那层）。**不要复制一份映射逻辑**。

- [ ] **Step 5: 变异**

```bash
# 变异 A：extra="forbid" 去掉
#   预期红：test_creating_a_source_rejects_a_request_body_that_carries_a_password
# 变异 B：password 缺失时用空串继续
#   预期红：test_sync_requires_a_password_in_the_request
# 变异 C：同步失败时也更新 last_sync_at
#   预期红：test_a_failed_sync_does_not_update_last_sync_at
# 变异 D：同步接受请求里的 query 覆盖存下来的
#   预期红：test_sync_reuses_the_stored_query_and_mapping
# 变异 E：异常消息里带上 DSN
#   预期红：test_a_failed_connection_returns_400_with_a_message_that_has_no_password
```

- [ ] **Step 6: 重启后端 + 提交**

```bash
git add app/api/admin_db_import_routes.py tests/api/test_admin_db_import_routes.py app/main.py
git commit -m "feat(api): 数据库导入端点，建数据源时请求体带密码直接报错"
```

---

## Task 4: 数据库导入页

**Files:**
- Create: `frontend/src/admin/DatabaseImportPage.tsx`
- Create: `frontend/src/admin/dbImport/PasswordPrompt.tsx`
- Modify: `frontend/src/App.tsx`（换掉阶段三的占位页）
- Test: `frontend/src/admin/databaseImport.test.tsx`

**Interfaces:**
- Consumes: Task 3 的六个端点
- Produces: 数据源列表 + 新建向导（四步：填连接 → 测连通 → 写 SQL 并预览 → 列映射）

- [ ] **Step 1: 写失败测试**

```tsx
it('测连通之前不让进下一步', async () => {
  // 没测就往下走的话，用户会在写完 SQL、配完映射之后才发现连不上——
  // 前面两步全白做。
})

it('连不通时把后端那句话原样显示', async () => {
  // 「连不上 10.0.0.5:3306」比「连接失败」有用得多。
})

it('预览把列名和前几行都摆出来', async () => {
  // 列映射那一步要照着列名配。只有行数的话，用户得自己去数据库里查列名。
})

it('保存数据源时请求体里没有 password', async () => {
  // 这是本计划面向用户的核心承诺在前端的那一半。后端有 extra="forbid"
  // 兜底，但前端本来就不该发。
  await saveSource()
  const body = JSON.parse(String(saveRequests[0].body))
  expect(body).not.toHaveProperty('password')
})

it('点重新同步会弹密码框，输完才发请求', async () => {
  // 密码不在库里（spec D3）。不弹框直接发的话，同步会带着空密码去连。
})

it('密码框取消时不发同步请求', async () => {})

it('密码框里的值不进任何 URL', async () => {
  // URL 会进浏览器历史、进服务端访问日志、进 Referer 头。
  expect(syncRequests.every((r) => !r.url.includes('hunter2'))).toBe(true)
})

it('列表上显示上次同步时间和行数', async () => {
  // 「上次同步 2 小时前 · 4,712 行」——只有时间的话，用户不知道那次
  // 同步是成功导了数据还是导了个空。
})

it('同步失败时列表上那一行标出来并说原因', async () => {})
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

`PasswordPrompt` 是一个受控的模态框：`type="password"`、
`autoComplete="current-password"`、提交后**立刻从 state 里清掉**。

**不要**用 `localStorage` 或 `sessionStorage` 记住密码——那等于把「库里零密码」
这个承诺在浏览器一侧作废。

- [ ] **Step 5: 变异**

```bash
# 变异 A：保存时把 password 一起发出去
#   预期红：test「保存数据源时请求体里没有 password」
# 变异 B：同步不弹密码框，直接发空密码
#   预期红：test「点重新同步会弹密码框」
# 变异 C：密码通过查询参数传
#   预期红：test「密码框里的值不进任何 URL」
# 变异 D：测连通那一步可跳过
#   预期红：test「测连通之前不让进下一步」
```

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/DatabaseImportPage.tsx frontend/src/admin/dbImport/PasswordPrompt.tsx frontend/src/admin/databaseImport.test.tsx frontend/src/App.tsx
git commit -m "feat(admin): 数据库导入页，重新同步时现填密码不留存"
```

---

## Task 5: 收尾验证

- [ ] **Step 1: 后端全量 + 前端全量 + 类型检查**

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider > "$TEMP/full.txt" 2>&1
tail -5 "$TEMP/full.txt"
```
```
cd frontend && npx vitest run src && npx tsc --noEmit
```

- [ ] **Step 2: 起一个本地数据库做端到端验证**

用 docker 起一个 MySQL 或 PostgreSQL，造一张十几行的商品表。
**不要连客户的真实库**。

- [ ] **Step 3: 手工走一遍**

九项：

1. 新建数据源 → 填错密码 → 报错，且**错误消息里没有密码**
2. 填对密码 → 测连通成功
3. 写 `SELECT * FROM goods` → 预览出列名和前几行
4. 配列映射 → 保存
5. 打开浏览器开发者工具的网络面板，**确认保存那个请求的 body 里没有 password**
6. 实体明细里能看到导进来的数据
7. 在数据库里改一行，点「重新同步」→ 弹密码框，输完同步成功，数据更新了
8. 密码框点取消 → 不发请求
9. **查一遍库**：`sqlite3 <review.db> "SELECT * FROM db_sources"`，确认没有任何字段像密码

第 1、5、9 项是本计划面向客户的全部承诺，**不能跳过**。

- [ ] **Step 4: 把已知限制写进页面**

在数据源列表页顶部加一行说明：

> 同步是手动的。密码不保存在系统里，所以每次同步需要重新输入——
> 这也意味着暂时不支持定时自动同步。

**必须写出来**。用户会问「为什么不能定时同步」，而这个限制是设计选择不是遗漏，
藏着不说会让人以为是 bug。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/admin/DatabaseImportPage.tsx
git commit -m "docs(import): 页面上写明手动同步与不存密码的取舍"
```

---

## Self-Review

**1. Spec coverage**

| Spec 要求 | 落在哪 |
|---|---|
| D3 存连接、能手动重跑 | Task 2 + Task 3 |
| D3 密码不入库 | Task 1（spec 对象无字段）+ Task 2（表无列）+ Task 3（请求体 forbid）+ Task 4（前端不发） |
| D3 拉到行之后共用表格导入管线 | Task 3 Step 4 + `test_sync_goes_through_the_same_mapping_pipeline_as_sheet_import` |
| §8 已知限制：做不了定时同步 | Task 5 Step 4（写进页面） |
| 「三个入口」里的数据库导入 | Task 4 |

密码不落库这一条**在四个层面各有一条用例钉住**：数据类没有字段、表没有列、
请求体拒绝、前端不发。任何一层被绕过都会有用例变红。

**2. Placeholder scan**：Task 3 / Task 4 的用例是骨架 + 理由，按仓库既有夹具补全。
Task 5 Step 2 的「起一个本地数据库」是手工验证步骤，不是代码占位。

**3. Type consistency**

- `DbConnectionSpec` 五个字段：Task 1 定义，Task 2 的 DDL 逐字对应，Task 3 消费。
- `fetch_rows(...) -> tuple[list[str], list[tuple[Any, ...]]]`：Task 1 定义，Task 3 消费。
- `create_db_source(...)` 的关键字参数：Task 2 定义，Task 3 消费。
- 六个端点路径：Task 3 定义，Task 4 消费。

---

## 已知的执行前不确定项

只剩两条，且都需要读一遍源码再定，不是简单查找：

1. **既有 ETL 管线的入口函数名与签名**（Task 3 Step 4）：读 `app/graphrag/schema_etl.py`，
   找到表格导入实际调用的那一层，**照抄**——不要复制一份映射逻辑。
2. **`mapping` 的实际形状**（Task 2）：读既有的 `ontology_etl_mapping` 表和
   `admin_schema_etl_routes.py`，用同一个形状。两种形状的话，
   数据库导入配好的映射在表格导入那边读不了。

依赖文件（`pyproject.toml`）和 psycopg 版本（v3，Python 3.12 支持）已核对，
结论写进了 Task 1 Step 3。
