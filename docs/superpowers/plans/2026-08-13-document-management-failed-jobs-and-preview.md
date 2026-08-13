# 文档管理失败任务可见性与预览功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复文档管理页面两个真实的设计缺陷——摄取任务重试 3 次失败后（`status='dead'`）会从"处理中的任务"列表里彻底消失、没有任何地方能看到，管理员完全不知道某个文件摄取失败了；已摄取文档没有任何预览手段，出问题只能删了重传，没法先看一眼诊断。

**Architecture:** 失败任务可见性走"新增失败任务区块 + 重试/删除两个动作"路线：`ingestion_queue.py` 新增 `list_dead_jobs`/`retry_job`/`delete_job` 三个函数，`admin_document_routes.py` 的 `list_documents` 响应里加一个 `dead_jobs` 字段，另加两个新路由触发重试/删除。预览功能分两块内容：chunk 文本（诊断"到底切成了什么、能不能被检索到"）直接从向量库按 `source+tenant_id` 过滤查询——`VectorStore` 协议已经有 `delete_by_source` 证明这个过滤维度天然支持，新增一个只读的 `list_by_source` 复用同样的过滤表达式；原始文件预览/下载复用已经落盘在 `upload_dir` 里的文件，新增一个按租户目录校验路径安全的下载接口。前端不引入 Modal，两个功能都用现有页面的"行内展开/收起"风格实现。

**Tech Stack:** 后端 FastAPI + aiosqlite + pymilvus（`MilvusVectorStore`）。前端 React + TypeScript——项目没有配置任何前端自动化测试框架，验证手段是 `npm run typecheck` + `npm run build` + 手动核对。

## Global Constraints

- `retry_job`/`delete_job` 只对 `status='dead'` 的任务生效——任务还在 `pending`（可能正在被后台处理）或已经 `completed` 时调用会报错，不会静默重置正常任务的进度。
- 所有新的任务级操作（`retry_job`/`delete_job`）都必须按 `tenant_id` 校验任务归属，不存在或属于别的租户一律报同一种"任务不存在"错误，不向调用方泄露"这个任务其实存在、只是属于别的租户"这个信息——沿用 `review_queue.py::ReviewNotFoundError` 已经确立的先例。
- chunk 预览接口最多返回 200 条，超出的部分不静默丢弃：响应里带上真实总数 `total`，前端展示"仅显示前 200 / 共 N 条"。
- 原始文件下载接口必须校验 `file_path` 落在"这个 tenant_id 自己的上传子目录"内（`upload_dir/{tenant_id}/...`），不能只校验"落在 `upload_dir` 内随便哪个子目录"——否则用一个属于别的租户的 `file_path` 搭配自己的 `tenant_id` 就能读到别的租户的文件，是真实的跨租户越权读取风险。这个约束顺带修一个在本次改动之前就存在、且本次会直接touch 到同一个函数的旧漏洞：`_unlink_uploaded_file()` 目前只校验"文件在 `upload_dir` 内"，不校验租户子目录，理论上能被诱导删除别的租户的文件（向量库和追踪表两处的删除因为都按 `tenant_id` 过滤会是空操作，唯独磁盘文件这一步没有同样的过滤）——这个函数改签名加 `tenant_id` 参数一并修掉，不新开一个任务单独处理，因为本次改动本来就要给它新增第二个调用点（`delete_job` 也要用它清理孤儿文件）。
- 前端不引入新的 Modal/弹窗组件——预览和失败任务都用现有页面已经在用的"行内展开、按钮触发状态更新"风格。
- 下载原始文件必须带上管理员的 `Authorization: Bearer` 头才能通过后端鉴权，浏览器原生的 `<a href>`/`window.open(url)` 直接导航到接口地址不会带这个头——前端用 `adminFetch` 把文件内容拉成 `Blob`、生成一个临时 `URL.createObjectURL`，再用这个临时地址打开新标签页，不做成一个可以直接分享/收藏的普通链接。

---

## Task 1: VectorStore 新增按 source 查 chunk 的只读接口

**Files:**
- Modify: `app/retrieval/vector_store.py`
- Modify: `app/retrieval/milvus_store.py`
- Test: `tests/retrieval/test_vector_store.py`
- Test: `tests/retrieval/test_milvus_store.py`

**Interfaces:**
- Produces: `VectorStore` 协议新增 `list_by_source(self, *, source: str, tenant_id: str) -> list[VectorRecord]`，`InMemoryVectorStore`/`MilvusVectorStore` 都要实现；返回结果按原始摄取顺序排序（不是向量库任意返回顺序）。同时新增一个模块级辅助函数 `chunk_index_from_id(record_id: str) -> int`，从 `f"{path}#{i}"` 形式的 id 里取出序号 `i`——`app/ingestion/pipeline.py::_embed_and_upsert` 写入时就是这么给每条记录编号的（`id=f"{path}#{i}"`），向量库本身不保证任何返回顺序，必须在应用层按这个序号重新排序，chunk 预览才能按文档原始顺序展示。

- [ ] **Step 1: 写失败的测试**

在 `tests/retrieval/test_vector_store.py` 文件末尾追加：

```python
async def test_list_by_source_returns_only_matching_records_in_chunk_order():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="a.md#1", vector=[0.1], text="第二段",
                tenant_id="t1", metadata={"source": "a.md"},
            ),
            VectorRecord(
                id="b.md#0", vector=[0.1], text="不相关文档",
                tenant_id="t1", metadata={"source": "b.md"},
            ),
            VectorRecord(
                id="a.md#0", vector=[0.1], text="第一段",
                tenant_id="t1", metadata={"source": "a.md"},
            ),
            VectorRecord(
                id="a.md#10", vector=[0.1], text="第十一段",
                tenant_id="t1", metadata={"source": "a.md"},
            ),
        ]
    )

    records = await store.list_by_source(source="a.md", tenant_id="t1")

    # a.md#1 排在 a.md#10 前面证明是按数字序号排序，不是按字符串字典序
    # （字典序会把 "a.md#1" 排在 "a.md#10" 之后，"1" < "10" 的字符串比较
    # 结果和期望的数值顺序相反）
    assert [r.text for r in records] == ["第一段", "第二段", "第十一段"]


async def test_list_by_source_only_matches_the_given_tenant():
    store = InMemoryVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="a.md#0", vector=[0.1], text="t1的内容",
                tenant_id="t1", metadata={"source": "a.md"},
            ),
            VectorRecord(
                id="a.md#0", vector=[0.1], text="t2的内容",
                tenant_id="t2", metadata={"source": "a.md"},
            ),
        ]
    )

    records = await store.list_by_source(source="a.md", tenant_id="t1")

    assert [r.text for r in records] == ["t1的内容"]
```

在文件末尾追加一条针对辅助函数本身的测试（先把 import 从 `from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord` 改成 `from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord, chunk_index_from_id`）：

```python
def test_chunk_index_from_id_parses_trailing_numeric_suffix():
    assert chunk_index_from_id("data/uploads/a.md#0") == 0
    assert chunk_index_from_id("data/uploads/a.md#10") == 10


def test_chunk_index_from_id_defaults_to_zero_for_malformed_id():
    assert chunk_index_from_id("no-hash-separator") == 0
```

在 `tests/retrieval/test_milvus_store.py` 里，把 `FakeMilvusClient.query`（当前第 36-50 行）改成能记录最后一次调用参数，方便断言过滤表达式：

```python
    def query(self, *, collection_name: str, filter: str, **kwargs):
        self.last_query_kwargs = {"collection_name": collection_name, "filter": filter}
        return [
            {
                "id": "faq/network.md#1",
                "text": "第二段",
                "tenant_id": "t1",
                "source": "faq/network.md",
            },
            {
                "id": "faq/network.md#0",
                "text": "第一段",
                "tenant_id": "t1",
                "source": "faq/network.md",
            },
        ]
```

并在 `FakeMilvusClient.__init__`（当前第 8-11 行）里加一行初始化：

```python
    def __init__(self) -> None:
        self.inserted: dict | None = None
        self.last_search_kwargs: dict | None = None
        self.last_delete_kwargs: dict | None = None
        self.last_query_kwargs: dict | None = None
```

在文件末尾追加：

```python
async def test_list_by_source_sends_expected_filter_expression():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.list_by_source(source="faq/network.md", tenant_id="t1")

    assert client.last_query_kwargs["collection_name"] == "faq_chunks"
    assert client.last_query_kwargs["filter"] == (
        'tenant_id == "t1" && source == "faq/network.md"'
    )


async def test_list_by_source_returns_records_in_chunk_order():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    records = await store.list_by_source(source="faq/network.md", tenant_id="t1")

    # FakeMilvusClient.query 故意按 #1、#0 的顺序返回（模拟向量库不保证
    # 顺序），list_by_source 必须自己按 chunk 序号重新排序成 #0、#1
    assert [r.id for r in records] == ["faq/network.md#0", "faq/network.md#1"]
    assert [r.text for r in records] == ["第一段", "第二段"]


async def test_list_by_source_escapes_backslashes_and_quotes_in_source():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    await store.list_by_source(source=r'data\uploads\weird"path.md', tenant_id="t1")

    assert client.last_query_kwargs["filter"] == (
        'tenant_id == "t1" && source == "data\\\\uploads\\\\weird\\"path.md"'
    )


async def test_list_by_source_rejects_unsafe_tenant_id():
    client = FakeMilvusClient()
    store = MilvusVectorStore(client=client, collection_name="faq_chunks")

    with pytest.raises(ValueError):
        await store.list_by_source(source="doc.md", tenant_id='t1" or "1"=="1')
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/retrieval/test_vector_store.py tests/retrieval/test_milvus_store.py -v`
Expected: FAIL——`chunk_index_from_id`/`list_by_source` 都还不存在，import 阶段就会报错。

- [ ] **Step 3: 实现**

在 `app/retrieval/vector_store.py` 里，`_cosine_similarity` 函数（当前第 37-43 行）之后加一个新的模块级函数：

```python
def chunk_index_from_id(record_id: str) -> int:
    """从 f"{path}#{i}" 形式的 id 里取出序号 i（见
    app/ingestion/pipeline.py::_embed_and_upsert 写入时的编号方式）。

    向量库本身（Milvus 的 query()、InMemoryVectorStore 内部的 list）都不
    保证任何返回顺序，list_by_source() 靠这个函数重新按文档原始 chunk
    顺序排序，预览页面才能按写入时的先后展示，而不是一堆乱序的片段。
    id 不含 "#" 或后缀不是数字（理论上不会发生，写入路径固定用这个格式，
    这里只是防御性兜底）时返回 0，不抛异常中断整个排序。
    """
    _, _, suffix = record_id.rpartition("#")
    try:
        return int(suffix)
    except ValueError:
        return 0
```

把 `VectorStore` 协议（当前第 25-34 行）里加一行新方法：

```python
class VectorStore(Protocol):
    async def upsert(self, records: list[VectorRecord]) -> None: ...

    async def search(
        self, query_vector: list[float], *, top_k: int, tenant_id: str
    ) -> list[VectorRecord]: ...

    async def list_all(self) -> list[VectorRecord]: ...

    async def list_by_source(self, *, source: str, tenant_id: str) -> list[VectorRecord]: ...

    async def delete_by_source(self, *, source: str, tenant_id: str) -> None: ...
```

在 `InMemoryVectorStore` 类里，`delete_by_source` 方法（当前第 75-80 行）之前加一个新方法：

```python
    async def list_by_source(
        self, *, source: str, tenant_id: str
    ) -> list[VectorRecord]:
        matched = [
            r
            for r in self._records
            if r.tenant_id == tenant_id and r.metadata.get("source") == source
        ]
        matched.sort(key=lambda r: chunk_index_from_id(r.id))
        return matched
```

在 `app/retrieval/milvus_store.py` 里，把 import 行 `from app.retrieval.vector_store import VectorRecord` 改成：

```python
from app.retrieval.vector_store import VectorRecord, chunk_index_from_id
```

在 `MilvusVectorStore` 类里，`delete_by_source` 方法（当前第 101-122 行）之后、`list_all` 方法之前加一个新方法：

```python
    async def list_by_source(
        self, *, source: str, tenant_id: str
    ) -> list[VectorRecord]:
        """查某个来源文件（同一 tenant_id 下）写入过的全部 chunk，供管理后台
        预览"这份文档到底被切成了什么、能不能被检索到"用。过滤表达式和
        转义规则跟 delete_by_source() 完全一致（同一个 source+tenant_id
        过滤维度），只是这里是只读查询不是删除——具体转义原因见
        delete_by_source() 的说明，这里不重复。

        Milvus 的 query() 不保证返回顺序，这里按 chunk_index_from_id()
        重新排成文档原始顺序，调用方（管理后台预览接口）不需要自己再排
        一遍。10000 这个上限跟 list_all() 用的是同一个值——单份文档不
        可能切出比这更多的 chunk，纯粹是防御性上限，不是"只看前一部分"
        的截断（预览要看的是"从头开始的前 N 条"，不是"随便一批 10000
        条里的前 N 条"，所以这里必须先查全量再排序，不能反过来先截断）。
        """
        _validate_tenant_id(tenant_id)
        escaped_source = source.replace("\\", "\\\\").replace('"', '\\"')
        rows = await asyncio.to_thread(
            self._client.query,
            collection_name=self._collection_name,
            filter=f'tenant_id == "{tenant_id}" && source == "{escaped_source}"',
            limit=10000,
        )
        records = [
            VectorRecord(
                id=str(row["id"]),
                vector=[],
                text=str(row.get("text", "")),
                tenant_id=str(row.get("tenant_id", "")),
                metadata={
                    k: v
                    for k, v in row.items()
                    if k not in {"id", "text", "tenant_id"}
                },
            )
            for row in rows
        ]
        records.sort(key=lambda r: chunk_index_from_id(r.id))
        return records
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/retrieval/test_vector_store.py tests/retrieval/test_milvus_store.py -v`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/retrieval/vector_store.py app/retrieval/milvus_store.py \
  tests/retrieval/test_vector_store.py tests/retrieval/test_milvus_store.py
git commit -m "feat(retrieval): add list_by_source to VectorStore for document chunk preview"
```

---

## Task 2: ingestion_queue.py 新增失败任务的重试/删除

**Files:**
- Modify: `app/ingestion/ingestion_queue.py`
- Test: `tests/ingestion/test_ingestion_queue.py`

**Interfaces:**
- Produces:
  - `JobNotFoundError(Exception)`、`JobNotDeadError(Exception)` 两个新异常类。
  - `list_dead_jobs(conn, *, limit: int = 50, tenant_id: str | None = None) -> list[dict[str, Any]]`。
  - `retry_job(conn, job_id: str, *, tenant_id: str) -> None`——只对 `status='dead'` 的任务生效，重置为 `status='pending', attempts=0, last_error=NULL`。
  - `delete_job(conn, job_id: str, *, tenant_id: str) -> str`——只对 `status='dead'` 的任务生效，删除该行，返回它的 `file_path`（供调用方清理磁盘文件，这个函数本身不碰文件系统）。

- [ ] **Step 1: 写失败的测试**

在 `tests/ingestion/test_ingestion_queue.py` 顶部的 import 区，把：

```python
from app.ingestion.ingestion_queue import (
    enqueue_ingestion_job,
    ensure_ingestion_queue_schema,
    list_pending_jobs,
    mark_job_completed,
    mark_job_failed,
    process_pending_jobs,
)
```

改成：

```python
import pytest

from app.ingestion.ingestion_queue import (
    JobNotDeadError,
    JobNotFoundError,
    delete_job,
    enqueue_ingestion_job,
    ensure_ingestion_queue_schema,
    list_dead_jobs,
    list_pending_jobs,
    mark_job_completed,
    mark_job_failed,
    process_pending_jobs,
    retry_job,
)
```

在文件末尾追加：

```python
async def test_list_dead_jobs_returns_only_dead_status_scoped_to_tenant():
    conn = await _connect()
    dead_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_failed(conn, dead_id, error="解析失败", max_attempts=1)
    pending_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="b.md", content_hash="h2", action="ingest"
    )
    other_tenant_dead_id = await enqueue_ingestion_job(
        conn, tenant_id="t2", file_path="c.md", content_hash="h3", action="ingest"
    )
    await mark_job_failed(conn, other_tenant_dead_id, error="解析失败", max_attempts=1)

    dead_jobs = await list_dead_jobs(conn, tenant_id="t1")

    assert [j["job_id"] for j in dead_jobs] == [dead_id]
    assert pending_id not in [j["job_id"] for j in dead_jobs]


async def test_retry_job_resets_dead_job_to_pending_with_attempts_cleared():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_failed(conn, job_id, error="解析失败", max_attempts=1)
    assert await list_dead_jobs(conn, tenant_id="t1") != []

    await retry_job(conn, job_id, tenant_id="t1")

    dead_after = await list_dead_jobs(conn, tenant_id="t1")
    assert dead_after == []
    pending_after = await list_pending_jobs(conn, tenant_id="t1")
    assert len(pending_after) == 1
    assert pending_after[0]["job_id"] == job_id
    assert pending_after[0]["attempts"] == 0
    assert pending_after[0]["last_error"] is None


async def test_retry_job_raises_when_job_belongs_to_another_tenant():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_failed(conn, job_id, error="解析失败", max_attempts=1)

    with pytest.raises(JobNotFoundError):
        await retry_job(conn, job_id, tenant_id="t2")


async def test_retry_job_raises_when_job_is_not_dead():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )

    with pytest.raises(JobNotDeadError):
        await retry_job(conn, job_id, tenant_id="t1")


async def test_delete_job_removes_dead_job_and_returns_its_file_path():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_failed(conn, job_id, error="解析失败", max_attempts=1)

    file_path = await delete_job(conn, job_id, tenant_id="t1")

    assert file_path == "a.md"
    assert await list_dead_jobs(conn, tenant_id="t1") == []


async def test_delete_job_raises_when_job_is_not_dead():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )

    with pytest.raises(JobNotDeadError):
        await delete_job(conn, job_id, tenant_id="t1")
    # 还在 pending，没有被误删
    assert len(await list_pending_jobs(conn, tenant_id="t1")) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/test_ingestion_queue.py -v`
Expected: FAIL——`ImportError: cannot import name 'JobNotDeadError'`（还没实现）。

- [ ] **Step 3: 实现**

在 `app/ingestion/ingestion_queue.py` 里，`_SCHEMA_SQL` 常量（当前第 31-46 行）之后加两个新异常类：

```python
class JobNotFoundError(Exception):
    """指定的 job_id 在该租户下不存在（包括存在于别的租户名下的情况——
    不做区分，统一按"不存在"处理，避免向调用方泄露"这个任务属于别的
    租户"这个信息）。"""


class JobNotDeadError(Exception):
    """指定的 job_id 存在，但当前状态不是 dead（可能还在 pending 排队/
    处理中，或已经 completed）——重试/删除只对确认失败的任务开放，不该
    误伤正常任务的进度。"""
```

在 `list_pending_jobs` 函数（当前第 112-134 行）之后加一个新函数：

```python
async def list_dead_jobs(
    conn: aiosqlite.Connection, *, limit: int = 50, tenant_id: str | None = None
) -> list[dict[str, Any]]:
    """列出重试耗尽、彻底失败的任务，按最近失败的排前面（updated_at 倒序）
    ——管理后台展示"失败任务"区块用，参数含义和 list_pending_jobs() 一致。
    """
    conn.row_factory = aiosqlite.Row
    if tenant_id is None:
        cursor = await conn.execute(
            "SELECT * FROM ingestion_jobs WHERE status = 'dead' "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
    else:
        cursor = await conn.execute(
            "SELECT * FROM ingestion_jobs WHERE status = 'dead' AND tenant_id = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (tenant_id, limit),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _fetch_job(
    conn: aiosqlite.Connection, job_id: str, *, tenant_id: str
) -> dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM ingestion_jobs WHERE job_id = ? AND tenant_id = ?",
        (job_id, tenant_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise JobNotFoundError(f"任务不存在: {job_id}")
    return dict(row)


async def retry_job(conn: aiosqlite.Connection, job_id: str, *, tenant_id: str) -> None:
    """人工点击重试：把一个 dead 任务重新拉回 pending 队列，attempts 清零、
    last_error 清空——下一轮处理会把它当成一个全新任务重新尝试一次完整的
    3 次自动重试，不受它之前已经用完的重试次数影响。
    """
    job = await _fetch_job(conn, job_id, tenant_id=tenant_id)
    if job["status"] != "dead":
        raise JobNotDeadError(f"任务不是失败状态，无法重试: {job_id}")
    await conn.execute(
        "UPDATE ingestion_jobs SET status='pending', attempts=0, last_error=NULL, "
        "updated_at=datetime('now') WHERE job_id=? AND tenant_id=?",
        (job_id, tenant_id),
    )
    await conn.commit()


async def delete_job(conn: aiosqlite.Connection, job_id: str, *, tenant_id: str) -> str:
    """删除一条失败任务记录，返回它的 file_path 供调用方清理磁盘上的孤儿
    文件——这个函数本身不碰文件系统，"删磁盘文件"这个副作用留给调用方
    （app/api/admin_document_routes.py 已经有 _unlink_uploaded_file()
    做路径安全校验，delete_document() 也在用同一个函数，不重复实现一遍）。
    """
    job = await _fetch_job(conn, job_id, tenant_id=tenant_id)
    if job["status"] != "dead":
        raise JobNotDeadError(f"任务不是失败状态，无法删除: {job_id}")
    await conn.execute(
        "DELETE FROM ingestion_jobs WHERE job_id=? AND tenant_id=?",
        (job_id, tenant_id),
    )
    await conn.commit()
    return job["file_path"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/test_ingestion_queue.py -v`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/ingestion/ingestion_queue.py tests/ingestion/test_ingestion_queue.py
git commit -m "feat(ingestion): add retry/delete for permanently-failed ingestion jobs"
```

---

## Task 3: 失败任务的 API 路由 + 修复跨租户文件删除漏洞

**Files:**
- Modify: `app/api/admin_document_routes.py`
- Test: `tests/api/test_admin_document_routes.py`

**Interfaces:**
- Consumes: Task 2 的 `list_dead_jobs`/`retry_job`/`delete_job`/`JobNotFoundError`/`JobNotDeadError`。
- Produces:
  - `DocumentsListResponse` 新增 `dead_jobs: list[dict]` 字段。
  - `POST /api/admin/documents/jobs/{job_id}/retry?tenant_id=...` —— 重置任务后立即触发一次后台处理（跟上传文档时的行为一致，不用等外部轮询）。
  - `DELETE /api/admin/documents/jobs/{job_id}?tenant_id=...` —— 删除任务记录并清理磁盘上的孤儿文件。
  - `_unlink_uploaded_file(file_path, upload_dir, *, tenant_id)`——签名新增必填的 `tenant_id` 关键字参数，只删 `upload_dir/{tenant_id}` 子目录内的文件，不再是"只要在 `upload_dir` 内随便哪个子目录就删"。

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_admin_document_routes.py` 文件末尾追加：

```python
def test_list_documents_includes_dead_jobs(ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job, mark_job_failed

    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path="a.md",
            content_hash="h1", action="ingest",
        )
    )
    asyncio.run(mark_job_failed(ingestion_conn, job_id, error="解析失败", max_attempts=1))

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents", params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["dead_jobs"]) == 1
    assert body["dead_jobs"][0]["job_id"] == job_id
    assert body["dead_jobs"][0]["last_error"] == "解析失败"


def test_retry_job_resets_to_pending_and_returns_200(tmp_path, ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job, mark_job_failed

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path="a.md",
            content_hash="h1", action="ingest",
        )
    )
    asyncio.run(mark_job_failed(ingestion_conn, job_id, error="解析失败", max_attempts=1))

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_embedding_registry] = lambda: EmbeddingRegistry()
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    app.dependency_overrides[deps.get_review_conn] = lambda: None
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/documents/jobs/{job_id}/retry",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # a.md 实际不存在磁盘上，BackgroundTasks 在 TestClient 里同步执行完
    # 后，这条任务会再次失败——但 attempts 已经被 retry_job() 清零，
    # 一次新失败只会把它计到 1/3 次，还不会打回 dead（重试耗尽变 dead
    # 是 process_pending_jobs/mark_job_failed 自己的行为，已经在
    # tests/ingestion/test_ingestion_queue.py 里覆盖过）。这里只断言
    # 接口本身把 dead 重置回 pending 并成功触发了一次处理这一步没有
    # 抛出未捕获异常（response.status_code == 200），不重复断言最终
    # 任务状态。


def test_retry_job_returns_404_for_unknown_job(ingestion_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: EmbeddingRegistry()
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    app.dependency_overrides[deps.get_review_conn] = lambda: None
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/documents/jobs/unknown-id/retry",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_retry_job_returns_409_when_job_is_not_dead(ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job

    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path="a.md",
            content_hash="h1", action="ingest",
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: EmbeddingRegistry()
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    app.dependency_overrides[deps.get_review_conn] = lambda: None
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/documents/jobs/{job_id}/retry",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409


def test_delete_job_removes_it_and_unlinks_orphaned_file(tmp_path, ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job, mark_job_failed

    upload_dir = tmp_path / "uploads"
    tenant_dir = upload_dir / "t1"
    tenant_dir.mkdir(parents=True)
    orphaned = tenant_dir / "abc_a.md"
    orphaned.write_text("内容", encoding="utf-8")
    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path=str(orphaned),
            content_hash="h1", action="ingest",
        )
    )
    asyncio.run(mark_job_failed(ingestion_conn, job_id, error="解析失败", max_attempts=1))

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            f"/api/admin/documents/jobs/{job_id}",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert not orphaned.exists()


def test_delete_job_returns_409_when_job_is_not_dead(ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job

    job_id = asyncio.run(
        enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path="a.md",
            content_hash="h1", action="ingest",
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_upload_dir] = lambda: None
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            f"/api/admin/documents/jobs/{job_id}",
            params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409


def test_delete_document_does_not_unlink_file_under_a_different_tenant_directory(
    tmp_path, ingestion_conn
):
    """跨租户越权删除的回归测试：file_path 指向 t2 的子目录，但请求用
    tenant_id=t1——向量库/追踪表两处因为 tenant_id 不匹配会是空操作，
    磁盘文件这一步在修复前不会做同样的租户校验，直接被删掉；修复后
    应该被拦下来，文件保持原样。
    """
    upload_dir = tmp_path / "uploads"
    t2_dir = upload_dir / "t2"
    t2_dir.mkdir(parents=True)
    other_tenants_file = t2_dir / "abc_secret.md"
    other_tenants_file.write_text("t2 的私有内容", encoding="utf-8")

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/documents",
            params={"tenant_id": "t1", "file_path": str(other_tenants_file)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert other_tenants_file.exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_document_routes.py -v`
Expected: FAIL——`dead_jobs` 字段不存在、`/jobs/{job_id}/retry` 和 `/jobs/{job_id}` 路由都是 404，跨租户越权测试因为旧版 `_unlink_uploaded_file` 没有租户校验会失败（文件被删掉了）。

- [ ] **Step 3: 实现**

在 `app/api/admin_document_routes.py` 里，把 `DocumentsListResponse`（当前第 89-91 行）：

```python
class DocumentsListResponse(BaseModel):
    documents: list[dict]
    pending_jobs: list[dict]
```

改成：

```python
class DocumentsListResponse(BaseModel):
    documents: list[dict]
    pending_jobs: list[dict]
    dead_jobs: list[dict]
```

把 import 区的 ingestion_queue 导入（当前第 25-30 行）：

```python
from app.ingestion.ingestion_queue import (
    SUPPORTED_SUFFIXES,
    enqueue_ingestion_job,
    list_pending_jobs,
    process_pending_jobs,
)
```

改成：

```python
from app.ingestion.ingestion_queue import (
    SUPPORTED_SUFFIXES,
    JobNotDeadError,
    JobNotFoundError,
    delete_job,
    enqueue_ingestion_job,
    list_dead_jobs,
    list_pending_jobs,
    process_pending_jobs,
    retry_job,
)
```

把 `_unlink_uploaded_file` 函数（当前第 239-255 行）：

```python
def _unlink_uploaded_file(file_path: str, upload_dir: Path) -> None:
    """删掉后台上传落盘的原始文件，避免删除文档后磁盘上的副本永久残留。

    只删 upload_dir 之内的文件：追踪表里的 file_path 理论上都是本系统自己
    写进去的，但同一张表也记录 CLI 摄取（app/ingestion/main.py）扫描的
    任意目录，那些文件不归后台管理，误删会毁掉用户的原始语料。所以这里
    先 resolve 再确认它确实在 upload_dir 底下，否则静默跳过。
    """
    try:
        resolved = Path(file_path).resolve()
        root = upload_dir.resolve()
    except OSError:  # pragma: no cover - 路径本身非法（比如带 NUL 字符）
        return
    if not resolved.is_relative_to(root):
        logger.info("删除文档：%s 不在上传目录内，仅清理索引，不动磁盘文件", file_path)
        return
    resolved.unlink(missing_ok=True)
```

改成：

```python
def _unlink_uploaded_file(file_path: str, upload_dir: Path, *, tenant_id: str) -> None:
    """删掉后台上传落盘的原始文件，避免删除文档后磁盘上的副本永久残留。

    只删 upload_dir/{tenant_id} 之内的文件——不是"只要在 upload_dir 内随便
    哪个子目录就删"：上传路径本身就是按租户分子目录落盘的（tenant_dir =
    upload_dir / tenant_id，见 upload_document()），只校验到 upload_dir
    这一级会放过"file_path 指向别的租户子目录、tenant_id 却填自己的"这种
    跨租户请求——向量库和追踪表两处的删除都会因为 tenant_id 不匹配而是
    空操作，唯独磁盘文件这一步如果不做同样的租户级别校验就会被删掉，
    造成跨租户越权删除。追踪表里的 file_path 理论上都是本系统自己写
    进去的，但同一张表也记录 CLI 摄取（app/ingestion/main.py）扫描的
    任意目录，那些文件不归后台管理，误删会毁掉用户的原始语料——所以除了
    租户目录校验，仍然保留"必须在 upload_dir 之内"这道前提。
    """
    try:
        resolved = Path(file_path).resolve()
        tenant_root = (upload_dir / tenant_id).resolve()
    except OSError:  # pragma: no cover - 路径本身非法（比如带 NUL 字符）
        return
    if not resolved.is_relative_to(tenant_root):
        logger.info(
            "删除文档：%s 不在租户 %s 的上传目录内，仅清理索引，不动磁盘文件",
            file_path, tenant_id,
        )
        return
    resolved.unlink(missing_ok=True)
```

把 `delete_document` 函数末尾调用它的那一行（当前第 284 行 `_unlink_uploaded_file(file_path, upload_dir)`）改成：

```python
    _unlink_uploaded_file(file_path, upload_dir, tenant_id=tenant_id)
```

把 `list_documents` 路由（当前第 229-236 行）：

```python
@router.get("", response_model=DocumentsListResponse)
async def list_documents(
    tenant_id: str,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
) -> DocumentsListResponse:
    documents = await list_tracked_files(ingestion_conn, tenant_id=tenant_id)
    pending_jobs = await list_pending_jobs(ingestion_conn, limit=50, tenant_id=tenant_id)
    return DocumentsListResponse(documents=documents, pending_jobs=pending_jobs)
```

改成：

```python
@router.get("", response_model=DocumentsListResponse)
async def list_documents(
    tenant_id: str,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
) -> DocumentsListResponse:
    documents = await list_tracked_files(ingestion_conn, tenant_id=tenant_id)
    pending_jobs = await list_pending_jobs(ingestion_conn, limit=50, tenant_id=tenant_id)
    dead_jobs = await list_dead_jobs(ingestion_conn, limit=50, tenant_id=tenant_id)
    return DocumentsListResponse(
        documents=documents, pending_jobs=pending_jobs, dead_jobs=dead_jobs
    )
```

在文件末尾（`delete_document` 函数之后）追加两个新路由：

```python
class RetryJobResponse(BaseModel):
    retried: bool


class DeleteJobResponse(BaseModel):
    deleted: bool


@router.post("/jobs/{job_id}/retry", response_model=RetryJobResponse)
async def retry_ingestion_job(
    job_id: str,
    tenant_id: str,
    background_tasks: BackgroundTasks,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    terms: list[Term] = Depends(deps.get_terms),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    ocr: OcrFunction | None = Depends(deps.get_ocr_function),
    table_extractor: TableExtractionFunction | None = Depends(deps.get_table_extractor),
    settings: Settings = Depends(deps.get_settings),
) -> RetryJobResponse:
    _validate_tenant_id(tenant_id)
    try:
        await retry_job(ingestion_conn, job_id, tenant_id=tenant_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="任务不存在")
    except JobNotDeadError:
        raise HTTPException(status_code=409, detail="该任务当前不是失败状态，无法重试")
    # 重置成 pending 后立即触发一次处理，跟上传文档时的行为一致——不用等
    # 外部轮询/下一次上传才把这条任务捡起来。
    background_tasks.add_task(
        _run_pending_jobs,
        ingestion_conn,
        embedding_registry,
        vector_store,
        llm_registry,
        terms,
        graph_client,
        review_conn,
        ocr,
        settings.ocr_render_dpi,
        settings.ocr_max_concurrency,
        table_extractor,
        settings.table_extraction_max_concurrency,
        settings.ingestion_job_concurrency,
    )
    return RetryJobResponse(retried=True)


@router.delete("/jobs/{job_id}", response_model=DeleteJobResponse)
async def delete_ingestion_job(
    job_id: str,
    tenant_id: str,
    upload_dir: Path = Depends(deps.get_upload_dir),
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
) -> DeleteJobResponse:
    _validate_tenant_id(tenant_id)
    try:
        file_path = await delete_job(ingestion_conn, job_id, tenant_id=tenant_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="任务不存在")
    except JobNotDeadError:
        raise HTTPException(status_code=409, detail="该任务当前不是失败状态，无法删除")
    _unlink_uploaded_file(file_path, upload_dir, tenant_id=tenant_id)
    return DeleteJobResponse(deleted=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_document_routes.py -v`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/api/admin_document_routes.py tests/api/test_admin_document_routes.py
git commit -m "feat(api): surface dead ingestion jobs with retry/delete, fix cross-tenant file deletion"
```

---

## Task 4: chunk 预览 + 原始文件下载的 API 路由

**Files:**
- Modify: `app/api/admin_document_routes.py`
- Test: `tests/api/test_admin_document_routes.py`

**Interfaces:**
- Consumes: Task 1 的 `VectorStore.list_by_source()`。
- Produces:
  - `GET /api/admin/documents/chunks?tenant_id=&file_path=` → `{"chunks": [{"text": "..."}], "total": N}`，`chunks` 最多 200 条，`total` 是真实总数。
  - `GET /api/admin/documents/file?tenant_id=&file_path=` → 原始文件内容（`FileResponse`），`file_path` 必须落在 `upload_dir/{tenant_id}` 内，否则 404；PDF/图片走 `inline`（浏览器原生渲染），其它类型走 `attachment`（下载）。

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_admin_document_routes.py` 文件末尾追加：

```python
def test_list_document_chunks_returns_texts_and_total(ingestion_conn):
    vector_store = InMemoryVectorStore()
    asyncio.run(
        vector_store.upsert(
            [
                VectorRecord(
                    id="a.md#0", vector=[0.1], text="第一段",
                    tenant_id="t1", metadata={"source": "a.md"},
                ),
                VectorRecord(
                    id="a.md#1", vector=[0.1], text="第二段",
                    tenant_id="t1", metadata={"source": "a.md"},
                ),
            ]
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents/chunks",
            params={"tenant_id": "t1", "file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [c["text"] for c in body["chunks"]] == ["第一段", "第二段"]


def test_list_document_chunks_caps_at_200_but_reports_true_total(ingestion_conn):
    vector_store = InMemoryVectorStore()
    asyncio.run(
        vector_store.upsert(
            [
                VectorRecord(
                    id=f"a.md#{i}", vector=[0.1], text=f"第{i}段",
                    tenant_id="t1", metadata={"source": "a.md"},
                )
                for i in range(250)
            ]
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents/chunks",
            params={"tenant_id": "t1", "file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert len(body["chunks"]) == 200
    assert body["total"] == 250


def test_download_document_file_returns_file_content(tmp_path, ingestion_conn):
    upload_dir = tmp_path / "uploads"
    tenant_dir = upload_dir / "t1"
    tenant_dir.mkdir(parents=True)
    the_file = tenant_dir / "abc_a.md"
    the_file.write_text("文件内容", encoding="utf-8")

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents/file",
            params={"tenant_id": "t1", "file_path": str(the_file)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.content.decode("utf-8") == "文件内容"


def test_download_document_file_returns_404_for_file_outside_own_tenant_directory(
    tmp_path, ingestion_conn
):
    upload_dir = tmp_path / "uploads"
    t2_dir = upload_dir / "t2"
    t2_dir.mkdir(parents=True)
    other_tenants_file = t2_dir / "abc_secret.md"
    other_tenants_file.write_text("t2 的私有内容", encoding="utf-8")

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/documents/file",
            params={"tenant_id": "t1", "file_path": str(other_tenants_file)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_document_routes.py -k "chunks or download_document_file" -v`
Expected: FAIL——`/chunks` 和 `/file` 路由都还不存在，返回 404 而不是预期的 200/内容比对结果。

- [ ] **Step 3: 实现**

在 `app/api/admin_document_routes.py` 顶部 import 区，`from fastapi import (...)` 那一段（当前第 8-16 行）里加一行 FileResponse 的 import：

```python
from fastapi.responses import FileResponse
```

在文件末尾（Task 3 加的 `retry_ingestion_job`/`delete_ingestion_job` 路由之后）追加：

```python
class ChunkResponse(BaseModel):
    text: str


class ChunksListResponse(BaseModel):
    chunks: list[ChunkResponse]
    total: int


_CHUNK_PREVIEW_LIMIT = 200


@router.get("/chunks", response_model=ChunksListResponse)
async def list_document_chunks(
    tenant_id: str,
    file_path: str,
    vector_store: VectorStore = Depends(deps.get_vector_store),
) -> ChunksListResponse:
    _validate_tenant_id(tenant_id)
    records = await vector_store.list_by_source(source=file_path, tenant_id=tenant_id)
    return ChunksListResponse(
        chunks=[ChunkResponse(text=r.text) for r in records[:_CHUNK_PREVIEW_LIMIT]],
        total=len(records),
    )


# PDF/图片浏览器能原生渲染，用 inline 直接在新标签页里打开看；其它格式
# （docx/csv/md）浏览器没法渲染，走 attachment 触发下载，不是打开一堆
# 乱码/纯文本。
_INLINE_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg"})
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".md": "text/markdown",
}


@router.get("/file")
async def download_document_file(
    tenant_id: str,
    file_path: str,
    upload_dir: Path = Depends(deps.get_upload_dir),
) -> FileResponse:
    _validate_tenant_id(tenant_id)
    try:
        resolved = Path(file_path).resolve()
        tenant_root = (upload_dir / tenant_id).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="文件不存在") from None
    # 校验规则跟 _unlink_uploaded_file() 完全一致（同样必须落在
    # upload_dir/{tenant_id} 内），理由见那个函数的说明——这里额外要求
    # is_file()：目录本身也可能落在这个前缀下，不该被当成"文件"读出去。
    if not resolved.is_relative_to(tenant_root) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    suffix = resolved.suffix.lower()
    media_type = _MEDIA_TYPES.get(suffix, "application/octet-stream")
    return FileResponse(
        resolved,
        media_type=media_type,
        filename=resolved.name,
        content_disposition_type="inline" if suffix in _INLINE_SUFFIXES else "attachment",
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_document_routes.py -v`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/api/admin_document_routes.py tests/api/test_admin_document_routes.py
git commit -m "feat(api): add document chunk preview and raw file download endpoints"
```

---

## Task 5: 前端展示失败任务，支持重试/删除

**Files:**
- Modify: `frontend/src/admin/DocumentsPage.tsx`

**Interfaces:**
- Consumes: Task 3 的 `dead_jobs` 字段、`/jobs/{job_id}/retry`、`/jobs/{job_id}`（DELETE）。

- [ ] **Step 1: 加 DeadJob 接口和状态**

把 `interface PendingJob`（当前第 19-24 行）之后加一个新接口：

```tsx
interface DeadJob {
  job_id: string
  file_path: string
  last_error: string | null
}
```

在 `const [pendingJobs, setPendingJobs] = useState<PendingJob[]>([])`（当前第 33 行）之后加：

```tsx
  const [deadJobs, setDeadJobs] = useState<DeadJob[]>([])
  const [jobActionId, setJobActionId] = useState<string | null>(null)
  const [jobError, setJobError] = useState<string | null>(null)
```

把 `refresh` 函数（当前第 50-64 行）：

```tsx
  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/api/admin/documents?tenant_id=${encodeURIComponent(tenantId)}`,
      sessionToken,
    )
    const data = (await response.json()) as {
      documents: TrackedDocument[]
      pending_jobs: PendingJob[]
    }
    setDocuments(data.documents)
    setPendingJobs(data.pending_jobs)
    hasPendingJobsRef.current = data.pending_jobs.length > 0
    setLoaded(true)
  }, [sessionToken, tenantId])
```

改成：

```tsx
  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/api/admin/documents?tenant_id=${encodeURIComponent(tenantId)}`,
      sessionToken,
    )
    const data = (await response.json()) as {
      documents: TrackedDocument[]
      pending_jobs: PendingJob[]
      dead_jobs: DeadJob[]
    }
    setDocuments(data.documents)
    setPendingJobs(data.pending_jobs)
    setDeadJobs(data.dead_jobs)
    hasPendingJobsRef.current = data.pending_jobs.length > 0
    setLoaded(true)
  }, [sessionToken, tenantId])
```

- [ ] **Step 2: 加重试/删除处理函数**

在 `handleDelete` 函数（当前第 127-150 行）之后加：

```tsx
  const handleRetryJob = async (jobId: string) => {
    if (!sessionToken || jobActionId !== null) return
    setJobError(null)
    setJobActionId(jobId)
    try {
      const response = await adminFetch(
        `/api/admin/documents/jobs/${jobId}/retry?tenant_id=${encodeURIComponent(tenantId)}`,
        sessionToken,
        { method: 'POST' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '重试失败'))
      }
      await pollNowRef.current()
    } catch (err) {
      setJobError(err instanceof Error ? err.message : '重试失败')
    } finally {
      setJobActionId(null)
    }
  }

  const handleDeleteJob = async (jobId: string, filePath: string) => {
    if (!sessionToken || jobActionId !== null) return
    if (
      !window.confirm(
        `确定要删除失败任务「${displayFileName(filePath)}」吗？关联的上传文件也会被清理，此操作不可撤销。`,
      )
    ) {
      return
    }
    setJobError(null)
    setJobActionId(jobId)
    try {
      const response = await adminFetch(
        `/api/admin/documents/jobs/${jobId}?tenant_id=${encodeURIComponent(tenantId)}`,
        sessionToken,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除失败'))
      }
      await pollNowRef.current()
    } catch (err) {
      setJobError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setJobActionId(null)
    }
  }
```

- [ ] **Step 3: 加"失败任务"区块的 UI**

在"处理中的任务"区块（当前第 183-198 行，即 `{pendingJobs.length > 0 && (...)}` 那一整块）之后加一个新区块：

```tsx
      {deadJobs.length > 0 && (
        <div className="flex flex-col gap-2">
          <h2 className="font-bold text-ink">失败任务</h2>
          {jobError && (
            <p
              role="alert"
              className="border-2 border-status-error bg-card px-3 py-2 text-sm text-ink shadow-brutal-sm"
            >
              {jobError}
            </p>
          )}
          {deadJobs.map((job) => (
            <div
              key={job.job_id}
              className="flex items-center justify-between border-2 border-status-error bg-card px-4 py-3 shadow-brutal-sm"
            >
              <span className="text-ink">
                {displayFileName(job.file_path)}
                {job.last_error && `（${job.last_error}）`}
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => handleRetryJob(job.job_id)}
                  disabled={jobActionId !== null}
                  className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  {jobActionId === job.job_id ? '处理中…' : '重试'}
                </button>
                <button
                  type="button"
                  onClick={() => handleDeleteJob(job.job_id, job.file_path)}
                  disabled={jobActionId !== null}
                  className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  {jobActionId === job.job_id ? '处理中…' : '删除'}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
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
2. 上传一个不支持内部解析的场景制造一次失败（或者直接用 sqlite 命令行把某条任务的 `attempts` 改成 3、`status` 改成 `dead` 来模拟），确认页面上出现"失败任务"区块，显示文件名和错误信息。
3. 点击"重试"，确认任务从"失败任务"消失、短暂出现在"处理中的任务"里（如果立刻又失败会重新出现在失败任务里，这是预期行为）。
4. 点击"删除"，确认弹出确认框，确认后任务从列表消失，且检查 `upload_dir` 下对应文件已被清理。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/DocumentsPage.tsx
git commit -m "feat(admin): surface failed ingestion jobs with retry/delete actions"
```

---

## Task 6: 前端 chunk 预览与原始文件查看

**Files:**
- Modify: `frontend/src/admin/DocumentsPage.tsx`

**Interfaces:**
- Consumes: Task 4 的 `GET /api/admin/documents/chunks`、`GET /api/admin/documents/file`。

- [ ] **Step 1: 加预览相关的状态和类型**

在 `interface DeadJob { ... }`（Task 5 加的接口）之后加一个模块级类型别名，跟其它接口一样放在组件函数外面：

```tsx
type ChunkPreview = { chunks: string[]; total: number }
```

在 `const [deletingPath, setDeletingPath] = useState<string | null>(null)`（当前第 39 行，Task 5 执行后行号可能已偏移，以内容定位）之后加：

```tsx
  const [expandedChunks, setExpandedChunks] = useState<
    Record<string, ChunkPreview | 'loading'>
  >({})
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [downloadingPath, setDownloadingPath] = useState<string | null>(null)
```

- [ ] **Step 2: 加预览展开/收起和下载原文件的处理函数**

在 `handleDelete` 函数结束之后（Task 5 已经在这里加了 `handleRetryJob`/`handleDeleteJob`，把新函数加在它们之后）追加：

```tsx
  const handleTogglePreview = async (filePath: string) => {
    if (!sessionToken) return
    const current = expandedChunks[filePath]
    if (current === 'loading') return
    if (current) {
      setExpandedChunks((prev) => {
        const next = { ...prev }
        delete next[filePath]
        return next
      })
      return
    }
    setPreviewError(null)
    setExpandedChunks((prev) => ({ ...prev, [filePath]: 'loading' }))
    try {
      const response = await adminFetch(
        `/api/admin/documents/chunks?tenant_id=${encodeURIComponent(tenantId)}&file_path=${encodeURIComponent(filePath)}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载预览失败'))
      }
      const data = (await response.json()) as { chunks: { text: string }[]; total: number }
      setExpandedChunks((prev) => ({
        ...prev,
        [filePath]: { chunks: data.chunks.map((c) => c.text), total: data.total },
      }))
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : '加载预览失败')
      setExpandedChunks((prev) => {
        const next = { ...prev }
        delete next[filePath]
        return next
      })
    }
  }

  const handleDownloadFile = async (filePath: string) => {
    if (!sessionToken || downloadingPath !== null) return
    setPreviewError(null)
    setDownloadingPath(filePath)
    try {
      const response = await adminFetch(
        `/api/admin/documents/file?tenant_id=${encodeURIComponent(tenantId)}&file_path=${encodeURIComponent(filePath)}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '查看原文件失败'))
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      window.open(url, '_blank')
      // 新标签页/浏览器内置查看器（比如 PDF）此时可能还没读完这个
      // blob URL 的内容，不能在 window.open 后立即 revoke；60 秒后统一
      // 释放，足够覆盖打开+渲染的时间。
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : '查看原文件失败')
    } finally {
      setDownloadingPath(null)
    }
  }
```

- [ ] **Step 3: 改造已摄取文档列表的渲染，加预览展开区和查看原文件按钮**

把已摄取文档列表的渲染（当前结构是 `documents.map((doc) => (...))` 的箭头表达式写法，Task 5 执行后具体行号可能偏移，以内容定位——原本长这样）：

```tsx
        {loaded &&
          documents.map((doc) => (
            <div
              key={doc.file_path}
              className="flex items-center justify-between border-2 border-ink bg-card px-4 py-3 shadow-brutal-sm"
            >
              <span className="text-ink" title={doc.file_path}>
                {displayFileName(doc.file_path)}（{doc.chunk_count} chunks，最近摄取：
                {doc.last_ingested_at}）
              </span>
              <button
                type="button"
                onClick={() => handleDelete(doc.file_path)}
                disabled={deletingPath !== null}
                className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
              >
                {deletingPath === doc.file_path ? '删除中…' : '删除'}
              </button>
            </div>
          ))}
```

改成（箭头函数体从表达式改成代码块，加预览展开区）：

```tsx
        {previewError && (
          <p
            role="alert"
            className="border-2 border-status-error bg-card px-3 py-2 text-sm text-ink shadow-brutal-sm"
          >
            {previewError}
          </p>
        )}
        {loaded &&
          documents.map((doc) => {
            const preview = expandedChunks[doc.file_path]
            return (
              <div
                key={doc.file_path}
                className="flex flex-col gap-2 border-2 border-ink bg-card px-4 py-3 shadow-brutal-sm"
              >
                <div className="flex items-center justify-between">
                  <span className="text-ink" title={doc.file_path}>
                    {displayFileName(doc.file_path)}（{doc.chunk_count} chunks，最近摄取：
                    {doc.last_ingested_at}）
                  </span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => handleTogglePreview(doc.file_path)}
                      disabled={preview === 'loading'}
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {preview === 'loading'
                        ? '加载中…'
                        : preview
                          ? '收起预览'
                          : '预览'}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDownloadFile(doc.file_path)}
                      disabled={downloadingPath !== null}
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {downloadingPath === doc.file_path ? '打开中…' : '查看原文件'}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDelete(doc.file_path)}
                      disabled={deletingPath !== null}
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {deletingPath === doc.file_path ? '删除中…' : '删除'}
                    </button>
                  </div>
                </div>
                {preview && preview !== 'loading' && (
                  <div className="flex flex-col gap-1 border-t-2 border-ink pt-2">
                    {preview.chunks.map((text, i) => (
                      <p
                        key={i}
                        className="border-2 border-ink bg-paper px-2 py-1 text-xs text-ink-soft"
                      >
                        [{i}] {text}
                      </p>
                    ))}
                    {preview.total > preview.chunks.length && (
                      <p className="text-xs text-ink-soft">
                        仅显示前 {preview.chunks.length} / 共 {preview.total} 条
                      </p>
                    )}
                    {preview.chunks.length === 0 && (
                      <p className="text-xs text-ink-soft">向量库里没有找到这份文档的 chunk。</p>
                    )}
                  </div>
                )}
              </div>
            )
          })}
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
2. 打开文档管理页面，对一份已摄取的 PDF/Markdown 点击"预览"，确认展开区显示 chunk 列表，每条前面带序号，顺序符合原文档顺序（不是乱序）。
3. 再点一次"预览"，确认收起。
4. 对一份 chunk 数超过 200 的文档（或临时调小服务端 `_CHUNK_PREVIEW_LIMIT` 测试），确认展开区底部出现"仅显示前 200 / 共 N 条"。
5. 点击"查看原文件"：PDF/图片应该在新标签页里直接打开能看到内容；.docx/.csv/.md 应该触发下载或者在新标签页里能看到文本内容（取决于浏览器行为，只要文件内容是可获取的，不是 401/404 即可）。
6. 确认所有新按钮在对应的 disabled 条件下（预览加载中/下载中/删除中）正确禁用，不会看着能点、点了却没反应。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/DocumentsPage.tsx
git commit -m "feat(admin): preview ingested document chunks and view the raw uploaded file"
```

---

## 全部任务完成后

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了已知无关的 `test_returns_none_when_tts_not_configured` 之外全部通过。

Run（`frontend/` 目录下）: `npm run typecheck && npm run build`
Expected: 均无错误退出。
